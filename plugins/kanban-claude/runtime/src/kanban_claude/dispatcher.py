"""The dispatcher daemon (DSP): a tick thread and a housekeeping thread behind one flock."""

import contextlib
import fcntl
import logging
import logging.handlers
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import housekeeping, protection, store, validate, web
from .errors import Error
from .paths import dirs, ensure_dir, run_files
from .procs import Procs

CRASH_GRACE_S = 30
HOUSEKEEPING_S = 60
LOCK_WAIT_S = 1.0
FAILURE_WAIT_S = 5.0
log = logging.getLogger("kanban.daemon")
Row = sqlite3.Row
Report = dict[str, Any]


@contextlib.contextmanager
def isolated(what: str) -> Iterator[None]:
    """One run's or one card's failure is logged and skipped; it never aborts the tick."""
    try:
        yield
    except Exception:
        log.exception("%s failed; skipped for this tick", what)


def reconcile(conn: sqlite3.Connection, ts: int, procs: Procs) -> list[dict[str, Any]]:
    """DSP-02: end every run whose runner is gone; a live runner is never touched.

    A liveness probe that fails (`ps` times out, unreadable output) raises out of `procs.alive`:
    the run is skipped for this tick. Unknown is never "dead", or a live worker would be killed.
    """
    ended = []
    for run in conn.execute("SELECT * FROM task_runs WHERE status = 'running'").fetchall():
        with isolated(f"run {run['id']}"):
            if procs.alive(run["runner_pid"], run["runner_started"]):
                continue
            if run["runner_pid"] is None:
                if ts - run["started_at"] < CRASH_GRACE_S:
                    continue
                outcome, error = "interrupted", "the runner never started"
            elif run["started_at"] < procs.boot_time():
                outcome, error = "interrupted", "the machine restarted during the run"
            else:
                if procs.alive(run["claude_pid"], run["claude_started"]):
                    procs.killpg(run["claude_pid"])
                outcome, error = "crashed", f"the runner (pid {run['runner_pid']}) died"
            with store.tx(conn):
                done = store.end_run(
                    conn, run["id"], outcome, "retry", actor="dispatcher",
                    event_kind=outcome, error=error, orphan_ok=True,
                )  # fmt: skip
            if done:
                ended.append({"run_id": run["id"], "task": run["task_id"], "outcome": outcome})
    return ended


def repair_running(conn: sqlite3.Connection) -> None:
    """STO-06: a `running` card without a running run goes back to its retry status."""
    for t in conn.execute(
        "SELECT * FROM tasks t WHERE status = 'running' AND NOT EXISTS "
        "(SELECT 1 FROM task_runs r WHERE r.task_id = t.id AND r.status = 'running')"
    ).fetchall():
        with isolated(f"card {t['id']}"):
            last = conn.execute(
                "SELECT kind FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (t["id"],)
            ).fetchone()
            with store.tx(conn):
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ?, version = version + 1 "
                    "WHERE id = ? AND status = 'running'",
                    (
                        "review" if last and last["kind"] == "review" else "ready",
                        store.now(),
                        t["id"],
                    ),
                )
                store.event(conn, t, "repaired", "dispatcher", {"reason": "running without a run"})


def stop(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """RUN-17: the DB first, then SIGTERM to the runner, whose pid and start time are verified."""
    data = store.stop(conn, task_id)
    if Procs().alive(data["runner_pid"], data["runner_started"]):
        os.kill(data["runner_pid"], signal.SIGTERM)
    return data


def expire_walls(conn: sqlite3.Connection, ts: int) -> None:
    """DSP-03, first half: a wall in the past is cleared, once, with an event."""
    for row in conn.execute("SELECT key FROM state WHERE key LIKE 'account:%'").fetchall():
        config_dir = row["key"].removeprefix("account:")
        with isolated(f"account {config_dir}"):
            acct = store.account_get(conn, config_dir)
            gone = {k: None for k in ("hard_until", "soft_until") if 0 < (acct.get(k) or 0) <= ts}
            if gone:
                with store.tx(conn):
                    store.account_set(conn, config_dir, **gone)
                    store.event(
                        conn, None, "account_resumed", "dispatcher", {"config_dir": config_dir}
                    )


def promote(conn: sqlite3.Connection) -> list[str]:
    """DSP-03, second half."""
    promoted = []
    for t in conn.execute("SELECT id FROM tasks WHERE status = 'todo'").fetchall():
        with isolated(f"card {t['id']}"):
            if store.parents_satisfied(conn, t["id"]) and store.promote_auto(conn, t["id"]):
                promoted.append(t["id"])
    return promoted


def skip_reason(
    conn: sqlite3.Connection, t: Row, ts: int, procs: Procs, roles: list[str], reviewer: str
) -> str | None:
    """DSP-04 gates for one card; the first that fails names the reason. No git, no gh."""
    if not t["assignee"]:
        return "no_assignee"
    allowed = [reviewer] if t["status"] == "review" else roles
    if t["assignee"] not in allowed:
        return "role_not_dispatchable"
    board = store.get_board(conn, t["board"])
    acct = store.account_get(conn, store.effective_config_dir(conn, board))
    if acct.get("auth_paused"):
        return "account_paused"
    if max(acct.get("hard_until") or 0, acct.get("soft_until") or 0) > ts:
        return "quota_wall"
    if (t["retry_after"] or 0) > ts:
        return "retry_after"
    last = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND status = 'ended' ORDER BY id DESC LIMIT 1",
        (t["id"],),
    ).fetchone()
    if last and (
        procs.alive(last["runner_pid"], last["runner_started"])
        or procs.alive(last["claude_pid"], last["claude_started"])
    ):
        return "previous_run_alive"
    if t["consecutive_failures"] >= (t["max_retries"] or store.cfg(conn, "dispatch.failure_limit")):
        return "failure_limit"
    if t["workspace_kind"] != "scratch":
        try:
            validate.workspace(
                t["workspace_kind"], t["workspace_path"] or "",
                claude_dirs=store.claude_dirs(conn, board), strict=False,
            )  # fmt: skip
        except Error:
            return "workspace_invalid"
        if not Path(t["workspace_path"]).is_dir():
            return "workspace_invalid"
    return None


def order(cards: list[Row], slots: int) -> list[Row]:
    """DSP-04: priority, then review before ready, then age; one slot is held for a review."""
    ranked = sorted(cards, key=lambda t: (-t["priority"], t["status"] != "review", t["created_at"]))
    chosen = ranked[: max(slots, 0)]
    review = next((t for t in ranked if t["status"] == "review"), None)
    if chosen and review and review not in chosen and all(t["status"] != "review" for t in chosen):
        chosen[-1] = review
    return chosen


def record_skips(conn: sqlite3.Connection, skipped: dict[str, str], memory: dict[str, str]) -> None:
    """A `skipped` event only when a card's reason changes, not on every tick."""
    for task_id in set(memory) - set(skipped):
        del memory[task_id]
    for task_id, reason in skipped.items():
        if memory.get(task_id) != reason:
            memory[task_id] = reason
            with store.tx(conn):
                store.event(
                    conn, store.get_task(conn, task_id), "skipped", "dispatcher", {"reason": reason}
                )


def claim_and_spawn(conn: sqlite3.Connection, t: Row, procs: Procs, report: Report) -> None:
    board = store.get_board(conn, t["board"])
    run_id = store.claim(conn, t["id"], config_dir=store.effective_config_dir(conn, board))
    if run_id is None:
        return
    n = conn.execute("SELECT n FROM task_runs WHERE id = ?", (run_id,)).fetchone()["n"]
    try:
        procs.spawn(run_id, run_files(t["board"], t["id"], n)[0])
    except OSError as e:
        with store.tx(conn):
            store.end_run(
                conn, run_id, "crashed", "retry", actor="dispatcher", event_kind="crashed",
                error=f"cannot start the runner: {e}",
            )  # fmt: skip
    report["claimed"].append({"id": t["id"], "run_id": run_id})


def dispatch_once(
    conn: sqlite3.Connection,
    ts: int,
    procs: Procs,
    *,
    dry_run: bool = False,
    memory: dict[str, str] | None = None,
) -> Report:
    """DSP-02..08. `dry_run` reads only: nothing is reconciled, promoted, claimed or recorded."""
    report: Report = {
        "paused": False,
        "reconciled": [],
        "promoted": [],
        "claimed": [],
        "skipped": {},
    }
    if not dry_run:
        report["reconciled"] = reconcile(conn, ts, procs)
        repair_running(conn)
        expire_walls(conn, ts)
        report["promoted"] = promote(conn)
    if store.state_get(conn, "dispatch_paused"):
        report["paused"] = True
        return report
    roles, reviewer = store.cfg(conn, "dispatch.roles"), store.cfg(conn, "dispatch.reviewer")
    ready = []
    for t in conn.execute("SELECT * FROM tasks WHERE status IN ('ready', 'review')").fetchall():
        reason: str | None = "error"
        with isolated(f"card {t['id']}"):
            reason = skip_reason(conn, t, ts, procs, roles, reviewer)
        if reason:
            report["skipped"][t["id"]] = reason
        else:
            ready.append(t)
    running = conn.execute("SELECT COUNT(*) FROM task_runs WHERE status = 'running'").fetchone()[0]
    report["slots"] = store.cfg(conn, "dispatch.max_in_progress") - running
    chosen = order(ready, report["slots"])
    if dry_run:
        report["claimed"] = [{"id": t["id"]} for t in chosen]
        return report
    if memory is not None:
        record_skips(conn, report["skipped"], memory)
    for t in chosen:
        with isolated(f"card {t['id']}"):
            claim_and_spawn(conn, t, procs, report)
    return report


# --- the daemon -----------------------------------------------------------------------------


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
            dispatch_once(conn, store.now(), procs, memory=memory)
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
    handler = logging.handlers.RotatingFileHandler(
        ensure_dir(home / "logs") / "daemon.log", maxBytes=10 * 1024 * 1024, backupCount=3
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger("kanban").addHandler(handler)
    logging.getLogger("kanban").setLevel(logging.INFO)
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
