"""TST-D (daemon): the flock singleton, the tick, housekeeping and board threads, SIGTERM (DSP-01)."""

import fcntl
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

from kanban_claude import daemon, dispatcher, housekeeping, paths, store, web

Card = Callable[..., str]


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
    monkeypatch.setattr(daemon, "FAILURE_WAIT_S", 0.01)
    daemon.tick_loop(stop, {})
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
    monkeypatch.setattr(daemon, "tick_loop", lambda stop, memory: None)
    assert daemon.serve() == 1
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
    monkeypatch.setattr(daemon, "tick_loop", tick)
    assert daemon.serve() == 1  # the stub tick returns, so its thread "dies"
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
    daemon.housekeeping_loop(stop, shared)
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
        assert daemon.serve() == 1
    assert "cannot serve the board" in (env / "kb-home" / "logs" / "daemon.log").read_text()


def test_sigterm_in_the_middle_of_a_tick_finishes_the_tick_and_exits_0(env: Path) -> None:
    started, finished = env / "tick-started", env / "tick-finished"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from kanban_claude import daemon, dispatcher\n"
        "real = dispatcher.dispatch_once\n"
        "def slow(*a, **k):\n"
        "    Path(sys.argv[1]).touch(); time.sleep(2); out = real(*a, **k)\n"
        "    Path(sys.argv[2]).touch(); return out\n"
        "dispatcher.dispatch_once = slow\n"
        "raise SystemExit(daemon.serve())\n"
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


def spawn_daemon(**extra: str) -> subprocess.Popen[str]:
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
    first = spawn_daemon()
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
    d = spawn_daemon()
    try:
        wait_for(lambda: store.get_task(conn, tid)["status"] == "done", "the card to finish")
    finally:
        d.send_signal(signal.SIGTERM)
        d.wait(timeout=20)
    run = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,)).fetchone()
    assert run["outcome"] == "completed" and run["runner_pid"]


def test_a_daemon_probe_ignores_a_concurrent_probe(env: Path) -> None:
    lock = env / "kb-home" / "daemon.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as other_probe:
        fcntl.flock(other_probe, fcntl.LOCK_SH)
        assert daemon.running() is False
        fcntl.flock(other_probe, fcntl.LOCK_UN)
        fcntl.flock(other_probe, fcntl.LOCK_EX)
        assert daemon.running() is True


def test_a_log_that_launchd_created_world_readable_is_made_private(tmp_path: Path) -> None:
    log = tmp_path / "logs" / "daemon.log"
    log.parent.mkdir()
    log.touch()
    log.chmod(0o644)  # launchd opens StandardOutPath under its own umask before the daemon starts
    logger = logging.getLogger("kanban.test-log-mode")
    handler = paths.add_file_log(logger, log)
    logger.removeHandler(handler)
    handler.close()
    assert log.stat().st_mode & 0o777 == 0o600


# --- notify.command (P1-4) ------------------------------------------------------------------


def notifier(env: Path) -> Path:
    """A command that appends its stdin to `notified.txt`."""
    path = env / "notify.sh"
    path.write_text(f"#!/bin/sh\ncat >> {env / 'notified.txt'}\n")
    path.chmod(0o755)
    return path


def raise_events(conn: Any, card: Card, *kinds: str) -> None:
    t = store.get_task(conn, card())
    with store.tx(conn):
        for kind in kinds:
            store.event(conn, t, kind, "worker:smith#1", {"kind": "needs_input"})


def sent(env: Path) -> list[str]:
    f = env / "notified.txt"
    return f.read_text().splitlines() if f.exists() else []


def test_notify_runs_the_command_once_per_new_actionable_event(
    conn: Any, card: Card, env: Path
) -> None:
    store.config_set(conn, "notify.command", f"{notifier(env)}")
    daemon.notify_new(conn)  # the first pass only marks where "new" begins
    raise_events(conn, card, "blocked", "commented", "merge_failed")
    daemon.notify_new(conn)
    lines = sent(env)
    assert [ln.split()[3] for ln in lines] == ["blocked", "merge_failed"]
    assert "(worker:smith#1)" in lines[0]
    daemon.notify_new(conn)
    assert sent(env) == lines


def test_notify_does_not_replay_old_events_after_a_restart_or_a_late_config(
    conn: Any, card: Card, env: Path
) -> None:
    raise_events(conn, card, "blocked")
    daemon.notify_new(conn)  # no command yet: the event is passed over, not queued
    store.config_set(conn, "notify.command", str(notifier(env)))
    daemon.notify_new(conn)
    assert sent(env) == []
    raise_events(conn, card, "gave_up")
    daemon.notify_new(store.connect())  # a restarted daemon: a new connection, the saved cursor
    assert [ln.split()[3] for ln in sent(env)] == ["gave_up"]


def test_notify_failures_are_logged_and_never_fatal(
    conn: Any,
    card: Card,
    env: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daemon.notify_new(conn)
    monkeypatch.setattr(daemon, "NOTIFY_TIMEOUT_S", 0.3)
    for command, why in (
        ("sh -c 'exit 3'", "exit status 3"),
        ("sleep 30", "timed out"),
        (str(env / "no-such-command"), "No such file"),
    ):
        store.config_set(conn, "notify.command", command)
        raise_events(conn, card, "blocked")
        with caplog.at_level(logging.WARNING, logger="kanban.daemon"):
            daemon.notify_new(conn)
        assert why in caplog.text, caplog.text
    monkeypatch.setattr(daemon, "NOTIFY_TIMEOUT_S", 10.0)
    store.config_set(conn, "notify.command", str(notifier(env)))
    raise_events(conn, card, "blocked")
    daemon.notify_new(conn)
    assert len(sent(env)) == 1
