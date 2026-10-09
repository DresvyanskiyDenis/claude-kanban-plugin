"""The daemon (DSP-01): one flock, a tick thread, a housekeeping thread and the board server."""

import contextlib
import fcntl
import logging
import logging.handlers
import os
import signal
import threading
import time
from pathlib import Path

from . import dispatcher, housekeeping, protection, store, web
from .paths import add_file_log, dirs, ensure_dir
from .procs import Procs

HOUSEKEEPING_S = 60
LOCK_WAIT_S = 1.0
FAILURE_WAIT_S = 5.0
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
