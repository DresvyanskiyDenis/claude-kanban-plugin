"""OBS-04: retention of runner logs and stream sidecars."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, housekeeping, store
from kanban_claude.paths import card_tmp, ensure_dir, run_files


def make(board: str, task: str, n: int, age_days: float) -> list[Path]:
    files = list(run_files(board, task, n))
    for f in files:
        ensure_dir(f.parent)
        f.write_text("x")
        aged = f.stat().st_mtime - age_days * 86400
        os.utime(f, (aged, aged))
    return files


def test_retention_removes_files_past_the_window_and_their_empty_directories(conn: Any) -> None:
    old = make("main", "t_old", 1, 40)
    fresh = make("main", "t_new", 1, 1)
    housekeeping.retention(conn, store.now())
    assert not any(f.exists() for f in old) and all(f.exists() for f in fresh)
    assert not old[0].parent.exists()


def test_retention_also_removes_the_prompt_and_gate_logs_of_an_old_run(conn: Any) -> None:
    run_dir = make("main", "t_old", 1, 40)[0].parent
    extra = [
        run_dir / "run-1.prompt.md",
        run_dir / "run-1.gate-1.log",
        run_dir / "run-1.gate-2.log",
    ]
    kept = [run_dir / "run-2.prompt.md", run_dir / "run-2.gate-1.log"]
    for f in [*extra, *kept]:
        f.write_text("x")
    for f in extra:
        os.utime(f, (1, 1))
    housekeeping.retention(conn, store.now())
    assert not any(f.exists() for f in extra) and all(f.exists() for f in kept)


def test_a_file_that_vanishes_during_the_sweep_does_not_end_it(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = make("main", "t_a", 1, 40), make("main", "t_b", 1, 40)
    real_unlink = Path.unlink
    raised: list[Path] = []

    def vanishing(self: Path, missing_ok: bool = False) -> None:
        if not raised:
            raised.append(self)
            real_unlink(self)
            raise FileNotFoundError(self)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", vanishing)
    housekeeping.retention(conn, store.now())
    assert not any(f.exists() for f in [*first, *second])


def card_with_tmp(conn: Any, tid: str, status: str, kind: str = "scratch") -> Any:
    conn.execute(
        "UPDATE tasks SET status = ?, workspace_kind = ? WHERE id = ?", (status, kind, tid)
    )
    ensure_dir(card_tmp(tid)).joinpath("pr.md").write_text("body")
    return store.get_task(conn, tid)


def test_the_sweep_removes_the_tmpdir_of_an_archived_scratch_card(
    conn: Any, card: Callable[..., str]
) -> None:
    done, archived = (
        card_with_tmp(conn, card("a"), "done"),
        card_with_tmp(conn, card("b"), "archived"),
    )
    housekeeping.sweep(conn)
    assert not card_tmp(archived["id"]).exists() and card_tmp(done["id"]).exists()


def test_the_sweep_removes_the_tmpdir_of_any_archived_or_deleted_card(
    conn: Any, card: Callable[..., str]
) -> None:
    kept = card_with_tmp(conn, card("running"), "running", "worktree")
    folder = card_with_tmp(conn, card("dir"), "archived", "dir")
    worktree = card_with_tmp(conn, card("wt"), "archived", "worktree")
    gone = ensure_dir(card_tmp("t_deleted"))
    housekeeping.sweep(conn)
    assert card_tmp(kept["id"]).exists()
    assert not card_tmp(folder["id"]).exists() and not card_tmp(worktree["id"]).exists()
    assert not gone.exists()


def test_cleanup_card_removes_the_tmpdir_even_when_the_worktree_is_gone(
    conn: Any, card: Callable[..., str]
) -> None:
    t = card_with_tmp(conn, card(), "done", "worktree")
    conn.execute(
        "UPDATE tasks SET workspace_path = '/nonexistent', branch_name = 'b' WHERE id = ?",
        (t["id"],),
    )
    housekeeping.cleanup_card(conn, store.get_task(conn, t["id"]))
    assert not card_tmp(t["id"]).exists()


def test_housekeeping_reprobes_paused_logins_and_survives_a_failure(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def reprobe(_conn: Any, ts: int) -> None:
        calls.append(ts)
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(dispatcher, "reprobe_logins", reprobe)
    monkeypatch.setattr(dispatcher, "reprobe_dispatch", reprobe)
    monkeypatch.setattr(housekeeping, "sweep", lambda _c: None)
    housekeeping.run_due(conn, 1000)
    assert calls == [1000, 1000]
