"""The daemon (DSP-01): one flock, a tick thread, a housekeeping thread and the board server."""

import contextlib
import fcntl
import logging
import logging.handlers
import os
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from . import dispatcher, housekeeping, protection, store, views, web
from .paths import add_file_log, dirs, ensure_dir
from .procs import Procs

HOUSEKEEPING_S = 60
LOCK_WAIT_S = 1.0
FAILURE_WAIT_S = 5.0
NOTIFY_S = 5
NOTIFY_TIMEOUT_S = 10.0
log = logging.getLogger("kanban.daemon")


def take_lock(path: Path) -> int | None:
    """DSP-01: a non-blocking exclusive flock, retried for 1 s (a CLI probe holds it for an instant)."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def running() -> bool:
    """The daemon holds `daemon.lock` exclusively; a shared probe never blocks another probe."""
    try:
        fd = os.open(dirs().home / "daemon.lock", os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def tick_loop(stop: threading.Event, memory: dict[str, str]) -> None:
    """`memory` holds the current skip reasons; the board reads it from its own threads."""
    conn, procs = store.connect(), Procs()
    nudge = dirs().home / "nudge"

    def stamp() -> int:
        try:
            return nudge.stat().st_mtime_ns
        except OSError:
            return 0

    while not stop.is_set():
        try:
            seen = stamp()
            dispatcher.dispatch_once(conn, store.now(), procs, memory=memory)
            for _ in range(int(store.cfg(conn, "dispatch.tick_seconds"))):
                if stop.wait(1) or stamp() != seen:
                    break
        except Exception:
            log.exception("tick failed")
            stop.wait(FAILURE_WAIT_S)


def housekeeping_loop(stop: threading.Event, unprotected: dict[str, list[str]]) -> None:
    """`unprotected` is the board's per-board list of repos without branch protection (GIT-08)."""
    conn = store.connect()
    while not stop.is_set():
        try:
            housekeeping.run_due(conn)
            protection.publish(conn, unprotected)
        except Exception:
            log.exception("housekeeping failed")
        stop.wait(HOUSEKEEPING_S)


def notify_new(conn: sqlite3.Connection) -> None:
    """Run `notify.command` once for each actionable event raised since the last pass, with the
    event line (as `kanban watch` prints it) on stdin. The cursor moves on whether or not a command
    is set or works, so a restart, a late `config set` or a failing command never replays events."""
    cursor = store.state_get(conn, "notify_cursor")
    if cursor is None:  # first pass: "new" begins here
        with store.tx(conn):
            top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
            store.state_put(conn, "notify_cursor", top)
        return
    while batch := store.events_since(conn, cursor, kinds=store.ACTIONABLE_EVENTS, limit=50):
        command = store.cfg(conn, "notify.command")
        for ev in batch:
            cursor = ev["id"]
            with store.tx(conn):  # before send: nothing that goes wrong there may replay or stall
                store.state_put(conn, "notify_cursor", cursor)
            if command and store.actionable(ev):
                send(command, views.clean(views.watch_line(ev)))


def argv(command: object) -> list[str]:
    """A list is the argv as it is (the form `jira.mcp_command` takes); a string is split like a shell would."""
    if isinstance(command, list) and command and all(isinstance(a, str) for a in command):
        return command
    if isinstance(command, str):
        return shlex.split(command)
    raise ValueError(f"not a command: {command!r}")


def send(command: object, line: str) -> None:
    """No shell, so the event text is never shell syntax; no output pipes, so a command that leaves
    a child behind cannot hold the thread past the timeout."""
    try:
        subprocess.run(
            argv(command), input=line + "\n", text=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=NOTIFY_TIMEOUT_S, check=True,
        )  # fmt: skip
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.warning("notify.command failed: %s", e)


def notify_loop(stop: threading.Event) -> None:
    conn = store.connect()
    while not stop.is_set():
        try:
            notify_new(conn)
        except Exception:
            log.exception("notify failed")
        stop.wait(NOTIFY_S)


def serve() -> int:
    """`kanban daemon`: 0 on SIGTERM and when another daemon already runs; 1 when a thread died
    or the board port cannot be bound."""
    home = ensure_dir(dirs().home)
    add_file_log(logging.getLogger("kanban"), home / "logs" / "daemon.log")
    lock = take_lock(home / "daemon.lock")
    if lock is None:
        log.info("another daemon holds %s; exiting", home / "daemon.lock")
        return 0
    memory: dict[str, str] = {}
    unprotected: dict[str, list[str]] = {}
    boot = store.connect()  # create and migrate once: two threads doing it together hit "locked"
    protection.publish(boot, unprotected)
    boot.close()
    try:  # dict.copy() is atomic under the GIL, so a request never sees a half-updated map
        board = web.make_server(
            dirs(),
            live=lambda slug: {"skips": memory.copy(), "unprotected": unprotected.get(slug, [])},
        )
    except OSError as e:
        log.error("cannot serve the board: %s", e)
        os.close(lock)
        return 1
    stop, signalled = threading.Event(), []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: signalled.append(True))
    threads = [
        threading.Thread(target=tick_loop, args=(stop, memory), name="tick", daemon=True),
        threading.Thread(target=notify_loop, args=(stop,), name="notify", daemon=True),
        threading.Thread(
            target=housekeeping_loop, args=(stop, unprotected), name="housekeeping", daemon=True
        ),
        threading.Thread(target=board.serve_forever, name="web", daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("daemon started (pid %s); board on http://127.0.0.1:%s/", os.getpid(), board.port)
    code = 0
    while not signalled:  # a handler must not take a lock: Event.set() there can deadlock us
        if dead := [t.name for t in threads if not t.is_alive()]:
            log.error("thread %s died; exiting so the supervisor restarts the daemon", dead[0])
            code = 1
            break
        time.sleep(0.2)
    stop.set()
    board.shutdown()
    for t in threads:
        t.join(timeout=5)
    board.server_close()
    log.info("daemon stopped; runners keep running")
    with contextlib.suppress(OSError):
        os.close(lock)
    return code
