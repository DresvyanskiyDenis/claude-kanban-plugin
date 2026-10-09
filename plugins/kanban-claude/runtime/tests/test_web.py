"""TST-W: the web board against a real server on an ephemeral port."""

import http.client
import json
import logging
import re
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import paths, store, web
from kanban_claude.errors import Refused

from .conftest import Kb

Card = Callable[..., str]
Reply = tuple[int, dict[str, Any], bytes]
Call = Callable[..., Reply]
LIVE: dict[str, Any] = {"skips": {}, "unprotected": []}


@pytest.fixture
def srv(board: str) -> Iterator[web.WebServer]:
    LIVE.update(skips={}, unprotected=[])
    s = web.make_server(paths.dirs(), port=0, live=lambda slug: LIVE)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


def send(
    port: int, method: str, path: str, headers: dict[str, str], body: bytes = b"",
) -> Reply:  # fmt: skip
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        c.putheader(k, v)
    if method != "GET" and "Content-Length" not in headers:
        c.putheader("Content-Length", str(len(body)))
    c.endheaders(body)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, {k.lower(): v for k, v in r.getheaders()}, data


@pytest.fixture
def key(srv: web.WebServer) -> bytes:
    return web.read_secret(srv.dirs.home)


@pytest.fixture
def call(srv: web.WebServer, key: bytes) -> Call:
    """A same-origin browser request; `over` replaces a header, None removes it."""

    def go(method: str, path: str, body: Any = None, **over: str | None) -> Reply:
        h = {
            "Host": f"127.0.0.1:{srv.port}",
            "Authorization": f"Bearer {web.mint(key, 'session', 600)}",
        }
        if method != "GET":
            h |= {
                "Origin": f"http://127.0.0.1:{srv.port}", "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
            }  # fmt: skip
        over = {k.replace("_", "-"): v for k, v in over.items()}
        h = {k: v for k, v in (h | over).items() if v is not None}
        raw = (
            body
            if isinstance(body, bytes)
            else json.dumps(body if body is not None else {}).encode()
        )
        return send(srv.port, method, path, h, raw if method != "GET" else b"")

    return go


def ok(reply: Reply, status: int = 200) -> Any:
    assert reply[0] == status, reply[2]
    return json.loads(reply[2])


def version(conn: sqlite3.Connection, tid: str) -> int:
    return int(conn.execute("SELECT version FROM tasks WHERE id = ?", (tid,)).fetchone()[0])


def put(conn: sqlite3.Connection, tid: str, status: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, implementer = 'smith', version = version + 1 WHERE id = ?",
        (status, tid),
    )


def test_w1_host_allowlist(srv: web.WebServer, key: bytes) -> None:
    p = srv.port
    for path in ("/", "/healthz?nonce=a", "/api/boards", "/board.js"):
        for host in ("evil.example", f"evil.example:{p}", "127.0.0.1", f"[::1]:{p}", "0.0.0.0:1"):
            assert send(p, "GET", path, {"Host": host})[0] == 421, (path, host)
        assert send(p, "GET", path, {})[0] == 421
    assert send(p, "GET", "/", {"Host": f"localhost:{p}"})[0] == 200
    assert send(p, "GET", "/", {"Host": f"127.0.0.1:{p}"})[0] == 200


@pytest.mark.parametrize("auth", [None, "Bearer", "Bearer x", "Bearer a.b", "Basic abc", ""])
def test_w2_bad_bearer_is_401(call: Call, auth: str | None) -> None:
    assert call("GET", "/api/boards", Authorization=auth)[0] == 401


def test_w2_token_kinds_and_tampering(call: Call, key: bytes) -> None:
    good = web.mint(key, "session", 60)
    payload, _, sig = good.partition(".")
    bad = [
        web.mint(key, "login", 60), web.mint(key, "session", -5), web.mint(b"x" * 32, "session", 60),
        good[:-2] + ("AA" if good[-2:] != "AA" else "BB"), f"{payload}x.{sig}", good.partition(".")[0],
        web.mint(key, "session", 60).replace(".", "..", 1), "." + sig,
    ]  # fmt: skip
    for tok in bad:
        assert call("GET", "/api/boards", Authorization=f"Bearer {tok}")[0] == 401, tok
    assert call("GET", "/api/boards", Authorization=good)[0] == 401
    assert call("GET", "/api/boards", Authorization=f"Bearer {good}")[0] == 200


@pytest.mark.parametrize(
    "over",
    [
        {"Origin": "http://evil.example"}, {"Origin": None}, {"Origin": "null"},
        {"Origin": "https://127.0.0.1:1"}, {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": None},
        {"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": "text/plain"},
        {"Content-Type": "multipart/form-data; boundary=x"}, {"Content-Type": None},
    ],
)  # fmt: skip
def test_w2_request_checks_are_403(call: Call, card: Card, over: dict[str, str | None]) -> None:
    tid = card()
    assert call("POST", f"/api/cards/{tid}/comments", {"body": "x"}, **over)[0] == 403
    assert call("PATCH", f"/api/cards/{tid}", {"version": 1, "title": "x"}, **over)[0] == 403


def test_w2_order_and_cors(call: Call, srv: web.WebServer) -> None:
    assert (
        call("POST", "/api/dispatch/pause", Authorization=None, Origin="http://evil.example")[0]
        == 401
    )
    ctype = {"Content-Type": "application/json; charset=utf-8"}
    assert call("POST", "/api/dispatch/resume", **ctype)[0] == 200
    for method in ("OPTIONS", "DELETE", "PUT", "HEAD"):
        status, headers, _ = send(
            srv.port, method, "/api/boards", {"Host": f"127.0.0.1:{srv.port}"}
        )
        assert status == 501
        assert not [h for h in headers if h.startswith("access-control")]
    status, headers, _ = call("GET", "/api/boards")
    assert status == 200 and not [h for h in headers if h.startswith("access-control")]


def test_w3_inert_markup(call: Call, srv: web.WebServer, board: str) -> None:
    evil = "<img src=x onerror=alert(1)><script>alert(2)</script>‮"
    made = ok(call("POST", f"/api/boards/{board}/cards", {"title": evil, "body": evil}))
    got = ok(call("GET", f"/api/cards/{made['id']}"))
    assert got["task"]["title"] == evil and got["task"]["body"] == evil
    status, headers, _ = call("GET", f"/api/cards/{made['id']}")
    assert headers["content-type"].startswith("application/json")
    assert headers["x-content-type-options"] == "nosniff"
    page = send(srv.port, "GET", "/", {"Host": f"127.0.0.1:{srv.port}"})
    csp = page[1]["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    js = send(srv.port, "GET", "/board.js", {"Host": f"127.0.0.1:{srv.port}"})[2].decode()
    for sink in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "srcdoc",
    ):
        assert sink not in js, sink
    html = page[2].decode()
    assert not re.search(r"<script(?![^>]*\bsrc=)|\son\w+=|\sstyle=", html)


def test_w4_version_conflict(call: Call, conn: sqlite3.Connection, card: Card) -> None:
    tid = card(triage=True)
    v = version(conn, tid)
    assert call("PATCH", f"/api/cards/{tid}", {"title": "no version"})[0] == 400
    assert call("PATCH", f"/api/cards/{tid}", {"version": True, "title": "x"})[0] == 400
    first = ok(call("PATCH", f"/api/cards/{tid}", {"version": v, "title": "first"}))
    assert first["card"]["title"] == "first" and first["card"]["version"] == v + 1
    stale = call("PATCH", f"/api/cards/{tid}", {"version": v, "title": "second"})
    assert stale[0] == 409 and "changed elsewhere" in json.loads(stale[2])["error"]
    assert call("POST", f"/api/cards/{tid}/move", {"version": v, "to": "ready"})[0] == 409
    assert conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "first"


MOVES = [
    ("triage", "ready", {}, 200, "ready"),
    ("triage", "todo", {}, 200, "ready"),
    ("todo", "ready", {"reason": "go"}, 200, "ready"),
    ("todo", "ready", {}, 422, "todo"),
    ("todo", "triage", {"reason": "rethink"}, 200, "triage"),
    ("ready", "triage", {"reason": "rethink"}, 200, "triage"),
    ("ready", "triage", {}, 422, "ready"),
    ("blocked", "ready", {"reason": "fixed"}, 200, "ready"),
    ("blocked", "todo", {"reason": "fixed"}, 422, "blocked"),
    ("blocked", "triage", {"reason": "rethink"}, 200, "triage"),
    ("review", "ready", {"reason": "changes"}, 200, "ready"),
    ("review", "todo", {"reason": "changes"}, 422, "review"),
    ("review", "ready", {}, 422, "review"),
    ("done", "ready", {"reason": "oops"}, 200, "ready"),
    ("review", "done", {"reason": "ok", "confirm": True}, 200, "done"),
    ("blocked", "done", {"reason": "ok", "confirm": True}, 200, "done"),
    ("review", "done", {"reason": "ok"}, 422, "review"),
    ("review", "done", {"reason": "ok", "confirm": "yes"}, 422, "review"),
    ("review", "done", {"confirm": True}, 422, "review"),
    ("ready", "archived", {"confirm": True}, 200, "archived"),
    ("done", "archived", {"confirm": True}, 200, "archived"),
    ("ready", "archived", {}, 422, "ready"),
    ("ready", "running", {}, 422, "ready"),
    ("todo", "blocked", {"reason": "x"}, 422, "todo"),
    ("running", "ready", {"reason": "x"}, 422, "running"),
    ("running", "archived", {"confirm": True}, 422, "running"),
    ("archived", "ready", {"reason": "x"}, 422, "archived"),
    ("done", "todo", {"reason": "x"}, 422, "done"),
    ("ready", "nowhere", {}, 422, "ready"),
]


@pytest.mark.parametrize(("frm", "to", "extra", "code", "final"), MOVES)
def test_w5_moves(
    call: Call, conn: sqlite3.Connection, card: Card,
    frm: str, to: str, extra: dict[str, Any], code: int, final: str,
) -> None:  # fmt: skip
    tid = card(triage=frm == "triage")
    if frm != "triage":
        put(conn, tid, frm)
    r = call("POST", f"/api/cards/{tid}/move", {"version": version(conn, tid), "to": to, **extra})
    assert r[0] == code, r[2]
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == final
    if code == 200:
        assert json.loads(r[2])["card"]["status"] == final
        actors = {
            e[0] for e in conn.execute("SELECT actor FROM task_events WHERE task_id = ?", (tid,))
        }
        assert "owner:web" in actors
    else:
        assert json.loads(r[2])["error"]


def test_w5_open_parent_blocks_promote_and_rows_carry_moves(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    parent, child = card(), card()
    store.link(conn, parent, child)
    row = next(c for c in ok(call("GET", f"/api/boards/{board}"))["cards"] if c["id"] == child)
    assert row["status"] == "todo" and row["open_parents"] == 1
    assert row["moves"] == {"ready": "reason", "triage": "reason", "archived": "confirm"}
    body = {"version": row["version"], "to": "ready", "reason": "now"}
    assert call("POST", f"/api/cards/{child}/move", body)[0] == 422


def test_w5_owner_moves_offer_one_lane_when_the_verb_picks_the_target(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    blocked, review = card(), card()
    put(conn, blocked, "blocked")
    put(conn, review, "review")
    rows = {c["id"]: c for c in ok(call("GET", f"/api/boards/{board}"))["cards"]}
    assert rows[blocked]["moves"] == {
        "ready": "reason", "triage": "reason", "done": "reason confirm", "archived": "confirm",
    }  # fmt: skip
    assert rows[review]["moves"] == {
        "ready": "reason",
        "done": "reason confirm",
        "archived": "confirm",
    }


def test_w5_parent_counts_split_open_from_waiting_for_merge(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    open_parent, merging, merged, child = card(), card(), card(), card()
    for parent in (open_parent, merging, merged):
        store.link(conn, parent, child)
    put(conn, merging, "done")
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (merging,))
    put(conn, merged, "done")
    conn.execute("UPDATE tasks SET merge_state = 'merged' WHERE id = ?", (merged,))
    rows = {c["id"]: c for c in ok(call("GET", f"/api/boards/{board}"))["cards"]}
    assert (rows[child]["open_parents"], rows[child]["merge_wait"]) == (1, 1)
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (open_parent,))
    conn.execute("UPDATE tasks SET merge_state = 'failed' WHERE id = ?", (merging,))
    row = next(c for c in ok(call("GET", f"/api/boards/{board}"))["cards"] if c["id"] == child)
    assert (row["open_parents"], row["merge_wait"]) == (0, 1)
    assert "last_outcome" not in row


def test_w6_login_is_single_use(call: Call, key: bytes) -> None:
    tok = web.mint(key, "login", 60)
    got = ok(call("POST", "/api/login", {"t": tok}, Authorization=None))
    assert got["expires_in"] == web.SESSION_TOKEN_S
    assert call("GET", "/api/boards", Authorization=f"Bearer {got['token']}")[0] == 200
    assert call("POST", "/api/login", {"t": tok}, Authorization=None)[0] == 401


def test_w6_login_rejects_expired_wrong_purpose_and_garbage(call: Call, key: bytes) -> None:
    cases: list[Any] = [
        web.mint(key, "login", -1),
        web.mint(key, "session", 60),
        web.mint(b"y" * 32, "login", 60),
        "",
        "a.b",
        5,
        None,
        ["x"],
    ]
    for t in cases:
        assert call("POST", "/api/login", {"t": t}, Authorization=None)[0] == 401, t
    assert call("POST", "/api/login", b"not json", Authorization=None)[0] == 400
    assert call("POST", "/api/login", b"[]", Authorization=None)[0] == 400
    cross = call(
        "POST",
        "/api/login",
        {"t": web.mint(key, "login", 60)},
        Authorization=None,
        Origin="http://evil.example",
    )
    assert cross[0] == 403


def test_w6_login_token_expires_after_sixty_seconds(
    call: Call, key: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    tok = web.mint(key, "login", web.LOGIN_TOKEN_S)
    real = time.time
    monkeypatch.setattr(web.time, "time", lambda: real() + web.LOGIN_TOKEN_S + 2)
    assert call("POST", "/api/login", {"t": tok}, Authorization=None)[0] == 401


def test_w6_revoke_invalidates_every_token(call: Call, key: bytes, kb: Kb) -> None:
    login = web.mint(key, "login", 60)
    session = ok(call("POST", "/api/login", {"t": web.mint(key, "login", 60)}, Authorization=None))[
        "token"
    ]
    assert call("GET", "/api/boards", Authorization=f"Bearer {session}")[0] == 200
    code, out, _ = kb("board", "--revoke")
    assert code == 0 and "revoked" in out
    assert call("GET", "/api/boards", Authorization=f"Bearer {session}")[0] == 401
    assert call("POST", "/api/login", {"t": login}, Authorization=None)[0] == 401
    fresh = web.mint(web.read_secret(paths.dirs().home), "session", 60)
    assert call("GET", "/api/boards", Authorization=f"Bearer {fresh}")[0] == 200


def test_secret_file_is_private(srv: web.WebServer) -> None:
    f = srv.dirs.home / "web.secret"
    assert f.stat().st_mode & 0o777 == 0o600 and len(f.read_bytes()) == 32
    web.write_secret(srv.dirs.home)
    assert f.stat().st_mode & 0o777 == 0o600


def test_a_missing_or_short_key_fails_closed(
    srv: web.WebServer, call: Call, key: bytes, kb: Kb
) -> None:
    f = srv.dirs.home / "web.secret"
    for weak in (b"", b"k" * 31):
        f.write_bytes(weak)
        forged = web.mint(weak, "session", 60)
        assert call("GET", "/api/boards", Authorization=f"Bearer {forged}")[0] == 503
        assert (
            call("POST", "/api/login", {"t": web.mint(weak, "login", 60)}, Authorization=None)[0]
            == 503
        )
        assert (
            send(srv.port, "GET", "/healthz?nonce=a", {"Host": f"127.0.0.1:{srv.port}"})[0] == 503
        )
    f.unlink()
    assert call("GET", "/api/boards")[0] == 503
    assert not f.exists(), "a request thread must never create the key"
    assert kb("config", "set", "web.port", str(srv.port))[0] == 0
    code, _, err = kb("board", "--no-open")
    assert code == 1 and "web key" in err and not f.exists()
    f.write_bytes(key)
    assert call("GET", "/api/boards")[0] == 200


def test_make_server_creates_the_key_once(tmp_path: Path) -> None:
    dirs = paths.Dirs(tmp_path / "home", tmp_path / "work")
    first = web.make_server(dirs, port=0)
    key = web.read_secret(dirs.home)
    first.server_close()
    second = web.make_server(dirs, port=0)
    second.server_close()
    assert len(key) == 32 and web.read_secret(dirs.home) == key


def test_w7_binds_loopback_only(srv: web.WebServer) -> None:
    assert srv.server_address[0] == "127.0.0.1"
    assert srv.socket.getsockname()[0] == "127.0.0.1"


def test_w8_healthz_proof(srv: web.WebServer, key: bytes) -> None:
    h = {"Host": f"127.0.0.1:{srv.port}"}
    got = json.loads(send(srv.port, "GET", "/healthz?nonce=abc123", h)[2])
    assert got["ok"] is True and got["proof"] == web.proof(key, "abc123")
    assert got["proof"] != web.proof(b"z" * 32, "abc123")
    assert got["proof"] != web.proof(key, "other")
    assert send(srv.port, "GET", "/healthz", h)[0] == 400
    assert send(srv.port, "GET", f"/healthz?nonce={'a' * 129}", h)[0] == 400


def test_w8_board_refuses_a_squatter(srv: web.WebServer, kb: Kb, tmp_path: Path) -> None:
    squatter = web.make_server(paths.Dirs(tmp_path / "other-home", tmp_path / "other-work"), port=0)
    threading.Thread(target=squatter.serve_forever, daemon=True).start()
    try:
        assert kb("config", "set", "web.port", str(squatter.port))[0] == 0
        code, out, err = kb("board", "--no-open")
        assert code == 1 and "not this board's server" in err and "t=" not in out + err
    finally:
        squatter.shutdown()
        squatter.server_close()


def test_board_cli_mints_a_working_login_link(
    srv: web.WebServer, call: Call, kb: Kb, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web.webbrowser, "open", opened.append)
    assert kb("config", "set", "web.port", str(srv.port))[0] == 0
    code, out, _ = kb("board", "--no-open")
    assert code == 0 and not opened
    url = out.strip()
    assert url.startswith(f"http://127.0.0.1:{srv.port}/login#t=")
    assert ok(call("POST", "/api/login", {"t": url.split("#t=")[1]}, Authorization=None))["token"]
    assert kb("board")[0] == 0 and len(opened) == 1 and "#t=" in opened[0]
    code, out, _ = kb("--json", "board", "--no-open")
    assert json.loads(out)["url"].startswith("http://127.0.0.1:")


def test_board_cli_without_a_server(kb: Kb) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert kb("config", "set", "web.port", str(port))[0] == 0
    code, _, err = kb("board", "--no-open")
    assert code == 1 and "nothing answers" in err


def test_w9_connection_cap(srv: web.WebServer, key: bytes) -> None:
    held = [socket.create_connection(("127.0.0.1", srv.port)) for _ in range(web.HTTP_MAX_CONN)]
    try:
        over = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        assert over.recv(100).startswith(b"HTTP/1.0 503")
        over.close()
    finally:
        for s in held:
            s.close()
    h = {"Host": f"127.0.0.1:{srv.port}"}
    for _ in range(50):
        if send(srv.port, "GET", "/", h)[0] == 200:
            return
        time.sleep(0.05)
    raise AssertionError("slots were not released after the idle connections closed")


def test_w10_access_log_has_no_query(
    srv: web.WebServer, call: Call, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    caplog.set_level(logging.INFO, logger="kanban.web")
    send(srv.port, "GET", "/healthz?nonce=SECRETNONCE", {"Host": f"127.0.0.1:{srv.port}"})
    call("GET", "/api/boards/main?since=SECRETSINCE&archived=1")
    call("GET", "/api/runs/1/log?tail=SECRETTAIL")
    assert "GET /healthz 200" in caplog.text and "GET /api/boards/main 400" in caplog.text
    assert not re.search(r"SECRET|\?|Bearer|Authorization", caplog.text)
    assert capsys.readouterr().err == ""


def test_static_assets_and_headers(srv: web.WebServer) -> None:
    h = {"Host": f"127.0.0.1:{srv.port}"}
    for path, ctype in (("/", "text/html"), ("/login", "text/html"), ("/board.js", "text/javascript"),
                        ("/board.css", "text/css")):  # fmt: skip
        status, headers, body = send(srv.port, "GET", path, h)
        assert status == 200 and headers["content-type"].startswith(ctype) and body
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["server"].strip() == "kanban"
    for path in ("/nope", "/../etc/passwd", "/board.js/x", "/web.secret", "/index.html", "/api"):
        assert send(srv.port, "GET", path, h)[0] == 404, path
    assert send(srv.port, "GET", "/api/boards", h)[1]["cache-control"] == "no-store"


def test_body_limits_and_malformed_requests(call: Call, srv: web.WebServer, card: Card) -> None:
    tid = card()
    path = f"/api/cards/{tid}/comments"
    assert call("POST", path, b"", Content_Length=str(web.BODY_LIMIT + 1))[0] == 413
    assert call("POST", path, b"{bad")[0] == 400
    assert call("POST", path, b"[1]")[0] == 400
    assert call("POST", path, {"body": 5})[0] == 400
    assert call("POST", path, Content_Length="-5")[0] == 400
    assert call("POST", path, Content_Length="abc")[0] == 400


def test_oversized_body_is_refused_and_nothing_is_written(
    srv: web.WebServer, key: bytes, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card()
    big = json.dumps({"body": "a" * web.BODY_LIMIT}).encode()
    head = (
        f"POST /api/cards/{tid}/comments HTTP/1.0\r\nHost: 127.0.0.1:{srv.port}\r\n"
        f"Authorization: Bearer {web.mint(key, 'session', 60)}\r\n"
        f"Origin: http://127.0.0.1:{srv.port}\r\nSec-Fetch-Site: same-origin\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(big)}\r\n\r\n"
    ).encode()
    reply = b""
    with socket.create_connection(("127.0.0.1", srv.port), timeout=10) as s:
        try:
            s.sendall(head + big)
            while chunk := s.recv(4096):
                reply += chunk
        except (BrokenPipeError, ConnectionResetError):
            pass  # the server may close before it has read the body; the refusal still stands
    assert reply == b"" or reply.startswith(b"HTTP/1.0 413")
    assert (
        conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0]
        == 0
    )


def test_routing_errors(call: Call, board: str) -> None:
    assert call("GET", "/api/nope")[0] == 404
    assert call("GET", "/api/cards/not-an-id")[0] == 404
    assert call("GET", "/api/cards/t_00000000")[0] == 404
    assert call("GET", "/api/boards/missing")[0] == 404
    assert call("POST", f"/api/boards/{board}")[0] == 405
    assert call("GET", "/api/dispatch/pause")[0] == 405
    assert call("GET", f"/api/boards/{board}?since=abc")[0] == 400


def test_unexpected_errors_do_not_leak(call: Call, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> Any:
        raise RuntimeError("secret internals /home/x")

    monkeypatch.setattr(web.store, "get_board", boom)
    status, _, body = call("GET", "/api/boards/main")
    assert status == 500 and json.loads(body) == {"error": "internal error"}


def test_operational_errors_are_logged_and_busy_only_when_locked(
    call: Call, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="kanban.web")
    message = {"text": "database is locked"}

    def fail(*_: Any) -> Any:
        raise sqlite3.OperationalError(message["text"])

    monkeypatch.setattr(web.store, "get_board", fail)
    status, _, body = call("GET", "/api/boards/main")
    assert status == 503 and "busy" in json.loads(body)["error"]
    message["text"] = "no such table: tasks"
    status, _, body = call("GET", "/api/boards/main")
    assert status == 503 and "busy" not in json.loads(body)["error"]
    assert "no such table: tasks" in caplog.text


def test_snapshot_cards_and_header(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    a, b = card("alpha", priority=2), card("beta", triage=True)
    run = store.claim(conn, a)
    assert run
    conn.execute("UPDATE task_runs SET cost_usd = 0.5 WHERE id = ?", (run,))
    snap = ok(call("GET", f"/api/boards/{board}"))
    rows = {c["id"]: c for c in snap["cards"]}
    assert rows[a]["status"] == "running" and rows[a]["run_started"] and rows[a]["run_max"]
    assert rows[a]["cost"] == 0.5 and rows[a]["moves"] == {}
    assert rows[b]["status"] == "triage" and rows[b]["cost"] is None
    assert snap["header"]["running"] == 1 and snap["header"]["cap"] == 2
    assert snap["header"]["cost_today"] == 0.5 and snap["header"]["cost_7d"] == 0.5
    assert snap["header"]["paused"] is None and snap["header"]["installed"] is None
    assert snap["incremental"] is False and snap["last_event_id"] > 0
    boards = ok(call("GET", "/api/boards"))
    assert boards["boards"][0]["slug"] == board and "smith" in boards["roles"]
    assert boards["template"].count("## ") == 6


def test_archived_cards_only_on_request(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    tid = card()
    store.archive(conn, tid)
    assert ok(call("GET", f"/api/boards/{board}"))["cards"] == []
    shown = ok(call("GET", f"/api/boards/{board}?archived=1"))["cards"]
    assert [c["id"] for c in shown] == [tid]


def test_incremental_feed_and_resync(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    a = card()
    cursor = ok(call("GET", f"/api/boards/{board}"))["last_event_id"]
    quiet = ok(call("GET", f"/api/boards/{board}?since={cursor}"))
    assert quiet["incremental"] and quiet["cards"] == [] and quiet["events"] == []
    assert quiet["last_event_id"] == cursor and not quiet["resync"]
    b = card("second")
    store.comment(conn, a, "hi")
    feed = ok(call("GET", f"/api/boards/{board}?since={cursor}"))
    assert {c["id"] for c in feed["cards"]} == {a, b}
    assert feed["events"] and feed["last_event_id"] > cursor
    assert ok(call("GET", f"/api/boards/{board}?since={feed['last_event_id'] + 50}"))["resync"]
    for _ in range(web.EVENT_PAGE + 1):
        store.comment(conn, a, "spam")
    assert ok(call("GET", f"/api/boards/{board}?since={cursor}"))["resync"] is True


def test_feed_is_scoped_to_the_board(
    call: Call, conn: sqlite3.Connection, board: str, tmp_path: Path
) -> None:
    cursor = ok(call("GET", f"/api/boards/{board}"))["last_event_id"]
    store.board_create(conn, "other", default_workdir=str(tmp_path))
    store.create(conn, "other", "elsewhere", triage=True)
    feed = ok(call("GET", f"/api/boards/{board}?since={cursor}"))
    assert feed["cards"] == [] and feed["events"] == []


def test_pause_resume_and_header_facts(
    call: Call, conn: sqlite3.Connection, srv: web.WebServer, board: str
) -> None:
    assert (
        ok(call("POST", "/api/dispatch/pause", {"reason": "lunch"}))["paused"]["detail"] == "lunch"
    )
    assert ok(call("GET", f"/api/boards/{board}"))["header"]["paused"]["reason"] == "manual"
    assert ok(call("POST", "/api/dispatch/resume"))["paused"] is None
    soon, past = int(time.time()) + 600, int(time.time()) - 600
    for name, state in (
        ("/c/one", {"auth_paused": True, "hard_until": past}),
        ("/c/two", {"hard_until": soon, "soft_until": past,
                    "last_rate_limit": {"five_hour": {"utilization": 0.4}}}),
        ("/c/three", {"last_rate_limit": "garbage"}),
    ):  # fmt: skip
        conn.execute(
            "INSERT INTO state(key, value) VALUES (?, ?)", (f"account:{name}", json.dumps(state))
        )
    (srv.dirs.home / "installed.json").write_text(json.dumps({"version": "9.9.9"}))
    LIVE["unprotected"] = ["acme/proj"]
    h = ok(call("GET", f"/api/boards/{board}"))["header"]
    accts = {a["name"]: a for a in h["accounts"]}
    assert accts["one"]["logged_out"] and accts["one"]["hard_until"] is None
    assert accts["two"]["hard_until"] == soon and accts["two"]["soft_until"] is None
    assert not {"five_hour", "seven_day"} & accts["two"].keys()
    assert h["installed"] == "9.9.9" and "unprotected" not in h
    snap = ok(call("GET", f"/api/boards/{board}"))
    assert snap["live"]["unprotected"] == ["acme/proj"]
    events = [
        e[0] for e in conn.execute("SELECT actor FROM task_events WHERE kind LIKE 'dispatch_%'")
    ]
    assert events == ["owner:web", "owner:web"]


def test_live_skip_reason_reaches_the_row(call: Call, card: Card, board: str) -> None:
    tid = card()
    LIVE["skips"] = {tid: "repo is dirty"}
    row = ok(call("GET", f"/api/boards/{board}"))["cards"][0]
    assert row["skip"] == "repo is dirty"


def test_every_feed_response_carries_the_live_map(
    call: Call, srv: web.WebServer, card: Card, board: str
) -> None:
    tid = card()
    snap = ok(call("GET", f"/api/boards/{board}"))
    assert snap["live"] == {"skips": {}, "unprotected": []}
    since = f"/api/boards/{board}?since={snap['last_event_id']}"
    LIVE.update(skips={tid: "account_paused"}, unprotected=["acme/proj"])
    quiet = ok(call("GET", since))
    assert quiet["cards"] == [] and quiet["events"] == []
    assert quiet["live"] == {"skips": {tid: "account_paused"}, "unprotected": ["acme/proj"]}
    LIVE.update(skips={}, unprotected=[])
    assert ok(call("GET", since))["live"] == {"skips": {}, "unprotected": []}
    assert ok(call("GET", f"{since}9999"))["live"] == {"skips": {}, "unprotected": []}
    srv.live = None
    assert ok(call("GET", since))["live"] == {"skips": {}, "unprotected": []}
    srv.live = lambda slug: {}
    assert ok(call("GET", since))["live"] == {"skips": {}, "unprotected": []}


def test_create_edit_comment_flow(call: Call, conn: sqlite3.Connection, board: str) -> None:
    made = ok(
        call("POST", f"/api/boards/{board}/cards", {"title": "Idea", "body": "b", "priority": 3})
    )
    tid = made["id"]
    assert made["card"]["status"] == "triage" and made["card"]["priority"] == 3
    ws = ok(call("POST", f"/api/boards/{board}/cards", {"title": "W", "workspace": "worktree"}))
    assert ws["card"]["status"] == "triage"
    detail = ok(call("GET", f"/api/cards/{tid}"))["task"]
    assert detail["created_by"] == "owner:web" and detail["workspace_kind"] == "scratch"
    v = version(conn, tid)
    edited = ok(call("PATCH", f"/api/cards/{tid}", {"version": v, "assignee": "smith", "body": ""}))
    assert edited["card"]["assignee"] == "smith"
    ok(call("POST", f"/api/cards/{tid}/comments", {"body": "note <b>x</b>"}))
    got = ok(call("GET", f"/api/cards/{tid}"))
    assert (
        got["comments"][-1]["body"] == "note <b>x</b>" and got["comments"][-1]["author"] == "owner"
    )
    assert {"row", "task", "runs", "events", "parents", "children", "grants"} <= got.keys()
    assert any(e["actor"] == "owner:web" for e in got["events"])


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "workspace": "dir:/etc"}, {"title": "x", "workspace": "elsewhere"},
        {"title": "x", "assignee": "nobody"}, {"title": ""}, {"title": "x", "effort": "huge"},
        {"title": "x", "max_runtime": "forever"}, {"title": "x", "budget": 99999},
        {"title": "x", "skills": ["Bad Skill"]}, {"title": "x", "branch": "bad..name"},
    ],
)  # fmt: skip
def test_create_validates_like_the_cli(call: Call, board: str, body: dict[str, Any]) -> None:
    status, _, raw = call("POST", f"/api/boards/{board}/cards", body)
    assert status == 422, raw


@pytest.mark.parametrize(
    "body",
    [
        {"title": "x", "bogus": 1}, {"title": 5}, {"title": "x", "priority": "high"},
        {"title": "x", "priority": True}, {"title": "x", "skills": "a"}, {"title": "x", "parents": "t"},
        {"title": "x", "triage": "yes"}, {"title": "x", "status": "done"},
        {"title": "x", "assignee": ["smith"]},
    ],
)  # fmt: skip
def test_create_rejects_bad_shapes(call: Call, board: str, body: dict[str, Any]) -> None:
    assert call("POST", f"/api/boards/{board}/cards", body)[0] == 400


HOSTILE: list[dict[str, Any]] = [
    {"priority": 2**70}, {"priority": -(2**70)}, {"max_runtime": 2**70}, {"max_runtime": f"{10**30}h"},
    {"max_runtime": "9" * 5000}, {"max_retries": 2**70}, {"max_retries": 0}, {"branch": "a\x00b"},
    {"base": "\x00"}, {"title": "a\x00b"}, {"title": "\ud800"}, {"body": "\ud800"},
    {"body": "a\x00b"}, {"model": "\ud800"}, {"effort": "\ud800"}, {"assignee": "\ud800"},
    {"skills": ["\ud800"]}, {"skills": ["a\x00"]}, {"skills": [[["x"]]]}, {"parents": ["\ud800"]},
    {"parents": [5]}, {"parents": [None]}, {"title": "x" * (web.TITLE_MAX + 1)},
    {"workspace": "\ud800"}, {"budget": 10**400}, {"budget": float("inf")},
]  # fmt: skip


@pytest.mark.parametrize("hostile", HOSTILE, ids=lambda h: str(h)[:40])
def test_hostile_card_values_get_a_4xx_never_a_traceback(
    call: Call, conn: sqlite3.Connection, card: Card, board: str, hostile: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    tid = card()
    before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    status, _, raw = call("POST", f"/api/boards/{board}/cards", {"title": "x", **hostile})
    assert 400 <= status < 500, raw
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
    patch = {k: v for k, v in hostile.items() if k != "parents"}
    if patch:
        v = version(conn, tid)
        status, _, raw = call("PATCH", f"/api/cards/{tid}", {"version": v, **patch})
        assert 400 <= status < 500, raw
        assert version(conn, tid) == v
    assert "unhandled" not in caplog.text


def test_title_cap_is_the_only_length_check_on_a_good_title(call: Call, board: str) -> None:
    ok(call("POST", f"/api/boards/{board}/cards", {"title": "t" * web.TITLE_MAX}))


def test_hostile_text_in_comments_reasons_and_pauses(
    call: Call, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card()
    assert 400 <= call("POST", f"/api/cards/{tid}/comments", {"body": "\ud800"})[0] < 500
    assert 400 <= call("POST", f"/api/cards/{tid}/comments", {"body": "a\x00"})[0] < 500
    assert 400 <= call("POST", "/api/dispatch/pause", {"reason": "\ud800"})[0] < 500
    assert 400 <= call("POST", "/api/dispatch/pause", {"reason": ["x"]})[0] < 500
    move = {"version": version(conn, tid), "to": "triage"}
    assert 400 <= call("POST", f"/api/cards/{tid}/move", {**move, "reason": "\ud800"})[0] < 500
    assert 400 <= call("POST", f"/api/cards/{tid}/move", {**move, "reason": {"a": 1}})[0] < 500
    assert (
        conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0]
        == 0
    )


def test_hostile_numbers_in_versions_queries_and_paths(
    call: Call, conn: sqlite3.Connection, card: Card, board: str
) -> None:
    tid = card()
    assert call("PATCH", f"/api/cards/{tid}", {"version": 2**70, "title": "x"})[0] == 409
    assert call("POST", f"/api/cards/{tid}/move", {"version": 2**70, "to": "triage"})[0] == 409
    assert ok(call("GET", f"/api/boards/{board}?since={2**70}"))["resync"] is True
    assert call("GET", f"/api/boards/{board}?since={'9' * 5000}")[0] == 400
    assert call("GET", "/api/runs/99999999999999999999999/log")[0] == 404


def test_hostile_login_bodies_are_rejected(call: Call) -> None:
    assert call("POST", "/api/login", {"t": "\ud800.x"}, Authorization=None)[0] == 401
    assert call("POST", "/api/login", {"t": "a.\ud800"}, Authorization=None)[0] == 401
    assert call("POST", "/api/login", b"[" * 400_000, Authorization=None)[0] == 400
    assert call("POST", "/api/login", b'{"a":' * 100_000, Authorization=None)[0] == 400
    assert call("POST", "/api/cards/t_00000000/comments", b"[" * 400_000)[0] == 400


def test_patch_refusals(call: Call, conn: sqlite3.Connection, card: Card) -> None:
    tid = card()
    v = version(conn, tid)
    assert call("PATCH", f"/api/cards/{tid}", {"version": v})[0] == 422
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "assignee": "nobody"})[0] == 422
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "assignee": None})[0] == 422
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "workspace": "scratch"})[0] == 422
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "title": ""})[0] == 422
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "bogus": 1})[0] == 400
    assert version(conn, tid) == v
    assert store.claim(conn, tid)
    assert (
        call("PATCH", f"/api/cards/{tid}", {"version": version(conn, tid), "title": "x"})[0] == 422
    )


def test_patch_assigns_first_and_a_refused_assign_saves_nothing(
    call: Call, conn: sqlite3.Connection, card: Card, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = card("keep me")
    v = version(conn, tid)

    def no(*_: Any, **__: Any) -> Any:
        raise Refused("assign says no")

    monkeypatch.setattr(web.store, "assign", no)
    status, _, raw = call(
        "PATCH", f"/api/cards/{tid}", {"version": v, "title": "x", "assignee": "designer"}
    )
    assert status == 422 and "assign says no" in json.loads(raw)["error"]
    assert version(conn, tid) == v
    assert conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "keep me"


def test_patch_edit_refused_after_the_assign_rolls_the_assign_back(
    call: Call, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card("keep me")
    v = version(conn, tid)
    before = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[
        0
    ]
    status, _, raw = call(
        "PATCH", f"/api/cards/{tid}", {"version": v, "body": "", "assignee": "designer"}
    )
    assert status == 422 and "wipe the card body" in json.loads(raw)["error"]
    row = conn.execute("SELECT assignee, version FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (row["assignee"], row["version"]) == ("smith", v)
    q = "SELECT COUNT(*) FROM task_events WHERE task_id = ?"
    assert conn.execute(q, (tid,)).fetchone()[0] == before


def test_a_stale_version_is_a_409_even_when_the_card_also_changed_state(
    call: Call, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card()
    v = version(conn, tid)
    assert store.claim(conn, tid)
    stale = call("POST", f"/api/cards/{tid}/move", {"version": v, "to": "triage", "reason": "x"})
    assert stale[0] == 409 and "changed elsewhere" in json.loads(stale[2])["error"]
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "title": "x"})[0] == 409
    assert call("PATCH", f"/api/cards/{tid}", {"version": v, "assignee": "designer"})[0] == 409
    assert call("PATCH", f"/api/cards/{tid}", {"version": -(2**70), "title": "x"})[0] == 409


def test_patch_with_a_new_role_and_fields_applies_both(
    call: Call, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card("old")
    v = version(conn, tid)
    row = ok(
        call("PATCH", f"/api/cards/{tid}", {"version": v, "title": "new", "assignee": "designer"})
    )["card"]
    assert (row["title"], row["assignee"], row["version"]) == ("new", "designer", v + 2)
    same = ok(
        call(
            "PATCH",
            f"/api/cards/{tid}",
            {"version": v + 2, "title": "newer", "assignee": "designer"},
        )
    )
    assert same["card"]["version"] == v + 3
    kinds = [
        e[0]
        for e in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))
    ]
    assert kinds[-3:] == ["assigned", "edited", "edited"]


def test_patch_moves_a_scratch_card_to_the_worktree_but_never_back(
    call: Call, conn: sqlite3.Connection, card: Card
) -> None:
    tid = card(triage=True)
    got = ok(
        call("PATCH", f"/api/cards/{tid}", {"version": version(conn, tid), "workspace": "worktree"})
    )
    assert got["card"]["id"] == tid
    kind = conn.execute("SELECT workspace_kind FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kind == "worktree"
    for ws in ("scratch", "dir:/etc"):
        body = {"version": version(conn, tid), "workspace": ws}
        assert call("PATCH", f"/api/cards/{tid}", body)[0] == 422


def test_stop_archive_and_merge_retry(call: Call, conn: sqlite3.Connection, card: Card) -> None:
    tid = card()
    assert call("POST", f"/api/cards/{tid}/stop")[0] == 422
    assert call("POST", f"/api/cards/{tid}/merge-retry")[0] == 422
    assert call("POST", f"/api/cards/{tid}/archive")[0] == 422
    assert store.claim(conn, tid)
    stopped = ok(call("POST", f"/api/cards/{tid}/stop"))["card"]
    assert stopped["status"] == "blocked" and stopped["block_kind"] == "stopped"
    assert (
        ok(call("POST", f"/api/cards/{tid}/archive", {"confirm": True}))["card"]["status"]
        == "archived"
    )


def test_card_detail_hides_run_internals(call: Call, conn: sqlite3.Connection, card: Card) -> None:
    parent, child = card("parent"), card("child")
    store.link(conn, parent, child)
    run = store.claim(conn, parent)
    assert run
    conn.execute(
        "UPDATE task_runs SET log_path = ?, stream_path = ? WHERE id = ?", ("/x/a.log", "/x/s", run)
    )
    got = ok(call("GET", f"/api/cards/{child}"))
    assert got["parents"] == [{"id": parent, "title": "parent", "status": "running"}]
    runs = ok(call("GET", f"/api/cards/{parent}"))["runs"]
    assert runs[0]["has_log"] is True
    assert (
        not {"log_path", "stream_path", "runner_pid", "session_id", "config_dir"} & runs[0].keys()
    )
    assert ok(call("GET", f"/api/cards/{parent}"))["children"][0]["id"] == child


def test_run_log_tail_is_cleaned_and_confined(
    call: Call, conn: sqlite3.Connection, card: Card, srv: web.WebServer, tmp_path: Path
) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    path = f"/api/runs/{run}/log"
    assert call("GET", path)[0] == 404 and call("GET", "/api/runs/9999/log")[0] == 404
    logs = srv.dirs.home / "runs"
    logs.mkdir()
    inside = logs / "a.log"
    inside.write_text("".join(f"line {i}\n" for i in range(300)) + "\x1b[31mred\x1b[0m \x07bell\n")
    outside = tmp_path / "outside.log"
    outside.write_text("secret")
    (logs / "link.log").symlink_to(outside)
    streams = srv.dirs.home / "streams"
    streams.mkdir()
    (streams / "s.log").write_text("secret tool output")

    def point(log: Path) -> Reply:
        conn.execute("UPDATE task_runs SET log_path = ? WHERE id = ?", (str(log), run))
        return call("GET", path)

    status, _, raw = point(inside)
    lines = json.loads(raw)["lines"]
    assert status == 200 and len(lines) == web.LOG_TAIL and lines[-1] == "red bell"
    assert b"\\u001b" not in raw and b"\\u0007" not in raw
    for bad in (
        outside,
        logs / "link.log",
        logs / ".." / "outside.log",
        srv.dirs.home / "kanban.db",
        streams / "s.log",
    ):
        status, _, raw = point(bad)
        assert status == 422 and b"secret" not in raw, bad
    point(inside)
    assert len(ok(call("GET", f"{path}?tail=5"))["lines"]) == 5
    assert len(ok(call("GET", f"{path}?tail=0"))["lines"]) == web.LOG_TAIL
    inside.unlink()
    assert call("GET", path)[0] == 404


def test_snapshot_of_500_cards_is_fast(call: Call, conn: sqlite3.Connection, board: str) -> None:
    now = int(time.time())
    conn.execute("BEGIN")
    for i in range(500):
        conn.execute(
            "INSERT INTO tasks(id, board, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                f"t_{i:08x}",
                board,
                f"card {i}",
                ("ready", "todo", "done", "triage")[i % 4],
                now,
                now,
            ),
        )
    conn.execute("COMMIT")
    start = time.perf_counter()
    snap = ok(call("GET", f"/api/boards/{board}"))
    assert len(snap["cards"]) == 500 and time.perf_counter() - start < 0.3


def test_concurrent_requests(call: Call, card: Card, board: str) -> None:
    card()
    codes: list[int] = []
    threads = [
        threading.Thread(target=lambda: codes.append(call("GET", f"/api/boards/{board}")[0]))
        for _ in range(12)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [200] * 12


def test_server_shape() -> None:
    assert issubclass(web.WebServer, ThreadingHTTPServer) and web.WebServer.daemon_threads
    assert web.Handler.protocol_version == "HTTP/1.0" and web.Handler.sys_version == ""
