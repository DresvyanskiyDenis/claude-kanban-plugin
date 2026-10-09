"""INS-12: `kanban uninstall` takes the daemon and the tool away and never a worktree."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import install, launchd, paths, store
from kanban_claude.procs import start_time

from .conftest import Calls, Kb, git

LAST = "/plugin uninstall kanban-claude@claude-kanban-plugin"


def removed(uv_calls: Calls) -> bool:
    return ["tool", "uninstall", "kanban-claude"] in uv_calls()


def test_uninstall_unloads_the_daemon_deletes_the_plist_and_the_tool_but_keeps_the_data(
    kb: Kb, healthy: int, agent_loaded: Callable[[], bool], uv_calls: Calls
) -> None:
    code, out, err = kb("uninstall")
    assert code == 0, err
    assert not agent_loaded() and not launchd.plist_path().exists() and removed(uv_calls)
    assert (paths.dirs().home / "kanban.db").exists() and paths.dirs().work.exists()
    assert out.splitlines()[-1] == LAST


def test_active_runs_refuse_the_uninstall_and_nothing_is_touched(
    kb: Kb,
    healthy: int,
    conn: Any,
    board: str,
    card: Callable[..., str],
    claim: Callable[[str], int],
    agent_loaded: Callable[[], bool],
    uv_calls: Calls,
) -> None:
    run = claim(card())
    code, _, err = kb("uninstall")
    assert code == 4 and "--force" in err and str(run) in err
    assert agent_loaded() and launchd.plist_path().exists() and not removed(uv_calls)


def test_force_stops_active_runs_as_an_owner_stop_and_signals_their_runners(
    kb: Kb,
    healthy: int,
    conn: Any,
    board: str,
    card: Callable[..., str],
    claim: Callable[[str], int],
    agent_loaded: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = card()
    run = claim(tid)
    monkeypatch.setattr(install, "STOP_WAIT_S", 0.3)
    runner = subprocess.Popen(["sleep", "60"])
    try:
        conn.execute(
            "UPDATE task_runs SET runner_pid = ?, runner_started = ? WHERE id = ?",
            (runner.pid, start_time(runner.pid), run),
        )
        code, out, err = kb("uninstall", "--force")
        assert code == 0, err
        assert runner.wait(timeout=10) == -15
    finally:
        runner.kill()
    task = store.get_task(conn, tid)
    assert task["status"] == "blocked" and task["block_kind"] == "stopped"
    assert not agent_loaded() and tid in out


def make_worktree(repo: Path, task_id: str, branch: str) -> Path:
    path = repo / ".worktrees" / task_id
    git("worktree", "add", "-b", branch, str(path), cwd=repo)
    return path


def worktree_card(conn: Any, repo: Path, branch: str) -> tuple[str, Path]:
    store.board_create(conn, "main", default_workdir=str(repo))
    tid = str(
        store.create(
            conn, "main", "Work", body="b", assignee="smith", workspace=f"worktree:{repo}",
            branch=branch,
        )["id"]
    )  # fmt: skip
    return tid, make_worktree(repo, tid, branch)


def test_dirty_and_unpushed_worktrees_are_listed_and_never_removed(
    kb: Kb, healthy: int, conn: Any, repo: Path
) -> None:
    dirty_id, dirty = worktree_card(conn, repo, "feat/dirty")
    (dirty / "scratch.txt").write_text("x")
    other = store.create(
        conn, "main", "Other", body="b", assignee="smith", workspace=f"worktree:{repo}",
        branch="feat/ahead",
    )  # fmt: skip
    ahead = make_worktree(repo, str(other["id"]), "feat/ahead")
    git("commit", "-q", "--allow-empty", "-m", "local only", cwd=ahead)
    code, out, err = kb("uninstall")
    assert code == 0, err
    assert f"{dirty_id} is dirty" in out and f"{other['id']} is unpushed" in out
    assert dirty.is_dir() and ahead.is_dir()
    assert out.splitlines()[-1] == LAST


def test_a_clean_pushed_worktree_is_not_listed(kb: Kb, healthy: int, conn: Any, repo: Path) -> None:
    tid, path = worktree_card(conn, repo, "feat/clean")
    git("push", "-q", "-u", "origin", "feat/clean", cwd=path)
    code, out, _ = kb("uninstall")
    assert code == 0 and tid not in out and path.is_dir()


def test_purge_without_yes_is_a_usage_error_and_changes_nothing(
    kb: Kb, healthy: int, agent_loaded: Callable[[], bool]
) -> None:
    code, _, err = kb("uninstall", "--purge")
    assert code == 2 and "--yes" in err
    assert agent_loaded()


def test_purge_prints_sizes_and_paths_then_deletes_both_directories(kb: Kb, healthy: int) -> None:
    d = paths.dirs()
    (d.work / "scratch").mkdir()
    (d.work / "scratch" / "blob").write_bytes(b"x" * 5000)
    code, out, err = kb("uninstall", "--purge", "--yes")
    assert code == 0, err
    assert str(d.home) in out and str(d.work) in out and "KB" in out
    assert not d.home.exists() and not d.work.exists()
    assert out.splitlines()[-1] == LAST


@pytest.mark.parametrize("target", ["home", "parent"])
def test_purge_refuses_a_directory_that_is_or_contains_the_users_home(
    kb: Kb, env: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    mine = Path.home() if target == "home" else Path.home().parent
    monkeypatch.setenv("KANBAN_HOME", str(mine))
    code, _, err = kb("uninstall", "--purge", "--yes")
    assert code == 4 and "refusing" in err
    assert Path.home().is_dir()


def test_uninstalling_what_was_never_installed_is_quiet_and_creates_nothing(
    kb: Kb, env: Path, uv_calls: Calls
) -> None:
    code, out, _ = kb("uninstall")
    assert code == 0 and out.splitlines()[-1] == LAST
    assert not (env / "kb-home").exists() and removed(uv_calls)


def test_json_lists_what_was_done(kb: Kb, healthy: int, conn: Any, repo: Path) -> None:
    tid, _ = worktree_card(conn, repo, "feat/dirty")
    (repo / ".worktrees" / tid / "x").write_text("x")
    code, out, _ = kb("uninstall", "--json")
    data = json.loads(out)
    assert (
        code == 0
        and data["worktrees"][0]["task"] == tid
        and data["worktrees"][0]["risk"] == "dirty"
    )
    assert data["next"] == LAST
