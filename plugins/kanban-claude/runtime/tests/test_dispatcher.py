"""TST-D (dispatcher) plus the daemon, STO-06 repair and the owner verbs that touch the tick."""

import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, gitops, housekeeping, runner, store, web
from kanban_claude.dispatcher import dispatch_once
from kanban_claude.errors import Refused

from .conftest import FakeProcs, Kb

Card = Callable[..., str]
NOW = 2_000_000_000


def tick(conn: Any, procs: FakeProcs, ts: int | None = None, **kw: Any) -> dict[str, Any]:
    return dispatch_once(conn, ts or int(time.time()), procs, **kw)


def run_row(conn: Any, run_id: int) -> Any:
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def kinds(conn: Any) -> list[str]:
    return [r["kind"] for r in conn.execute("SELECT kind FROM task_events ORDER BY id")]


def to_review(conn: Any, tid: str) -> None:
    run = store.claim(conn, tid)
    assert run
    store.request_review(conn, run, "PR #7", pr_number=7, pr_url="https://example.invalid/p/7")


def test_d1_the_cap_limits_claims(conn: Any, card: Card, procs: FakeProcs) -> None:
    ids = [card(f"c{i}") for i in range(4)]
    first = tick(conn, procs)
    assert len(first["claimed"]) == 2 and first["slots"] == 2
    assert len(procs.spawned) == 2
    again = tick(conn, procs)
    assert again["claimed"] == [] and again["slots"] == 0
    assert sum(store.get_task(conn, i)["status"] == "running" for i in ids) == 2


def set_card(conn: Any, tid: str, **cols: Any) -> None:
    with store.tx(conn):
        for k, v in cols.items():
            conn.execute(f"UPDATE tasks SET {k} = ? WHERE id = ?", (v, tid))


def test_d2_ordering_priority_then_review_then_age(conn: Any, card: Card, procs: FakeProcs) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 1)
    old, newer, high = card("old"), card("newer"), card("high")
    set_card(conn, old, created_at=100)
    set_card(conn, newer, created_at=200)
    set_card(conn, high, created_at=300, priority=5)

    def first() -> list[str]:
        return [c["id"] for c in tick(conn, procs, dry_run=True)["claimed"]]

    assert first() == [high]
    set_card(conn, high, priority=0)
    assert first() == [old]
    rev = card("rev")
    to_review(conn, rev)
    set_card(conn, rev, created_at=900)
    assert first() == [rev], "a review goes before ready cards of equal priority"
    set_card(conn, high, priority=9)
    assert first() == [rev], "one slot is held for the review"


def test_d3_failure_limit_gives_up(conn: Any, card: Card, claim: Callable[[str], int]) -> None:
    tid = card("diagnostic")
    for i in range(2):
        run = claim(tid)
        with store.tx(conn):
            store.end_run(conn, run, "timed_out", "retry", actor="runner", event_kind="timed_out")
        assert store.get_task(conn, tid)["status"] == ("ready" if i == 0 else "blocked")
    t = store.get_task(conn, tid)
    assert t["block_kind"] == "gave_up" and "gave_up" in kinds(conn)


def test_d3_a_ready_card_at_the_limit_is_skipped(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = 2 WHERE id = ?", (tid,))
    assert tick(conn, procs)["skipped"] == {tid: "failure_limit"}


def test_a_ready_card_on_an_archived_board_is_skipped(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    tid = card()
    store.board_archive(conn, "main")
    report = tick(conn, procs)
    assert report["skipped"] == {tid: "board_archived"} and report["claimed"] == []
    assert procs.spawned == [] and store.get_task(conn, tid)["status"] == "ready"


def test_d4_quota_walls_hold_and_expire(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()
    cfg_dir = store.effective_config_dir(conn, store.get_board(conn, "main"))
    for field in ("hard_until", "soft_until"):
        with store.tx(conn):
            store.account_set(conn, cfg_dir, **{field: NOW + 100})
        held = tick(conn, procs, NOW)
        assert held["skipped"] == {tid: "quota_wall"} and held["claimed"] == []
        freed = tick(conn, procs, NOW + 200)
        assert [c["id"] for c in freed["claimed"]] == [tid]
        assert store.account_get(conn, cfg_dir)[field] is None
        with store.tx(conn):
            store.end_run(
                conn, run_of(conn, tid), "interrupted", "ready", actor="t", event_kind="interrupted"
            )
    assert kinds(conn).count("account_resumed") == 2


def run_of(conn: Any, tid: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()[0]
    )


def test_d5_unblock_starts_a_new_run(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    store.block(conn, claim(tid), "needs_input", "which branch?")
    assert store.get_task(conn, tid)["status"] == "blocked"
    assert tick(conn, procs)["claimed"] == []
    store.unblock(conn, tid, "use main")
    again = tick(conn, procs)
    assert [c["id"] for c in again["claimed"]] == [tid]
    assert (
        conn.execute(
            "SELECT n FROM task_runs WHERE id = ?", (again["claimed"][0]["run_id"],)
        ).fetchone()[0]
        == 2
    )


def test_d6_a_live_runner_with_stale_output_is_untouched(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    run = claim(card())
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 111, runner_started = 1, last_output_at = 1 WHERE id = ?",
            (run,),
        )
    procs.live.add(111)
    assert tick(conn, procs)["reconciled"] == []
    assert run_row(conn, run)["status"] == "running"


def test_d6_a_null_pid_is_ended_only_past_the_grace(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    run = claim(card())
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 5, run))
    assert tick(conn, procs)["reconciled"] == []
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 60, run))
    done = tick(conn, procs)["reconciled"]
    assert done == [
        {"run_id": run, "task": run_row(conn, run)["task_id"], "outcome": "interrupted"}
    ]


def test_d7_a_dead_runner_is_crashed_and_its_claude_group_killed(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 222, runner_started = 1, claude_pid = 333, "
            "claude_started = 1 WHERE id = ?",
            (run,),
        )
    procs.live.add(333)
    assert tick(conn, procs)["reconciled"][0]["outcome"] == "crashed"
    assert procs.killed == [333]
    t = store.get_task(conn, tid)
    assert t["consecutive_failures"] == 1 and t["status"] == "ready" and t["retry_after"]


def test_d8_a_run_older_than_the_boot_is_interrupted(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 222, runner_started = 1 WHERE id = ?", (run,)
        )
    procs.boot = store.now() + 10
    assert tick(conn, procs)["reconciled"][0]["outcome"] == "interrupted"
    assert store.get_task(conn, tid)["consecutive_failures"] == 0


def test_d9_a_restarted_dispatcher_adopts_running_runners(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 1)
    first, second = card("a"), card("b")
    claimed = tick(conn, procs)["claimed"]
    run = claimed[0]["run_id"]
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 444, runner_started = 1 WHERE id = ?", (run,)
        )
    fresh = FakeProcs()
    fresh.live.add(444)
    report = tick(conn, fresh)
    assert report["reconciled"] == [] and report["claimed"] == [] and report["slots"] == 0
    assert {store.get_task(conn, first)["status"], store.get_task(conn, second)["status"]} == {
        "running",
        "ready",
    }


def test_d10_a_live_previous_run_blocks_the_card(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 555, runner_started = 1 WHERE id = ?", (run,)
        )
        store.end_run(conn, run, "interrupted", "ready", actor="t", event_kind="interrupted")
    procs.live.add(555)
    assert tick(conn, procs)["skipped"] == {tid: "previous_run_alive"}


def test_d11_a_missing_workspace_is_skipped(
    conn: Any, board: str, env: Path, procs: FakeProcs
) -> None:
    work = env / "repos" / "gone"
    work.mkdir()
    tid = str(
        store.create(conn, board, "d", body="b", assignee="smith", workspace=f"dir:{work}")["id"]
    )
    assert [c["id"] for c in tick(conn, procs, dry_run=True)["claimed"]] == [tid]
    work.rmdir()
    assert tick(conn, procs)["skipped"] == {tid: "workspace_invalid"}


def test_d12_a_paused_account_does_not_hold_another(conn: Any, env: Path, procs: FakeProcs) -> None:
    dirs_ = {slug: env / f"claude-{slug}" for slug in ("a", "b")}
    ids = {}
    for slug, cfg_dir in dirs_.items():
        cfg_dir.mkdir()
        work = env / "repos" / slug
        work.mkdir(parents=True)
        store.board_create(conn, slug, default_workdir=str(work), claude_config_dir=str(cfg_dir))
        ids[slug] = str(store.create(conn, slug, "t", body="b", assignee="smith")["id"])
    with store.tx(conn):
        store.account_set(conn, str(dirs_["a"]), auth_paused={"since": 1, "error": "x"})
    report = tick(conn, procs)
    assert report["skipped"] == {ids["a"]: "account_paused"}
    assert [c["id"] for c in report["claimed"]] == [ids["b"]]


def test_d13_the_tick_never_calls_git_or_gh(
    conn: Any, card: Card, repo: Path, monkeypatch: pytest.MonkeyPatch, procs: FakeProcs
) -> None:
    store.board_create(conn, "w", default_workdir=str(repo))
    store.create(
        conn, "w", "wt", body="b", assignee="smith", workspace=f"worktree:{repo}", branch="feat/y"
    )
    card("scratch")
    card("plain", workspace=f"dir:{repo}")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the tick spawned a process")

    for target in (subprocess.run, subprocess.Popen, subprocess.check_output, os.system):
        monkeypatch.setattr(
            target.__module__ and sys.modules[target.__module__], target.__name__, boom
        )
    monkeypatch.setattr(gitops, "run", boom)
    report = tick(conn, procs)
    assert len(report["claimed"]) == 2 and not report["skipped"]


def test_a_paused_dispatcher_claims_nothing(conn: Any, card: Card, procs: FakeProcs) -> None:
    card()
    store.pause(conn, "testing")
    report = tick(conn, procs)
    assert report["paused"] and report["claimed"] == [] and procs.spawned == []


def test_a_dry_run_changes_nothing(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    ready = card()
    dead = claim(card("other"))
    before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 99, dead))
    report = tick(conn, procs, dry_run=True)
    assert [c["id"] for c in report["claimed"]] == [ready]
    assert run_row(conn, dead)["status"] == "running" and procs.spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before


def test_sto06_a_running_card_without_a_run_goes_back(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    tick(conn, procs, dry_run=False)
    assert "repaired" in kinds(conn)
    assert store.get_task(conn, tid)["status"] in ("ready", "running")


def test_never_started_runners_trip_the_systemic_pause(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 5)
    for i in range(3):
        run = claim(card(f"c{i}"))
        with store.tx(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 60, run)
            )
    assert len(tick(conn, procs)["reconciled"]) == 3
    paused = store.state_get(conn, "dispatch_paused")
    assert paused and paused["reason"] == "systemic"
    assert all(
        store.get_task(conn, r["task_id"])["consecutive_failures"] == 0
        for r in conn.execute("SELECT task_id FROM task_runs")
    )


def test_a_spawn_failure_ends_the_run(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()

    def bad(run_id: int, log: Path) -> None:
        raise OSError("no fork")

    procs.spawn = bad  # type: ignore[method-assign]
    report = tick(conn, procs)
    assert (
        report["claimed"] and run_row(conn, report["claimed"][0]["run_id"])["outcome"] == "crashed"
    )
    assert store.get_task(conn, tid)["status"] == "ready"


# --- one failure never stops the tick (DSP-02, DSP-04) ----------------------------------------


class ProbeFails(FakeProcs):
    """`ps` itself fails for the listed pids: a timeout, or output that does not parse."""

    def __init__(self, *failing: int) -> None:
        super().__init__()
        self.failing = set(failing)

    def alive(self, pid: int | None, started: int | None) -> bool:
        if pid in self.failing:
            raise subprocess.TimeoutExpired("ps", 10)
        return super().alive(pid, started)


def test_a_failed_liveness_probe_skips_the_run_and_never_kills_it(
    conn: Any, card: Card, claim: Callable[[str], int]
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 3)
    unsure, gone = claim(card("unsure")), claim(card("gone"))
    fresh = card("fresh")
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 111, runner_started = 1, claude_pid = 112, "
            "claude_started = 1 WHERE id = ?",
            (unsure,),
        )
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 90, gone))
    probes = ProbeFails(111)
    report = tick(conn, probes)
    assert run_row(conn, unsure)["status"] == "running" and probes.killed == []
    assert [r["run_id"] for r in report["reconciled"]] == [gone]
    claimed = {c["id"] for c in report["claimed"]}
    assert fresh in claimed and run_row(conn, unsure)["task_id"] not in claimed, "the tick went on"


def test_a_card_whose_probe_fails_is_skipped_and_the_others_dispatch(
    conn: Any, card: Card, claim: Callable[[str], int]
) -> None:
    held = card("held")
    held_run = claim(held)
    with store.tx(conn):
        conn.execute(
            "UPDATE task_runs SET runner_pid = 777, runner_started = 1 WHERE id = ?", (held_run,)
        )
        store.end_run(conn, held_run, "interrupted", "ready", actor="t", event_kind="interrupted")
    fine = card("fine")
    report = tick(conn, ProbeFails(777))
    assert report["skipped"] == {held: "error"}
    assert [c["id"] for c in report["claimed"]] == [fine]


def test_a_claim_that_raises_does_not_stop_the_others(
    conn: Any, card: Card, procs: FakeProcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursed, fine = card("cursed"), card("fine")
    real_claim = store.claim

    def claim_fails(c: Any, task_id: str, **kw: Any) -> int | None:
        if task_id == cursed:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(c, task_id, **kw)

    monkeypatch.setattr(store, "claim", claim_fails)
    assert [c["id"] for c in tick(conn, procs)["claimed"]] == [fine]


@pytest.mark.parametrize("error", [sqlite3.IntegrityError("UNIQUE"), Refused("changed")])
def test_a_claim_that_loses_the_race_says_so_in_the_log(
    conn: Any, card: Card, error: Exception, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    tid = card()

    def lose(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr(store, "_cas", lose)
    assert store.claim(conn, tid) is None
    assert tid in caplog.text and "not claimed" in caplog.text


def test_a_run_whose_card_is_not_running_is_ended_without_the_card(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    set_card(conn, tid, status="blocked")
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET started_at = ? WHERE id = ?", (store.now() - 90, run))
    assert tick(conn, procs)["reconciled"][0]["run_id"] == run
    assert (
        run_row(conn, run)["status"] == "ended" and run_row(conn, run)["outcome"] == "interrupted"
    )
    assert store.get_task(conn, tid)["status"] == "blocked"
    assert tick(conn, procs)["slots"] == 2, "the run no longer holds a slot"


def test_the_boot_time_is_read_only_when_a_dead_runner_needs_it(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tick(conn, procs)
    assert procs.boot_calls == 0
    run = claim(card())
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET runner_pid = 5, runner_started = 1 WHERE id = ?", (run,))
    tick(conn, procs)
    assert procs.boot_calls == 1


# --- the daemon threads (DSP-01) ------------------------------------------------------------


def test_the_tick_thread_survives_a_failure_anywhere_in_its_body(
    conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop, calls = threading.Event(), []

    def once(*_a: Any, **_k: Any) -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 4:
            stop.set()
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return {}

    real_cfg = store.cfg

    def cfg(c: Any, key: str) -> Any:
        if key == "dispatch.tick_seconds":
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return 0
        return real_cfg(c, key)

    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self: self.name == "nudge" or real_exists(self)
    )  # the nudge file vanishes between exists() and stat()
    monkeypatch.setattr(dispatcher, "dispatch_once", once)
    monkeypatch.setattr(store, "cfg", cfg)
    monkeypatch.setattr(dispatcher, "FAILURE_WAIT_S", 0.01)
    dispatcher.tick_loop(stop, {})
    assert len(calls) == 4


def any_port() -> None:
    """The daemon serves the board on `web.port`; 0 keeps tests off a real daemon's port."""
    c = store.connect()
    with store.tx(c):
        store.config_put(c, "web.port", 0)
    c.close()


@pytest.fixture
def in_process_daemon() -> Any:
    """`serve()` takes SIGTERM/SIGINT and adds a log handler; both are put back afterwards."""
    any_port()
    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    kanban_log = logging.getLogger("kanban")
    before = list(kanban_log.handlers)
    yield
    for s, h in old.items():
        signal.signal(s, h)
    for h in kanban_log.handlers[:]:
        if h not in before:
            kanban_log.removeHandler(h)
            h.close()


def test_a_dead_thread_makes_the_daemon_exit_non_zero(
    env: Path, in_process_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher, "tick_loop", lambda stop, memory: None)
    assert dispatcher.serve() == 1
    assert "thread tick died" in (env / "kb-home" / "logs" / "daemon.log").read_text()


def test_the_daemon_serves_the_board_with_the_tick_skip_reasons(
    env: Path, in_process_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}
    real = web.make_server
    c = store.connect()
    store.state_put(c, "protection", {"boards": {"any": ["host/acme/proj"]}})
    c.close()

    def make_server(*a: Any, **k: Any) -> web.WebServer:
        seen["srv"] = real(*a, **k)
        return seen["srv"]

    def tick(stop: threading.Event, memory: dict[str, str]) -> None:
        memory["t_1"] = "no_assignee"
        srv = seen["srv"]
        seen["live"] = srv.live("any")
        url = f"http://127.0.0.1:{srv.port}/healthz?nonce=n"
        with urllib.request.urlopen(url, timeout=5) as r:
            seen["status"] = r.status

    monkeypatch.setattr(web, "make_server", make_server)
    monkeypatch.setattr(dispatcher, "tick_loop", tick)
    assert dispatcher.serve() == 1  # the stub tick returns, so its thread "dies"
    assert seen["status"] == 200
    assert seen["live"] == {"skips": {"t_1": "no_assignee"}, "unprotected": ["host/acme/proj"]}
    assert seen["srv"].socket.fileno() == -1, "the board socket outlived the daemon"


def test_the_housekeeping_loop_publishes_the_fresh_protection_list_after_each_wakeup(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop, shared = threading.Event(), {"old": ["gone/x/y"]}

    def wakeup(conn: Any) -> None:
        store.state_put(conn, "protection", {"boards": {"main": ["host/acme/proj"]}})
        stop.set()

    monkeypatch.setattr(housekeeping, "run_due", wakeup)
    dispatcher.housekeeping_loop(stop, shared)
    assert shared == {"main": ["host/acme/proj"]}


def test_a_daemon_that_cannot_bind_the_board_port_exits_non_zero(
    env: Path, in_process_daemon: Any
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        c = store.connect()
        store.config_set(c, "web.port", taken.getsockname()[1])
        c.close()
        assert dispatcher.serve() == 1
    assert "cannot serve the board" in (env / "kb-home" / "logs" / "daemon.log").read_text()


def test_sigterm_in_the_middle_of_a_tick_finishes_the_tick_and_exits_0(env: Path) -> None:
    started, finished = env / "tick-started", env / "tick-finished"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from kanban_claude import dispatcher\n"
        "real = dispatcher.dispatch_once\n"
        "def slow(*a, **k):\n"
        "    Path(sys.argv[1]).touch(); time.sleep(2); out = real(*a, **k)\n"
        "    Path(sys.argv[2]).touch(); return out\n"
        "dispatcher.dispatch_once = slow\n"
        "raise SystemExit(dispatcher.serve())\n"
    )
    any_port()
    d = subprocess.Popen(
        [sys.executable, "-c", code, str(started), str(finished)], env=os.environ.copy(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    wait_for(started.exists, "the tick to start")
    d.send_signal(signal.SIGTERM)
    assert d.wait(timeout=20) == 0
    assert finished.exists(), "the tick was cut short"
    assert "runners keep running" in (env / "kb-home" / "logs" / "daemon.log").read_text()


# --- the daemon and the owner verbs ---------------------------------------------------------


def daemon(**extra: str) -> subprocess.Popen[str]:
    any_port()
    return subprocess.Popen(
        [sys.executable, "-m", "kanban_claude", "daemon"], env={**os.environ, **extra},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip


def wait_for(check: Callable[[], bool], what: str, timeout: float = 30) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_the_daemon_is_a_singleton_and_leaves_runners_alone(env: Path, conn: Any) -> None:
    first = daemon()
    log = env / "kb-home" / "logs" / "daemon.log"
    wait_for(lambda: log.exists() and "daemon started" in log.read_text(), "the daemon to start")
    second = subprocess.run(
        [sys.executable, "-m", "kanban_claude", "daemon"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 0 and "another daemon" in log.read_text()
    first.send_signal(signal.SIGTERM)
    assert first.wait(timeout=20) == 0
    assert "runners keep running" in log.read_text()


def test_the_daemon_dispatches_a_card_end_to_end(
    env: Path, conn: Any, card: Card, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.config_set(conn, "dispatch.tick_seconds", 1)
    tid = card("e2e")
    monkeypatch.setenv("FAKE_CLAUDE_TERMINAL", json.dumps(["complete", tid, "--summary", "ok"]))
    d = daemon()
    try:
        wait_for(lambda: store.get_task(conn, tid)["status"] == "done", "the card to finish")
    finally:
        d.send_signal(signal.SIGTERM)
        d.wait(timeout=20)
    run = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
    assert run["outcome"] == "completed" and run["runner_pid"]


def test_pause_resume_and_dry_run_verbs(kb: Kb, card: Card) -> None:
    tid = card()
    assert kb("pause", "--reason", "maintenance")[0] == 0
    code, out, _ = kb("dispatch", "--dry-run", "--json")
    assert code == 0 and json.loads(out)["paused"] is True
    assert kb("resume")[0] == 0
    code, out, _ = kb("dispatch", "--dry-run", "--json")
    assert [c["id"] for c in json.loads(out)["claimed"]] == [tid]
    assert (Path(os.environ["KANBAN_HOME"]) / "nudge").exists()


def test_resume_clears_only_accounts_whose_login_passes(
    kb: Kb, conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("good", "bad"):
        with store.tx(conn):
            store.account_set(conn, name, auth_paused={"since": 1, "error": "x"})
    monkeypatch.setattr(
        runner, "auth_probe", lambda _conn, cfg: None if cfg == "good" else "probe failed: boom"
    )
    code, out, _ = kb("resume", "--json")
    data = json.loads(out)
    assert code == 0 and data["accounts_cleared"] == ["good"]
    assert data["accounts_failing"] == {"bad": "probe failed: boom"}
    assert not store.account_get(conn, "good").get("auth_paused")
    assert store.account_get(conn, "bad").get("auth_paused")
    text = kb("resume")[1]
    assert "bad: probe failed: boom" in text and "login still failing" not in text


def test_snapshot_verb_prints_a_path(kb: Kb, origin: Path, repo: Path) -> None:
    code, out, _ = kb("snapshot", str(repo))
    assert code == 0 and Path(out.strip()).is_dir()
    assert (Path(out.strip()) / "a.txt").exists()
