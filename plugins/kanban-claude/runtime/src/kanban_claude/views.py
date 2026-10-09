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
RECENT_RUNS = 5
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
    who = f" (@{ev['assignee']})" if ev.get("assignee") else ""
    payload = f" {ev['payload']}" if ev.get("payload") else ""
    return (
        f"[{iso(ev['created_at'])}] #{ev['id']} {ev['task_id'] or '-'} {ev['kind']}{who}{payload}"
    )


def _json(value: str | None) -> Any:
    return json.loads(value) if value else None


def show_data(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """CLI-02: legacy field names kept (`task.*`, `latest_summary`, `parents`, `comments[].body`)."""
    t = store.get_task(conn, task_id)
    task = dict(t)
    task["skills"] = json.loads(task["skills"])

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
    if t["pr_url"]:
        lines.append(f"pr: #{t['pr_number']} {t['pr_url']}")
    if t["block_kind"]:
        lines.append(f"block: {t['block_kind']}")
    if t["merge_state"]:
        lines.append(f"merge: {t['merge_state']}")
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
    out = [
        f"# Kanban task {t['id']}: {t['title']}",
        f"- assignee: {t['assignee'] or '-'}",
        f"- status: {t['status']}",
        f"- workspace: {t['workspace_kind']}:{t['workspace_path'] or '-'}",
        f"- max runtime: {runtime}",
        f"- branch: {t['branch_name'] or '-'}",
        f"- base: {t['base_branch'] or '-'}",
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

    if t["assignee"]:
        recent = conn.execute(
            "SELECT t.id, t.title, r.outcome, r.summary, r.ended_at FROM task_runs r "
            "JOIN tasks t ON t.id = r.task_id WHERE r.role = ? AND t.board = ? AND t.id <> ? "
            "AND r.outcome IN ('completed', 'review_requested') ORDER BY r.ended_at DESC, r.id DESC LIMIT ?",
            (t["assignee"], t["board"], task_id, RECENT_RUNS),
        ).fetchall()
        if recent:
            out += ["", f"## Recent work by @{t['assignee']}"]
            for r in recent:
                first = (r["summary"] or "").strip().splitlines()[:1]
                out.append(
                    f"- {r['id']} {r['title']}: {r['outcome']} {cap(' '.join(first), 200)}".rstrip()
                )

    comments = conn.execute(
        "SELECT * FROM (SELECT * FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
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
        "SELECT id, title, status, assignee, priority, branch_name, pr_number, created_at, "
        f"updated_at, completed_at FROM tasks WHERE {' AND '.join(where)} ORDER BY {SORTS[sort]}"
    )
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]


def runs_list(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    store.get_task(conn, task_id)
    cols = (
        "id, n, kind, role, status, outcome, started_at, ended_at, model, effort, cost_usd, error"
    )
    q = f"SELECT {cols} FROM task_runs WHERE task_id = ? ORDER BY n"
    return [dict(r) for r in conn.execute(q, (task_id,))]
