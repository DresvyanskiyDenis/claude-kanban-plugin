"""Read-only renderings: `show`, `context`, `watch` lines, and inert output (CLI-02/06/09/11)."""

import json
import os
import re
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import store
from .errors import Refused
from .paths import dirs

_OSC = re.compile(r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?")
_CSI = re.compile(r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

BODY_CAP = 8 * 1024
RUN_FIELD_CAP = 4 * 1024
COMMENT_CAP = 2 * 1024
PRIOR_RUNS = 10
COMMENTS = 30
SHOW_EVENTS = 200
LOG_BYTES = 1024 * 1024


def clean(text: str) -> str:
    """CLI-11: drop C0/C1 controls and ANSI CSI/OSC sequences, keeping newline and tab."""
    return _CTRL.sub("", _CSI.sub("", _OSC.sub("", text)))


def clean_obj(value: Any) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {clean(str(k)): clean_obj(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [clean_obj(v) for v in value]
    return value


def log_path(raw: str, home: Path, subs: Sequence[str]) -> Path:
    """SEC: a run's `log_path` is data, read only when it resolves under `home/<sub>`."""
    path = Path(os.path.realpath(raw))
    if not any(home.resolve() / sub in path.parents for sub in subs):
        raise Refused(f"that log is outside {' and '.join(subs)} under the kanban home")
    return path


def log_lines(path: Path) -> list[str]:
    """The last `LOG_BYTES` of a log as lines; the caller words an `OSError`."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - LOG_BYTES))
        return f.read().decode(errors="replace").splitlines()


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def local(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cap(text: str | None, limit: int) -> str:
    raw = (text or "").encode()
    if len(raw) <= limit:
        return text or ""
    return raw[:limit].decode(errors="ignore") + f"\n[… truncated {len(raw) - limit} bytes]"


def watch_line(ev: dict[str, Any]) -> str:
    who = f" ({ev['actor']})" if ev.get("actor") else ""
    payload = f" {ev['payload']}" if ev.get("payload") else ""
    return (
        f"[{iso(ev['created_at'])}] #{ev['id']} {ev['task_id'] or '-'} {ev['kind']}{who}{payload}"
    )


def _json(value: str | None) -> Any:
    return json.loads(value) if value else None


# Why a card waits (`todo` only): its parents that do not satisfy `store._all_done`.
_WAITING_SQL = """(CASE WHEN t.status = 'todo' THEN (
  SELECT json_group_array(json_object('id', id, 'status', status, 'block_kind', block_kind,
                                      'merge_state', merge_state))
  FROM (SELECT p.id, p.status, p.block_kind, p.merge_state FROM task_links l
        JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = t.id
        AND NOT (p.status IN ('done', 'archived') AND (p.merge_state IS NULL OR p.merge_state = 'merged'))
        ORDER BY p.id)) END) AS waiting_for"""
# A done card whose approved PR no one has merged: the last word on it was `awaiting_owner_merge`.
_AWAITING_WHERE = """t.status = 'done' AND (
  (t.merge_state IS NULL AND t.merged_at IS NULL) OR (t.owner_merge = 1 AND t.merge_state = 'pending')) AND (
  SELECT e.kind FROM task_events e WHERE e.task_id = t.id
  AND e.kind IN ('awaiting_owner_merge', 'completed', 'completed_by_owner', 'reopened')
  ORDER BY e.id DESC LIMIT 1) = 'awaiting_owner_merge'"""
_AWAITING_SQL = f"({_AWAITING_WHERE}) AS awaiting_merge"
# Why the dispatcher last passed over a card that still waits for a run: its latest `skipped` event,
# unless a claim came after it. The time shows how old the reason is: a card that waits only for a
# free slot records nothing, so the reason may have lapsed.
_SKIP_SQL = """(CASE WHEN t.status IN ('ready', 'review') THEN (
  SELECT json_object('reason', reason, 'at', at) FROM (
    SELECT e.kind, json_extract(e.payload, '$.reason') AS reason, e.created_at AS at
    FROM task_events e WHERE e.task_id = t.id AND e.kind IN ('skipped', 'claimed')
    ORDER BY e.id DESC LIMIT 1) WHERE kind = 'skipped') END) AS skip"""


def awaiting_owner_merge(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The cards `_AWAITING_SQL` marks, that have a PR and a repository to ask about it."""
    return conn.execute(
        f"SELECT t.* FROM tasks t WHERE {_AWAITING_WHERE} "
        "AND t.pr_number IS NOT NULL AND t.workspace_path IS NOT NULL"
    ).fetchall()


def _why(p: dict[str, Any]) -> str:
    if p["status"] == "blocked" and p["block_kind"]:
        return f"blocked: {p['block_kind']}"
    if p["status"] == "done" and p["merge_state"]:
        return f"done, merge {p['merge_state']}"
    return str(p["status"])


def _flags(c: dict[str, Any]) -> dict[str, Any]:
    return {
        **c,
        "waiting_for": json.loads(c["waiting_for"] or "[]"),
        "awaiting_merge": bool(c["awaiting_merge"]),
        "skip": json.loads(c["skip"]) if c["skip"] else None,
    }


def notes(c: dict[str, Any]) -> list[str]:
    """The `list`/`show` words for a card's wait: what it waits for, or that its PR awaits a merge."""
    out = []
    if c["waiting_for"]:
        parents = ", ".join(f"{p['id']} ({_why(p)})" for p in c["waiting_for"])
        out.append(f"waits for {parents}")
    if c["awaiting_merge"]:
        out.append("awaiting merge")
    if c.get("skip"):
        out.append(f"not started: {c['skip']['reason']} (since {local(c['skip']['at'])})")
    return out


def show_data(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """CLI-02: legacy field names kept (`task.*`, `latest_summary`, `parents`, `comments[].body`)."""
    t = store.get_task(conn, task_id)
    task = dict(t)
    task["skills"] = json.loads(task["skills"])
    flags = conn.execute(
        f"SELECT {_WAITING_SQL}, {_AWAITING_SQL}, {_SKIP_SQL} FROM tasks t WHERE t.id = ?",
        (task_id,),
    )
    task.update(_flags(dict(flags.fetchone())))

    def ids(sql: str) -> list[str]:
        return [r[0] for r in conn.execute(sql, (task_id,))]

    events = []
    q = "SELECT * FROM (SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?)"
    for r in conn.execute(q + " ORDER BY id", (task_id, SHOW_EVENTS)):
        e = dict(r)
        e["payload"] = _json(e["payload"])
        events.append(e)
    runs = [
        dict(r)
        for r in conn.execute("SELECT * FROM task_runs WHERE task_id = ? ORDER BY n", (task_id,))
    ]
    latest = next((r["summary"] for r in reversed(runs) if r["summary"]), None)
    grants = [
        {**dict(g), "rules": json.loads(g["rules"])}
        for g in conn.execute("SELECT * FROM grants WHERE task_id = ? ORDER BY id", (task_id,))
    ]
    return {
        "schema": 1,
        "task": task,
        "latest_summary": latest,
        "parents": ids("SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id"),
        "children": ids("SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id"),
        "comments": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
        "runs": runs,
        "events": events,
        "attachments": [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
        "grants": grants,
    }


def show_text(d: dict[str, Any]) -> str:
    t = d["task"]
    lines = [
        f"{t['id']}  [{t['status']}]  @{t['assignee'] or '-'}  priority {t['priority']}  board {t['board']}",
        t["title"],
        f"workspace: {t['workspace_kind']}:{t['workspace_path'] or '-'}  branch: {t['branch_name'] or '-'}  base: {t['base_branch'] or '-'}",
    ]
    if t["touches"]:
        lines.append(f"touches: {', '.join(json.loads(t['touches']))}")
    if t["pr_url"]:
        lines.append(f"pr: #{t['pr_number']} {t['pr_url']}")
    if t["block_kind"]:
        lines.append(f"block: {t['block_kind']}")
    if t["merge_state"]:
        lines.append(f"merge: {t['merge_state']}{' (owner merges)' if t['owner_merge'] else ''}")
    if t["merge_method"] not in (None, "squash"):
        lines.append(f"merge method: {t['merge_method']}")
    lines += notes(t)
    lines.append(
        f"parents: {', '.join(d['parents']) or '-'}  children: {', '.join(d['children']) or '-'}"
    )
    lines += ["", t["body"] or "(no body)"]
    if d["comments"]:
        lines += ["", "-- comments --"]
        lines += [f"[{local(c['created_at'])}] {c['author']}: {c['body']}" for c in d["comments"]]
    if d["runs"]:
        lines += ["", "-- runs --"]
        lines += [
            f"#{r['n']} {r['kind']} {r['role']} {r['status']} {r['outcome'] or ''}".rstrip()
            for r in d["runs"]
        ]
    return "\n".join(lines)


def _frame(what: str, author: str | None, ts: int | None, text: str, limit: int) -> str:
    """Worker-authored text reads as data, never as an instruction (CLI-09)."""
    when = f" at {local(ts)}" if ts else ""
    return f"{what} from worker `{author or 'unknown'}`{when}:\n{cap(text, limit)}"


def context_md(conn: sqlite3.Connection, task_id: str) -> str:
    t = store.get_task(conn, task_id)
    runtime = f"{t['max_runtime_seconds']} s" if t["max_runtime_seconds"] else "-"
    pr = f"#{t['pr_number']} {t['pr_url']}" if t["pr_number"] else "-"
    worktree = (
        f"{t['workspace_path']}/.worktrees/{task_id}" if t["workspace_kind"] == "worktree" else "-"
    )
    out = [
        f"# Kanban task {t['id']}: {t['title']}",
        f"- assignee: {t['assignee'] or '-'}",
        f"- status: {t['status']}",
        f"- workspace: {t['workspace_kind']}:{t['workspace_path'] or '-'}",
        f"- max runtime: {runtime}",
        f"- branch: {t['branch_name'] or '-'}",
        f"- base: {t['base_branch'] or '-'}",
        f"- pr: {pr}",
        f"- worktree: {worktree}",
        "",
        "## Body",
        cap(t["body"], BODY_CAP) or "(empty)",
    ]
    att = conn.execute(
        "SELECT stored_path FROM task_attachments WHERE task_id = ? ORDER BY id", (task_id,)
    )
    paths = [dirs().home / "attachments" / task_id / r[0].rsplit("/", 1)[-1] for r in att]
    if paths:
        out += ["", "## Attachments", *(f"- {p}" for p in paths)]

    prior = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND status = 'ended' ORDER BY n DESC LIMIT ?",
        (task_id, PRIOR_RUNS),
    ).fetchall()
    if prior:
        out += ["", "## Prior attempts on this task"]
        for r in prior:
            out.append(f"### Run {r['n']} ({r['kind']}, {r['role']}): {r['outcome']}")
            if r["summary"]:
                out.append(_frame("summary", r["role"], r["ended_at"], r["summary"], RUN_FIELD_CAP))
            if r["error"]:
                out.append(_frame("error", r["role"], r["ended_at"], r["error"], RUN_FIELD_CAP))

    parents = conn.execute(
        "SELECT p.* FROM task_links l JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? "
        "ORDER BY p.id",
        (task_id,),
    ).fetchall()
    if parents:
        out += ["", "## Parent task results"]
        for p in parents:
            out.append(f"### {p['id']}: {p['title']} ({p['status']})")
            s = conn.execute(
                "SELECT summary, role, ended_at FROM task_runs WHERE task_id = ? "
                "AND summary IS NOT NULL AND summary <> '' ORDER BY n DESC LIMIT 1",
                (p["id"],),
            ).fetchone()
            if s:
                out.append(_frame("summary", s["role"], s["ended_at"], s["summary"], RUN_FIELD_CAP))
            if p["pr_url"]:
                out.append(f"PR: {p['pr_url']}")

    comments = conn.execute(
        "SELECT * FROM (SELECT * FROM task_comments WHERE task_id = ? AND body NOT LIKE '[run]%' "
        "ORDER BY id DESC LIMIT ?) ORDER BY id",
        (task_id, COMMENTS),
    ).fetchall()
    if comments:
        out += ["", "## Comment thread"]
        out += [
            _frame("comment", c["author"], c["created_at"], c["body"], COMMENT_CAP)
            for c in comments
        ]

    grants = store.grants_list(conn, task_id)
    active = [g for g in grants if g["revoked_at"] is None]
    if active:
        out += ["", "## Grants"]
        out += [
            "The board owner approved exactly these actions for this card "
            f"({g['approved']}): {', '.join(g['rules'])}. Anything beyond them is still a stop signal."
            for g in active
        ]
    return "\n".join(out) + "\n"


SORTS = {
    "created": "created_at DESC, id",
    "priority": "priority DESC, created_at, id",
    "completed-desc": "completed_at IS NULL, completed_at DESC, id",
}


def cards_list(
    conn: sqlite3.Connection,
    board: str,
    *,
    status: str | None = None,
    assignee: str | None = None,
    sort: str = "created",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    where: list[str] = ["board = ?"]
    args: list[Any] = [board]
    if status:
        where.append("status = ?")
        args.append(status)
    if assignee:
        where.append("assignee = ?")
        args.append(assignee)
    sql = (
        "SELECT t.id, t.title, t.status, t.assignee, t.priority, t.branch_name, t.pr_number, "
        f"t.created_at, t.updated_at, t.completed_at, {_WAITING_SQL}, {_AWAITING_SQL}, {_SKIP_SQL} "
        f"FROM tasks t WHERE {' AND '.join(where)} ORDER BY {SORTS[sort]}"
    )
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [_flags(dict(r)) for r in conn.execute(sql, args)]


def runs_list(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    store.get_task(conn, task_id)
    cols = (
        "id, n, kind, role, status, outcome, started_at, ended_at, model, effort, cost_usd, "
        "input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, model_usage, error"
    )
    q = f"SELECT {cols} FROM task_runs WHERE task_id = ? ORDER BY n"
    return [
        {**r, "model_usage": json.loads(r["model_usage"]) if r["model_usage"] else None}
        for r in map(dict, conn.execute(q, (task_id,)))
    ]


def account_states(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The logins that hold dispatch right now: a login pause or a quota wall that has not lapsed."""
    ts, out = store.now(), []
    for row in conn.execute("SELECT key FROM state WHERE key LIKE 'account:%' ORDER BY key"):
        config_dir = row["key"].removeprefix("account:")
        acct = store.account_get(conn, config_dir)
        until = max(acct.get("hard_until") or 0, acct.get("soft_until") or 0)
        wall = until if until > ts else None
        if acct.get("auth_paused") or wall:
            out.append(
                {
                    "config_dir": config_dir,
                    "auth_paused": acct.get("auth_paused"),
                    "wall_until": wall,
                }
            )
    return out


# The events that ask the owner to act. The lead unifies this with `watch --actionable`'s kinds at integration.
NEEDS_OWNER = (
    "blocked", "block_loop_detected", "gave_up", "merge_failed", "account_paused", "quota_wall",
    "awaiting_owner_merge",
)  # fmt: skip


def _still_open(conn: sqlite3.Connection, ev: dict[str, Any], payload: dict[str, Any]) -> bool:
    """A `NEEDS_OWNER` event counts only while the card or login it names is still in that state."""
    kind = ev["kind"]
    if kind == "quota_wall":
        return (payload.get("until") or 0) > store.now()
    if kind == "account_paused":
        return bool(store.account_get(conn, payload.get("config_dir", "")).get("auth_paused"))
    t = conn.execute(
        "SELECT status, merge_state FROM tasks WHERE id = ?", (ev["task_id"],)
    ).fetchone()
    if t is None:
        return False
    if kind == "merge_failed":
        return bool(t["merge_state"] == "failed")
    if kind == "awaiting_owner_merge":
        found = conn.execute(
            f"SELECT 1 FROM tasks t WHERE t.id = ? AND {_AWAITING_WHERE}", (ev["task_id"],)
        )
        return found.fetchone() is not None
    return bool(t["status"] in ("blocked", "triage"))


def digest(conn: sqlite3.Connection, board: str | None, window: int) -> dict[str, Any]:
    """What happened in the last `window` seconds and what needs the owner. Read-only."""
    ts = store.now()
    first = conn.execute(
        "SELECT MIN(id) FROM task_events WHERE created_at >= ?", (ts - window,)
    ).fetchone()[0]
    events = (
        store.events_since(conn, first - 1, kinds=(*NEEDS_OWNER, "merged"), board=board, limit=-1)
        if first
        else []
    )

    def item(ev: dict[str, Any]) -> dict[str, Any]:
        row = conn.execute("SELECT title FROM tasks WHERE id = ?", (ev["task_id"],)).fetchone()
        return {
            "task_id": ev["task_id"], "kind": ev["kind"], "at": ev["created_at"],
            "title": row["title"] if row else None, "payload": _json(ev["payload"]) or {},
        }  # fmt: skip

    open_items: dict[tuple[Any, ...], dict[str, Any]] = {}
    merged = []
    for ev in events:
        i = item(ev)
        if ev["kind"] == "merged":
            merged.append(i)
        elif _still_open(conn, ev, i["payload"]):
            open_items[(ev["kind"], ev["task_id"], i["payload"].get("config_dir"))] = i
    by_status: dict[str, list[dict[str, Any]]] = {"review": [], "running": []}
    for t in conn.execute(
        "SELECT id, title, status, assignee FROM tasks WHERE status IN ('review', 'running')"
        + (" AND board = ?" if board else "")
        + " ORDER BY id",
        [board] if board else [],
    ):
        by_status[t["status"]].append(
            {"task_id": t["id"], "title": t["title"], "assignee": t["assignee"]}
        )
    runs, cost = conn.execute(
        "SELECT COUNT(*), ROUND(COALESCE(SUM(r.cost_usd), 0), 4) FROM task_runs r "
        "JOIN tasks t ON t.id = r.task_id WHERE r.started_at >= ?"
        + (" AND t.board = ?" if board else ""),
        [ts - window, *([board] if board else [])],
    ).fetchone()
    return {
        "since": ts - window, "needs_you": list(open_items.values()), "merged": merged,
        "in_review": by_status["review"], "running": by_status["running"], "cost_usd": cost,
        "runs": runs,
    }  # fmt: skip


def digest_text(d: dict[str, Any]) -> str:
    def line(i: dict[str, Any]) -> str:
        if i.get("kind") == "merged":
            p = i["payload"]
            tail = f"  (PR #{p.get('pr')} into {p.get('base')})"
        else:
            tail = f"  {json.dumps(i['payload'])}" if i.get("payload") else ""
        who = f"  @{i['assignee']}" if i.get("assignee") else ""
        return f"- {i['task_id'] or '-'} {i.get('kind', '')}  {i['title'] or ''}{who}{tail}"

    out = [f"Needs you: {len(d['needs_you'])} (since {local(d['since'])})"]
    out += [line(i) for i in d["needs_you"]]
    for label, key in (("Merged", "merged"), ("In review", "in_review"), ("Running", "running")):
        if d[key]:
            out += [f"{label}: {len(d[key])}", *(line(i) for i in d[key])]
    out.append(f"Cost: ${d['cost_usd']:.2f} over {d['runs']} runs")
    return "\n".join(out)


def followups(conn: sqlite3.Connection, board: str | None, window: int) -> list[dict[str, Any]]:
    """Comments that start with `FOLLOW-UP:`, for the owner to turn into `create --triage` cards."""
    rows = conn.execute(
        "SELECT c.task_id, c.author, c.created_at, c.body FROM task_comments c "
        "JOIN tasks t ON t.id = c.task_id WHERE c.created_at >= ? AND substr(ltrim(c.body), 1, 10) = 'FOLLOW-UP:'"
        + (" AND t.board = ?" if board else "")
        + " ORDER BY c.id",
        [store.now() - window, *([board] if board else [])],
    )
    return [
        {
            "task_id": r["task_id"],
            "author": r["author"],
            "at": r["created_at"],
            "text": r["body"].lstrip().removeprefix("FOLLOW-UP:").strip(),
        }  # fmt: skip
        for r in rows
    ]


def followups_text(rows: list[dict[str, Any]]) -> str:
    lines = []
    for r in rows:
        text = "\n    ".join(r["text"].splitlines())
        lines.append(f"{r['task_id']}  {r['author']}  {local(r['at'])}  {text}")
    return "\n".join(lines) or "(no follow-ups)"
