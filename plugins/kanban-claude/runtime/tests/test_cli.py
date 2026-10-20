"""TST-L: board resolution (L1), worker-mode gating (L2), show --json (L3), watch replay (L4)."""

import fcntl
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import store

from .conftest import Kb, git


@pytest.fixture
def workdirs(env: Path, kb: Kb) -> tuple[Path, Path]:
    dirs = []
    for slug in ("alpha", "beta"):
        d = env / "repos" / slug
        d.mkdir(parents=True)
        git("init", "-b", "main", cwd=d)
        assert kb("boards", "create", slug, "--default-workdir", str(d))[0] == 0
        dirs.append(d)
    return dirs[0], dirs[1]


def test_l1_board_resolution_order(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = workdirs
    monkeypatch.chdir(env_of(alpha))
    code, _, err = kb("list")
    assert code == 2 and "no board" in err
    monkeypatch.chdir(beta)
    assert kb("create", "In beta", "--body", "x", "--assignee", "smith", "--json")[0] == 0
    assert json.loads(kb("list", "--json")[1])["board"] == "beta"
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    assert json.loads(kb("list", "--json")[1])["board"] == "alpha"
    assert json.loads(kb("--board", "beta", "list", "--json")[1])["board"] == "beta"
    assert json.loads(kb("list", "--board", "beta", "--json")[1])["cards"][0]["title"] == "In beta"
    assert kb("--board", "nope", "list")[0] == 3


def env_of(path: Path) -> Path:
    return path.parent.parent


def test_create_show_list_round_trip(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    monkeypatch.setattr(sys, "stdin", io.StringIO("multi\nline body"))
    code, out, err = kb("create", "Title", "--assignee", "smith", "--body-file", "-", "--json")
    assert code == 0 and "daemon not running" in err
    tid = json.loads(out)["id"]
    d = json.loads(kb("show", tid, "--json")[1])
    assert d["schema"] == 1 and d["task"]["body"] == "multi\nline body"
    assert d["task"]["status"] == "ready"
    assert tid in kb("list", "--status", "ready")[1]
    assert kb("show", "t_00000000")[0] == 3
    assert kb("show")[0] == 2
    assert kb("create", "x", "--body", "a", "--body-file", "-")[0] == 2


def test_cli_08_warning_follows_the_daemon_lock(
    kb: Kb, workdirs: tuple[Path, Path], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    args = ("create", "T", "--body", "b", "--assignee", "smith")
    assert "daemon not running" in kb(*args)[2]
    lock = env / "kb-home" / "daemon.lock"
    with lock.open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        code, _, err = kb(*args)
    assert code == 0 and err == ""


def test_lifecycle_verbs_and_exit_codes(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    tid = json.loads(kb("create", "T", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    assert kb("hold", tid, "--reason", "wait")[0] == 0
    assert json.loads(kb("show", tid, "--json")[1])["task"]["status"] == "triage"
    assert kb("unblock", tid, "--reason", "not blocked")[0] == 4
    assert kb("specify", tid, "--assignee", "smith")[0] == 0
    assert kb("specify", tid, "--assignee", "smith")[0] == 4
    assert kb("promote", tid, "--reason", "already ready")[0] == 4
    assert kb("hold", tid)[0] == 2
    assert kb("archive", tid, "--rm")[0] == 2
    assert kb("archive", tid, "--rm", "--yes")[0] == 0
    assert kb("show", tid)[0] == 3


def test_config_pause_stats_and_stubs(kb: Kb, workdirs: tuple[Path, Path]) -> None:
    assert kb("config", "set", "dispatch.max_in_progress", "3")[0] == 0
    assert kb("config", "get", "dispatch.max_in_progress")[1].strip() == "3"
    assert kb("config", "set", "no.such.key", "1")[0] != 0
    assert kb("config", "get", "no.such.key")[0] == 3
    assert "dispatch.roles" in kb("config", "list")[1]
    assert kb("pause", "--reason", "x")[0] == 0 and kb("resume")[1].strip() == "dispatch resumed"
    assert json.loads(kb("stats", "--json")[1])["schema"] == 1
    assert kb("migrate")[1].strip().startswith("schema version")
    assert kb("block", "t_1", "--kind", "needs_input", "--reason", "x")[0] == 5


def test_sec4_output_is_inert(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    created = kb(
        "create", "ti\x1b[2Jtle\x07", "--body", "b\x1b]0;x\x07", "--assignee", "smith", "--json"
    )
    tid = json.loads(created[1])["id"]
    assert kb("comment", tid, "c\x1b[31mred")[0] == 0
    for args in (("list",), ("list", "--json"), ("show", tid), ("show", tid, "--json")):
        out = kb(*args)[1]
        assert "\x1b" not in out and "\x07" not in out
    assert "title" in kb("list")[1]


def test_context_via_cli_frames_comments(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    tid = json.loads(kb("create", "T", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    kb("comment", tid, "hello", "--author", "smith")
    out = kb("context", tid)[1]
    assert out.startswith(f"# Kanban task {tid}: T") and "comment from worker `smith`" in out


def test_attach_and_grant_and_runs(
    kb: Kb, workdirs: tuple[Path, Path], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    tid = json.loads(kb("create", "T", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    f = env / "report.txt"
    f.write_text("data")
    assert kb("attach", tid, str(f))[0] == 0
    assert kb("attach", tid, str(env / "missing"))[0] == 1
    blob_dir = Path(os.environ["KANBAN_HOME"]) / "attachments" / tid
    assert [p.read_text() for p in blob_dir.iterdir()] == ["data"]
    assert str(blob_dir) in kb("context", tid)[1]
    assert kb("grant", tid, "--allow", "Bash(make test)", "--approved", "ok · chat · today")[0] == 0
    assert "Bash(make test)" in kb("grants", tid)[1]
    assert kb("grant", tid, "--allow", "Bash(*)", "--approved", "x")[0] == 4
    assert kb("grant", tid, "--revoke")[0] == 0
    assert kb("runs", tid)[1].strip() == "(no runs)"
    assert kb("log", tid)[0] == 3


def test_log_reads_only_under_runs_and_streams(
    kb: Kb, workdirs: tuple[Path, Path], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    tid = json.loads(kb("create", "T", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    conn = store.connect()
    run = store.claim(conn, tid)
    good = Path(os.environ["KANBAN_HOME"]) / "runs" / "alpha" / tid / "run-1.log"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("one\ntwo\nthree\n")
    conn.execute("UPDATE task_runs SET log_path = ? WHERE id = ?", (str(good), run))
    assert kb("log", tid, "--tail", "2")[1] == "two\nthree\n"
    for elsewhere in (
        env / "elsewhere.log",
        Path(os.environ["KANBAN_HOME"]) / "logs" / "daemon.log",
    ):
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text("secret")
        conn.execute("UPDATE task_runs SET log_path = ? WHERE id = ?", (str(elsewhere), run))
        code, out, _ = kb("log", tid)
        assert code == 4 and "secret" not in out


# --- worker mode ----------------------------------------------------------------------------


def test_l2_owner_verbs_exit_5_without_touching_the_db(
    kb: Kb, wt: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("an owner verb in worker mode opened the DB")

    monkeypatch.setattr(store, "connect", forbidden)
    monkeypatch.setenv("KANBAN_RUN_ID", str(wt[1]))
    for args in (
        ("list",),
        ("create", "x"),
        ("promote", "t_1", "--reason", "r"),
        ("config", "list"),
        ("watch",),
        ("boards", "list"),
    ):
        code, _, err = kb(*args)
        assert code == 5, args
        assert "owner verb" in err


def test_l2_worker_verbs_act_on_the_run_card_only(
    kb: Kb, wt: tuple[str, int], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid, run = wt
    monkeypatch.setenv("KANBAN_RUN_ID", str(run))
    assert json.loads(kb("show", "--json")[1])["task"]["id"] == tid
    assert "# Kanban task" in kb("context")[1]
    assert kb("show", "t_other")[0] == 5
    assert kb("comment", "t_other", "hi")[0] == 5
    assert kb("comment", tid, "from the worker")[0] == 0
    f = env / "scout.md"
    f.write_text("report")
    assert kb("attach", tid, str(f))[0] == 0
    assert kb("attach", tid, str(env / "missing"))[0] == 1
    assert kb("block", tid, "--kind", "needs_input", "--reason", "which one?")[0] == 0
    assert (
        kb("block", tid, "--kind", "needs_input", "--reason", "which one?")[1].strip()
        == "already done"
    )
    for args in (("comment", tid, "late"), ("show",), ("complete", tid, "--summary", "s")):
        code, _, err = kb(*args)
        assert code == 4 and "run ended" in err, args
    conn = store.connect()
    row = conn.execute("SELECT author FROM task_comments WHERE body = ?", ("from the worker",))
    assert row.fetchone()["author"] == "smith"
    assert store.get_task(conn, tid)["status"] == "blocked"


def test_worker_mode_needs_a_valid_run(kb: Kb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANBAN_RUN_ID", "nope")
    assert kb("show", "t_1")[0] == 2
    monkeypatch.setenv("KANBAN_RUN_ID", "9999")
    code, _, err = kb("show")
    assert code == 4 and "run ended" in err


# --- watch ----------------------------------------------------------------------------------


def test_l4_watch_replays_from_an_event_id(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    a = json.loads(kb("create", "A", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    b = json.loads(kb("create", "B", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    assert kb("watch")[1] == ""
    everything = kb("watch", "--since-start")[1].splitlines()
    assert any(f" {a} created" in line for line in everything)
    first = int(everything[0].split("#")[1].split()[0])
    replay = kb("watch", "--since", str(first))[1].splitlines()
    assert len(replay) == len(everything) - 1
    only = kb("watch", "--since-start", "--kinds", "created")[1].splitlines()
    assert [line.split()[2] for line in only] == [a, b]
    lines = kb("watch", "--since-start", "--json")[1].splitlines()
    assert json.loads(lines[0])["schema"] == 1


def test_l4_watch_shows_the_events_of_the_daemon_and_of_housekeeping(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    a = json.loads(kb("create", "A", "--body", "b", "--assignee", "smith", "--json")[1])["id"]
    conn = store.connect()
    with store.tx(conn):
        t = store.get_task(conn, a)
        store.event(conn, t, "repaired", "dispatcher", {"reason": "running without a run"})
        store.event(conn, t, "worktree_kept", "housekeeping", {"state": "kept_dirty"})
        store.event(conn, None, "account_resumed", "dispatcher", {"config_dir": "x"})
    conn.close()
    out = kb("watch", "--since-start")[1]
    assert all(kind in out for kind in ("repaired", "worktree_kept", "account_resumed"))


def test_l4_watch_follow_exits_0_on_sigterm(workdirs: tuple[Path, Path], kb: Kb) -> None:
    assert kb("create", "A", "--board", "alpha", "--body", "b", "--assignee", "smith")[0] == 0
    p = subprocess.Popen(
        [sys.executable, "-m", "kanban_claude", "watch", "--since-start", "--follow"],
        stdout=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    assert p.stdout and "created" in p.stdout.readline()
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=20) == 0


SEC3_NEGATIVES = [
    ("boards", "create", "Bad Slug!", "--default-workdir", "$HOME"),
    ("show", "not-an-id"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--model", "bad model!"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--effort", "extreme"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--budget", "0"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--budget", "100000"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--skill", "../escape"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--max-runtime", "soon"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--branch", "HEAD"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--branch", "a..b"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--workspace", "dir:relative/path"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--workspace", "dir:$HOME"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--workspace", "dir:$HOME/.ssh"),
    ("create", "T", "--assignee", "smith", "--body", "b", "--workspace", "dir:$KANBAN_HOME"),
]


@pytest.mark.parametrize("argv", SEC3_NEGATIVES, ids=lambda a: " ".join(a[3:]) or a[0])
def test_sec3_each_validator_refuses_through_the_cli(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, argv: tuple[str, ...]
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    code, out, err = kb(*(os.path.expandvars(a) for a in argv))
    assert code == 4, (out, err)
    assert kb("list", "--json")[0] == 0
    assert json.loads(kb("list", "--json")[1])["cards"] == []


def test_boards_refuses_the_dropped_deny_read_flag(kb: Kb, tmp_path: Path) -> None:
    code, _, err = kb(
        "boards", "create", "x", "--default-workdir", str(tmp_path), "--deny-read", "/p"
    )
    assert code == 2 and "--deny-read" in err


def test_a_corrupt_database_is_a_clean_error(kb: Kb, env: Path) -> None:
    db = env / "kb-home" / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a database\n" * 64)
    code, _, err = kb("list")
    assert code == 1 and "database" in err and "Traceback" not in err


def test_touches_flag_repeats_and_edit_clears_it(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    code, out, err = kb(
        "create", "T", "--body", "b", "--assignee", "smith", "--touches", "docs/**",
        "--touches", "README.md", "--json",
    )  # fmt: skip
    assert code == 0, err
    tid = json.loads(out)["id"]
    shown = kb("show", tid)[1]
    assert "touches: docs/**, README.md" in shown
    assert kb("edit", tid, "--touches", "")[0] == 0
    assert "touches:" not in kb("show", tid)[1]
    assert json.loads(kb("show", tid, "--json")[1])["task"]["touches"] is None


def test_create_scratch_with_a_branch_names_the_fix(
    kb: Kb, workdirs: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_BOARD", "alpha")
    code, _, err = kb("create", "T", "--body", "b", "--assignee", "smith", "--branch", "feat/x")
    assert code == 4 and "--workspace worktree:<repo>" in err


def test_config_set_turns_the_budget_caps_on_and_off(kb: Kb, workdirs: tuple[Path, Path]) -> None:
    def get(key: str) -> Any:
        return json.loads(kb("config", "get", key, "--json")[1])["value"]

    assert get("budget.max_usd_per_card") == 15.0
    assert kb("config", "set", "budget.max_usd_per_card", "0")[0] == 0
    assert get("budget.max_usd_per_card") == 0
    assert kb("config", "set", "budget.max_usd_per_board_day", "75")[0] == 0
    assert get("budget.max_usd_per_board_day") == 75
    assert kb("config", "set", "budget.max_usd_per_board_day", "null")[0] == 0
    assert get("budget.max_usd_per_board_day") is None
    assert kb("config", "set", "budget.max_usd_per_board_day", '"lots"')[0] == 4
    assert kb("config", "set", "budget.max_usd_per_card", "-3")[0] == 4
