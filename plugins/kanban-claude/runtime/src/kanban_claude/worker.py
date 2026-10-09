"""The worker verbs (RUN-19, CLI-10): run in-process against the DB, bound to the run's own card.

Anything that needs git or the network runs before the transaction a terminal verb opens.
"""

import re
import sqlite3
from typing import Any

from . import ghops, gitops, store, validate, views
from .errors import Error, NotAllowed, Refused, Usage

MAX_TEXT = 1024 * 1024
VERBS = (
    "show",
    "context",
    "comment",
    "attach",
    "block",
    "complete",
    "request-review",
    "request-changes",
)
_PR_RE = re.compile(r"PR #(\d+)|/pull/(\d+)")


def _text(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise Usage(f"field {key!r} is required and must be text")
    if len(value.encode()) > MAX_TEXT:
        raise Refused(f"{key} is larger than {MAX_TEXT} bytes (1 MiB)")
    return value


def _facts(t: sqlite3.Row, run: sqlite3.Row) -> tuple[int, str | None]:
    """(commits not on origin/<base>, default branch) for `complete`; git runs before BEGIN."""
    if t["workspace_kind"] != "worktree":
        return 0, None
    root = t["workspace_path"]
    default = ghops.default_branch(root)
    ahead = 0
    if run["kind"] == "work" and run["role"] in store.BUILDERS and t["branch_name"]:
        base = t["base_branch"] or default
        if base is None:
            raise Error("cannot determine the default branch; run: kanban doctor")
        ahead = gitops.commits_ahead(root, base, t["branch_name"])
    return ahead, default


def _check_pr(t: sqlite3.Row, summary: str) -> tuple[int, str]:
    """REV-01: the PR is open, its head is the card's branch and its base the card's base."""
    m = _PR_RE.search(summary)
    if not m:
        raise Refused("the summary must name the PR as 'PR #<n>' or its URL")
    number = int(m[1] or m[2])
    if t["workspace_kind"] != "worktree" or not t["branch_name"]:
        raise Refused("request-review needs a worktree card with a branch; use complete instead")
    root = t["workspace_path"]
    origin = gitops.origin_url(root)
    if not origin:
        raise Error(f"{root} has no origin remote; run: kanban doctor")
    repo = gitops.repo_slug(origin)
    pr = ghops.pr_view(repo, number)
    base = t["base_branch"] or ghops.default_branch(root)
    if pr.get("state") != "OPEN":
        raise Refused(f"PR #{number} is {pr.get('state')}, not OPEN")
    if pr.get("headRefName") != t["branch_name"]:
        raise Refused(
            f"PR #{number} is from {pr.get('headRefName')}, not the card branch {t['branch_name']}"
        )
    if pr.get("baseRefName") != base:
        raise Refused(f"PR #{number} targets {pr.get('baseRefName')}, not {base}")
    url = validate.pr_url(str(pr.get("url")), {repo.split("/")[0], "github.com"})
    return number, url


def act(conn: sqlite3.Connection, run_id: int, verb: str, args: dict[str, Any]) -> Any:
    """Apply one worker verb to the card of run `run_id`; errors are the usual `Error`s."""
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise Refused(store.RUN_ENDED)
    tid = run["task_id"]
    if args.get("id") not in (None, tid):
        raise NotAllowed(f"this run serves card {tid} only; {args['id']} is not it")
    if run["status"] != "running":
        if run["terminal_verb"] == verb:
            return {"noop": True}
        raise Refused(store.RUN_ENDED)
    t = store.get_task(conn, tid)
    who = f"worker:{run['role']}#{run_id}"
    if verb == "show":
        return views.show_data(conn, tid)
    if verb == "context":
        return {"text": views.context_md(conn, tid)}
    if verb == "comment":
        return store.comment(
            conn, tid, _text(args, "text"), author=run["role"], actor=who, run_id=run_id
        )
    if verb == "attach":
        return store.attach(
            conn, tid, _text(args, "filename"), args["data"],
            uploaded_by=run["role"], actor=who, run_id=run_id,
        )  # fmt: skip
    if verb == "block":
        return store.block(conn, run_id, _text(args, "kind"), _text(args, "reason"))
    if verb == "request-changes":
        return store.request_changes(conn, run_id, _text(args, "reason"))
    summary = _text(args, "summary")
    if verb == "complete":
        ahead, default = _facts(t, run)
        return store.complete(conn, run_id, summary, commits_ahead=ahead, default_branch=default)
    number, url = _check_pr(t, summary)
    reviewer = args.get("reviewer")
    return store.request_review(
        conn, run_id, summary, pr_number=number, pr_url=url,
        reviewer=reviewer if isinstance(reviewer, str) else None,
    )  # fmt: skip
