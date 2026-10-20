"""GitHub sync (GHS): a paired board and the issues of one repository (and, optionally, one Projects v2
board) stay in step through the `gh` CLI, with no model in the loop. GitHub owns the text, the board
owns execution. The same rules as `jira.py`, which this module does not import.

Every call is `gh api` (REST or GraphQL). A pass talks to GitHub outside a transaction; each link is
then written in one `BEGIN IMMEDIATE` through the same domain functions as the owner verbs, with actor
`github`.

Done is read from one thing only: the issue's open or closed state. The Projects v2 Status moves
outbound (the card's status picks the option), never inbound: a Status change does not bump the
issue's `updated_at`, so an incremental pull could not see it anyway.
"""

import contextlib
import fcntl
import json
import logging
import re
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

from . import store
from .errors import Error, Refused
from .paths import dirs

GH_CALL_S = 60
PAGE = 100
CLOSABLE = ("triage", "todo", "ready", "blocked")
OPENABLE = ("triage", "todo", "ready", "running", "review")
VERBS = {
    ("work", "complete"): "completed",
    ("work", "request-review"): "ready for review",
    ("work", "block"): "blocked",
    ("review", "complete"): "review approved",
    ("review", "request-changes"): "review changes requested",
    ("review", "block"): "review blocked",
}
# a login that is gone or short of a scope, a rate limit, or a network or server failure: pause
PAUSE = re.compile(
    r"HTTP (401|403|5\d\d)|Bad credentials|rate limit|INSUFFICIENT_SCOPES|required scopes|"
    r"error connecting|dial tcp|no such host|timed? ?out|connection (refused|reset)|gh auth login",
    re.IGNORECASE,
)
log = logging.getLogger("kanban.github")
Row = sqlite3.Row
Issue = dict[str, Any]


class Paused(Error):
    """GitHub cannot be used (GHS-08): the board's sync pauses and the next pass retries."""


class LinkError(Error):
    """One link failed; the message stays on the link."""


class Gone(LinkError):
    """GitHub reports the issue gone."""


class Busy(Refused):
    """Another sync holds `github.lock`."""


# --- gh -------------------------------------------------------------------------------------


def split(repo: str) -> tuple[str, str, str]:
    host, owner, name = repo.split("/")
    return host, owner, name


def call(
    host: str,
    endpoint: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    body: Any = None,
) -> Any:
    """One `gh api` call: GET parameters are query fields, any other body goes in as JSON on stdin.
    The parsed answer; a failure that no retry of this link will fix is a `Paused`, any other a
    `LinkError` carrying gh's own words."""
    argv = ["gh", "api", "--hostname", host, "-X", method]
    for k, v in (params or {}).items():
        argv += ["-f", f"{k}={v}"]
    if body is not None:
        argv += ["--input", "-"]
    try:
        p = subprocess.run(
            [*argv, endpoint], capture_output=True, text=True, timeout=GH_CALL_S,
            **({"input": json.dumps(body)} if body is not None else {"stdin": subprocess.DEVNULL}),
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Paused(f"cannot run gh: {e}") from e
    if p.returncode != 0:
        text = (p.stderr or p.stdout).strip()
        if PAUSE.search(text):
            hint = f"; fix: gh auth refresh -h {host} -s project" if "scope" in text.lower() else ""
            raise Paused(f"gh api {endpoint}: {text[:200]}{hint}")
        raise LinkError(f"gh api {endpoint}: {text[:200]}")
    try:
        return json.loads(p.stdout) if p.stdout.strip() else None
    except ValueError as e:
        raise LinkError(f"gh api {endpoint}: the answer is not JSON") from e


def graphql(host: str, query: str, **variables: Any) -> Any:
    got = call(host, "graphql", method="POST", body={"query": query, "variables": variables})
    return (got or {}).get("data")


def pages(host: str, endpoint: str, **params: Any) -> Iterator[Issue]:
    for n in range(1, 1000):
        got = call(host, endpoint, params={**params, "per_page": PAGE, "page": n})
        items = got["items"] if isinstance(got, dict) else got
        yield from items
        if len(items) < PAGE:
            return


@contextlib.contextmanager
def lock() -> Iterator[None]:
    """One sync at a time, daemon or CLI: two would each create the issue for a new card."""
    with open(dirs().home / "github.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Busy("a GitHub sync is already running; try again in a minute") from e
        yield


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --- one repository -------------------------------------------------------------------------


class Project(NamedTuple):
    id: str
    field: str
    options: dict[str, str]  # lower-case option name -> option id


class Repo:
    """The board's repository, and its Projects v2 board once `use_project` has resolved it."""

    def __init__(self, b: Row) -> None:
        self.slug, self.name, self.query = b["slug"], b["github_repo"], b["github_query"]
        self.host, owner, repo = split(self.name)
        self.path = f"repos/{owner}/{repo}"
        self.project_ref: str | None = b["github_project"]
        self.project: Project | None = None

    def ref(self, number: int) -> str:
        return f"{self.name.partition('/')[2]}#{number}"

    def use_project(self) -> None:
        """Resolve the project's Status field once per pass; a project that cannot be read pauses the board."""
        if not self.project_ref:
            return
        owner, _, number = self.project_ref.partition("/")
        data = graphql(
            self.host,
            "query($o:String!,$n:Int!){repositoryOwner(login:$o){... on ProjectV2Owner{projectV2(number:$n)"
            '{id field(name:"Status"){... on ProjectV2SingleSelectField{id options{id name}}}}}}}',
            o=owner, n=int(number),
        )  # fmt: skip
        found = ((data or {}).get("repositoryOwner") or {}).get("projectV2")
        if not found:
            raise Paused(f"project {self.project_ref} not found, or this login cannot see it")
        field = found.get("field") or {}
        if not field.get("id"):
            raise Paused(f"project {self.project_ref} has no single-select Status field")
        opts = {o["name"].lower(): o["id"] for o in field["options"]}
        self.project = Project(found["id"], field["id"], opts)

    def get(self, number: int) -> Issue:
        """The one read that can say an issue is gone: a 404 on an issue in a repository that still
        reads. A repository that does not read either is the login's problem, not the issue's."""
        try:
            issue: Issue = call(self.host, f"{self.path}/issues/{number}")
        except LinkError as e:
            if "HTTP 404" in str(e) or "HTTP 410" in str(e):
                call(self.host, self.path)
                raise Gone(str(e)) from e
            raise
        return issue

    def fetch(self, number: int) -> Issue:
        return self.comments(self.get(number))

    def comments(self, raw: Issue) -> Issue:
        """The issue with its comments under `_comments`; a count of zero costs no call."""
        raw["_comments"] = (
            list(pages(self.host, f"{self.path}/issues/{raw['number']}/comments"))
            if raw.get("comments")
            else []
        )
        return raw

    def labelled(self, label: str, since: str | None) -> list[Issue]:
        """Issues with the board's label, from the REST list, which is consistent."""
        got = pages(self.host, f"{self.path}/issues", state="all", labels=label,
                    **({"since": since} if since else {}))  # fmt: skip
        return [i for i in got if "pull_request" not in i]

    def members(self, label: str, since: str | None) -> list[Issue]:
        """Issues that belong to the board. The search index lags, so it is used only for a
        `--github-query`."""
        if not self.query:
            return self.labelled(label, since)
        esc = label.replace("\\", "\\\\").replace('"', '\\"')
        q = f'repo:{self.name.partition("/")[2]} is:issue label:"{esc}" {self.query}'
        got = pages(self.host, "search/issues", q=q + (f" updated:>={since}" if since else ""))
        return [i for i in got if "pull_request" not in i]

    def changed(self, since: str) -> Iterator[Issue]:
        """Every issue updated since, labelled or not: a linked issue that lost its label still syncs."""
        return (i for i in pages(self.host, f"{self.path}/issues", state="all", since=since)
                if "pull_request" not in i)  # fmt: skip

    def set_status(self, node_id: str, status: str | None) -> str:
        """Add the issue to the project (a no-op when it is there) and set its Status, when there is
        one to set; the item id."""
        assert self.project
        option = self.project.options.get((status or "").lower())
        if status and not option:
            names = ", ".join(sorted(self.project.options))
            raise LinkError(f"project {self.project_ref} has no Status option {status!r} ({names})")
        item = self.add_item(node_id)
        if not option:
            return item
        graphql(
            self.host,
            "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue(input:{projectId:$p,"
            "itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}",
            p=self.project.id, i=item, f=self.project.field, o=option,
        )  # fmt: skip
        return item

    def add_item(self, node_id: str) -> str:
        assert self.project
        data = graphql(
            self.host,
            "mutation($p:ID!,$c:ID!){addProjectV2ItemById(input:{projectId:$p,contentId:$c}){item{id}}}",
            p=self.project.id, c=node_id,
        )  # fmt: skip
        return str(data["addProjectV2ItemById"]["item"]["id"])


# --- reading --------------------------------------------------------------------------------


def norm(text: str | None) -> str:
    """CRLF to LF, trailing whitespace per line stripped, ends trimmed."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def is_done(issue: Issue) -> bool:
    return issue.get("state") == "closed"


def crossed(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """The GitHub id of every comment that crossed, either way. Comment ids are global and in
    `idx_comments_github`, so the pulled half spans every card; the pushed half is this card's."""
    rows = conn.execute(
        "SELECT gh_comment_id FROM task_comments WHERE gh_comment_id IS NOT NULL "
        "UNION SELECT json_extract(payload, '$.comment') FROM task_events "
        "WHERE task_id = ? AND kind = 'github_pushed' AND json_extract(payload, '$.comment') IS NOT NULL",
        (task_id,),
    )
    return {r[0] for r in rows}


def note(conn: sqlite3.Connection, task_id: str, text: str) -> None:
    store.comment(conn, task_id, f"[github] {text}", author="github", actor="github")


def pull_comments(conn: sqlite3.Connection, task_id: str, issue: Issue) -> int:
    """Each GitHub comment not yet seen becomes a card comment, author `github:<login>`."""
    seen, pulled = crossed(conn, task_id), 0
    for c in issue.get("_comments") or []:
        cid = str(c["node_id"])
        if cid in seen or not norm(c.get("body")):
            continue
        who = (c.get("user") or {}).get("login") or "unknown"
        got = store.comment(conn, task_id, c["body"], author=f"github:{who}", actor="github")
        conn.execute(
            "UPDATE task_comments SET gh_comment_id = ? WHERE id = ?", (cid, got["comment_id"])
        )
        pulled += 1
    return pulled


# --- merging one link -----------------------------------------------------------------------


class Merge(NamedTuple):
    pull: dict[str, str]  # card column -> the GitHub value that replaces it
    push: dict[str, str]  # card column -> the card value that replaces GitHub's
    agree: dict[str, str]  # card column -> a value both sides already hold
    lost: dict[str, str]  # card column -> the card value that lost to GitHub


class Plan(NamedTuple):
    act: str | None  # `closed` or `reopened`: what the issue's state does to the card
    conflict: str | None  # why it cannot, for the owner
    state: str | None  # `closed` or `open`: the state to put the issue in
    option: str | None  # the project Status option to set


def merge_text(t: Row, lk: Row, issue: Issue) -> Merge:
    """Each side is compared with what it held at the last sync: GitHub with the read-back, the card
    with its own text then."""
    m = Merge({}, {}, {}, {})
    for col in ("title", "body"):
        card, theirs = t[col], issue.get(col) or ""
        card_edited = norm(card) != lk[f"card_{col}"]
        if norm(theirs) != lk[f"base_{col}"]:
            if norm(card) == norm(theirs):
                m.agree[col] = norm(theirs)
            else:
                m.pull[col] = theirs
                if card_edited:
                    m.lost[col] = card
        elif card_edited:
            m.push[col] = card
    return m


def plan_status(conn: sqlite3.Connection, repo: Repo, t: Row, lk: Row, issue: Issue) -> Plan:
    """A flip of the issue's state closes or reopens the card unless that would start or cut short
    work: a running or review card, or a `todo` child that closing would make ready, leaves a conflict
    for the owner. The card's own status is pushed only when it changed since the last sync."""
    done_now = is_done(issue)
    flipped = done_now != (lk["base_state"] == "done")
    if flipped and done_now and t["status"] in ("running", "review"):
        return Plan(None, f"closed in GitHub while the card is {t['status']}", None, None)
    if flipped and done_now and t["status"] in CLOSABLE:
        waiting = conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? AND c.status = 'todo'",
            (t["id"],),
        ).fetchone()
        if waiting:
            return Plan(None, "closed in GitHub while a child card waits on it", None, None)
        return Plan("closed", None, None, None)
    if flipped and not done_now and t["status"] in ("done", "archived"):
        return Plan("reopened", None, None, None)
    mapped = store.cfg(conn, "github.status_map").get(t["status"])
    if not mapped or t["status"] == lk["synced_status"]:
        return Plan(None, None, None, None)
    state = None
    if t["status"] == "done" and not done_now:
        state = "closed"
    elif t["status"] in OPENABLE and done_now:
        state = "open"
    return Plan(None, None, state, mapped if repo.project else None)


def outbox(conn: sqlite3.Connection, lk: Row, t: Row) -> list[tuple[str, int, str]]:
    """What has not crossed yet. The owner's comments, and the terminal-verb text of each work run and
    each review run's verdict, never `[run]` lines or the worker's own comments."""
    items = [
        ("comment", r["id"], r["body"])
        for r in conn.execute(
            "SELECT id, body FROM task_comments WHERE task_id = ? AND author = 'owner' "
            "AND gh_comment_id IS NULL AND created_at >= ? ORDER BY id",
            (t["id"], lk["created_at"]),
        )
    ]
    for r in conn.execute(
        "SELECT id, kind, terminal_verb, summary FROM task_runs WHERE task_id = ? "
        "AND terminal_verb IN ('complete', 'request-review', 'block', 'request-changes') "
        "AND COALESCE(summary, '') <> '' "
        "AND ended_at >= ? AND NOT EXISTS "
        "(SELECT 1 FROM task_events WHERE run_id = task_runs.id AND kind = 'github_pushed') ORDER BY id",
        (t["id"], lk["created_at"]),
    ):
        if (r["kind"], r["terminal_verb"]) not in VERBS:
            continue
        text = f"kanban {VERBS[r['kind'], r['terminal_verb']]}: {r['summary']}"
        if t["pr_url"] and t["pr_url"] not in text:
            text += f"\nPR: {t['pr_url']}"
        items.append(("run", r["id"], text))
    return items


def push_comments(
    conn: sqlite3.Connection, repo: Repo, lk: Row, t: Row, issue: Issue,
    items: list[tuple[str, int, str]],
) -> None:  # fmt: skip
    """Each id is recorded in a transaction of its own, right after the call, so no later failure can
    send the same comment twice. A `github_sending` marker goes in first, also on its own: a comment
    with the same text as one whose send has a marker, left by a call whose answer never came, is
    adopted instead of sent again. Without a marker, an equal text is somebody else's."""
    seen = crossed(conn, t["id"])
    there = {str(c["node_id"]): norm(c.get("body")) for c in issue.get("_comments") or []}
    there = {i: body for i, body in there.items() if i not in seen}
    for kind, ref, text in items:
        tried = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'github_sending' "
            "AND json_extract(payload, '$.kind') = ? AND json_extract(payload, '$.ref') = ?",
            (t["id"], kind, ref),
        ).fetchone()
        cid = next((i for i, body in there.items() if tried and body == norm(text)), None)
        if cid:
            del there[cid]
        else:
            if not tried:
                with store.tx(conn):
                    store.event(conn, t, "github_sending", "github", {"kind": kind, "ref": ref})
            sent = call(
                repo.host, f"{repo.path}/issues/{lk['number']}/comments", method="POST",
                body={"body": text},
            )  # fmt: skip
            cid = str(sent["node_id"])
        with store.tx(conn):
            if kind == "comment":
                conn.execute("UPDATE task_comments SET gh_comment_id = ? WHERE id = ?", (cid, ref))
            store.event(
                conn, t, "github_pushed", "github", {"comment": cid},
                run_id=ref if kind == "run" else None,
            )  # fmt: skip


def status_option(conn: sqlite3.Connection, t: Row) -> str | None:
    return store.cfg(conn, "github.status_map").get(t["status"])  # type: ignore[no-any-return]


def push(
    conn: sqlite3.Connection, repo: Repo, lk: Row, t: Row, issue: Issue, m: Merge, plan: Plan,
    items: list[tuple[str, int, str]],
) -> tuple[list[str], str | None, Issue, str | None]:  # fmt: skip
    """The card's side to GitHub. A failure in the text, the state or the project stops those and stays
    on the link, but the comments go out on their own; whatever did go out is read back, and that
    read-back, not the card's text, is GitHub's side's new base. The last value is the project item."""
    pushed, error, after, item = [], None, issue, lk["project_item_id"]
    edit: dict[str, str] = dict(m.push)
    if plan.state:
        edit |= {"state": plan.state}
        if plan.state == "closed":
            edit["state_reason"] = "completed"
    try:
        if edit:
            call(repo.host, f"{repo.path}/issues/{lk['number']}", method="PATCH", body=edit)
            pushed += sorted(m.push) + (["status"] if plan.state else [])
        if repo.project and (plan.option or item is None):
            item = repo.set_status(issue["node_id"], plan.option or status_option(conn, t))
            pushed += ["project"] if plan.option else []
    except LinkError as e:
        error = str(e)
    try:
        push_comments(conn, repo, lk, t, issue, items)
    except LinkError as e:
        error = error or str(e)
    if edit:
        try:
            after = repo.fetch(lk["number"])
        except LinkError as e:
            error = error or str(e)
    return pushed, error, after, item


def pull_text(conn: sqlite3.Connection, t: Row, m: Merge) -> None:
    """GitHub's text onto the card. An approved card (todo, ready, blocked) goes back to triage first,
    so the owner sees the new text before an agent works from it."""
    held = t["status"] in ("todo", "ready", "blocked")
    if held:
        store.hold(conn, t["id"], "text changed in GitHub", actor="github")
    store.edit(
        conn,
        t["id"],
        {c: norm(v) for c, v in m.pull.items()},
        actor="github",
        allow_empty_body=True,
    )
    if held:
        note(conn, t["id"], f"{' and '.join(sorted(m.pull))} changed; the card is back in triage")
    for col, value in m.lost.items():
        note(
            conn, t["id"],
            f"conflict: GitHub's {col} replaced the card's. The card's value was:\n{value}",
        )  # fmt: skip
        store.event(conn, t, "github_conflict", "github", {"field": col})


def sync_link(conn: sqlite3.Connection, repo: Repo, lk: Row, issue: Issue) -> None:
    """One link, both ways: decide from the card as read here, write to GitHub, then apply to the
    board in one transaction, only if the card is still at the version it was read at."""
    t = store.get_task(conn, lk["task_id"])
    tid, m, items = t["id"], merge_text(t, lk, issue), outbox(conn, lk, t)
    plan = plan_status(conn, repo, t, lk, issue)
    pushed, error, after, item = push(conn, repo, lk, t, issue, m, plan, items)
    with store.tx(conn):
        cur = store.get_task(conn, tid)
        fresh = cur["version"] == t["version"]
        base = {c: lk[f"base_{c}"] for c in ("title", "body")}
        mine = {c: lk[f"card_{c}"] for c in ("title", "body")}
        for c, v in m.agree.items():
            base[c] = mine[c] = v
        for c in pushed:
            if c in m.push:
                base[c], mine[c] = norm(after.get(c)), norm(m.push[c])
        changed: list[str] = []
        # a running or review card keeps GitHub's text out and its base where it was: the next pass,
        # after the card has left those states, picks it up
        if fresh and m.pull and cur["status"] not in ("running", "review"):
            try:
                pull_text(conn, cur, m)
            except Refused as e:
                error = error or str(e)
            else:
                changed += sorted(m.pull)
                for c, v in m.pull.items():
                    base[c] = mine[c] = norm(v)
        if fresh and plan.act:
            why = f"{plan.act} in GitHub"
            if plan.act == "closed":
                store.mark_done(conn, tid, why, actor="github", expected=CLOSABLE)
            else:
                store.reopen(
                    conn, tid, why, actor="github", expected=("done", "archived"), status="triage"
                )
            note(conn, tid, why)
            changed.append("status")
        comments = pull_comments(conn, tid, after)
        if changed or comments:
            store.event(
                conn, cur, "github_pulled", "github", {"fields": changed, "comments": comments}
            )
        if pushed:
            store.event(conn, cur, "github_pushed", "github", {"fields": pushed})
        conflict = plan.conflict
        if conflict and conflict != lk["conflict"]:
            store.event(conn, cur, "github_conflict", "github", {"detail": conflict})
        # a push that failed leaves the card's version and status as they were, so the link stays
        # wanted and the next pass sends it again
        settled = fresh and not error
        new_item = bool(repo.project and lk["project_item_id"] is None)
        attempted = bool(m.push or m.pull or plan.state or plan.option or items or new_item)
        error = error or (None if attempted else lk["error"])
        if error and error != lk["error"]:
            store.event(conn, cur, "github_error", "github", {"detail": error})
        keep = plan.conflict or (not fresh and plan.act)
        now = store.get_task(conn, tid)
        conn.execute(
            "UPDATE github_links SET base_title = ?, base_body = ?, card_title = ?, card_body = ?, "
            "base_state = ?, gh_updated = ?, synced_status = ?, synced_version = ?, "
            "project_item_id = ?, conflict = ?, error = ? WHERE task_id = ?",
            (
                base["title"], base["body"], mine["title"], mine["body"],
                lk["base_state"] if keep else ("done" if is_done(after) else "open"),
                after.get("updated_at"),
                now["status"] if settled else lk["synced_status"],
                now["version"] if settled else lk["synced_version"],
                item, conflict, error, tid,
            ),
        )  # fmt: skip


# --- membership -----------------------------------------------------------------------------


def add_link(
    conn: sqlite3.Connection,
    t: Row,
    repo: str,
    issue: Issue,
    actor: str,
    *,
    visit: bool = False,
    adopted: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Link card `t`. GitHub's base is `issue`'s text and the card side's is the card's own, as it is
    now. `visit`: the two have nothing in common yet, so both are empty and the next pass fetches the
    issue whatever the listing says. `adopted`: the issue came from an earlier call, so the card side
    is what GitHub holds and an edit made since reads as one."""
    title, body = issue.get("title") or "", issue.get("body") or ""
    mine = (title, body) if adopted else (t["title"], t["body"])
    if visit:
        title = body = ""
        mine = ("", "")
    conn.execute(
        "INSERT INTO github_links(task_id, repo, number, node_id, base_title, base_body, card_title, "
        "card_body, base_state, gh_updated, synced_status, synced_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            t["id"], repo, issue["number"], issue["node_id"], norm(title), norm(body),
            norm(mine[0]), norm(mine[1]), "done" if is_done(issue) else "open",
            None if visit else issue.get("updated_at"), t["status"], t["version"], store.now(),
        ),
    )  # fmt: skip
    store.event(
        conn, t, "github_linked", actor, {"issue": f"{repo}#{issue['number']}", **(extra or {})}
    )


def add_card(
    conn: sqlite3.Connection, slug: str, repo: str, issue: Issue, actor: str
) -> str | None:
    """A new triage card for an issue. None when an active card already stands for it."""
    ref = f"{repo}#{issue['number']}"
    with store.tx(conn):
        if conn.execute(
            "SELECT 1 FROM github_links WHERE repo = ? AND number = ?", (repo, issue["number"])
        ).fetchone():
            raise Refused(f"{ref} is already linked to a card")
        got = store.create(
            conn, slug, issue["title"], body=norm(issue.get("body")), triage=True,
            actor=actor, idempotency_key=f"github:{ref}",
        )  # fmt: skip
        if got["existing"]:
            return None
        add_link(conn, store.get_task(conn, got["id"]), repo, issue, actor)
        pull_comments(conn, got["id"], issue)
    return str(got["id"])


def drop(conn: sqlite3.Connection, lk: Row, reason: str, actor: str = "github") -> None:
    with store.tx(conn):
        t = store.get_task(conn, lk["task_id"])
        conn.execute("DELETE FROM github_links WHERE task_id = ?", (lk["task_id"],))
        ref = f"{lk['repo']}#{lk['number']}"
        store.event(conn, t, "github_unlinked", actor, {"issue": ref, "reason": reason})
        if reason == "gone":
            note(conn, t["id"], f"{ref} no longer exists in GitHub; the card is kept and unlinked")


def last_error(conn: sqlite3.Connection, t: Row) -> str | None:
    row = conn.execute(
        "SELECT json_extract(payload, '$.detail') FROM task_events "
        "WHERE task_id = ? AND kind = 'github_error' ORDER BY id DESC LIMIT 1",
        (t["id"],),
    ).fetchone()
    return None if row is None else str(row[0])


def export(conn: sqlite3.Connection, repo: Repo, label: str) -> None:
    """An issue for each card without one. A card that was unlinked, or whose issue is gone, stays out;
    so do finished cards, which would only be history. A `github_exporting` marker goes in before the
    create, so a pass whose answer never came finds the issue by its title among those updated since
    the marker and adopts it, not makes it twice. A card that fails keeps its error as an event and
    the rest go on."""
    cards = conn.execute(
        "SELECT * FROM tasks t WHERE board = ? AND status NOT IN ('archived', 'done') "
        "AND NOT EXISTS (SELECT 1 FROM github_links WHERE task_id = t.id) "
        "AND NOT EXISTS (SELECT 1 FROM task_events WHERE task_id = t.id AND kind = 'github_unlinked') "
        "ORDER BY created_at",
        (repo.slug,),
    ).fetchall()
    for t in cards:
        try:
            marked = conn.execute(
                "SELECT MIN(created_at) FROM task_events WHERE task_id = ? AND kind = 'github_exporting'",
                (t["id"],),
            ).fetchone()[0]
            hit = None
            if marked:
                taken = {
                    r[0]
                    for r in conn.execute(
                        "SELECT number FROM github_links WHERE repo = ?", (repo.name,)
                    )
                }
                hit = next(
                    (
                        i for i in repo.labelled(label, iso(marked - 60))
                        if i["title"] == t["title"] and i["number"] not in taken
                    ),
                    None,
                )  # fmt: skip
            adopted = hit is not None
            if not hit:
                if not marked:
                    with store.tx(conn):
                        store.event(conn, t, "github_exporting", "github", {})
                hit = call(
                    repo.host, f"{repo.path}/issues", method="POST",
                    body={"title": t["title"], "body": t["body"], "labels": [label]},
                )  # fmt: skip
            issue = repo.comments(hit)
        except LinkError as e:
            log.warning("%s: %s", t["id"], e)
            with store.tx(conn):
                if last_error(conn, t) != str(e):
                    store.event(conn, t, "github_error", "github", {"detail": str(e)})
            continue
        with store.tx(conn):
            add_link(conn, t, repo.name, issue, "github", adopted=adopted, extra={"created": True})


def sync_one(conn: sqlite3.Connection, repo: Repo, number: int, issue: Issue | None) -> None:
    link = conn.execute(
        "SELECT * FROM github_links WHERE repo = ? AND number = ?", (repo.name, number)
    ).fetchone()
    try:
        raw = issue or repo.get(number)
    except Gone:
        if link:
            drop(conn, link, "gone")
        return
    if link:
        sync_link(conn, repo, link, repo.comments(raw))
    elif (
        not is_done(raw)
        and not conn.execute(
            "SELECT 1 FROM task_events WHERE kind = 'github_unlinked' AND json_extract(payload, '$.issue') = ?",
            (f"{repo.name}#{number}",),
        ).fetchone()
    ):  # an unlinked issue stays out until `kanban github import` brings it back
        add_card(conn, repo.slug, repo.name, repo.comments(raw), "github")


def link_error(conn: sqlite3.Connection, repo: str, number: int, detail: str) -> None:
    with store.tx(conn):
        row = conn.execute(
            "SELECT l.task_id, l.error, t.board FROM github_links l JOIN tasks t ON t.id = l.task_id "
            "WHERE l.repo = ? AND l.number = ?",
            (repo, number),
        ).fetchone()
        if row and row["error"] != detail:
            conn.execute(
                "UPDATE github_links SET error = ? WHERE task_id = ?", (detail, row["task_id"])
            )
            store.event(
                conn, {"board": row["board"], "id": None}, "github_error", "github",
                {"detail": detail, "card": row["task_id"]},
            )  # fmt: skip


def sync_board(conn: sqlite3.Connection, repo: Repo) -> None:
    """Export, pull, push, apply, for one paired board. Export comes first: an issue made by an
    earlier, interrupted pass carries the board's label and would otherwise be imported as a second
    card."""
    slug, started, label = repo.slug, store.now(), store.cfg(conn, "github.label")
    repo.use_project()
    export(conn, repo, label)
    since = (store.state_get(conn, f"github:{slug}") or {}).get("last_pull_at")
    window = None if since is None else iso(since - 120)
    links = {
        lk["number"]: lk
        for lk in conn.execute(
            "SELECT l.* FROM github_links l JOIN tasks t ON t.id = l.task_id "
            "WHERE t.board = ? AND l.repo = ?",
            (slug, repo.name),
        )
    }
    found: dict[int, Issue | None] = {i["number"]: i for i in repo.members(label, window)}
    if window:
        for i in repo.changed(window):
            if i["number"] in links:
                found.setdefault(i["number"], i)
    else:
        found |= {n: None for n in links}
    for n, lk in links.items():
        t = store.get_task(conn, lk["task_id"])
        if (
            t["version"] > lk["synced_version"]
            or lk["gh_updated"] is None
            or outbox(conn, lk, t)
            or (repo.project and lk["project_item_id"] is None)
        ):
            found.setdefault(n, None)
    clean = True
    for n, issue in found.items():
        try:
            sync_one(conn, repo, n, issue)
        except (LinkError, Refused, sqlite3.Error) as e:
            clean = False
            log.warning("%s: %s", repo.ref(n), e)
            link_error(conn, repo.name, n, str(e))
    with store.tx(conn):
        st = store.state_get(conn, f"github:{slug}") or {}
        st.pop("paused", None)
        if clean:
            st["last_pull_at"] = started
        store.state_put(conn, f"github:{slug}", st)


def pause(conn: sqlite3.Connection, slug: str, detail: str) -> None:
    """The sync of this board stops being attempted until a pass succeeds; dispatch never notices."""
    with store.tx(conn):
        st = store.state_get(conn, f"github:{slug}") or {}
        was = st.get("paused")
        st["paused"] = {"since": was["since"] if was else store.now(), "detail": detail}
        store.state_put(conn, f"github:{slug}", st)
        if not was or was["detail"] != detail:
            store.event(
                conn, {"board": slug, "id": None}, "github_error", "github", {"detail": detail}
            )


def paired(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute(
        "SELECT * FROM boards WHERE github_repo IS NOT NULL AND archived_at IS NULL ORDER BY slug"
    ).fetchall()


def sync_all(conn: sqlite3.Connection, only: str | None = None) -> list[str]:
    """One pass over every paired board (or `only`). The slugs that synced; a board that GitHub could
    not serve is paused instead, and the others go on."""
    boards = [b for b in paired(conn) if only in (None, b["slug"])]
    if only and not boards:
        raise Refused(
            f"board {only!r} is not paired with a GitHub repository; pair it with: "
            f"kanban boards edit {only} --github-repo [HOST/]OWNER/REPO"
        )
    synced: list[str] = []
    if not boards:
        return synced
    with lock():
        for b in boards:
            try:
                sync_board(conn, Repo(b))
                synced.append(b["slug"])
            except (Paused, LinkError) as e:
                pause(conn, b["slug"], str(e))
    return synced


def poll(conn: sqlite3.Connection) -> None:
    """The housekeeping job: skipped while a CLI sync holds the lock.

    shortcut: the pass runs inside the housekeeping tick, so a slow gh call delays the other jobs by
    up to `GH_CALL_S`; give it a thread of its own if that shows up.
    """
    with contextlib.suppress(Busy):
        sync_all(conn)


# --- owner verbs ----------------------------------------------------------------------------


def _paired(conn: sqlite3.Connection, slug: str) -> Repo:
    b = store.get_board(conn, slug)
    if not b["github_repo"]:
        raise Refused(
            f"board {slug!r} is not paired with a GitHub repository; pair it with: "
            f"kanban boards edit {slug} --github-repo [HOST/]OWNER/REPO"
        )
    return Repo(b)


def number_of(repo: Repo, raw: str) -> int:
    """`12`, `OWNER/REPO#12` or the issue's URL; a reference to another repository is refused."""
    m = re.fullmatch(
        r"(?:(?:https?://([^/]+)/)?([\w.-]+/[\w.-]+)(?:#|/issues/))?(\d+)", raw.strip()
    )
    if not m:
        raise Refused(f"{raw!r} is not an issue like 12, OWNER/REPO#12 or its URL")
    host, name = (m[1] or repo.host), m[2] or repo.name.partition("/")[2]
    if f"{host}/{name}".lower() != repo.name.lower():
        raise Refused(f"{raw!r} is not in {repo.name}, the repository of board {repo.slug!r}")
    return int(m[3])


def read_issue(repo: Repo, raw: str) -> Issue:
    with lock():
        try:
            issue = repo.fetch(number_of(repo, raw))
        except (Gone, Paused) as e:
            raise Refused(str(e)) from e
    if "pull_request" in issue:
        raise Refused(f"#{issue['number']} is a pull request, not an issue")
    return issue


def import_issue(conn: sqlite3.Connection, slug: str, raw: str) -> dict[str, Any]:
    """Link an issue outside the board's label or query by making a triage card for it."""
    repo = _paired(conn, slug)
    issue = read_issue(repo, raw)
    tid = add_card(conn, slug, repo.name, issue, store.OWNER)
    if tid is None:
        raise Refused(
            f"an active card already stands for {repo.ref(issue['number'])}; "
            f"link it with: kanban github link ID {issue['number']}"
        )
    return {"id": tid, "issue": repo.ref(issue["number"])}


def link_card(conn: sqlite3.Connection, task_id: str, raw: str) -> dict[str, Any]:
    """Link an existing card. Nothing is common yet, so on the first merge GitHub wins field by field
    and the card's text goes into a conflict comment; equal texts just agree."""
    repo = _paired(conn, store.get_task(conn, task_id)["board"])
    issue = read_issue(repo, raw)
    with store.tx(conn):
        t = store.get_task(conn, task_id)
        if conn.execute(
            "SELECT 1 FROM github_links WHERE task_id = ? OR (repo = ? AND number = ?)",
            (task_id, repo.name, issue["number"]),
        ).fetchone():
            raise Refused(
                f"card {task_id} or issue {repo.ref(issue['number'])} is already linked; unlink it first"
            )
        add_link(conn, t, repo.name, issue, store.OWNER, visit=True)
    return {"id": task_id, "issue": repo.ref(issue["number"])}


def unlink_card(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    link = conn.execute("SELECT * FROM github_links WHERE task_id = ?", (task_id,)).fetchone()
    if link is None:
        store.get_task(conn, task_id)
        raise Refused(f"card {task_id} is not linked to a GitHub issue")
    drop(conn, link, "owner", store.OWNER)
    return {"id": task_id, "issue": f"{link['repo'].partition('/')[2]}#{link['number']}"}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """Paired boards with their last pull and pause state, and the links in trouble."""
    boards = [
        {
            "board": b["slug"],
            "repo": b["github_repo"],
            "project": b["github_project"],
            **(store.state_get(conn, f"github:{b['slug']}") or {}),
        }
        for b in paired(conn)
    ]
    broken = conn.execute(
        "SELECT t.board, l.task_id, l.repo, l.number, l.conflict, l.error FROM github_links l "
        "JOIN tasks t ON t.id = l.task_id WHERE l.conflict IS NOT NULL OR l.error IS NOT NULL "
        "ORDER BY l.task_id"
    ).fetchall()
    return {"boards": boards, "links": [dict(r) for r in broken]}
