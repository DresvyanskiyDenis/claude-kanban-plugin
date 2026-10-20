"""The worker verbs (RUN-19, CLI-10): run in-process against the DB, bound to the run's own card.

Anything that needs git or the network runs before the transaction a terminal verb opens.
"""

import json
import sqlite3
import subprocess
import time
from collections.abc import Callable
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
    "wait-ci",
)
BUDGET_S = 540  # under the 600 s a worker's shell command may run
POLL_S = 20
CI_EXIT = {"passed": 0, "failed": 1, "pending": 2}
_FAILED_RUNS = ("failure", "timed_out", "cancelled", "startup_failure", "action_required")


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


def _repo(t: sqlite3.Row) -> str:
    """The card's repository as `HOST/OWNER/REPO`, from its worktree's origin (GIT-11)."""
    if t["workspace_kind"] != "worktree" or not t["branch_name"]:
        raise Refused("this needs a worktree card with a branch; use complete instead")
    origin = gitops.origin_url(t["workspace_path"])
    if not origin:
        raise Error(f"{t['workspace_path']} has no origin remote; run: kanban doctor")
    return gitops.repo_slug(origin)


def _find_pr(t: sqlite3.Row) -> tuple[str, int, str]:
    """(repo, number, url) of the card's one open PR, found by its branch and base (REV-01)."""
    repo = _repo(t)
    base = t["base_branch"] or ghops.default_branch(t["workspace_path"])
    if base is None:
        raise Error("cannot determine the default branch; run: kanban doctor")
    found = ghops.pr_list(repo, t["branch_name"], base)
    where = f"from {t['branch_name']} into {base} in {repo}"
    if not found:
        raise Refused(
            f"no open PR {where}; push the branch and open the PR (gh pr create --base {base}), "
            "then run this again"
        )
    if len(found) > 1:
        numbers = ", ".join(f"#{pr.get('number')}" for pr in found)
        raise Refused(f"several open PRs {where}: {numbers}; close the extra ones, then run again")
    return repo, int(found[0]["number"]), str(found[0].get("url"))


def _check_pr(t: sqlite3.Row) -> tuple[int, str]:
    repo, number, url = _find_pr(t)
    return number, validate.pr_url(url, {repo.split("/")[0], "github.com"})


def _verdict(rows: list[tuple[str, str]]) -> tuple[str, str]:
    """Fold (name, bucket) pairs, bucket being pass, fail, pending, skipping or cancel."""
    failed = [n for n, b in rows if b in ("fail", "cancel")]
    pending = [n for n, b in rows if b == "pending"]
    if failed:
        return "failed", "failed: " + ", ".join(failed[:8])
    if pending:
        return "pending", f"pending: {len(pending)} of {len(rows)} checks still running"
    return "passed", f"passed: {len(rows)} check{'' if len(rows) == 1 else 's'}"


def _runs(gh: Callable[..., "subprocess.CompletedProcess[str]"], number: int) -> tuple[str, str]:
    """The PR reports no checks yet: judge the workflow runs of its head commit instead."""
    view = gh("pr", "view", str(number), "--json", "headRefOid")
    if view.returncode != 0:
        raise Error(f"gh pr view failed: {view.stderr.strip()[:300]}")
    sha = json.loads(view.stdout)["headRefOid"]
    listed = gh("run", "list", "--commit", sha, "--json", "name,status,conclusion")
    if listed.returncode != 0:
        raise Error(f"gh run list failed: {listed.stderr.strip()[:300]}")
    rows = [
        (r["name"], "pending" if r["status"] != "completed"
         else "fail" if r["conclusion"] in _FAILED_RUNS else "pass")
        for r in json.loads(listed.stdout)
    ]  # fmt: skip
    return _verdict(rows) if rows else ("pending", "pending: no checks reported yet")


def _poll(gh: Callable[..., "subprocess.CompletedProcess[str]"], number: int) -> tuple[str, str]:
    p = gh("pr", "checks", str(number), "--json", "name,bucket")
    try:
        rows = json.loads(p.stdout)
    except ValueError:
        rows = None
    if isinstance(rows, list) and rows:
        return _verdict([(str(r["name"]), str(r["bucket"])) for r in rows])
    # gh gives no stable exit code for "no checks yet", so the message decides, not the code.
    if rows == [] or "no checks reported" in p.stdout + p.stderr:
        return _runs(gh, number)
    raise Error(f"gh pr checks failed: {(p.stderr or p.stdout).strip()[:300]}")


def wait_ci(repo: str, number: int, deadline: float | None = None) -> tuple[str, str]:
    """(state, one line): poll the PR's checks until `deadline` (BUDGET_S from now). `pending`
    means ask again; a gh error or a PR with no checks yet is pending too, since the next call may
    see them."""
    deadline = deadline or time.monotonic() + BUDGET_S

    def gh(*args: str) -> "subprocess.CompletedProcess[str]":
        left = max(1, min(30, int(deadline - time.monotonic())))
        return ghops.gh_rc(*args, "-R", repo, timeout=left)

    while True:
        try:
            state, line = _poll(gh, number)
        except (Error, KeyError, TypeError, ValueError) as e:
            state, line = "pending", f"pending: gh did not answer, will retry ({e})"
        if state != "pending" or time.monotonic() + POLL_S > deadline:
            return state, line
        time.sleep(POLL_S)


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
        after = args.get("retry_after")
        return store.block(
            conn, run_id, _text(args, "kind"), _text(args, "reason"),
            retry_after_s=None if after is None else validate.duration(after),
        )  # fmt: skip
    if verb == "wait-ci":
        deadline = time.monotonic() + BUDGET_S  # the PR lookup spends the same budget
        try:  # the card's stored number may be an old PR (REV-01); the branch says which is open
            number = args.get("pr") or _find_pr(t)[1]
        except Refused:
            raise
        except Error as e:  # exit 1 would read as "CI failed"
            return {
                "state": "pending",
                "summary": f"pending: cannot look up the PR ({e})",
                "pr": None,
            }
        state, line = wait_ci(_repo(t), number, deadline)
        return {"state": state, "summary": line, "pr": number}
    if verb == "request-changes":
        return store.request_changes(conn, run_id, _text(args, "reason"))
    summary = _text(args, "summary")
    if verb == "complete":
        ahead, default = _facts(t, run)
        return store.complete(conn, run_id, summary, commits_ahead=ahead, default_branch=default)
    number, url = _check_pr(t)
    reviewer = args.get("reviewer")
    return store.request_review(
        conn, run_id, summary, pr_number=number, pr_url=url,
        reviewer=reviewer if isinstance(reviewer, str) else None,
    )  # fmt: skip
