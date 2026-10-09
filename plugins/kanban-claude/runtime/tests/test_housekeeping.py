"""OBS-04: retention of runner logs and stream sidecars."""

import os
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import housekeeping, store
from kanban_claude.paths import ensure_dir, run_files


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
