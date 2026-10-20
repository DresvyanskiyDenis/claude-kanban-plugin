"""TST-D (daemon): the flock singleton, the tick, housekeeping and board threads, SIGTERM (DSP-01)."""

import fcntl
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import __version__, daemon, dispatcher, housekeeping, paths, store

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


@pytest.fixture
def in_process_daemon() -> Any:
    """`serve()` takes SIGTERM/SIGINT and adds a log handler; both are put back afterwards."""
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


def test_the_housekeeping_loop_runs_what_is_due_after_each_wakeup(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stop, calls = threading.Event(), []
    monkeypatch.setattr(housekeeping, "run_due", lambda conn: (calls.append(1), stop.set()))
    daemon.housekeeping_loop(stop)
    assert calls == [1]


def test_the_daemon_leaves_its_pid_and_version_in_the_lock_for_probes(
    env: Path, in_process_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def tick(stop: threading.Event, memory: dict[str, str]) -> None:
        seen["held"] = daemon.probe()

    monkeypatch.setattr(daemon, "tick_loop", tick)
    assert daemon.serve() == 1  # the stub tick returns, so its thread "dies"
    assert seen["held"] == {"pid": os.getpid(), "version": __version__}
    assert daemon.probe() is None


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


def test_a_probe_tells_a_held_lock_from_a_missing_or_released_one(env: Path) -> None:
    lock = env / "kb-home" / "daemon.lock"
    assert daemon.probe() is None
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()
    assert daemon.probe() is None
    with lock.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        assert daemon.probe() == {}  # held, but the holder has not written its record yet
        holder.write("not json")
        holder.flush()
        assert daemon.probe() == {}


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
