"""`kanban web`: a read-only dashboard on loopback, started by the owner and stopped with Ctrl-C.

Nothing here writes. It opens the database `mode=ro` on every request and never migrates it, so
it reads a board that an older daemon still owns, and it reads schemas newer than this code knows
(`SELECT *` into dicts, new columns optional). Log and stream paths come only from a run's row,
looked up by run id, and must resolve under the kanban home (`views.log_path`).
"""

import argparse
import contextlib
import json
import re
import sqlite3
import sys
import time
import webbrowser
from collections.abc import Iterator
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import __version__, daemon, store, views
from .errors import Error, NotFound, Refused, Usage
from .paths import dirs

DEFAULT_PORT = 8742
PAGE_BYTES = 384 * 1024  # a stream page stops after the line that crosses this
LOG_PAGE = 128 * 1024
MAX_LINE = 16 * 1024 * 1024  # a longer line is one `raw` event and is skipped, not parsed
TEXT_CAP, THINK_CAP, RESULT_CAP, INPUT_CAP, HOOK_CAP, EXPAND_CAP = (
    12_000,
    6_000,
    4_000,
    2_400,
    1_500,
    2_000_000,
)
DONE_SHOWN = 40
ACTIVITY_TAIL = 96 * 1024
LANES = ("triage", "todo", "ready", "running", "review", "blocked", "done")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
}
ASSET = re.compile(r"^/assets/([a-z0-9_-]+\.(?:js|css|svg))$")
JSON_RUN_COLS = ("metadata", "model_usage", "session_model_usage", "params", "harness")
# Stream events that carry no reading value; their bytes still count toward offsets.
NOISE_SYSTEM = {
    "ui_status", "ui_panes", "ui_log", "ui_toast", "thinking_tokens", "session_title_changed",
    "commands_changed", "permission_check_status", "background_tasks_changed", "task_updated",
    "task_progress", "hook_started", "status", "command_lifecycle",
}  # fmt: skip


# --- database -------------------------------------------------------------------------------


@contextlib.contextmanager
def reader(home: Path) -> Iterator[sqlite3.Connection]:
    """A fresh read-only connection; the file is never created and its schema never touched."""
    db = home / "kanban.db"
    if not db.is_file():
        raise Error(f"no board database at {db}; is KANBAN_HOME right?")
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    try:
        yield conn
    finally:
        conn.close()


def rows(conn: sqlite3.Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, args)]


def has_table(conn: sqlite3.Connection, name: str) -> bool:
    q = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
    return conn.execute(q, (name,)).fetchone() is not None


def parse_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def one_line(text: Any, limit: int = 160) -> str:
    first = next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")
    return first if len(first) <= limit else first[: limit - 1] + "…"


# --- stream files ---------------------------------------------------------------------------


def run_row(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFound(f"no run {run_id}")
    return dict(row)


def run_file(row: dict[str, Any], key: str, subs: tuple[str, ...]) -> Path | None:
    """The file a run row names, if it exists under the kanban home; a path outside is refused."""
    raw = row.get(key)
    if not raw:
        return None
    path = views.log_path(str(raw), dirs().home, subs)
    return path if path.is_file() else None


def prompt_file(row: dict[str, Any]) -> Path | None:
    prompt_path = (parse_json(row.get("params")) or {}).get("prompt_path")
    if not isinstance(prompt_path, str) or not prompt_path:
        return None
    try:
        return run_file({"p": prompt_path}, "p", ("runs", "streams", "prompts"))
    except Refused:
        return None


def clip(text: Any, cap: int | None) -> tuple[str, int | None]:
    """`text` cut to `cap` characters, and its full length when it was cut."""
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return (s, None) if cap is None or len(s) <= cap else (s[:cap], len(s))


def tool_label(name: str, inp: Any) -> str:
    """The one thing worth reading about a tool call: the command, path, skill or query."""
    i = inp if isinstance(inp, dict) else {}
    pick = {
        "Bash": "command", "Read": "file_path", "Edit": "file_path", "Write": "file_path",
        "MultiEdit": "file_path", "NotebookEdit": "notebook_path", "Skill": "skill",
        "Grep": "pattern", "Glob": "pattern", "WebFetch": "url", "WebSearch": "query",
        "LSP": "operation", "ToolSearch": "query",
    }.get(name)  # fmt: skip
    if name in ("Agent", "Task"):
        return " · ".join(str(i[k]) for k in ("subagent_type", "description") if i.get(k))
    if name == "Read" and i.get("offset"):
        return f"{i.get('file_path', '')} @{i['offset']}"
    if pick and i.get(pick) is not None:
        return str(i[pick])
    for k in ("description", "path", "file_path", "query", "url", "name", "id", "title"):
        if i.get(k):
            return str(i[k])
    return ""


def result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")  # never ship a base64 payload
            else:
                parts.append(json.dumps(b, default=str)[:300])
        return "\n".join(parts)
    return "" if content is None else str(content)


def convert(obj: Any, at: int, caps: bool = True) -> list[dict[str, Any]]:
    """One stream line as zero or more readable events. `caps=False` lifts the per-field cuts."""

    def c(n: int) -> int | None:
        return n if caps else EXPAND_CAP

    if not isinstance(obj, dict):
        text, t = clip(obj, c(TEXT_CAP))
        return [{"k": "raw", "at": at, "text": text, "trunc": t}]
    typ, sub = obj.get("type"), obj.get("subtype")
    parent = obj.get("parent_tool_use_id")
    base: dict[str, Any] = {"at": at, **({"sub": parent} if parent else {})}
    out: list[dict[str, Any]] = []
    if typ in ("assistant", "user"):
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content if isinstance(content, list) else []:
            bt = b.get("type") if isinstance(b, dict) else None
            if bt == "text":
                text, t = clip(b.get("text"), c(TEXT_CAP))
                out.append(
                    {
                        **base,
                        "k": "text" if typ == "assistant" else "user",
                        "text": text,
                        "trunc": t,
                    }
                )
            elif bt == "thinking":
                text, t = clip(b.get("thinking"), c(THINK_CAP))
                out.append({**base, "k": "thinking", "text": text, "trunc": t, "empty": not text})
            elif bt in ("tool_use", "server_tool_use"):
                name = str(b.get("name") or "tool")
                inp, t = clip(b.get("input"), c(INPUT_CAP))
                out.append({**base, "k": "tool", "id": b.get("id"), "name": name,
                            "label": one_line(tool_label(name, b.get("input")), 400),
                            "input": inp, "trunc": t})  # fmt: skip
            elif bt == "tool_result":
                raw = result_text(b.get("content"))
                text, t = clip(raw, c(RESULT_CAP))
                out.append({**base, "k": "result", "id": b.get("tool_use_id"), "ok": not b.get("is_error"),
                            "text": text, "trunc": t, "size": len(raw)})  # fmt: skip
            elif bt == "advisor_tool_result":
                text, t = clip(result_text(b.get("content")), c(RESULT_CAP))
                out.append(
                    {**base, "k": "advisor", "id": b.get("tool_use_id"), "text": text, "trunc": t}
                )
            elif bt:
                text, t = clip(b, c(RESULT_CAP))
                out.append({**base, "k": "raw", "text": text, "trunc": t})
    elif typ == "system":
        if sub == "init":
            keep = ("model", "cwd", "permissionMode", "claude_code_version", "tools", "skills",
                    "plugins", "agents", "mcp_servers", "apiKeySource")  # fmt: skip
            out.append({**base, "k": "init", **{k: obj.get(k) for k in keep}})
        elif sub == "hook_response":
            text, _ = clip(obj.get("output") or obj.get("stderr") or "", c(HOOK_CAP))
            out.append({**base, "k": "hook", "name": obj.get("hook_name"), "event": obj.get("hook_event"),
                        "outcome": obj.get("outcome"), "exit": obj.get("exit_code"), "text": text})  # fmt: skip
        elif sub == "compact_boundary":
            meta = obj.get("compact_metadata") or {}
            out.append(
                {
                    **base,
                    "k": "compact",
                    "pre": meta.get("pre_tokens"),
                    "trigger": meta.get("trigger"),
                }
            )
        elif sub in ("api_retry", "permission_denied", "informational"):
            text, _ = clip(
                {k: v for k, v in obj.items() if k not in ("uuid", "session_id", "type")}, 600
            )
            out.append({**base, "k": "error" if sub != "informational" else "note",
                        "title": sub.replace("_", " "), "text": text})  # fmt: skip
        elif sub == "code_change_published":
            out.append({**base, "k": "note", "title": "pull request", "text": str(obj.get("url") or ""),
                        "url": obj.get("url")})  # fmt: skip
        elif sub == "vcs_state_changed":
            out.append(
                {
                    **base,
                    "k": "note",
                    "title": str(obj.get("kind") or "vcs"),
                    "text": str(obj.get("branch") or ""),
                }
            )
        elif sub in ("task_started", "task_notification"):
            out.append({**base, "k": "note", "title": f"background {sub.removeprefix('task_')}",
                        "text": one_line(obj.get("description") or obj.get("summary"), 300)})  # fmt: skip
        elif sub not in NOISE_SYSTEM:
            text, _ = clip({k: v for k, v in obj.items() if k not in ("uuid", "session_id")}, 400)
            out.append({**base, "k": "note", "title": str(sub), "text": text})
    elif typ == "rate_limit_event":
        info = obj.get("rate_limit_info") or {}
        windows = info.get("unifiedWindows") or {}
        out.append({**base, "k": "rate", "status": info.get("status"), "type": info.get("rateLimitType"),
                    "resets": info.get("resetsAt"),
                    "util": {k: (v or {}).get("utilization") for k, v in windows.items()}})  # fmt: skip
    elif typ == "result":
        text, t = clip(obj.get("result") or "", c(TEXT_CAP))
        out.append({**base, "k": "final", "subtype": sub, "error": bool(obj.get("is_error")),
                    "cost": obj.get("total_cost_usd"), "turns": obj.get("num_turns"),
                    "duration_ms": obj.get("duration_ms"), "api_ms": obj.get("duration_api_ms"),
                    "usage": obj.get("usage"), "models": obj.get("modelUsage"),
                    "stop": obj.get("stop_reason"), "terminal": obj.get("terminal_reason"),
                    "denials": obj.get("permission_denials"), "text": text, "trunc": t})  # fmt: skip
    elif typ not in ("tool_progress", "stream_event", "command_lifecycle"):
        text, t = clip(obj, 400)
        out.append({**base, "k": "note", "title": str(typ), "text": text})
    for i, e in enumerate(out):
        e["i"] = i
    return out


def read_lines(
    path: Path, start: int, stop: int | None, budget: int | None
) -> tuple[list[tuple[int, bytes]], int]:
    """Whole `\\n`-ended lines from `start` while under `budget` bytes (and before `stop`); at least
    one if one is complete, so a line longer than the budget still moves a reader forward.
    Returns them with their offsets and the offset after the last one."""
    out: list[tuple[int, bytes]] = []
    pos, used = start, 0
    with path.open("rb") as f:
        f.seek(start)
        while (stop is None or pos < stop) and (budget is None or used < budget or not out):
            line = f.readline(MAX_LINE)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) < MAX_LINE:
                    break  # the writer is mid-line: wait for the rest
                at, pos = pos, pos + len(line)
                while not line.endswith(b"\n") and (line := f.readline(MAX_LINE)):
                    pos += len(line)
                out.append((at, b'"[line over 16 MiB skipped]"\n'))
                used += MAX_LINE
                continue
            out.append((pos, line))
            pos += len(line)
            used += len(line)
    return out, pos


def align(path: Path, offset: int) -> int:
    """`offset` moved forward to the start of the next line (itself when a line starts there)."""
    if offset <= 0:
        return 0
    with path.open("rb") as f:
        f.seek(offset - 1)
        if f.read(1) == b"\n":
            return offset
        f.readline(MAX_LINE)
        return f.tell()


def decode(line: bytes, at: int, caps: bool = True) -> list[dict[str, Any]]:
    try:
        return convert(json.loads(line), at, caps)
    except ValueError:
        text, t = clip(line.decode(errors="replace").rstrip("\n"), TEXT_CAP)
        return [{"k": "raw", "at": at, "i": 0, "text": text, "trunc": t}]


def stream_page(path: Path, offset: str | None, before: int | None) -> dict[str, Any]:
    """A page of events: `before` pages back from that byte, `offset` forward (`tail`: the end)."""
    size = path.stat().st_size
    if before is not None:
        end = min(before, size)
        start = align(path, max(0, end - PAGE_BYTES))
        got, nxt = read_lines(path, start, end, None)
    elif offset in (None, "", "tail"):
        start = align(path, max(0, size - PAGE_BYTES))
        got, nxt = read_lines(path, start, None, None)
    else:
        start = align(path, min(int(offset), size))
        got, nxt = read_lines(path, start, None, PAGE_BYTES)
    first = got[0][0] if got else start
    events = [e for at, line in got for e in decode(line, at)]
    return {"events": events, "start": first, "end": nxt, "size": size, "at_start": first == 0,
            "at_end": nxt >= size, "lines": len(got)}  # fmt: skip


def log_page(path: Path, offset: str | None, before: int | None) -> dict[str, Any]:
    size = path.stat().st_size
    if before is not None:
        end = min(before, size)
        start = align(path, max(0, end - LOG_PAGE))
    elif offset in (None, "", "tail"):
        end, start = size, align(path, max(0, size - LOG_PAGE))
    else:
        start = align(path, min(int(offset), size))
        end = min(size, start + LOG_PAGE)
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(max(0, end - start))
    if before is None and data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n") + 1  # whole lines only, so the next page starts clean
        if cut or end >= size:  # shortcut: a line over a page is cut mid-line, its tail is skipped
            data, end = data[:cut], start + cut
    return {"text": data.decode(errors="replace"), "start": start, "end": end, "size": size,
            "at_start": start == 0, "at_end": end >= size}  # fmt: skip


_activity: dict[tuple[str, int], str] = {}


def activity_line(path: Path | None) -> str:
    """What a running card did last: the tail of its stream, as one line."""
    if path is None or not path.is_file():
        return ""
    try:
        size = path.stat().st_size
        key = (str(path), size)
        if key in _activity:
            return _activity[key]
        got, _ = read_lines(path, align(path, max(0, size - ACTIVITY_TAIL)), None, None)
    except OSError:
        return ""
    line = ""
    for at, raw in reversed(got):
        for e in reversed(decode(raw, at)):
            if e["k"] == "text" and e["text"].strip() and not e.get("sub"):
                line = one_line(e["text"])
            elif e["k"] == "tool":
                line = f"{e['name']}: {e['label']}" if e["label"] else e["name"]
            if line:
                break
        if line:
            break
    if len(_activity) > 64:
        _activity.clear()
    _activity[key] = line
    return line


# --- JSON views -----------------------------------------------------------------------------


def board_counts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    boards = rows(conn, "SELECT slug, name, default_workdir, archived_at FROM boards")
    latest = {r["board"]: r["at"] for r in conn.execute(
        "SELECT board, MAX(updated_at) at FROM tasks GROUP BY board")}  # fmt: skip
    counts: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT board, status, COUNT(*) n FROM tasks GROUP BY board, status"):
        counts.setdefault(r["board"], {})[r["status"]] = r["n"]
    for b in boards:
        b["counts"] = counts.get(b["slug"], {})
        b["last_activity"] = latest.get(b["slug"])
    return sorted(
        boards, key=lambda b: (b["archived_at"] is not None, -(b["last_activity"] or 0), b["slug"])
    )


def api_boards(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    return {"boards": board_counts(conn)}


def find_board(conn: sqlite3.Connection, slug: str | None) -> dict[str, Any]:
    boards = board_counts(conn)
    if slug is None:
        pick = next((b for b in boards if not b["archived_at"]), None)
        if pick is None:
            raise NotFound("no boards yet; create one with: kanban boards create")
        return pick
    found = next((b for b in boards if b["slug"] == slug), None)
    if found is None:
        raise NotFound(f"no board {slug!r}")
    return found


def api_board(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    board = find_board(conn, q.get("b"))
    slug = board["slug"]
    cards = {c["id"]: c for c in views.cards_list(conn, slug, sort="priority")}
    extra = {r["id"]: r for r in conn.execute(
        "SELECT id, substr(body, 1, 400) AS body_excerpt, assignee, implementer, model_override, "
        "reasoning_effort, pr_url, merge_state, worktree_state, retry_after, last_failure_error, "
        "skills FROM tasks WHERE board = ?", (slug,))}  # fmt: skip
    cost = {r["task_id"]: r for r in conn.execute(
        "SELECT task_id, ROUND(SUM(cost_usd), 4) cost, COUNT(*) runs FROM task_runs GROUP BY task_id")}  # fmt: skip
    latest = {r["task_id"]: dict(r) for r in conn.execute(
        "SELECT r.task_id, r.id, r.n, r.role, r.kind, r.status, r.outcome, r.model, r.effort, "
        "r.started_at, r.ended_at, r.cost_usd, r.num_turns, substr(r.summary, 1, 400) AS summary, "
        "substr(r.error, 1, 400) AS error, r.stream_path FROM task_runs r "
        "JOIN (SELECT task_id, MAX(n) n FROM task_runs GROUP BY task_id) m "
        "ON m.task_id = r.task_id AND m.n = r.n")}  # fmt: skip
    out: dict[str, list[dict[str, Any]]] = {lane: [] for lane in LANES}
    done_total = 0
    for cid, card in cards.items():
        if card["status"] not in out:
            continue
        x, run = dict(extra[cid]), latest.get(cid)
        card.update({k: v for k, v in x.items() if k != "skills"})
        card["skills"] = parse_json(x["skills"]) or []
        card["cost"] = (cost.get(cid) or {"cost": None})["cost"]
        card["run_count"] = (cost.get(cid) or {"runs": 0})["runs"]
        card["run"], card["activity"] = None, ""
        card["model"] = card["model_override"]
        card["effort"] = card["reasoning_effort"]
        if run:
            path = None
            if run["status"] == "running":
                with contextlib.suppress(Refused, OSError):
                    path = run_file(run, "stream_path", ("streams",))
            card["model"] = run["model"] or card["model"]
            card["effort"] = run["effort"] or card["effort"]
            card["run"] = {k: v for k, v in run.items() if k not in ("task_id", "stream_path")}
            card["activity"] = activity_line(path) or one_line(run["summary"] or run["error"])
        card["reason"] = (
            one_line(
                card["last_failure_error"]
                or (run or {}).get("summary")
                or (run or {}).get("error"),
                240,
            )
            if card["status"] == "blocked"
            else ""
        )
        card.pop("last_failure_error", None)
        out[card["status"]].append(card)
    out["done"].sort(key=lambda c: -(c["completed_at"] or c["updated_at"] or 0))
    done_total = len(out["done"])
    if q.get("all") != "1":
        out["done"] = out["done"][:DONE_SHOWN]
    since = int(time.mktime(date.today().timetuple()))
    today, running = conn.execute(
        "SELECT ROUND(COALESCE(SUM(cost_usd), 0), 4), (SELECT COUNT(*) FROM tasks WHERE status = 'running') "
        "FROM task_runs WHERE started_at >= ?", (since,)).fetchone()  # fmt: skip
    held = daemon.probe()
    return {
        "board": {k: board[k] for k in ("slug", "name", "default_workdir", "archived_at")},
        "boards": board_counts(conn), "lanes": out, "done_total": done_total,
        "daemon": {"alive": held is not None, "version": (held or {}).get("version")},
        "accounts": views.account_states(conn), "paused": store.state_get(conn, "dispatch_paused"),
        "today_cost": today, "running": running, "version": __version__, "now": store.now(),
        "schema": conn.execute("PRAGMA user_version").fetchone()[0],
    }  # fmt: skip


def run_out(row: dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    for key in JSON_RUN_COLS:
        if key in r:
            r[key] = parse_json(r[key])
    for src, dst, subs in (
        ("stream_path", "stream_file", ("streams",)),
        ("log_path", "log_file", ("runs", "streams")),
    ):
        try:
            path = run_file(r, src, subs)
        except Refused:
            path = None
        r[dst] = str(path) if path else None
        r.pop(src, None)
    prompt = prompt_file(r)
    r["prompt_file"] = str(prompt) if prompt else None
    return r


def api_card(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    tid = q.get("id") or ""
    if not re.fullmatch(r"t_[0-9a-f]{8}", tid):
        raise Usage("card ids look like t_0123abcd")
    data = views.show_data(conn, tid)
    task = data["task"]
    task["touches"] = parse_json(task.get("touches")) or []
    data["runs"] = [run_out(r) for r in data["runs"]]
    data["comments"] = [c for c in data["comments"]]

    def info(ids: list[str]) -> list[dict[str, Any]]:
        return [dict(r) for i in ids for r in conn.execute(
            "SELECT id, title, status, merge_state FROM tasks WHERE id = ?", (i,))]  # fmt: skip

    data["parents_info"], data["children_info"] = info(data["parents"]), info(data["children"])
    data["links"] = {}
    for table in ("jira_links", "github_links"):
        if has_table(conn, table):
            row = conn.execute(f"SELECT * FROM {table} WHERE task_id = ?", (tid,)).fetchone()  # noqa: S608
            if row:
                link = {k: v for k, v in dict(row).items() if k not in ("base_body", "card_body")}
                data["links"][table.removesuffix("_links")] = link
    data["cost"] = round(sum(r["cost_usd"] or 0 for r in data["runs"]), 4)
    data["board_name"] = (
        conn.execute("SELECT name FROM boards WHERE slug = ?", (task["board"],)).fetchone()
        or [None]
    )[0]
    data["schema"] = conn.execute("PRAGMA user_version").fetchone()[0]
    return data


def int_arg(q: dict[str, str], key: str, default: int | None = None) -> int | None:
    if key not in q or q[key] == "":
        return default
    try:
        return int(q[key])
    except ValueError:
        raise Usage(f"{key} must be a whole number") from None


def need_run(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    run_id = int_arg(q, "id")
    if run_id is None:
        raise Usage("id is the run's number")
    return run_row(conn, run_id)


def open_file(row: dict[str, Any], key: str, subs: tuple[str, ...], what: str) -> Path:
    try:
        path = run_file(row, key, subs)
    except Refused as e:
        raise Refused(str(e)) from e
    if path is None:
        raise NotFound(f"that run has no {what} yet")
    return path


def api_stream(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    row = need_run(conn, q)
    path = open_file(row, "stream_path", ("streams",), "stream")
    off = q.get("offset")
    if off not in (None, "", "tail") and int_arg(q, "offset") is None:
        raise Usage("offset is a byte offset or `tail`")
    page = stream_page(path, off, int_arg(q, "before"))
    return {**page, "run": row["id"], "status": row["status"], "file": str(path)}


def api_event(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    row = need_run(conn, q)
    path = open_file(row, "stream_path", ("streams",), "stream")
    at = int_arg(q, "at")
    if at is None or at < 0 or at != align(path, at):
        raise Usage("at is the byte offset of a line start, as the stream page gave it")
    got, _ = read_lines(path, at, None, 1)
    return {"events": [e for pos, line in got[:1] for e in decode(line, pos, caps=False)]}


def api_log(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    row = need_run(conn, q)
    path = open_file(row, "log_path", ("runs", "streams"), "log")
    if q.get("offset") not in (None, "", "tail") and int_arg(q, "offset") is None:
        raise Usage("offset is a byte offset or `tail`")
    return {**log_page(path, q.get("offset"), int_arg(q, "before")), "run": row["id"],
            "status": row["status"], "file": str(path)}  # fmt: skip


def api_stats(conn: sqlite3.Connection, q: dict[str, str]) -> dict[str, Any]:
    days = int_arg(q, "days", 7)
    if days is None or not 1 <= days <= 3650:
        raise Usage("days is between 1 and 3650")
    board = q.get("b") or None
    if board:
        find_board(conn, board)
    try:
        data = store.stats(conn, board=board, days=days)
    except (
        sqlite3.OperationalError
    ) as e:  # a newer stats() asking this schema for a column it lacks
        data = {"days": days, "unavailable": str(e)}
    data["accounts"] = views.account_states(conn)
    return data


ROUTES = {
    "/api/boards": api_boards, "/api/board": api_board, "/api/card": api_card,
    "/api/run/stream": api_stream, "/api/run/event": api_event, "/api/run/log": api_log,
    "/api/stats": api_stats,
}  # fmt: skip
STATUS = {Usage: 400, NotFound: 404, Refused: 403}


# --- http -----------------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    # BSD sockets let a second bind succeed beside a wildcard listener when this is on
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, port: int, home: Path) -> None:
        self.home = home
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except OSError as e:
            raise Error(
                f"cannot listen on 127.0.0.1:{port}: {e.strerror}; pick another with --port"
            ) from e
        self.allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def port(self) -> int:
        return int(self.server_address[1])


class Handler(BaseHTTPRequestHandler):
    server: Server
    protocol_version = "HTTP/1.1"
    server_version = "kanban-web"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send(
        self, status: int, body: bytes, ctype: str, head: bool, extra: dict[str, str] | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def json(self, status: int, data: Any, head: bool) -> None:
        body = json.dumps(views.clean_obj(data), separators=(",", ":")).encode()
        self.send(status, body, "application/json", head)

    def deny(
        self, status: int, message: str, head: bool = False, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps({"error": message}).encode()
        self.send(status, body, "application/json", head, extra)

    def do_GET(self) -> None:
        self.serve(head=False)

    def do_HEAD(self) -> None:
        self.serve(head=True)

    def refuse_method(self) -> None:
        self.close_connection = True
        self.deny(405, "read-only: GET and HEAD only", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = refuse_method

    def serve(self, head: bool) -> None:
        if self.headers.get("Host") not in self.server.allowed:
            self.close_connection = True
            return self.deny(403, "unexpected Host header", head)
        url = urlsplit(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self.asset("index.html", head)
            if m := ASSET.match(url.path):
                return self.asset(m.group(1), head)
            if url.path == "/api/run/raw":
                return self.raw(q, head)
            if url.path in ROUTES:
                with reader(self.server.home) as conn:
                    return self.json(200, ROUTES[url.path](conn, q), head)
            self.deny(404, "not found", head)
        except Error as e:
            self.deny(next((s for t, s in STATUS.items() if isinstance(e, t)), 500), str(e), head)
        except sqlite3.Error as e:
            self.deny(503, f"database unavailable: {e}", head)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except OSError as e:
            self.deny(404, f"cannot read the file: {e.strerror}", head)
        except Exception as e:  # noqa: BLE001
            print(f"kanban web: {self.path}: {e!r}", file=sys.stderr)
            self.deny(500, "internal error", head)

    def asset(self, name: str, head: bool) -> None:
        try:
            body = resources.files("kanban_claude").joinpath("webui", name).read_bytes()
        except (FileNotFoundError, OSError):
            return self.deny(404, "not found", head)
        self.send(200, body, TYPES[Path(name).suffix], head, {"Cache-Control": "no-cache"})

    def raw(self, q: dict[str, str], head: bool) -> None:
        kind = q.get("kind", "stream")
        if kind not in ("stream", "log", "prompt"):
            raise Usage("kind is stream, log or prompt")
        with reader(self.server.home) as conn:
            row = need_run(conn, q)
        if kind == "prompt":
            path = prompt_file(row)
            if path is None:
                raise NotFound("that run has no prompt file")
        else:
            key, subs = (
                ("stream_path", ("streams",))
                if kind == "stream"
                else ("log_path", ("runs", "streams"))
            )
            path = open_file(row, key, subs, kind)
        name = f"run-{row['id']}-{kind}{'.jsonl' if kind == 'stream' else '.txt'}"
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        if head:
            return
        sent = 0
        with path.open("rb") as f:
            while sent < size and (chunk := f.read(min(65536, size - sent))):
                self.wfile.write(chunk)
                sent += len(chunk)


def serve(a: argparse.Namespace) -> int:
    """`kanban web`: foreground until Ctrl-C. Never creates, migrates or writes the database."""
    home = dirs().home
    port = DEFAULT_PORT if a.port is None else a.port
    if not 0 <= port <= 65535:
        raise Usage("--port is between 0 and 65535")
    with reader(home) as conn:
        board = find_board(conn, a.board)["slug"] if a.board else None
    server = Server(port, home)
    url = f"http://127.0.0.1:{server.port}/" + (f"#/board/{board}" if board else "")
    print(f"kanban web {__version__}: read-only board at {url}  (Ctrl-C stops)", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0
