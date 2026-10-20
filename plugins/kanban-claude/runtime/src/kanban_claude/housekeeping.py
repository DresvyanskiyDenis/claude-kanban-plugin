"""Jobs that may call git and gh, off the dispatch tick (DSP-01): merges, worktree sweep, retention."""

import contextlib
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Callable
from typing import Any

from . import dispatcher, ghops, gitops, jira, protection, store, views
from .errors import Error
from .paths import card_tmp, dirs

SWEEP_S = 600
RETENTION_S = 86400
MERGE_RETRY_S = 120
MERGE_MAX_ATTEMPTS = 3
log = logging.getLogger("kanban.housekeeping")
Row = sqlite3.Row
METHOD_DONE = {
    "squash": "squash-merged",
    "merge": "merged (merge commit)",
    "rebase": "rebase-merged",
}
# One fix per precondition failure (REV-05), not one reopen-review string for all of them.
FIX = {
    "approval_not_clean": "the approval is not clean; merge the PR on GitHub if you accept it, or run: kanban reopen-review {id} --reason TEXT",
    "pr_not_open": "the PR is not open; reopen it or mark it ready on GitHub, then run: kanban merge-retry {id}",
    "default_branch_unknown": "gh could not name the repository's default branch; check gh auth and the network, then run: kanban merge-retry {id}",
    "head_branch_changed": "the PR now comes from another branch than the card's; run: kanban reopen-review {id} --reason TEXT",
    "base_is_default": "the PR targets the default branch, which only the owner merges; merge it on GitHub, or retarget it and run: kanban merge-retry {id}",
    "base_changed": "the PR was retargeted; point it back at {base} and run: kanban merge-retry {id}",
    "head_moved": "commits were pushed after the approval; run: kanban reopen-review {id} --reason TEXT so warden reviews the new head",
}


def say(conn: sqlite3.Connection, t: Row, text: str) -> None:
    store.comment(conn, t["id"], text, author="kanban", actor="housekeeping")


def precondition_failure(
    t: Row, approved: dict[str, Any], summary: str, pr: dict[str, Any], default: str
) -> str | None:
    """REV-05 preconditions in order (the PR was fetched by its approved number); None means all hold."""
    if not store.APPROVED_RE.search(summary) or store.PARTIAL_REVIEW in summary:
        return "approval_not_clean"
    if pr.get("state") != "OPEN":
        return "pr_not_open"
    if not default:
        return "default_branch_unknown"
    if pr.get("headRefName") != t["branch_name"]:
        return "head_branch_changed"
    if pr.get("baseRefName") == default:
        return "base_is_default"
    if pr.get("baseRefName") != t["base_branch"]:
        return "base_changed"
    if not str(pr.get("headRefOid", "")).startswith(approved["sha"]):
        return "head_moved"
    return None


def settle(
    conn: sqlite3.Connection, t: Row, state: str, kind: str, payload: dict[str, Any], text: str
) -> None:
    """End the pending merge. The event and comment follow only if this call changed the card."""
    ts = store.now()
    with store.tx(conn):
        cur = conn.execute(
            "UPDATE tasks SET merge_state = ?, merged_at = ?, merge_next_at = NULL, "
            "updated_at = ?, version = version + 1 WHERE id = ? AND merge_state = 'pending'",
            (state, ts if state == "merged" else None, ts, t["id"]),
        )
        if cur.rowcount == 1:
            store.event(conn, t, kind, "housekeeping", payload)
    if cur.rowcount == 1:
        say(conn, t, text)


def claim_merge(conn: sqlite3.Connection, t: Row) -> bool:
    """CAS on the row `run_due` selected, taken just before `gh pr merge`: false when the card left the
    queue since (reopened, archived) or another claim holds it; a crash leaves the lease to lapse."""
    with store.tx(conn):
        cur = conn.execute(
            "UPDATE tasks SET merge_next_at = ? WHERE id = ? AND status = 'done' "
            "AND merge_state = 'pending' AND merge_next_at = ?",
            (store.now() + MERGE_RETRY_S, t["id"], t["merge_next_at"]),
        )
    return cur.rowcount == 1


def merge_one(conn: sqlite3.Connection, t: Row) -> None:
    """One REV-05 job: read the PR first, then decide; a failed read or merge is an attempt."""
    found = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'merge_pending' ORDER BY id DESC LIMIT 1",
        (t["id"],),
    ).fetchone()
    approved = json.loads(found["payload"]) if found else {}
    n: int = approved.get("pr", 0)
    base = t["base_branch"]
    fields = "state,number,headRefName,headRefOid,baseRefName"

    def fail(reason: str, **extra: Any) -> None:
        settle(
            conn, t, "failed", "merge_failed", {"reason": reason, "pr": n, **extra},
            f"[merge] PR #{n} into {base} not merged: {reason}"
            + (f" ({extra['fix']})" if "fix" in extra else ""),
        )  # fmt: skip

    method = t["merge_method"] or "squash"

    def merged(pr: dict[str, Any], landed: bool = False) -> None:
        sha, into = pr.get("headRefOid"), pr.get("baseRefName")
        text = (
            f"[merge] gh did not merge PR #{n}, but its content is already on {into or base}; "
            f"close PR #{n} by hand"
            if landed
            else f"[merge] {METHOD_DONE[method]} PR #{n} into {into or base}"
        )
        payload = {"pr": n, "base": into, "sha": sha, **({"landed": True} if landed else {})}
        settle(conn, t, "merged", "merged", payload, text)

    def repo() -> str:
        return gitops.repo_slug(gitops.origin_url(t["workspace_path"]) or "")

    try:
        run = conn.execute(
            "SELECT summary FROM task_runs WHERE id = ?", (approved.get("run_id"),)
        ).fetchone()
        slug = repo()
        pr = ghops.pr_view(slug, n, fields)
        if pr.get("state") == "CLOSED":
            return fail("closed")
        if pr.get("state") != "MERGED":
            summary = (run["summary"] if run else "") or ""
            reason = precondition_failure(t, approved, summary, pr, ghops.repo_default_branch(slug))
            if reason:
                return fail(reason, fix=FIX[reason].format(id=t["id"], base=base))
            if not claim_merge(conn, t):
                return
            ghops.merge(slug, n, pr["headRefOid"], method)
        merged(pr)
    except Exception as e:
        log.exception("merge of PR #%s for %s failed", n, t["id"])
        attempts = t["merge_attempts"] + 1
        with contextlib.suppress(Exception):  # gh may report failure after GitHub merged (REV-06)
            pr = ghops.pr_view(repo(), n, fields)
            if pr.get("state") == "MERGED":
                return merged(pr)
            head = str(pr.get("headRefOid", ""))
            if (
                pr.get("state") == "OPEN"
                and head.startswith(approved.get("sha") or "-")
                and gitops.content_landed(
                    t["workspace_path"], pr["baseRefName"], pr["headRefName"], head
                )
            ):
                return merged(pr, landed=True)
        if attempts >= MERGE_MAX_ATTEMPTS:
            return fail(f"failed after {attempts} attempts: {e}", error=str(e))
        with store.tx(conn):
            cur = conn.execute(
                "UPDATE tasks SET merge_attempts = ?, merge_next_at = ? "
                "WHERE id = ? AND merge_state = 'pending'",
                (attempts, store.now() + MERGE_RETRY_S, t["id"]),
            )
        if cur.rowcount == 1:
            say(conn, t, f"[merge] attempt {attempts} of {MERGE_MAX_ATTEMPTS} failed: {e}")


def cleanup_card(conn: sqlite3.Connection, t: Row) -> None:
    """GIT-06 for one finished worktree card; the state is recorded, an event only on change."""
    shutil.rmtree(card_tmp(t["id"]), ignore_errors=True)
    root = t["workspace_path"]
    merged = t["merge_state"] == "merged"
    if not merged and t["pr_number"]:
        with contextlib.suppress(Error):
            repo = gitops.repo_slug(gitops.origin_url(root) or "")
            merged = ghops.pr_view(repo, t["pr_number"])["state"] == "MERGED"
    try:
        state = gitops.cleanup(root, t["id"], t["branch_name"], merged=merged)
    except Error:
        return
    if state == t["worktree_state"]:
        return
    with store.tx(conn):
        conn.execute("UPDATE tasks SET worktree_state = ? WHERE id = ?", (state, t["id"]))
        if state != "removed":
            store.event(conn, t, "worktree_kept", "housekeeping", {"state": state})


def settle_owner_merges(conn: sqlite3.Connection) -> None:
    """A PR the owner merges on GitHub (into the default branch, or a partial review) is seen by nothing
    else: note it, so the card stops being `awaiting merge`; a partial review's pending merge settles
    and its children start. Its worktree is usually gone by then (GIT-06)."""
    for t in views.awaiting_owner_merge(conn):
        with contextlib.suppress(Error):
            repo = gitops.repo_slug(gitops.origin_url(t["workspace_path"]) or "")
            if ghops.pr_view(repo, t["pr_number"])["state"] != "MERGED":
                continue
            if t["owner_merge"]:  # held back by merge_state 'pending' until now
                n = t["pr_number"]
                settle(conn, t, "merged", "merged", {"pr": n, "owner": True},
                       f"[merge] PR #{n} was merged by the owner on GitHub")  # fmt: skip
            else:
                ts = store.now()
                with store.tx(conn):
                    conn.execute(
                        "UPDATE tasks SET merged_at = ?, updated_at = ?, version = version + 1 "
                        "WHERE id = ? AND merge_state IS NULL AND merged_at IS NULL",
                        (ts, ts, t["id"]),
                    )


def sweep(conn: sqlite3.Connection) -> None:
    """GIT-06: finished worktree cards, and the scratch directories of archived cards."""
    for t in conn.execute(
        "SELECT * FROM tasks WHERE status IN ('done', 'archived') AND workspace_kind = 'worktree' "
        "AND worktree_state IN ('active', 'kept_dirty', 'kept_unpushed')"
    ).fetchall():
        cleanup_card(conn, t)
    settle_owner_merges(conn)
    scratch = dirs().work / "scratch"
    for t in conn.execute(
        "SELECT id FROM tasks WHERE status = 'archived' AND workspace_kind = 'scratch'"
    ).fetchall():
        shutil.rmtree(scratch / t["id"], ignore_errors=True)
        shutil.rmtree(card_tmp(t["id"]), ignore_errors=True)


def retention(conn: sqlite3.Connection, ts: int) -> None:
    """OBS-04: runner logs and stream sidecars older than the retention window."""
    cutoff = ts - store.cfg(conn, "retention.stream_days") * 86400
    for sub in ("runs", "streams"):
        for f in (dirs().home / sub).rglob("*"):
            with contextlib.suppress(FileNotFoundError):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    for d in (f.parent, f.parent.parent):
                        with contextlib.suppress(OSError):
                            d.rmdir()


def run_due(conn: sqlite3.Connection, ts: int | None = None) -> None:
    """One housekeeping wake-up: every job that is due, one at a time; a job's failure stays its own."""
    ts = ts or int(time.time())
    for t in conn.execute(
        "SELECT * FROM tasks WHERE status = 'done' AND merge_state = 'pending' AND owner_merge = 0 "
        "AND merge_next_at <= ? "
        "ORDER BY merge_next_at",
        (ts,),
    ).fetchall():
        try:
            merge_one(conn, t)
        except Exception:
            log.exception("merge job for %s failed", t["id"])
    try:
        dispatcher.reprobe_logins(conn, ts)
    except Exception:
        log.exception("login re-probe failed")
    due = store.state_get(conn, "housekeeping") or {}
    jobs: tuple[tuple[str, int, Callable[[], object]], ...] = (
        ("sweep", SWEEP_S, lambda: sweep(conn)),
        ("retention", RETENTION_S, lambda: retention(conn, ts)),
        ("protection", protection.PROTECTION_S, lambda: protection.refresh(conn)),
        ("jira", store.cfg(conn, "jira.poll_s"), lambda: jira.poll(conn)),
    )
    for name, every, job in jobs:
        if ts >= due.get(name, 0) + every:
            try:
                job()
            except Exception:
                log.exception("%s failed", name)
            else:
                due[name] = ts
    with store.tx(conn):
        store.state_put(conn, "housekeeping", due)
