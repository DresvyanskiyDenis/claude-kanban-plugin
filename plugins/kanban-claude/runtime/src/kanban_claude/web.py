"""Web board (WEB-01..13): a loopback HTTP server, a bearer-token JSON API and the static page."""

import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from argparse import Namespace
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import __version__, paths, store, validate, views
from .errors import Error, NotAllowed, NotFound, Refused, Stale, Usage

log = logging.getLogger("kanban.web")

LOGIN_TOKEN_S = 60
SESSION_TOKEN_S = 86400
HTTP_MAX_CONN = 16
BODY_LIMIT = 1024 * 1024
SOCKET_TIMEOUT = 30
EVENT_PAGE = 200
LOG_TAIL = 200
KEY_MIN = 32
TITLE_MAX = 200
SHORT_MAX = 200
LIST_MAX = 50
INT_RANGE = {"priority": (-(10**6), 10**6), "max_retries": (1, 1000), "max_runtime": (1, 7 * 86400)}
ACTOR = "owner:web"
BUSY = b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"

Live = Callable[[str], dict[str, Any]]
"""`live(slug)`, what only the daemon knows about one board; see `make_server` for the shape."""

SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)
ASSETS = {
    "/": ("index.html", "text/html"),
    "/login": ("index.html", "text/html"),
    "/board.js": ("board.js", "text/javascript"),
    "/board.css": ("board.css", "text/css"),
}
BODY_TEMPLATE = "".join(
    f"## {h}\n\n\n"
    for h in (
        "Goal", "Context", "Acceptance gate", "Constraints / never do", "Out of scope",
        "Deliverable",
    )
)  # fmt: skip

# (from, to) -> (verb, prompts the page must collect); WEB-08. Nothing else is an owner move.
# `unblock` and `reopen_review` offer one lane: the verb lands the card on ready, todo or review.
_MOVES: list[tuple[tuple[str, ...], tuple[str, ...], str, str]] = [
    (("triage",), ("todo", "ready"), "specify", ""),
    (("todo",), ("ready",), "promote", "reason"),
    (("todo", "ready", "blocked"), ("triage",), "hold", "reason"),
    (("blocked",), ("ready",), "unblock", "reason"),
    (("review",), ("ready",), "reopen_review", "reason"),
    (("done",), ("ready",), "reopen", "reason"),
    (("review", "blocked"), ("done",), "mark_done", "reason confirm"),
    (("triage", "todo", "ready", "review", "blocked", "done"), ("archived",), "archive", "confirm"),
]
MOVES = {(f, t): (verb, ask) for froms, tos, verb, ask in _MOVES for f in froms for t in tos}
REASONED: dict[str, Callable[..., dict[str, Any]]] = {
    "promote": store.promote, "hold": store.hold, "unblock": store.unblock,
    "reopen_review": store.reopen_review, "reopen": store.reopen, "mark_done": store.mark_done,
}  # fmt: skip

FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "title": (str,), "body": (str,), "assignee": (str, type(None)), "workspace": (str,),
    "branch": (str, type(None)), "base": (str, type(None)), "priority": (int,),
    "max_runtime": (str, int), "max_retries": (int,), "skills": (list,),
    "model": (str, type(None)), "effort": (str, type(None)), "budget": (int, float, type(None)),
}  # fmt: skip
RUN_COLS = (
    "id", "n", "kind", "role", "status", "outcome", "summary", "error", "started_at", "ended_at",
    "model", "effort", "cost_usd", "num_turns",
)  # fmt: skip
STATUS = {Usage: 400, NotFound: 404, NotAllowed: 403, Refused: 422, Stale: 409}
TID = r"(t_[0-9a-f]{8})"
SLUG = r"([a-z0-9][a-z0-9_-]{0,63})"

CARD_SQL = """
SELECT t.id, t.title, t.status, t.assignee, t.priority, t.pr_url, t.pr_number, t.version,
  t.block_kind, t.merge_state, t.worktree_state, t.retry_after, t.created_at, t.updated_at,
  t.completed_at, r.started_at AS run_started, jl.jira_key, jl.error IS NOT NULL AS jira_error,
  jl.conflict IS NOT NULL AS jira_conflict,
  COALESCE(r.max_runtime_seconds, t.max_runtime_seconds) AS run_max,
  (SELECT SUM(cost_usd) FROM task_runs WHERE task_id = t.id) AS cost,
  (SELECT COUNT(*) FROM task_links l JOIN tasks p ON p.id = l.parent_id
     WHERE l.child_id = t.id AND p.status NOT IN ('done', 'archived')) AS open_parents,
  (SELECT COUNT(*) FROM task_links l JOIN tasks p ON p.id = l.parent_id
     WHERE l.child_id = t.id AND p.status IN ('done', 'archived')
       AND p.merge_state IN ('pending', 'failed')) AS merge_wait
FROM tasks t LEFT JOIN task_runs r ON r.task_id = t.id AND r.status = 'running'
  LEFT JOIN jira_links jl ON jl.task_id = t.id
"""


class Fail(Error):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --- secret and tokens (WEB-03) -------------------------------------------------------------


def write_secret(home: Path) -> bytes:
    """A fresh 0600 key, swapped in atomically so a concurrent reader never sees half a key."""
    key = secrets.token_bytes(32)
    tmp = paths.ensure_dir(home) / f"web.secret.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.replace(tmp, home / "web.secret")
    return key


def read_secret(home: Path) -> bytes:
    """Read on every request, so `board --revoke` takes effect without a daemon restart.

    Only `make_server` creates the key; a request that finds it missing or short fails closed.
    """
    try:
        key = (home / "web.secret").read_bytes()
    except OSError:
        key = b""
    if len(key) < KEY_MIN:
        raise Fail(503, "the web key is missing or too short; restart the board server")
    return key


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(key: bytes, text: str) -> str:
    return _b64(hmac.new(key, text.encode(), hashlib.sha256).digest())


def mint(key: bytes, purpose: str, ttl: int) -> str:
    claims = {"purpose": purpose, "exp": int(time.time()) + ttl, "nonce": secrets.token_hex(8)}
    payload = _b64(json.dumps(claims).encode())
    return f"{payload}.{_sign(key, payload)}"


def verify(key: bytes, token: str, purpose: str) -> dict[str, Any] | None:
    if not token.isascii():
        return None
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(sig.encode(), _sign(key, payload).encode()):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError:
        return None
    if not isinstance(claims, dict) or claims.get("purpose") != purpose:
        return None
    exp, nonce = claims.get("exp"), claims.get("nonce")
    if not isinstance(nonce, str) or isinstance(exp, bool) or not isinstance(exp, int):
        return None
    return claims if exp >= time.time() else None


def proof(key: bytes, nonce: str) -> str:
    return hmac.new(key, f"healthz:{nonce}".encode(), hashlib.sha256).hexdigest()


# --- reads ----------------------------------------------------------------------------------


@dataclass
class Req:
    srv: "WebServer"
    conn: sqlite3.Connection
    query: dict[str, str]
    body: dict[str, Any]

    def live(self, slug: str) -> dict[str, Any]:
        got = self.srv.live(slug) if self.srv.live else {}
        return {
            "skips": dict(got.get("skips") or {}),
            "unprotected": list(got.get("unprotected") or []),
        }


def moves_from(status: str) -> dict[str, str]:
    return {to: ask for (frm, to), (_, ask) in MOVES.items() if frm == status}


def card_rows(r: Req, slug: str, where: str, args: Sequence[Any]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in r.conn.execute(f"{CARD_SQL} WHERE {where}", args)]
    skips = r.live(slug)["skips"]
    for row in rows:
        row["moves"] = moves_from(row["status"])
        row["skip"] = skips.get(row["id"])
    return rows


def one(r: Req, task_id: str) -> dict[str, Any]:
    slug = store.get_task(r.conn, task_id)["board"]
    return card_rows(r, slug, "t.id = ?", [task_id])[0]


def accounts(conn: sqlite3.Connection, slug: str, now: int) -> list[dict[str, Any]]:
    """The login this board runs under, if it has a problem, and never which config folder it is:
    the page may be on a shared screen."""
    s = store.account_get(conn, store.effective_config_dir(conn, store.get_board(conn, slug)))
    if not isinstance(s, dict):
        return []
    acct = {
        "logged_out": bool(s.get("auth_paused")),
        "hard_until": s["hard_until"] if (s.get("hard_until") or 0) > now else None,
        "soft_until": s["soft_until"] if (s.get("soft_until") or 0) > now else None,
    }
    return [acct] if any(acct.values()) else []


def installed_version(home: Path) -> str | None:
    try:
        return str(json.loads((home / "installed.json").read_text())["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def header(r: Req, slug: str) -> dict[str, Any]:
    now = int(time.time())
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    today, week = r.conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN r.started_at >= ? THEN r.cost_usd END), 0), "
        "COALESCE(SUM(r.cost_usd), 0) FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.board = ? AND r.started_at >= ?",
        (int(midnight.timestamp()), slug, now - 7 * 86400),
    ).fetchone()
    running, here = r.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(board = ?), 0) FROM tasks WHERE status = 'running'", (slug,)
    ).fetchone()
    jira_base = store.cfg(r.conn, "web.jira_base_url")
    return {
        "paused": store.state_get(r.conn, "dispatch_paused"),
        "running": running,
        "running_here": here,
        "cap": store.cfg(r.conn, "dispatch.max_in_progress"),
        "cost_today": today,
        "cost_7d": week,
        "accounts": accounts(r.conn, slug, now),
        "jira_paused": (store.state_get(r.conn, f"jira:{slug}") or {}).get("paused"),
        "jira_base": jira_base if str(jira_base).startswith("https://") else None,
        "version": __version__,
        "installed": installed_version(r.srv.dirs.home),
    }


def qint(query: dict[str, str], key: str, default: int | None = None) -> int | None:
    try:
        return int(query[key]) if key in query else default
    except ValueError:
        raise Usage(f"{key} must be a whole number") from None


def event_json(e: dict[str, Any]) -> dict[str, Any]:
    """A board-less event (an account resumed, a setting changed) can carry a config folder or a
    path; the page only needs its kind."""
    payload = json.loads(e["payload"]) if e["payload"] and e["board"] else None
    return e | {"payload": payload}


# --- writes: field checks -------------------------------------------------------------------


def text(value: Any, name: str, limit: int = SHORT_MAX) -> str:
    """Text the store can hold: no NUL, no lone surrogate (neither survives SQLite or argv)."""
    if not isinstance(value, str):
        raise Usage(f"{name} must be text")
    try:
        value.encode()
    except UnicodeEncodeError:
        raise Refused(f"{name} has characters that cannot be stored") from None
    if "\0" in value:
        raise Refused(f"{name} must not contain a NUL character")
    if len(value) > limit:
        raise Refused(f"{name} is over {limit} characters")
    return value


def text_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > LIST_MAX:
        raise Usage(f"{name} must be a list of at most {LIST_MAX} entries")
    return [text(x, name) for x in value]


def bounded(key: str, value: Any) -> None:
    n = validate.duration(value) if key == "max_runtime" else value
    lo, hi = INT_RANGE[key]
    if not lo <= n <= hi:
        raise Refused(f"{key} must be between {lo} and {hi}")


def card_fields(body: dict[str, Any], board: sqlite3.Row) -> dict[str, Any]:
    """Type-check and bound the card fields; the store then validates them as on the CLI (SEC-12)."""
    out: dict[str, Any] = {}
    for key, value in body.items():
        if key in ("version", "parents", "triage"):
            continue
        kinds = FIELD_TYPES.get(key)
        if kinds is None:
            raise Usage(f"unknown field {key!r}")
        if isinstance(value, bool) or not isinstance(value, kinds):
            raise Usage(f"{key} has the wrong type")
        if isinstance(value, str):
            text(value, key, {"title": TITLE_MAX, "body": BODY_LIMIT}.get(key, SHORT_MAX))
        if key in INT_RANGE:
            bounded(key, value)
        if key == "skills":
            text_list(value, key)
        out[key] = value
    if "workspace" in out:
        out["workspace"] = workspace(board, out["workspace"])
    return out


def workspace(board: sqlite3.Row, value: str) -> str | None:
    if value == "scratch":
        return None
    if value == "worktree" and board["default_workdir"]:
        return f"worktree:{board['default_workdir']}"
    raise Refused(
        "workspace must be 'scratch' or 'worktree' (the board's default directory); "
        "dir: workspaces are CLI-only"
    )


def need_version(body: dict[str, Any]) -> int:
    v = body.get("version")
    if isinstance(v, bool) or not isinstance(v, int):
        raise Usage("version is required: the card version you last read")
    return v if -(2**63) <= v < 2**63 else -1  # beyond SQLite's integers: no card has it, so stale


@contextlib.contextmanager
def versioned(r: Req, task_id: str, version: int) -> Iterator[None]:
    """WEB-09. The store's version guard decides the write. A refusal for a card that has changed
    since the page read it is reported as that change, so the page reloads instead of showing a
    reason that no longer applies."""
    try:
        yield
    except Refused as e:
        if isinstance(e, Stale) or store.get_task(r.conn, task_id)["version"] == version:
            raise
        raise Stale("this card changed elsewhere; reload it and retry") from e


def reason_of(body: dict[str, Any]) -> str:
    return text(body.get("reason") or "", "reason", BODY_LIMIT)


def confirmed(body: dict[str, Any]) -> None:
    if body.get("confirm") is not True:
        raise Refused("this needs an explicit confirmation")


# --- routes (§17.2) -------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []


def route(method: str, pattern: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        ROUTES.append((method, re.compile(pattern), fn))
        return fn

    return register


@route("POST", "/api/login")
def login(r: Req) -> Any:
    key, token = read_secret(r.srv.dirs.home), r.body.get("t")
    claims = verify(key, token, "login") if isinstance(token, str) else None
    if claims is None or not r.srv.redeem(claims):
        raise Fail(401, "this login link expired or was already used; run: kanban board")
    return {"token": mint(key, "session", SESSION_TOKEN_S), "expires_in": SESSION_TOKEN_S}


@route("GET", "/api/boards")
def boards(r: Req) -> Any:
    return {
        "boards": [
            {
                "slug": b["slug"],
                "name": b["name"],
                "archived": b["archived_at"] is not None,
                "workdir": Path(b["default_workdir"]).name if b["default_workdir"] else None,
            }
            for b in store.boards_list(r.conn)
        ],
        "roles": store.active_roles(r.conn),
        "efforts": list(validate.EFFORTS),
        "template": BODY_TEMPLATE,
    }


@route("GET", f"/api/boards/{SLUG}")
def board(r: Req, slug: str) -> Any:
    b = store.get_board(r.conn, slug)
    top = r.conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    since = qint(r.query, "since")
    out: dict[str, Any] = {
        "board": {"slug": slug, "name": b["name"]},
        "header": header(r, slug),
        "live": r.live(slug),
        "incremental": since is not None,
        "resync": False,
        "events": [],
        "cards": [],
        "last_event_id": top,
    }
    if since is None:
        archived = "" if r.query.get("archived") == "1" else " AND t.status <> 'archived'"
        out["cards"] = card_rows(r, slug, f"t.board = ?{archived}", [slug])
        return out
    if not 0 <= since <= top:
        return out | {"resync": True}
    events = store.events_since(r.conn, since, board=slug, limit=EVENT_PAGE + 1)
    if len(events) > EVENT_PAGE:
        return out | {"resync": True}
    ids = sorted({e["task_id"] for e in events if e["task_id"]})
    if ids:
        out["cards"] = card_rows(r, slug, f"t.id IN ({','.join('?' * len(ids))})", ids)
    out["events"] = [event_json(e) for e in events]
    out["last_event_id"] = max(top, events[-1]["id"] if events else since)
    return out


@route("GET", f"/api/cards/{TID}")
def card(r: Req, task_id: str) -> Any:
    d = views.show_data(r.conn, task_id)

    def linked(mine: str, other: str) -> list[dict[str, Any]]:
        q = (
            f"SELECT t.id, t.title, t.status FROM task_links l JOIN tasks t ON t.id = l.{other}_id "
            f"WHERE l.{mine}_id = ? ORDER BY t.id"
        )
        return [dict(x) for x in r.conn.execute(q, (task_id,))]

    runs = [{k: run[k] for k in RUN_COLS} | {"has_log": bool(run["log_path"])} for run in d["runs"]]
    return {
        "row": one(r, task_id),
        "task": d["task"],
        "latest_summary": d["latest_summary"],
        "parents": linked("child", "parent"),
        "children": linked("parent", "child"),
        "comments": d["comments"],
        "runs": runs,
        "events": d["events"],
        "grants": d["grants"],
    }


@route("GET", r"/api/runs/(\d{1,18})/log")
def run_log(r: Req, run_id: str) -> Any:
    run = r.conn.execute(
        "SELECT log_path, status FROM task_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    if run is None or not run["log_path"]:
        raise NotFound(f"run {run_id} has no log yet")
    path = views.log_path(run["log_path"], r.srv.dirs.home, ("runs",))
    tail = min(max(qint(r.query, "tail", LOG_TAIL) or LOG_TAIL, 1), 1000)
    try:
        lines = views.log_lines(path)
    except OSError:
        raise NotFound("the log file is gone") from None
    return {"status": run["status"], "lines": [views.clean(x) for x in lines[-tail:]]}


@route("POST", f"/api/boards/{SLUG}/cards")
def create(r: Req, slug: str) -> Any:
    fields = card_fields(r.body, store.get_board(r.conn, slug))
    if fields.get("workspace", "") is None:
        del fields["workspace"]
    parents = text_list(r.body.get("parents", []), "parents")
    triage = r.body.get("triage", True)
    if not isinstance(triage, bool):
        raise Usage("triage must be a boolean")
    made = store.create(
        r.conn, slug, fields.pop("title", ""), actor=ACTOR, triage=triage, parents=parents, **fields
    )
    return {"id": made["id"], "card": one(r, made["id"])}


@route("PATCH", f"/api/cards/{TID}")
def patch(r: Req, task_id: str) -> Any:
    version = need_version(r.body)
    t = store.get_task(r.conn, task_id)
    fields = card_fields(r.body, store.get_board(r.conn, t["board"]))
    assign = "assignee" in fields
    role = fields.pop("assignee", None)
    if "workspace" in fields and fields["workspace"] is None:
        raise Refused("an existing card cannot go back to scratch")
    if assign and role not in store.active_roles(r.conn):
        raise Refused(f"assignee {role!r} must be one of the dispatchable roles")
    if not fields and not assign:
        raise Refused("nothing to change")
    with versioned(r, task_id, version):
        if fields:
            store.edit(
                r.conn, task_id, fields, actor=ACTOR, expect_version=version,
                allow_empty_body=t["status"] == "triage", assignee=role,
            )  # fmt: skip
        else:
            store.assign(r.conn, task_id, role, actor=ACTOR, expect_version=version)
    return {"card": one(r, task_id)}


@route("POST", f"/api/cards/{TID}/move")
def move(r: Req, task_id: str) -> Any:
    version, t, to = need_version(r.body), store.get_task(r.conn, task_id), r.body.get("to")
    if not isinstance(to, str):
        raise Usage("to must be a lane name")
    with versioned(r, task_id, version):
        step = MOVES.get((t["status"], to))
        if step is None:
            if t["status"] == "running":
                raise Refused("this card is running; use Stop, the run then ends as blocked")
            allowed = ", ".join(moves_from(t["status"])) or "nowhere"
            hint = "; only the dispatcher starts runs" if to in ("running", "blocked") else ""
            raise Refused(
                f"cannot move a {t['status']} card to {to[:30]}{hint}; it can go to: {allowed}"
            )
        verb, ask = step
        if "confirm" in ask:
            confirmed(r.body)
        reason = reason_of(r.body)
        if verb in REASONED:
            REASONED[verb](r.conn, task_id, reason, actor=ACTOR, expect_version=version)
        elif verb == "specify":
            store.specify(r.conn, task_id, {}, actor=ACTOR, expect_version=version)
        else:
            store.archive(r.conn, task_id, actor=ACTOR, expect_version=version)
    return {"card": one(r, task_id)}


@route("POST", f"/api/cards/{TID}/comments")
def add_comment(r: Req, task_id: str) -> Any:
    return store.comment(r.conn, task_id, text(r.body.get("body"), "body", BODY_LIMIT), actor=ACTOR)


@route("POST", f"/api/cards/{TID}/stop")
def stop(r: Req, task_id: str) -> Any:
    store.stop(r.conn, task_id, actor=ACTOR)
    return {"card": one(r, task_id)}


@route("POST", f"/api/cards/{TID}/archive")
def archive(r: Req, task_id: str) -> Any:
    confirmed(r.body)
    store.archive(r.conn, task_id, actor=ACTOR)
    return {"card": one(r, task_id)}


@route("POST", f"/api/cards/{TID}/merge-retry")
def merge_retry(r: Req, task_id: str) -> Any:
    store.merge_retry(r.conn, task_id, actor=ACTOR)
    return {"card": one(r, task_id)}


@route("POST", r"/api/dispatch/(pause|resume)")
def dispatch(r: Req, action: str) -> Any:
    if action == "pause":
        store.pause(r.conn, reason_of(r.body), actor=ACTOR)
    else:
        store.resume(r.conn, actor=ACTOR)
    return {"paused": store.state_get(r.conn, "dispatch_paused")}


# --- server ---------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server: "WebServer"
    timeout = SOCKET_TIMEOUT
    server_version = "kanban"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        """Silent on purpose: only `log_request` logs, and never a query string (WEB-01)."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        log.info("%s %s %s", self.command, urlsplit(self.path).path, code)

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def send_body(self, status: int, body: bytes, ctype: str, cache: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        self.send_body(status, json.dumps(payload).encode(), "application/json", "no-store")

    def do_GET(self) -> None:
        try:
            self.serve()
        except Error as e:
            status = e.status if isinstance(e, Fail) else STATUS.get(type(e), 500)
            self.send_json(status, {"error": views.clean(str(e))})
        except sqlite3.OperationalError as e:
            log.warning("web: database error: %s", e)
            what = "busy; retry" if "locked" in str(e) else "unavailable; see the daemon log"
            self.send_json(503, {"error": f"the database is {what}"})
        except Exception:
            log.exception("web: unhandled error")
            self.send_json(500, {"error": "internal error"})

    do_POST = do_PATCH = do_GET

    def check_request(self) -> None:
        """WEB-04: defence in depth on top of the bearer token."""
        h = self.headers
        same_origin = h.get("Origin") == f"http://{h.get('Host')}"
        sec = h.get("Sec-Fetch-Site") == "same-origin"
        json_type = (h.get("Content-Type") or "").split(";")[
            0
        ].strip().lower() == "application/json"
        if not (same_origin and sec and json_type):
            raise Fail(403, "refused: only same-origin JSON requests are accepted")

    def read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if size < 0:
            raise Usage("Content-Length must be a non-negative number")
        if size > BODY_LIMIT:
            raise Fail(413, "request body over 1 MB")
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, RecursionError):
            raise Usage("body must be JSON") from None
        if not isinstance(body, dict):
            raise Usage("body must be a JSON object")
        return body

    def serve(self) -> None:
        u = urlsplit(self.path)
        port = self.server.port
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise Fail(421, "unexpected Host header; open the board at http://127.0.0.1:<port>")
        key = read_secret(self.server.dirs.home)
        query = dict(parse_qsl(u.query))
        if self.command == "GET" and u.path == "/healthz":
            nonce = query.get("nonce", "")
            if not 0 < len(nonce) <= 128:
                raise Usage("healthz needs ?nonce=<1 to 128 characters>")
            return self.send_json(
                200, {"ok": True, "version": __version__, "proof": proof(key, nonce)}
            )
        if self.command == "GET" and u.path in self.server.assets:
            page, ctype = self.server.assets[u.path]
            return self.send_body(200, page, ctype, "no-cache")
        if self.command == "GET" and u.path == "/theme.css":
            # An optional private look kept outside the plugin; read per request, so a reload shows edits.
            theme = self.server.dirs.home / "theme.css"
            css = theme.read_bytes() if theme.is_file() else b""
            return self.send_body(200, css, "text/css", "no-cache")
        if not u.path.startswith("/api/"):
            raise Fail(404, "not found")
        if (self.command, u.path) != ("POST", "/api/login"):
            auth = self.headers.get("Authorization", "")
            if verify(key, auth[7:] if auth.startswith("Bearer ") else "", "session") is None:
                raise Fail(401, "missing or invalid session; open the board with: kanban board")
        body: dict[str, Any] = {}
        if self.command != "GET":
            self.check_request()
            body = self.read_json()
        known = False
        for method, rx, fn in ROUTES:
            m = rx.fullmatch(u.path)
            if not m:
                continue
            known = True
            if method == self.command:
                with contextlib.closing(store.connect(self.server.dirs.home, upgrade=False)) as c:
                    return self.send_json(200, fn(Req(self.server, c, query, body), *m.groups()))
        raise Fail(405 if known else 404, "no such route")


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, dirs: paths.Dirs, port: int, live: Live | None) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.port: int = self.socket.getsockname()[1]
        self.dirs = dirs
        self.live = live
        self.redeemed: dict[str, int] = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(HTTP_MAX_CONN)
        root = resources.files("kanban_claude") / "web"
        self.assets = {
            p: ((root / name).read_bytes(), ctype) for p, (name, ctype) in ASSETS.items()
        }

    def redeem(self, claims: dict[str, Any]) -> bool:
        """A login token works once; the set only has to remember it until it expires."""
        now = time.time()
        with self.lock:
            self.redeemed = {n: e for n, e in self.redeemed.items() if e >= now}
            if claims["nonce"] in self.redeemed:
                return False
            self.redeemed[claims["nonce"]] = claims["exp"]
        return True

    def process_request(self, request: Any, client_address: Any) -> None:
        if self.slots.acquire(blocking=False):
            super().process_request(request, client_address)
            return
        with contextlib.suppress(OSError):
            request.settimeout(1)
            request.sendall(BUSY)
        self.shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(
    dirs: paths.Dirs, *, port: int | None = None, live: Live | None = None
) -> WebServer:
    """Bind 127.0.0.1 only (WEB-01) and make sure a web key exists; nothing else creates one.

    `port` defaults to `web.port`; 0 picks a free one.

    `live(slug)` is how the daemon tells the page what only it knows about one board. The server
    calls it on every feed request, from a request thread, so it must be cheap and thread-safe.
    It returns a dict; both keys are optional and anything else in it is ignored:

        {"skips": {"<card id>": "<reason>"},   # why dispatch is passing a card over right now,
                                               # for example "account_paused" or "no_assignee"
         "unprotected": ["<owner/repo>", ...]}  # this board's repos without branch protection

    Every board response carries the normalised map as `live`, and the page re-applies it to all
    cards on each poll, so a skip that appears or clears shows up without a card event.
    """
    conn = store.connect(dirs.home)
    try:
        if port is None:
            port = int(store.cfg(conn, "web.port"))
    finally:
        conn.close()
    try:
        read_secret(dirs.home)
    except Fail:
        write_secret(dirs.home)
    return WebServer(dirs, port, live)


# --- `kanban board` -------------------------------------------------------------------------


def ask_board(port: int, home: Path) -> tuple[bytes, dict[str, Any]]:
    """Ask `/healthz` on `port` for the HMAC proof (WEB-01) and check it; returns the web key and
    the reply (`version` is the daemon's)."""
    nonce = secrets.token_hex(16)
    probe = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        probe.request("GET", f"/healthz?nonce={nonce}")
        reply = json.loads(probe.getresponse().read(4096))
    except (OSError, ValueError, http.client.HTTPException) as e:
        raise Error(
            f"nothing answers on 127.0.0.1:{port}; start the daemon (kanban doctor) "
            "or run: python -m kanban_claude.web"
        ) from e
    finally:
        probe.close()
    key = read_secret(home)
    got = str(reply.get("proof", "")) if isinstance(reply, dict) else ""
    if not hmac.compare_digest(got.encode(), proof(key, nonce).encode()):
        raise Error(f"the process on port {port} is not this board's server; no login link sent")
    return key, reply


def verify_board(port: int, home: Path) -> bytes:
    return ask_board(port, home)[0]


def cli(a: Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    home = paths.dirs().home
    if a.revoke:
        write_secret(home)
        message = "every board session is revoked; open a new one with: kanban board"
        return {"revoked": True}, message
    port = int(store.cfg(conn, "web.port"))
    key = verify_board(port, home)
    url = f"http://127.0.0.1:{port}/login#t={mint(key, 'login', LOGIN_TOKEN_S)}"
    if not a.no_open:
        webbrowser.open(url)
    return {"url": url}, url


def main() -> int:
    """Serve the board on its own, against `$KANBAN_HOME` (development; the daemon embeds it)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    srv = make_server(paths.dirs())
    print(
        f"board on http://127.0.0.1:{srv.port}/ ; get a login link with: kanban board", flush=True
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
