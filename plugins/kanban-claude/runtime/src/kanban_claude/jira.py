"""Jira sync (JIR): a paired board and its issues stay in step through the owner's own Atlassian MCP
server, with no model in the loop. Jira owns the text, the board owns execution.

Every pass runs outside a transaction while it talks to the server; each link is then written in
one `BEGIN IMMEDIATE` through the same domain functions as the owner verbs, with actor `jira`.
"""

import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import select
import signal
import sqlite3
import subprocess
import time
from collections.abc import Iterator
from typing import Any, NamedTuple

from . import store
from .errors import Error, Refused
from .paths import dirs
from .procs import signal_group

NO_COMMAND = "jira.mcp_command is not set; add an atlassian MCP server and run /kanban-claude:setup"
JIRA_CALL_S = 60
PROTOCOL = "2025-06-18"
TOOLS = (
    "jira_search", "jira_get_issue", "jira_create_issue", "jira_update_issue",
    "jira_get_transitions", "jira_transition_issue", "jira_add_comment",
)  # fmt: skip
CLOSABLE = ("triage", "todo", "ready", "blocked")
JIRA_NAME = {"title": "summary", "body": "description"}
VERBS = {"complete": "completed", "request-review": "ready for review", "block": "blocked"}
AUTH = re.compile(
    r"(?<![\w-])(401|403)(?![\w-])|\b(unauthori[sz]ed|forbidden|authentication)\b", re.IGNORECASE
)
KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
PAGE = 50
log = logging.getLogger("kanban.jira")
Row = sqlite3.Row
Issue = dict[str, Any]


class Paused(Error):
    """The server cannot be used (JIR-08): the board's sync pauses and the next pass retries."""


class LinkError(Error):
    """One link failed (JIR-05); the message stays on the link."""


class Gone(LinkError):
    """Jira reports the issue gone (JIR-03)."""


class Busy(Refused):
    """Another sync holds `jira.lock`."""


# --- the MCP client -------------------------------------------------------------------------


class Mcp:
    """JIR-02: the owner's Atlassian MCP server over stdio, one JSON object per line. Close it with
    `contextlib.closing`: a leaked child is a leaked Atlassian session.

    ponytail: one process per board and pass; keep it alive between passes if startup time shows up
    in `stats`.
    """

    def __init__(self, argv: list[str]) -> None:
        self.buf, self.n = b"", 0
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )  # fmt: skip
        except OSError as e:
            raise Paused(f"cannot start {argv[0]}: {e}") from e
        try:
            self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "kanban-claude", "version": "1"},
                },
            )
            self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Error as e:
            self.close()
            raise Paused(f"initialize failed: {e}") from e

    def send(self, msg: dict[str, Any]) -> None:
        """ponytail: the write has no deadline. A body over the 64 KiB pipe buffer to a server that has
        stopped reading blocks the thread; non-blocking writes under `select` would bound it."""
        assert self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise Paused(f"the server closed its input: {e}") from e

    def read(self, deadline: float) -> dict[str, Any]:
        assert self.proc.stdout
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([fd], [], [], wait)[0]:
                raise Paused(f"no answer within {JIRA_CALL_S} s")
            chunk = os.read(fd, 65536)
            if not chunk:
                raise Paused("the server exited")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        try:
            msg = json.loads(line)
        except ValueError:
            return {}
        return msg if isinstance(msg, dict) else {}

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        deadline = time.monotonic() + JIRA_CALL_S
        while (msg := self.read(deadline)).get("id") != self.n:
            pass
        if "error" in msg:
            raise LinkError(f"{method}: {msg['error'].get('message', msg['error'])}")
        result: dict[str, Any] = msg.get("result") or {}
        return result

    def call(self, tool: str, **args: Any) -> Any:
        """The parsed JSON of a tool result. `isError` is a `Paused` for authentication and a
        `LinkError` for the rest."""
        res = self.request("tools/call", {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        if res.get("isError"):
            raise (Paused if AUTH.search(text) else LinkError)(f"{tool}: {text[:200]}")
        try:
            return json.loads(text) if text.strip() else None
        except ValueError as e:
            raise LinkError(f"{tool}: the result is not JSON") from e

    def close(self) -> None:
        """Whatever the server did, its process group goes: a wrapper script leaves grandchildren."""
        with contextlib.suppress(OSError):
            assert self.proc.stdin
            self.proc.stdin.close()
        try:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)
        finally:
            signal_group(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


@contextlib.contextmanager
def lock() -> Iterator[None]:
    """One sync at a time, daemon or CLI: two would each create the issue for a new card."""
    with open(dirs().home / "jira.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise Busy("a Jira sync is already running; try again in a minute") from e
        yield


def probe(command: list[str]) -> list[str]:
    """Doctor check 20: the tool names the server lists."""
    with contextlib.closing(Mcp(command)) as mcp:
        return [t["name"] for t in mcp.request("tools/list", {}).get("tools", [])]


# --- reading the server (shapes of mcp-atlassian 0.23.1) --------------------------------------


def norm(text: str | None) -> str:
    """JIR-04: CRLF to LF, trailing whitespace per line stripped, ends trimmed."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def comment_id(reply: Any) -> str:
    """The id `jira_add_comment` returns; a reply without one still gets a unique stand-in, so the
    comment never goes out twice."""
    return str(
        (reply.get("id") if isinstance(reply, dict) else None) or f"unknown-{secrets.token_hex(4)}"
    )


def board_jql(conn: sqlite3.Connection, b: Row) -> str:
    if b["jira_jql"]:
        return str(b["jira_jql"])
    label = store.cfg(conn, "jira.label").replace("\\", "\\\\").replace('"', '\\"')
    return f'project = {b["jira_project"]} AND labels = "{label}" AND assignee = currentUser()'


def fetch(mcp: Mcp, key: str) -> Issue:
    """The one read that can say an issue is gone (JIR-03): the same words from any other tool are
    just an error on that link."""
    try:
        issue: Issue = mcp.call(
            "jira_get_issue", issue_key=key, include="comments", update_history=False
        )
    except LinkError as e:
        if "not found" in str(e).lower():
            raise Gone(str(e)) from e
        raise
    return issue


def search(mcp: Mcp, jql: str) -> list[str]:
    """JIR-07: every key `jql` matches, paginated by `start_at`. A refused query pauses the board."""
    keys: list[str] = []
    while True:
        try:
            page = mcp.call(
                "jira_search", jql=jql, fields="summary", limit=PAGE, start_at=len(keys)
            )
        except LinkError as e:
            raise Paused(f"the search was refused: {e}") from e
        got = [i["key"] for i in page.get("issues", [])]
        keys += got
        total = page.get("total", -1)
        if not got or (len(keys) >= total if total >= 0 else len(got) < PAGE):
            return keys
        if total < 0:
            raise Paused("the search cannot be paged: the server reports no total")


def crossed(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """JIR-06: the Jira id of every comment that crossed, either way. Comment ids are global in Jira
    and in `idx_comments_jira`, so the pulled half spans every card; the pushed half is this card's."""
    rows = conn.execute(
        "SELECT jira_comment_id FROM task_comments WHERE jira_comment_id IS NOT NULL "
        "UNION SELECT json_extract(payload, '$.comment') FROM task_events "
        "WHERE task_id = ? AND kind = 'jira_pushed' AND json_extract(payload, '$.comment') IS NOT NULL",
        (task_id,),
    )
    return {r[0] for r in rows}


def note(conn: sqlite3.Connection, task_id: str, text: str) -> None:
    store.comment(conn, task_id, f"[jira] {text}", author="jira", actor="jira")


def pull_comments(conn: sqlite3.Connection, task_id: str, issue: Issue) -> int:
    """JIR-06: each Jira comment not yet seen becomes a card comment, author `jira:<display name>`."""
    seen, pulled = crossed(conn, task_id), 0
    for c in issue.get("comments") or []:
        cid = str(c.get("id"))
        if cid in seen or not norm(c.get("body")):
            continue
        who = (c.get("author") or {}).get("display_name") or "unknown"
        store.comment(conn, task_id, c["body"], author=f"jira:{who}", actor="jira", jira_id=cid)
        pulled += 1
    return pulled


# --- merging one link -----------------------------------------------------------------------


class Merge(NamedTuple):
    pull: dict[str, str]  # card column -> the Jira value that replaces it
    push: dict[str, str]  # card column -> the card value that replaces Jira's
    agree: dict[str, str]  # card column -> a value both sides already hold
    lost: dict[str, str]  # card column -> the card value that lost to Jira


class Plan(NamedTuple):
    act: str | None  # `closed` or `reopened`: what the issue's status does to the card
    conflict: str | None  # why it cannot, for the owner
    want: str | None  # the status to move the issue to


def merge_text(t: Row, lk: Row, issue: Issue) -> Merge:
    """JIR-04. Each side is compared with what it held at the last sync: Jira with the read-back,
    the card with its own text then. Neither is the other's, since Jira rewrites what it stores."""
    m = Merge({}, {}, {}, {})
    for col, name in JIRA_NAME.items():
        card, theirs = t[col], issue.get(name) or ""
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


def plan_status(conn: sqlite3.Connection, t: Row, lk: Row, issue: Issue) -> Plan:
    """JIR-05. A flip of the issue's Done closes or reopens the card unless that would start or cut
    short work: a running or review card, or a `todo` child that closing would make ready, leaves a
    conflict for the owner. The card's own status is pushed only when it changed since the last sync."""
    st = issue.get("status") or {}
    done_now = (st.get("category") or "").lower() == "done"
    flipped = done_now != ((lk["base_jira_status"] or "").lower() == "done")
    if flipped and done_now and t["status"] in ("running", "review"):
        return Plan(None, f"closed in Jira while the card is {t['status']}", None)
    if flipped and done_now and t["status"] in CLOSABLE:
        waiting = conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks c ON c.id = l.child_id "
            "WHERE l.parent_id = ? AND c.status = 'todo'",
            (t["id"],),
        ).fetchone()
        return (
            Plan(None, "closed in Jira while a child card waits on it", None)
            if waiting
            else Plan("closed", None, None)
        )
    if flipped and not done_now and t["status"] in ("done", "archived"):
        return Plan("reopened", None, None)
    target = store.cfg(conn, "jira.status_map").get(t["status"])
    if (
        not target
        or t["status"] == lk["synced_status"]
        or target.lower() == (st.get("name") or "").lower()
        or (t["status"] == "done" and done_now)
    ):
        return Plan(None, None, None)
    return Plan(None, None, target)


def outbox(conn: sqlite3.Connection, lk: Row, t: Row) -> list[tuple[str, int, str]]:
    """JIR-06: what has not crossed yet. The owner's comments, and the terminal-verb text of each
    work run, never `[run]` lines or review ledgers."""
    items = [
        ("comment", r["id"], r["body"])
        for r in conn.execute(
            "SELECT id, body FROM task_comments WHERE task_id = ? AND author = 'owner' "
            "AND jira_comment_id IS NULL AND created_at >= ? ORDER BY id",
            (t["id"], lk["created_at"]),
        )
    ]
    for r in conn.execute(
        "SELECT id, terminal_verb, summary FROM task_runs WHERE task_id = ? AND kind = 'work' "
        "AND terminal_verb IN ('complete', 'request-review', 'block') AND COALESCE(summary, '') <> '' "
        "AND ended_at >= ? AND NOT EXISTS "
        "(SELECT 1 FROM task_events WHERE run_id = task_runs.id AND kind = 'jira_pushed') ORDER BY id",
        (t["id"], lk["created_at"]),
    ):
        text = f"kanban {VERBS[r['terminal_verb']]}: {r['summary']}"
        if t["pr_url"] and t["pr_url"] not in text:
            text += f"\nPR: {t['pr_url']}"
        items.append(("run", r["id"], text))
    return items


def push_comments(
    conn: sqlite3.Connection,
    mcp: Mcp,
    key: str,
    t: Row,
    issue: Issue,
    items: list[tuple[str, int, str]],
) -> None:
    """Each id is recorded in a transaction of its own, right after the call, so no later failure can
    send the same comment twice. A `jira_sending` marker goes in first, also on its own: a comment
    with the same text as one whose send has a marker, left by a call whose answer never came, is
    adopted instead of sent again. Without a marker, an equal text is somebody else's."""
    seen = crossed(conn, t["id"])
    there = {str(c["id"]): norm(c.get("body")) for c in issue.get("comments") or []}
    there = {i: body for i, body in there.items() if i not in seen}
    for kind, ref, text in items:
        marker = {"kind": kind, "ref": ref}
        tried = conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'jira_sending' "
            "AND json_extract(payload, '$.kind') = ? AND json_extract(payload, '$.ref') = ?",
            (t["id"], kind, ref),
        ).fetchone()
        cid = next((i for i, body in there.items() if tried and body == norm(text)), None)
        if cid:
            del there[cid]
        else:
            if not tried:
                with store.tx(conn):
                    store.event(conn, t, "jira_sending", "jira", marker)
            cid = comment_id(mcp.call("jira_add_comment", issue_key=key, body=text))
        with store.tx(conn):
            if kind == "comment":
                conn.execute(
                    "UPDATE task_comments SET jira_comment_id = ? WHERE id = ?", (cid, ref)
                )
            store.event(
                conn,
                t,
                "jira_pushed",
                "jira",
                {"comment": cid},
                run_id=ref if kind == "run" else None,
            )


def move(mcp: Mcp, key: str, target: str) -> None:
    """JIR-05: one direct transition to `target`; never a chain. One that needs fields is refused by
    the server, which `call` makes an error on the link."""
    for tr in mcp.call("jira_get_transitions", issue_key=key) or []:
        if (tr.get("to_status") or "").lower() == target.lower():
            mcp.call("jira_transition_issue", issue_key=key, transition_id=str(tr["id"]))
            return
    raise LinkError(f"no transition from the current status to {target!r}")


def push(
    conn: sqlite3.Connection, mcp: Mcp, lk: Row, t: Row, issue: Issue, m: Merge, plan: Plan,
    items: list[tuple[str, int, str]],
) -> tuple[list[str], str | None, Issue]:  # fmt: skip
    """The card's side to Jira. A failure in the text or the status stops those two and stays on the
    link, but the comments go out on their own; whatever did go out is read back, and that read-back, not the card's text, is the Jira side's new base."""
    key, pushed, error, after = lk["jira_key"], [], None, issue
    try:
        if m.push:
            fields = {JIRA_NAME[c]: v for c, v in m.push.items()}
            mcp.call("jira_update_issue", issue_key=key, fields=json.dumps(fields))
            pushed += sorted(m.push)
        if plan.want:
            move(mcp, key, plan.want)
            pushed.append("status")
    except LinkError as e:
        error = str(e)
    try:
        push_comments(conn, mcp, key, t, issue, items)
    except LinkError as e:
        error = error or str(e)
    if pushed:
        try:
            after = fetch(mcp, key)
        except LinkError as e:
            error = error or str(e)
    return pushed, error, after


def pull_text(conn: sqlite3.Connection, t: Row, m: Merge) -> None:
    """JIR-04: Jira's text onto the card. An approved card (todo, ready, blocked) goes back to triage
    first, so the owner sees the new text before an agent works from it."""
    held = t["status"] in ("todo", "ready", "blocked")
    if held:
        store.hold(conn, t["id"], "text changed in Jira", actor="jira")
    store.edit(
        conn, t["id"], {c: norm(v) for c, v in m.pull.items()}, actor="jira", allow_empty_body=True
    )
    if held:
        note(conn, t["id"], f"{' and '.join(sorted(m.pull))} changed; the card is back in triage")
    for col, value in m.lost.items():
        note(
            conn,
            t["id"],
            f"conflict: Jira's {col} replaced the card's. The card's value was:\n{value}",
        )
        store.event(conn, t, "jira_conflict", "jira", {"field": col})


def sync_link(conn: sqlite3.Connection, mcp: Mcp, lk: Row, issue: Issue) -> None:
    """One link, both ways (JIR-04..06): decide from the card as read here, write to Jira, then apply
    to the board in one transaction, only if the card is still at the version it was read at."""
    t = store.get_task(conn, lk["task_id"])
    tid, m, items = t["id"], merge_text(t, lk, issue), outbox(conn, lk, t)
    plan = plan_status(conn, t, lk, issue)
    pushed, error, after = push(conn, mcp, lk, t, issue, m, plan, items)
    with store.tx(conn):
        cur = store.get_task(conn, tid)
        fresh = cur["version"] == t["version"]
        jb = {c: lk[f"base_{c}"] for c in JIRA_NAME}
        cb = {c: lk[f"card_{c}"] for c in JIRA_NAME}
        for c, v in m.agree.items():
            jb[c] = cb[c] = v
        for c in pushed:
            if c in m.push:
                jb[c], cb[c] = norm(after.get(JIRA_NAME[c])), norm(m.push[c])
        changed: list[str] = []
        # a running or review card keeps Jira's text out and its base where it was: the next pass,
        # after the card has left those states, picks it up
        if fresh and m.pull and cur["status"] not in ("running", "review"):
            try:
                pull_text(conn, cur, m)
            except Refused as e:
                error = error or str(e)
            else:
                changed += sorted(m.pull)
                for c, v in m.pull.items():
                    jb[c] = cb[c] = norm(v)
        if fresh and plan.act:
            why = f"{plan.act} in Jira"
            if plan.act == "closed":
                store.mark_done(conn, tid, why, actor="jira", expected=CLOSABLE)
            else:
                store.reopen(
                    conn, tid, why, actor="jira", expected=("done", "archived"), status="triage"
                )
            note(conn, tid, why)
            changed.append("status")
        comments = pull_comments(conn, tid, after)
        if changed or comments:
            store.event(conn, cur, "jira_pulled", "jira", {"fields": changed, "comments": comments})
        if pushed:
            store.event(conn, cur, "jira_pushed", "jira", {"fields": pushed})
        if plan.conflict and plan.conflict != lk["conflict"]:
            store.event(conn, cur, "jira_conflict", "jira", {"detail": plan.conflict})
        # a push that failed leaves the card's version and status as they were, so the link stays
        # wanted and the next pass sends it again
        settled = fresh and not error
        attempted = bool(m.push or m.pull or plan.want or items)
        error = error or (None if attempted else lk["error"])
        if error and error != lk["error"]:
            store.event(conn, cur, "jira_error", "jira", {"detail": error})
        keep = plan.conflict or (not fresh and plan.act)
        now = store.get_task(conn, tid)
        conn.execute(
            "UPDATE jira_links SET base_title = ?, base_body = ?, card_title = ?, card_body = ?, "
            "base_jira_status = ?, jira_updated = ?, synced_status = ?, synced_version = ?, "
            "conflict = ?, error = ? WHERE task_id = ?",
            (
                jb["title"], jb["body"], cb["title"], cb["body"],
                lk["base_jira_status"] if keep else (after.get("status") or {}).get("category"),
                after.get("updated"),
                now["status"] if settled else lk["synced_status"],
                now["version"] if settled else lk["synced_version"],
                plan.conflict, error, tid,
            ),
        )  # fmt: skip


# --- membership -----------------------------------------------------------------------------


def add_link(
    conn: sqlite3.Connection,
    t: Row,
    key: str,
    issue: Issue,
    actor: str,
    *,
    visit: bool = False,
    adopted: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Link card `t`. The Jira side's base is `issue`'s text and the card side's is the card's own, as
    it is now. `visit`: the two have nothing in common yet, so both are empty and the next pass fetches
    the issue whatever the search says. `adopted`: the issue came from an earlier call, so the card
    side is what Jira holds and an edit made since reads as one."""
    title, body = issue.get("summary") or "", issue.get("description") or ""
    mine = (title, body) if adopted else (t["title"], t["body"])
    if visit:
        title = body = ""
        mine = ("", "")
    conn.execute(
        "INSERT INTO jira_links(task_id, jira_key, base_title, base_body, card_title, card_body, "
        "base_jira_status, jira_updated, synced_status, synced_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            t["id"], key, norm(title), norm(body), norm(mine[0]), norm(mine[1]),
            (issue.get("status") or {}).get("category"), None if visit else issue.get("updated"),
            t["status"], t["version"], store.now(),
        ),
    )  # fmt: skip
    store.event(conn, t, "jira_linked", actor, {"key": key, **(extra or {})})


def add_card(conn: sqlite3.Connection, slug: str, issue: Issue, actor: str) -> str | None:
    """JIR-03: a new triage card for an issue. None when an active card already stands for it."""
    key = issue["key"]
    with store.tx(conn):
        if conn.execute("SELECT 1 FROM jira_links WHERE jira_key = ?", (key,)).fetchone():
            raise Refused(f"{key} is already linked to a card")
        got = store.create(
            conn, slug, issue["summary"], body=norm(issue.get("description")), triage=True,
            actor=actor, idempotency_key=f"jira:{key}",
        )  # fmt: skip
        if got["existing"]:
            return None
        add_link(conn, store.get_task(conn, got["id"]), key, issue, actor)
        pull_comments(conn, got["id"], issue)
    return str(got["id"])


def drop(conn: sqlite3.Connection, lk: Row, reason: str, actor: str = "jira") -> None:
    with store.tx(conn):
        t = store.get_task(conn, lk["task_id"])
        conn.execute("DELETE FROM jira_links WHERE task_id = ?", (lk["task_id"],))
        store.event(conn, t, "jira_unlinked", actor, {"key": lk["jira_key"], "reason": reason})
        if reason == "gone":
            note(
                conn,
                t["id"],
                f"{lk['jira_key']} no longer exists in Jira; the card is kept and unlinked",
            )


def export(conn: sqlite3.Connection, mcp: Mcp, b: Row) -> None:
    """JIR-03: an issue for each card without one. A card that was unlinked, or whose issue is gone,
    stays out; so do finished cards, which would only be history. Each issue carries a label of its
    card's id, so one made by a call whose answer never came is found and adopted, not made twice.
    A card that fails keeps its error as an event and the rest go on."""
    cards = conn.execute(
        "SELECT * FROM tasks t WHERE board = ? AND status NOT IN ('archived', 'done') "
        "AND NOT EXISTS (SELECT 1 FROM jira_links WHERE task_id = t.id) "
        "AND NOT EXISTS (SELECT 1 FROM task_events WHERE task_id = t.id AND kind = 'jira_unlinked') "
        "ORDER BY created_at",
        (b["slug"],),
    ).fetchall()
    for t in cards:
        label = f"kanban-{t['id']}"
        try:
            hit = search(mcp, f'labels = "{label}"')
            if hit:
                key = hit[0]
            else:
                made = mcp.call(
                    "jira_create_issue", project_key=b["jira_project"], summary=t["title"],
                    issue_type=store.cfg(conn, "jira.issue_type"), description=t["body"],
                    assignee=store.cfg(conn, "jira.assignee"),
                    additional_fields=json.dumps({"labels": [store.cfg(conn, "jira.label"), label]}),
                )  # fmt: skip
                key = made["issue"]["key"]
            issue = fetch(mcp, key)
        except LinkError as e:
            log.warning("%s: %s", t["id"], e)
            with store.tx(conn):
                last = conn.execute(
                    "SELECT json_extract(payload, '$.detail') FROM task_events "
                    "WHERE task_id = ? AND kind = 'jira_error' ORDER BY id DESC LIMIT 1",
                    (t["id"],),
                ).fetchone()
                if not last or last[0] != str(e):
                    store.event(conn, t, "jira_error", "jira", {"detail": str(e)})
            continue
        with store.tx(conn):
            add_link(conn, t, key, issue, "jira", adopted=bool(hit), extra={"created": True})


def sync_one(conn: sqlite3.Connection, mcp: Mcp, slug: str, key: str) -> None:
    link = conn.execute("SELECT * FROM jira_links WHERE jira_key = ?", (key,)).fetchone()
    try:
        issue = fetch(mcp, key)
    except Gone:
        if link:
            drop(conn, link, "gone")
        return
    if link:
        sync_link(conn, mcp, link, issue)
    elif ((issue.get("status") or {}).get("category") or "").lower() != "done" and not conn.execute(
        "SELECT 1 FROM task_events WHERE kind = 'jira_unlinked' AND json_extract(payload, '$.key') = ?",
        (key,),
    ).fetchone():  # an unlinked issue stays out until `kanban jira import` brings it back
        add_card(conn, slug, issue, "jira")


def link_error(conn: sqlite3.Connection, key: str, detail: str) -> None:
    with store.tx(conn):
        row = conn.execute(
            "SELECT l.task_id, l.error, t.board FROM jira_links l JOIN tasks t ON t.id = l.task_id "
            "WHERE l.jira_key = ?",
            (key,),
        ).fetchone()
        if row and row["error"] != detail:
            conn.execute(
                "UPDATE jira_links SET error = ? WHERE task_id = ?", (detail, row["task_id"])
            )
            store.event(
                conn,
                {"board": row["board"], "id": row["task_id"]},
                "jira_error",
                "jira",
                {"detail": detail},
            )


def sync_board(conn: sqlite3.Connection, mcp: Mcp, b: Row) -> None:
    """JIR-07: export, pull, push, apply, for one paired board. Export comes first: an issue made by
    an earlier, interrupted pass matches the board's query and would otherwise be imported as a
    second card."""
    slug, started = b["slug"], store.now()
    export(conn, mcp, b)
    since = (store.state_get(conn, f"jira:{slug}") or {}).get("last_pull_at")
    window = "" if since is None else f" AND updated >= -{(started - since) // 60 + 2}m"
    linked, wanted = [], []
    for lk in conn.execute(
        "SELECT l.* FROM jira_links l JOIN tasks t ON t.id = l.task_id WHERE t.board = ?", (slug,)
    ).fetchall():
        t = store.get_task(conn, lk["task_id"])
        linked.append(lk["jira_key"])
        if t["version"] > lk["synced_version"] or lk["jira_updated"] is None or outbox(conn, lk, t):
            wanted.append(lk["jira_key"])
    found = search(mcp, f"({board_jql(conn, b)}){window}")
    if since is None:
        found += linked
    for i in range(0, len(linked) * (since is not None), PAGE):
        found += search(mcp, f"key in ({', '.join(linked[i : i + PAGE])}){window}")
    clean = True
    for key in dict.fromkeys([*found, *wanted]):
        try:
            sync_one(conn, mcp, slug, key)
        except (LinkError, Refused, sqlite3.Error) as e:
            clean = False
            log.warning("%s: %s", key, e)
            link_error(conn, key, str(e))
    with store.tx(conn):
        st = store.state_get(conn, f"jira:{slug}") or {}
        st.pop("paused", None)
        if clean:
            st["last_pull_at"] = started
        store.state_put(conn, f"jira:{slug}", st)


def pause(conn: sqlite3.Connection, slug: str, detail: str) -> None:
    """JIR-08: the sync of this board stops being attempted until a pass succeeds; dispatch never notices."""
    with store.tx(conn):
        st = store.state_get(conn, f"jira:{slug}") or {}
        was = st.get("paused")
        st["paused"] = {"since": was["since"] if was else store.now(), "detail": detail}
        store.state_put(conn, f"jira:{slug}", st)
        if not was or was["detail"] != detail:
            store.event(conn, {"board": slug, "id": None}, "jira_error", "jira", {"detail": detail})


def paired(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute(
        "SELECT * FROM boards WHERE jira_project IS NOT NULL AND archived_at IS NULL ORDER BY slug"
    ).fetchall()


def sync_all(conn: sqlite3.Connection, only: str | None = None) -> list[str]:
    """One pass over every paired board (or `only`), a server process for each (JIR-07). The slugs
    that synced; a board whose server failed is paused instead, and the others go on."""
    boards = [b for b in paired(conn) if only in (None, b["slug"])]
    if only and not boards:
        raise Refused(
            f"board {only!r} is not paired with a Jira project; pair it with: kanban boards edit {only} --jira-project KEY"
        )
    command, synced = store.cfg(conn, "jira.mcp_command"), list[str]()
    if not boards:
        return synced
    with lock():
        for b in boards:
            try:
                if not command:
                    raise Paused(NO_COMMAND)
                with contextlib.closing(Mcp(command)) as mcp:
                    sync_board(conn, mcp, b)
                synced.append(b["slug"])
            except Paused as e:
                pause(conn, b["slug"], str(e))
    return synced


def poll(conn: sqlite3.Connection) -> None:
    """The housekeeping job: skipped while a CLI sync holds the lock.

    ponytail: the pass runs inside the housekeeping tick, so a slow server delays the other jobs by up
    to `JIRA_CALL_S` per call; give it a thread of its own if that shows up.
    """
    with contextlib.suppress(Busy):
        sync_all(conn)


# --- owner verbs ----------------------------------------------------------------------------


def _key(raw: str) -> str:
    if not KEY_RE.fullmatch(raw.upper()):
        raise Refused(f"{raw!r} is not an issue key like KAN-12")
    return raw.upper()


def _paired(conn: sqlite3.Connection, slug: str) -> Row:
    b = store.get_board(conn, slug)
    if not b["jira_project"]:
        raise Refused(
            f"board {slug!r} is not paired with a Jira project; pair it with: kanban boards edit {slug} --jira-project KEY"
        )
    return b


def read_issue(conn: sqlite3.Connection, raw: str) -> Issue:
    command = store.cfg(conn, "jira.mcp_command")
    if not command:
        raise Refused(NO_COMMAND)
    with lock(), contextlib.closing(Mcp(command)) as mcp:
        try:
            return fetch(mcp, _key(raw))
        except Gone as e:
            raise Refused(str(e)) from e


def import_issue(conn: sqlite3.Connection, slug: str, raw: str) -> dict[str, Any]:
    """JIR-03: link an issue outside the board's JQL by making a triage card for it."""
    _paired(conn, slug)
    issue = read_issue(conn, raw)
    tid = add_card(conn, slug, issue, store.OWNER)
    if tid is None:
        raise Refused(
            f"an active card already stands for {issue['key']}; link it with: kanban jira link ID {issue['key']}"
        )
    return {"id": tid, "key": issue["key"]}


def link_card(conn: sqlite3.Connection, task_id: str, raw: str) -> dict[str, Any]:
    """JIR-03: link an existing card. Nothing is common yet, so on the first merge Jira wins field by
    field and the card's text goes into a conflict comment; equal texts just agree."""
    _paired(conn, store.get_task(conn, task_id)["board"])
    issue = read_issue(conn, raw)
    with store.tx(conn):
        t = store.get_task(conn, task_id)
        if conn.execute(
            "SELECT 1 FROM jira_links WHERE task_id = ? OR jira_key = ?", (task_id, issue["key"])
        ).fetchone():
            raise Refused(
                f"card {task_id} or issue {issue['key']} is already linked; unlink it first"
            )
        add_link(conn, t, issue["key"], issue, store.OWNER, visit=True)
    return {"id": task_id, "key": issue["key"]}


def unlink_card(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    link = conn.execute("SELECT * FROM jira_links WHERE task_id = ?", (task_id,)).fetchone()
    if link is None:
        store.get_task(conn, task_id)
        raise Refused(f"card {task_id} is not linked to a Jira issue")
    drop(conn, link, "owner", store.OWNER)
    return {"id": task_id, "key": link["jira_key"]}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    """JIR-09: paired boards with their last pull and pause state, and the links in trouble."""
    boards = [
        {
            "board": b["slug"],
            "project": b["jira_project"],
            **(store.state_get(conn, f"jira:{b['slug']}") or {}),
        }
        for b in paired(conn)
    ]
    broken = conn.execute(
        "SELECT t.board, l.task_id, l.jira_key, l.conflict, l.error FROM jira_links l "
        "JOIN tasks t ON t.id = l.task_id WHERE l.conflict IS NOT NULL OR l.error IS NOT NULL ORDER BY l.task_id"
    ).fetchall()
    return {"boards": boards, "links": [dict(r) for r in broken]}
