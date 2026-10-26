"""The worker verbs (RUN-19, CLI-10): run in-process against the DB, bound to the run's own card.

Anything that needs git or the network runs before the transaction a terminal verb opens.
"""

import json
import os
import re
import sqlite3
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import ghops, gitops, store, validate, views
from .errors import Error, NotAllowed, Refused, Usage
from .paths import ensure_dir, run_files

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
    "gate",
)
BUDGET_S = 540  # under the 600 s a worker's shell command may run
POLL_S = 20
# GitHub registers a check within seconds of a push; a minute covers a slow one before a repo
# with no workflows is called CI-less. With workflows, 3 minutes without any check means a path
# or event filter kept them off the head, so the wait ends instead of burning the whole budget.
NO_CI_GRACE_S = 60
NO_CHECKS_CAP_S = 180
CI_EXIT = {"passed": 0, "failed": 1, "pending": 2, "no_ci": 6, "skipped": 7}  # neither is a pass
GATE_LINES = 40
GATE_RE = re.compile(r"FAIL|Error|error:|failed|Traceback|AssertionError|✗")
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
        raise Refused(
            "this needs a worktree card with a branch. This card is scratch: if you opened a PR, "
            "do not use complete (the PR would skip the warden); run kanban block --kind "
            "needs_input and name the PR and the fix, a worktree workspace and a branch. "
            "With no PR to review, use complete"
        )
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


def _closes(conn: sqlite3.Connection, t: sqlite3.Row, number: int) -> None:
    """A card tied to a GitHub issue needs `Closes OWNER/REPO#N` in its PR body, or the merge leaves
    the issue open and the warden sends the PR back (LPS: 5 wasted rounds). Jira and unlinked cards pass."""
    lk = conn.execute(
        "SELECT repo, number FROM github_links WHERE task_id = ?", (t["id"],)
    ).fetchone()
    if lk is None:
        return
    repo, ref = _repo(t), views.issue_ref(lk)
    owner_repo, n = ref.rsplit("#", 1)
    forms = [re.escape(ref), rf"https?://\S*/{re.escape(owner_repo)}/issues/{n}"]
    if repo.lower() == lk["repo"].lower():
        forms.append(f"#{n}")  # GitHub resolves a bare #N inside the issue's own repo
    keyword = rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+(?:{'|'.join(forms)})(?!\d)"
    if not re.search(keyword, str(ghops.pr_view(repo, number, "body").get("body") or ""), re.I):
        raise Refused(
            f"this card is tied to GitHub issue {ref}; add the line `Closes {ref}` to the body of "
            f"PR #{number} (gh pr edit {number} -R {repo} --body ...), then run this again"
        )


def _verdict(rows: list[tuple[str, str]]) -> tuple[str, str]:
    """Fold (name, bucket) pairs, bucket being pass, fail, pending, skipping or cancel."""
    failed = [n for n, b in rows if b in ("fail", "cancel")]
    pending = [n for n, b in rows if b == "pending"]
    if failed:
        return "failed", "failed: " + ", ".join(failed[:8])
    if pending:
        return "pending", f"pending: {len(pending)} of {len(rows)} checks still running"
    # nothing ran when every check was skipped: a pass needs at least one passing check
    if all(b == "skipping" for _, b in rows):
        return "skipped", f"skipped: all {len(rows)} check{'' if len(rows) == 1 else 's'} skipped"
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
    return _verdict(rows) if rows else ("none", "pending: no checks reported yet")


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


def _api(repo: str, path: str) -> "subprocess.CompletedProcess[str]":
    host, _, slug = repo.partition("/")
    return ghops.gh_rc("api", f"repos/{slug}/{path}", "--hostname", host, timeout=30)


def _no_ci(
    gh: Callable[..., "subprocess.CompletedProcess[str]"], repo: str, number: int
) -> bool | None:
    """True when the repo has no workflow, on its default branch and on the PR head; False when it
    has one; None when GitHub did not say, which counts as "has CI" for the caller."""
    try:
        listed = _api(repo, "actions/workflows")
        if listed.returncode != 0:
            return None
        if json.loads(listed.stdout)["total_count"] != 0:
            return False
        view = gh("pr", "view", str(number), "--json", "headRefOid")
        if view.returncode != 0:
            return None
        head = _api(repo, f"contents/.github/workflows?ref={json.loads(view.stdout)['headRefOid']}")
    except (Error, KeyError, TypeError, ValueError):
        return None
    if head.returncode == 0:  # the PR itself adds the first workflow; its checks are still to come
        return False
    return True if "404" in head.stderr + head.stdout else None


def wait_ci(repo: str, number: int, deadline: float | None = None) -> tuple[str, str]:
    """(state, one line): poll the PR's checks until `deadline` (BUDGET_S from now). `pending`
    means ask again; a gh error or a PR with no checks yet is pending too, since the next call may
    see them. `no_ci` means the repo has no workflows at all: not a pass, there is nothing to trust."""
    start = time.monotonic()
    deadline = deadline or start + BUDGET_S
    none_ci: bool | None = None

    def gh(*args: str) -> "subprocess.CompletedProcess[str]":
        left = max(1, min(30, int(deadline - time.monotonic())))
        return ghops.gh_rc(*args, "-R", repo, timeout=left)

    while True:
        try:
            state, line = _poll(gh, number)
        except (Error, KeyError, TypeError, ValueError) as e:
            state, line = "pending", f"pending: gh did not answer, will retry ({e})"
        if state == "none":
            waited = time.monotonic() - start
            none_ci = (
                _no_ci(gh, repo, number) if none_ci is None and waited >= NO_CI_GRACE_S else none_ci
            )
            if none_ci:
                return "no_ci", "no_ci: this repo has no CI workflows, so no check will ever report"
            state = "pending"
            if waited >= NO_CHECKS_CAP_S:
                return state, f"pending: no checks reported yet after {NO_CHECKS_CAP_S} s"
        if state != "pending" or time.monotonic() + POLL_S > deadline:
            return state, line
        time.sleep(POLL_S)


def gate_log(t: sqlite3.Row, run: sqlite3.Row) -> Path:
    """A fresh `run-N.gate-K.log` next to the run's log, under KANBAN_HOME: never in the worktree,
    where it would land in the PR diff. Created exclusively, so parallel gates get different K."""
    d = ensure_dir(run_files(t["board"], t["id"], run["n"])[0].parent)
    k = 1
    while True:
        path = d / f"run-{run['n']}.gate-{k}.log"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return path
        except FileExistsError:
            k += 1


def gate_digest(lines: Iterable[str], limit: int) -> tuple[int, list[str]]:
    """The line count of a gate's output and at most `limit` lines of it: the failure-looking ones
    first (once each, in order, half the budget at most), then the tail, which is where test runners
    print their summary. Streams the lines, so a huge log never sits in memory."""
    cap, bad, last, n = limit - limit // 2, dict[str, None](), deque[str](maxlen=limit), 0
    for x in lines:
        n += 1
        if len(bad) < cap and GATE_RE.search(x):
            bad.setdefault(x)
        last.append(x)
    room = limit - len(bad) - (1 if bad else 0)
    tail = [x for x in list(last)[-room:] if x not in bad] if room > 0 else []
    return n, [*bad, *(["--- last lines ---"] if bad and tail else []), *tail][:limit]


def gate(t: sqlite3.Row, run: sqlite3.Row, command: list[str], limit: int | None) -> dict[str, Any]:
    """L3: run the gate command with its whole output in a log file and return a short digest.
    One argument is a shell string (`bash -o pipefail -c`), several are an argv."""
    if not command:
        raise Usage("gate needs a command: kanban gate -- CMD [ARG...]")
    limit = GATE_LINES if limit is None else limit
    if limit < 1:
        raise Usage("--lines must be at least 1")
    argv = ["bash", "-o", "pipefail", "-c", command[0]] if len(command) == 1 else command
    log = gate_log(t, run)
    started = time.monotonic()
    with log.open("wb") as f:
        try:
            code = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT
            ).returncode
        except OSError as e:
            f.write(f"kanban gate: cannot run {argv[0]}: {e.strerror}\n".encode())
            code = 127
    code = code if code >= 0 else 128 - code
    with log.open(errors="replace") as f:
        n, digest = gate_digest((x.rstrip("\n") for x in f), limit)
    head = (
        f"gate: exit {code} after {round(time.monotonic() - started)}s, {n} lines; full log: {log}"
    )
    return {"exit": code, "text": "\n".join([head, *digest]), "log": str(log)}


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
    if verb == "gate":
        return gate(t, run, args.get("command") or [], args.get("lines"))
    if verb == "request-changes":
        return store.request_changes(conn, run_id, _text(args, "reason"))
    summary = _text(args, "summary")
    if verb == "complete":
        ahead, default = _facts(t, run)
        return store.complete(conn, run_id, summary, commits_ahead=ahead, default_branch=default)
    number, url = _check_pr(t)
    _closes(conn, t, number)
    reviewer = args.get("reviewer")
    return store.request_review(
        conn, run_id, summary, pr_number=number, pr_url=url,
        reviewer=reviewer if isinstance(reviewer, str) else None,
    )  # fmt: skip
