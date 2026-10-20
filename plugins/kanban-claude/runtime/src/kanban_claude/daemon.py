"""The daemon (DSP-01): one flock, a tick thread and a housekeeping thread."""

import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, dispatcher, housekeeping, store
from .paths import add_file_log, dirs, ensure_dir
from .procs import Procs

HOUSEKEEPING_S = 60
LOCK_WAIT_S = 1.0
FAILURE_WAIT_S = 5.0
log = logging.getLogger("kanban.daemon")


def take_lock(path: Path) -> int | None:
    """DSP-01: a non-blocking exclusive flock, retried for 1 s (a CLI probe holds it for an instant).
    The holder writes its pid and version into the file for `probe`."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "version": __version__}).encode())
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def probe() -> dict[str, Any] | None:
    """The daemon holds `daemon.lock` exclusively; a shared probe never blocks another probe.
    Returns what the holder wrote ({} if it has not yet), or None when no daemon holds the lock."""
    try:
        fd = os.open(dirs().home / "daemon.lock", os.O_RDONLY)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        try:
            held = json.loads(os.read(fd, 256))
        except ValueError:
            return {}
        return held if isinstance(held, dict) else {}
    finally:
        os.close(fd)
    return None


def running() -> bool:
    return probe() is not None


def tick_loop(stop: threading.Event, memory: dict[str, str]) -> None:
    """`memory` holds the current skip reasons, so a `skipped` event fires only when one changes."""
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


def housekeeping_loop(stop: threading.Event) -> None:
    conn = store.connect()
    while not stop.is_set():
        try:
            housekeeping.run_due(conn)
        except Exception:
            log.exception("housekeeping failed")
        stop.wait(HOUSEKEEPING_S)


def serve() -> int:
    """`kanban daemon`: 0 on SIGTERM and when another daemon already runs; 1 when a thread died."""
    home = ensure_dir(dirs().home)
    add_file_log(logging.getLogger("kanban"), home / "logs" / "daemon.log")
    lock = take_lock(home / "daemon.lock")
    if lock is None:
        log.info("another daemon holds %s; exiting", home / "daemon.lock")
        return 0
    memory: dict[str, str] = {}
    store.connect().close()  # create and migrate once: two threads doing it together hit "locked"
    stop, signalled = threading.Event(), []
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: signalled.append(True))
    threads = [
        threading.Thread(target=tick_loop, args=(stop, memory), name="tick", daemon=True),
        threading.Thread(target=housekeeping_loop, args=(stop,), name="housekeeping", daemon=True),
    ]
    for t in threads:
        t.start()
    log.info("daemon started (pid %s)", os.getpid())
    code = 0
    while not signalled:  # a handler must not take a lock: Event.set() there can deadlock us
        if dead := [t.name for t in threads if not t.is_alive()]:
            log.error("thread %s died; exiting so the supervisor restarts the daemon", dead[0])
            code = 1
            break
        time.sleep(0.2)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    log.info("daemon stopped; runners keep running")
    with contextlib.suppress(OSError):
        os.close(lock)
    return code
