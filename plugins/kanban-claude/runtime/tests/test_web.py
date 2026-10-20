"""`kanban web`: the read-only dashboard against a real server on an ephemeral port."""

import hashlib
import http.client
import json
import socket
import sqlite3
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import cli, paths, store, web

Card = Callable[..., str]
Reply = tuple[int, dict[str, str], bytes]
Get = Callable[..., Reply]

V6_COLUMNS = (
    "advisor TEXT", "advisor_calls INTEGER", "cache_write_1h_tokens INTEGER",
    "cache_write_5m_tokens INTEGER", "api_ms INTEGER", "harness TEXT", "params TEXT",
)  # fmt: skip
HARNESS = {
    "turns": 4, "tools": {"Bash": 3}, "skills": {"kanban": 1}, "mcp": {"context7": 2},
    "agents": {"Explore": 1}, "hooks": {"SessionStart:startup": {"ok": 1, "failed": 0}},
    "compactions": 0, "permission_denials": 0, "tool_errors": 1,
    "subagents": {"spawned": 1, "failed": 0}, "terminal_reason": "completed",
}  # fmt: skip
INIT = {
    "model": "claude-sonnet-5-5", "permissionMode": "auto", "claude_code_version": "2.1.302",
    "tools": ["Bash", "Read"], "mcp_servers": [{"name": "context7", "status": "connected"}],
    "skills": ["kanban"], "plugins": ["kanban-claude"], "agents": ["smith"],
}  # fmt: skip


def jline(obj: dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


def stream_lines(n: int = 3, big: int = 0) -> str:
    out = [jline({"type": "system", "subtype": "init", **INIT, "cwd": "/w"})]
    out.append(jline({"type": "system", "subtype": "ui_status", "text": None}))
    for i in range(n):
        out.append(jline({"type": "assistant", "message": {"content": [
            {"type": "text", "text": f"step {i} <b>bold</b>"},
            {"type": "tool_use", "id": f"tu{i}", "name": "Bash", "input": {"command": f"echo {i}"}},
        ]}}))  # fmt: skip
        out.append(jline({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": "x" * big or f"out {i}"}
        ]}}))  # fmt: skip
    out.append(jline({"type": "result", "subtype": "success", "total_cost_usd": 0.5, "num_turns": n,
                      "result": "done", "usage": {}, "modelUsage": {}}))  # fmt: skip
    return "".join(out)


@pytest.fixture
def home() -> Path:
    return paths.dirs().home


@pytest.fixture
def run_row(conn: sqlite3.Connection, card: Card, home: Path) -> Callable[..., int]:
    """A run row for a fresh card, with the stream and log it names written under the home."""

    def make(stream: str | bytes | None = None, status: str = "ended", **cols: Any) -> int:
        tid = card("Run card")
        n = 1
        stream_path = home / "streams" / "main" / tid / f"run-{n}.jsonl"
        log_path = home / "runs" / "main" / tid / f"run-{n}.log"
        for p in (stream_path, log_path):
            p.parent.mkdir(parents=True, exist_ok=True)
        data = stream_lines() if stream is None else stream
        stream_path.write_bytes(data if isinstance(data, bytes) else data.encode())
        log_path.write_text("[kanban] line one\n[run] done\n")
        row = {
            "task_id": tid, "n": n, "role": "smith", "kind": "work", "status": status,
            "started_at": store.now(), "stream_path": str(stream_path), "log_path": str(log_path),
            "model": "sonnet", "effort": "high", "cost_usd": 0.5, **cols,
        }  # fmt: skip
        marks = ",".join("?" * len(row))
        conn.execute(f"INSERT INTO task_runs({','.join(row)}) VALUES ({marks})", list(row.values()))
        return int(conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0])

    return make


@pytest.fixture
def srv(board: str, home: Path) -> Iterator[web.Server]:
    s = web.Server(0, home)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s
    s.shutdown()
    s.server_close()


@pytest.fixture
def get(srv: web.Server) -> Get:
    def go(path: str, method: str = "GET", **headers: str) -> Reply:
        c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        headers.setdefault("Host", f"127.0.0.1:{srv.port}")
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            c.putheader(k.replace("_", "-"), v)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, body

    return go


def js(reply: Reply) -> Any:
    assert reply[0] == 200, reply[2][:300]
    return json.loads(reply[2])


def fingerprint(home: Path) -> tuple[str, int, list[tuple[str, int]]]:
    """The main file's hash, its schema version and every table's row count. SQLite may leave empty
    -wal/-shm files beside a WAL database it was asked to read, so those are not compared."""
    db = home / "kanban.db"
    c = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    tables = [
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")
    ]
    counts = [(t, c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables]
    version = c.execute("PRAGMA user_version").fetchone()[0]
    c.close()
    return hashlib.sha256(db.read_bytes()).hexdigest(), version, counts


def to_v5(conn: sqlite3.Connection) -> None:
    """Strip what schemas 6 and 7 added: the web never migrates, so it must read an older daemon's database."""
    conn.execute("DROP INDEX idx_comments_github")
    conn.execute("DROP TABLE github_links")
    for table, cols in (
        ("task_runs", [c.split()[0] for c in V6_COLUMNS]),
        ("boards", ["github_repo", "github_query", "github_project"]),
        ("task_comments", ["gh_comment_id"]),
    ):
        for col in cols:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    conn.execute("PRAGMA user_version = 5")


def to_v6(conn: sqlite3.Connection) -> None:
    for col in V6_COLUMNS:
        conn.execute(f"ALTER TABLE task_runs ADD COLUMN {col}")
    conn.execute("PRAGMA user_version = 6")


# --- the door -------------------------------------------------------------------------------


def test_host_header_must_be_loopback(get: Get, srv: web.Server) -> None:
    assert get("/api/boards")[0] == 200
    assert get("/api/boards", Host=f"localhost:{srv.port}")[0] == 200
    for host in ("evil.example", f"evil.example:{srv.port}", "127.0.0.1", "127.0.0.1:1"):
        status, _, body = get("/api/boards", Host=host)
        assert status == 403 and b"Host" in body
    assert get("/", Host="rebind.example")[0] == 403
    assert get("/assets/app.js", Host="rebind.example")[0] == 403


def test_only_get_and_head(get: Get, srv: web.Server) -> None:
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        status, headers, _ = get("/api/boards", method=method)
        assert status == 405 and headers["allow"] == "GET, HEAD"
    status, headers, body = get("/api/boards", method="HEAD")
    assert status == 200 and body == b"" and int(headers["content-length"]) > 0


def test_headers_forbid_inline_code_and_cross_origin_reads(get: Get) -> None:
    for path in ("/", "/api/boards"):
        _, headers, _ = get(path)
        csp = headers["content-security-policy"]
        assert "default-src 'self'" in csp and "unsafe-inline" not in csp
        assert headers["x-content-type-options"] == "nosniff"
        assert not any(k.startswith("access-control") for k in headers)


def test_binds_loopback_only_and_fails_when_the_port_is_busy(srv: web.Server, home: Path) -> None:
    assert srv.server_address[0] == "127.0.0.1"
    with pytest.raises(web.Error, match="cannot listen"):
        web.Server(srv.port, home)
    wild = socket.socket()
    wild.bind(("0.0.0.0", 0))  # noqa: S104
    wild.listen()
    try:
        with pytest.raises(web.Error, match="cannot listen"):
            web.Server(wild.getsockname()[1], home)
    finally:
        wild.close()


def test_page_and_assets_are_served_from_the_package(get: Get) -> None:
    status, headers, body = get("/")
    assert status == 200 and headers["content-type"] == "text/html" and b"<script" in body
    for src in ("app.js", "app.css"):
        assert get(f"/assets/{src}")[0] == 200
    assert get("/assets/../web.py")[0] == 404
    assert get("/assets/nope.js")[0] == 404
    assert get("/etc/passwd")[0] == 404


def test_the_verb_is_registered_without_the_migrating_connection() -> None:
    assert cli.LONG_RUNNING["web"] is web.serve
    assert "web" not in cli.OWNER


# --- read-only ------------------------------------------------------------------------------


@pytest.mark.parametrize("v6", [False, True], ids=["v5", "current"])
def test_every_route_leaves_the_database_untouched(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int], home: Path, v6: bool
) -> None:
    if not v6:
        to_v5(conn)
    rid = run_row(
        **(
            {
                "harness": json.dumps(HARNESS),
                "params": json.dumps({"argv": ["claude"], "init": INIT}),
            }
            if v6
            else {}
        )
    )
    tid = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (rid,)).fetchone()[0]
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    for sidecar in home.glob("kanban.db-*"):
        sidecar.unlink()
    before = fingerprint(home)
    paths_ = [
        "/api/boards", "/api/board?b=main", f"/api/card?id={tid}", f"/api/run/stream?id={rid}",
        f"/api/run/log?id={rid}", f"/api/run/raw?id={rid}&kind=stream", "/api/stats?days=30",
        f"/api/run/event?id={rid}&at=0",
    ]  # fmt: skip
    for p in paths_:
        assert get(p)[0] == 200, p
    assert fingerprint(home) == before
    assert before[0] and before[1] == (len(store.MIGRATIONS) if v6 else 5)


def test_a_missing_database_is_refused_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(web.Error, match="no board database"), web.reader(empty):
        pass
    assert not (empty / "kanban.db").exists()


def test_a_newer_schema_with_unknown_columns_is_read_as_it_is(
    conn: sqlite3.Connection, get: Get, card: Card
) -> None:
    conn.execute("ALTER TABLE tasks ADD COLUMN from_the_future TEXT")
    conn.execute("PRAGMA user_version = 99")
    tid = card("Future")
    assert js(get(f"/api/card?id={tid}"))["task"]["title"] == "Future"
    assert js(get("/api/board?b=main"))["schema"] == 99


# --- shapes ---------------------------------------------------------------------------------


def test_boards_and_board_shape(conn: sqlite3.Connection, get: Get, card: Card) -> None:
    a, b = card("First"), card("Second")
    boards = js(get("/api/boards"))["boards"]
    assert boards[0]["slug"] == "main" and boards[0]["counts"]
    data = js(get("/api/board?b=main"))
    assert set(data["lanes"]) == set(web.LANES)
    ids = {c["id"] for lane in data["lanes"].values() for c in lane}
    assert {a, b} <= ids
    sample = next(c for lane in data["lanes"].values() for c in lane if c["id"] == a)
    for key in (
        "title",
        "status",
        "assignee",
        "priority",
        "cost",
        "run",
        "activity",
        "pr_number",
        "reason",
    ):
        assert key in sample
    assert data["daemon"] == {"alive": False, "version": None}
    assert {"accounts", "today_cost", "running", "schema", "board"} <= set(data)
    assert get("/api/board?b=nope")[0] == 404


def test_board_summarises_the_latest_run_and_a_running_cards_last_action(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int]
) -> None:
    rid = run_row(status="running")
    tid = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (rid,)).fetchone()[0]
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    card = next(c for c in js(get("/api/board?b=main"))["lanes"]["running"] if c["id"] == tid)
    assert card["run"]["status"] == "running" and card["model"] == "sonnet" and card["cost"] == 0.5
    assert card["activity"] == "Bash: echo 2"


def test_done_lane_is_capped_unless_asked(conn: sqlite3.Connection, get: Get, card: Card) -> None:
    for i in range(web.DONE_SHOWN + 3):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (card(f"c{i}"),))
    data = js(get("/api/board?b=main"))
    assert len(data["lanes"]["done"]) == web.DONE_SHOWN and data["done_total"] == web.DONE_SHOWN + 3
    assert len(js(get("/api/board?b=main&all=1"))["lanes"]["done"]) == web.DONE_SHOWN + 3


def test_card_has_every_column_runs_deps_and_comments(
    conn: sqlite3.Connection, get: Get, card: Card, run_row: Callable[..., int]
) -> None:
    rid = run_row(
        metadata='{"infrastructure": true}', model_usage='{"claude-x": {"inputTokens": 1}}'
    )
    tid = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (rid,)).fetchone()[0]
    parent = card("Parent")
    store.link(conn, parent, tid)
    conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, 'a', 'hi', 1)",
        (tid,),
    )
    d = js(get(f"/api/card?id={tid}"))
    assert d["task"]["id"] == tid and "workspace_kind" in d["task"] and d["task"]["touches"] == []
    assert d["parents_info"][0]["id"] == parent and d["comments"][0]["body"] == "hi"
    run = d["runs"][0]
    assert run["metadata"] == {"infrastructure": True} and run["model_usage"] == {
        "claude-x": {"inputTokens": 1}
    }
    assert run["stream_file"].endswith(".jsonl") and run["log_file"].endswith(".log")
    assert "stream_path" not in run and d["cost"] == 0.5 and d["links"] == {}
    assert get("/api/card?id=nope")[0] == 400
    assert get("/api/card?id=t_00000000")[0] == 404


def test_card_reads_a_jira_link_when_the_table_exists(
    conn: sqlite3.Connection, get: Get, card: Card
) -> None:
    tid = card("Linked")
    conn.execute(
        "INSERT INTO jira_links(task_id, jira_key, base_title, base_body, card_title, card_body, "
        "synced_status, synced_version, created_at) VALUES (?, 'LDC-1', 't', 'SECRET-BODY', 't', 'b', 'todo', 1, 1)",
        (tid,),
    )  # fmt: skip
    link = js(get(f"/api/card?id={tid}"))["links"]["jira"]
    assert link["jira_key"] == "LDC-1" and "base_body" not in link


def test_v6_run_fields_arrive_parsed_and_v5_runs_simply_lack_them(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int]
) -> None:
    old = run_row()
    new = run_row(
        advisor="on", advisor_calls=2, harness=json.dumps(HARNESS),
        params=json.dumps({"argv": ["claude", "-p", "<prompt>"], "env": {"A": "1"}, "init": INIT}),
    )  # fmt: skip
    tid_old = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (old,)).fetchone()[0]
    tid_new = conn.execute("SELECT task_id FROM task_runs WHERE id = ?", (new,)).fetchone()[0]
    run = js(get(f"/api/card?id={tid_new}"))["runs"][0]
    assert (
        run["harness"]["tools"] == {"Bash": 3}
        and run["params"]["init"]["model"] == "claude-sonnet-5-5"
    )
    assert run["advisor"] == "on"
    run = js(get(f"/api/card?id={tid_old}"))["runs"][0]
    assert run.get("harness") is None and run.get("params") is None


def test_stats_need_the_v6_columns_and_say_so_on_a_v5_database(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int]
) -> None:
    to_v5(conn)
    run_row()
    data = js(get("/api/stats?days=30&b=main"))
    assert "no such column" in data["unavailable"] and data["accounts"] == []
    to_v6(conn)
    data = js(get("/api/stats?days=30&b=main"))
    assert (
        data["days"] == 30 and data["cost_by_role"][0]["role"] == "smith" and data["accounts"] == []
    )
    assert js(get("/api/stats"))["days"] == 7
    assert get("/api/stats?days=0")[0] == 400
    assert get("/api/stats?days=x")[0] == 400
    assert get("/api/stats?b=nope")[0] == 404


# --- streams --------------------------------------------------------------------------------


def test_a_stream_line_of_an_odd_shape_is_shown_raw_not_a_server_error() -> None:
    line = b'{"type":"assistant","message":"x"}\n'
    assert [e["k"] for e in web.decode(line, 7)] == ["raw"]


def test_stream_events_are_readable_and_noise_is_dropped(
    get: Get, run_row: Callable[..., int]
) -> None:
    rid = run_row()
    d = js(get(f"/api/run/stream?id={rid}&offset=tail"))
    kinds = [e["k"] for e in d["events"]]
    assert kinds == [
        "init",
        "text",
        "tool",
        "result",
        "text",
        "tool",
        "result",
        "text",
        "tool",
        "result",
        "final",
    ]
    assert d["start"] == 0 and d["at_start"] and d["at_end"] and d["end"] == d["size"]
    tool = d["events"][2]
    assert tool["name"] == "Bash" and tool["label"] == "echo 0" and tool["id"] == "tu0"
    assert d["events"][3]["id"] == "tu0" and d["events"][3]["ok"] is True
    assert d["events"][0]["mcp_servers"][0]["status"] == "connected"
    assert d["events"][-1]["cost"] == 0.5 and d["status"] == "ended"


def test_stream_paging_forward_back_and_tail_align_to_lines(
    monkeypatch: pytest.MonkeyPatch, get: Get, run_row: Callable[..., int]
) -> None:
    monkeypatch.setattr(web, "PAGE_BYTES", 600)
    rid = run_row(stream_lines(30))
    everything = js(get(f"/api/run/stream?id={rid}&offset=0"))
    size = everything["size"]
    seen: list[str] = []
    off = 0
    for _ in range(200):
        page = js(get(f"/api/run/stream?id={rid}&offset={off}"))
        seen += [f"{e['at']}:{e['i']}" for e in page["events"]]
        if page["at_end"]:
            break
        assert page["end"] > off
        off = page["end"]
    ats = [int(s.split(":")[0]) for s in seen]
    assert len(set(seen)) == len(seen) and ats == sorted(ats) and off >= 0

    tail = js(get(f"/api/run/stream?id={rid}&offset=tail"))
    assert tail["end"] == size and not tail["at_start"] and tail["events"][0]["at"] == tail["start"]
    back: list[int] = []
    cursor = tail["start"]
    while cursor > 0:
        page = js(get(f"/api/run/stream?id={rid}&before={cursor}"))
        back = [e["at"] for e in page["events"]] + back
        assert page["start"] < cursor
        cursor = page["start"]
    assert set(back) | {e["at"] for e in tail["events"]} == {int(s.split(":")[0]) for s in seen}


def test_an_offset_in_the_middle_of_a_line_moves_to_the_next_line(
    get: Get, run_row: Callable[..., int]
) -> None:
    rid = run_row()
    first = js(get(f"/api/run/stream?id={rid}&offset=0"))
    second_at = first["events"][1]["at"]
    page = js(get(f"/api/run/stream?id={rid}&offset={second_at - 5}"))
    assert page["start"] == second_at


def test_an_offset_past_the_end_is_empty_not_an_error(
    get: Get, run_row: Callable[..., int]
) -> None:
    rid = run_row()
    size = js(get(f"/api/run/stream?id={rid}"))["size"]
    page = js(get(f"/api/run/stream?id={rid}&offset={size + 1000}"))
    assert page["events"] == [] and page["end"] == size and page["at_end"]
    assert get(f"/api/run/stream?id={rid}&offset=abc")[0] == 400


def test_a_half_written_last_line_waits_for_its_newline(
    get: Get, run_row: Callable[..., int]
) -> None:
    whole = jline({"type": "assistant", "message": {"content": [{"type": "text", "text": "one"}]}})
    rid = run_row(whole + '{"type":"assistant","message":{"content":[{"type":"te')
    page = js(get(f"/api/run/stream?id={rid}&offset=0"))
    assert [e["text"] for e in page["events"]] == ["one"] and page["end"] == len(whole)
    assert not page["at_end"]


def test_a_line_longer_than_a_page_still_comes_back_cut_per_field(
    monkeypatch: pytest.MonkeyPatch, get: Get, run_row: Callable[..., int]
) -> None:
    monkeypatch.setattr(web, "PAGE_BYTES", 1000)
    rid = run_row(stream_lines(1, big=50_000))
    page = js(get(f"/api/run/stream?id={rid}&offset=0"))
    assert page["end"] > 0
    result = next(e for e in page["events"] if e["k"] == "result")
    assert (
        len(result["text"]) == web.RESULT_CAP
        and result["trunc"] == 50_000
        and result["size"] == 50_000
    )
    full = js(get(f"/api/run/event?id={rid}&at={result['at']}"))["events"][0]
    assert len(full["text"]) == 50_000 and full["trunc"] is None
    assert get(f"/api/run/event?id={rid}&at={result['at'] + 1}")[0] == 400


def test_bad_lines_and_odd_blocks_do_not_break_a_page(
    get: Get, run_row: Callable[..., int]
) -> None:
    lines = "not json at all\n" + jline({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "is_error": True,
         "content": [{"type": "text", "text": "boom"}, {"type": "image", "source": {"data": "A" * 9000}}]},
    ]}}) + jline({"type": "assistant", "message": {"content": [
        {"type": "server_tool_use", "id": "s1", "name": "advisor", "input": {}},
        {"type": "advisor_tool_result", "tool_use_id": "s1", "content": {"type": "advisor_result", "text": "ok"}},
        {"type": "thinking", "thinking": ""},
        {"type": "mystery", "x": 1},
    ]}, "parent_tool_use_id": "tu9"}) + jline({"type": "system", "subtype": "hook_response",
                                            "hook_name": "Stop", "exit_code": 2, "outcome": "error", "output": "bad"})  # fmt: skip
    rid = run_row(lines)
    events = js(get(f"/api/run/stream?id={rid}&offset=0"))["events"]
    assert events[0]["k"] == "raw" and events[0]["text"] == "not json at all"
    res = events[1]
    assert (
        res["ok"] is False and res["text"] == "boom\n[image]" and "AAAA" not in json.dumps(events)
    )
    assert [e["k"] for e in events[2:6]] == ["tool", "advisor", "thinking", "raw"]
    assert all(e["sub"] == "tu9" for e in events[2:6])
    assert events[-1]["k"] == "hook" and events[-1]["exit"] == 2


def test_control_characters_and_ansi_are_cleaned(get: Get, run_row: Callable[..., int]) -> None:
    rid = run_row(
        jline(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "a\x1b[31mred\x1b[0m\x07b"}]},
            }
        )
    )
    assert js(get(f"/api/run/stream?id={rid}&offset=0"))["events"][0]["text"] == "aredb"


def test_log_pages_by_bytes_and_tail(
    monkeypatch: pytest.MonkeyPatch,
    get: Get,
    run_row: Callable[..., int],
    home: Path,
    conn: sqlite3.Connection,
) -> None:
    rid = run_row()
    log = Path(conn.execute("SELECT log_path FROM task_runs WHERE id = ?", (rid,)).fetchone()[0])
    log.write_text("".join(f"line {i:03d}\n" for i in range(100)))
    monkeypatch.setattr(web, "LOG_PAGE", 200)
    tail = js(get(f"/api/run/log?id={rid}&offset=tail"))
    assert (
        tail["text"].endswith("line 099\n") and tail["end"] == tail["size"] and not tail["at_start"]
    )
    back = js(get(f"/api/run/log?id={rid}&before={tail['start']}"))
    assert back["end"] == tail["start"] and back["text"].endswith("\n")
    fwd = js(get(f"/api/run/log?id={rid}&offset=0"))
    assert fwd["text"].startswith("line 000") and fwd["end"] <= 200 and fwd["at_start"]
    beyond = js(get(f"/api/run/log?id={rid}&offset=999999"))
    assert beyond["text"] == "" and beyond["at_end"]


def test_raw_downloads_the_files_as_text(get: Get, run_row: Callable[..., int]) -> None:
    rid = run_row()
    status, headers, body = get(f"/api/run/raw?id={rid}&kind=log")
    assert status == 200 and body.startswith(b"[kanban] line one")
    assert (
        headers["content-type"].startswith("text/plain")
        and "attachment" in headers["content-disposition"]
    )
    status, headers, body = get(f"/api/run/raw?id={rid}&kind=stream")
    assert status == 200 and body.count(b"\n") >= 5
    assert get(f"/api/run/raw?id={rid}&kind=stream", method="HEAD")[2] == b""
    assert get(f"/api/run/raw?id={rid}&kind=prompt")[0] == 404
    assert get(f"/api/run/raw?id={rid}&kind=../../etc/passwd")[0] == 400


# --- paths come from rows only --------------------------------------------------------------


def test_the_request_cannot_name_a_file(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int], tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n")
    rid = run_row()
    for q in (f"path={secret}", f"file={secret}", f"stream_path={secret}", f"log_path={secret}"):
        for route in ("stream", "log"):
            reply = get(f"/api/run/{route}?id={rid}&{q}")
            assert b"TOP-SECRET" not in reply[2]
        assert b"TOP-SECRET" not in get(f"/api/run/raw?id={rid}&kind=log&{q}")[2]
    assert get(f"/api/run/stream?id={secret}")[0] == 400
    assert get("/api/run/stream?id=424242")[0] == 404


@pytest.mark.parametrize("column", ["stream_path", "log_path"])
def test_a_row_pointing_outside_the_home_is_refused(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int], tmp_path: Path, column: str
) -> None:
    secret = tmp_path / "outside.jsonl"
    secret.write_text(
        jline(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "TOP-SECRET"}]}}
        )
    )
    rid = run_row()
    conn.execute(f"UPDATE task_runs SET {column} = ? WHERE id = ?", (str(secret), rid))
    for url in (f"/api/run/stream?id={rid}", f"/api/run/log?id={rid}", f"/api/run/raw?id={rid}&kind=stream",
                f"/api/run/raw?id={rid}&kind=log", f"/api/run/event?id={rid}&at=0"):  # fmt: skip
        reply = get(url)
        assert b"TOP-SECRET" not in reply[2], url
    card = js(
        get(
            f"/api/card?id={conn.execute('SELECT task_id FROM task_runs WHERE id = ?', (rid,)).fetchone()[0]}"
        )
    )
    assert card["runs"][0][column.replace("_path", "_file")] is None


def test_a_symlink_out_of_the_home_is_refused(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int], tmp_path: Path
) -> None:
    secret = tmp_path / "outside.log"
    secret.write_text("TOP-SECRET\n")
    rid = run_row()
    log = Path(conn.execute("SELECT log_path FROM task_runs WHERE id = ?", (rid,)).fetchone()[0])
    log.unlink()
    log.symlink_to(secret)
    assert b"TOP-SECRET" not in get(f"/api/run/log?id={rid}")[2]
    assert b"TOP-SECRET" not in get(f"/api/run/raw?id={rid}&kind=log")[2]


def test_a_prompt_file_is_served_only_from_inside_the_home(
    conn: sqlite3.Connection, get: Get, run_row: Callable[..., int], home: Path, tmp_path: Path
) -> None:
    inside = home / "runs" / "main" / "prompt-1.txt"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("THE PROMPT")
    outside = tmp_path / "prompt.txt"
    outside.write_text("TOP-SECRET")
    ok = run_row(params=json.dumps({"prompt_path": str(inside)}))
    bad = run_row(params=json.dumps({"prompt_path": str(outside)}))
    assert get(f"/api/run/raw?id={ok}&kind=prompt")[2] == b"THE PROMPT"
    assert get(f"/api/run/raw?id={bad}&kind=prompt")[0] == 404


# --- escaping -------------------------------------------------------------------------------


def test_markup_in_cards_comes_back_as_json_text_never_as_html(
    conn: sqlite3.Connection, get: Get, card: Card
) -> None:
    hostile = "<script>alert(1)</script><img src=x onerror=alert(2)>"
    tid = card(hostile, body=hostile)
    conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at) VALUES (?, ?, ?, 1)",
        (tid, hostile, hostile),
    )
    status, headers, body = get(f"/api/card?id={tid}")
    assert status == 200 and headers["content-type"] == "application/json"
    assert json.loads(body)["task"]["title"] == hostile
    assert get("/<script>")[0] == 404


def test_the_shipped_page_has_no_inline_script_or_style(get: Get) -> None:
    html = get("/")[2].decode()
    assert "style=" not in html
    for tag in html.split("<script")[1:]:
        assert "src=" in tag.split(">")[0]
    for name in ("app.js", "app.css"):
        assert get(f"/assets/{name}")[0] == 200
    js_src = get("/assets/util.js")[2].decode()
    assert "innerHTML" not in js_src


# --- the verb -------------------------------------------------------------------------------


def test_serve_prints_the_url_and_refuses_an_unknown_board(
    conn: sqlite3.Connection,
    board: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_forever(self: web.Server, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(web.Server, "serve_forever", fake_forever)
    assert cli.main(["web", "--port", "0", "--board", "main"]) == 0
    out = capsys.readouterr().out
    assert "read-only board at http://127.0.0.1:" in out and "/#/board/main" in out
    assert cli.main(["web", "--port", "0", "--board", "nope"]) != 0
    assert cli.main(["web", "--port", "70000"]) != 0
