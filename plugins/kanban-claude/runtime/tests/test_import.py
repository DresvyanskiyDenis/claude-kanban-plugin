"""TST-M: the legacy importer (MIG-01..07) against a synthetic database built here."""

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from kanban_claude import importer, store

from .conftest import Kb

DDL = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
  status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL,
  started_at INTEGER, completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch',
  workspace_path TEXT, branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
  tenant TEXT, result TEXT, idempotency_key TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
  worker_pid INTEGER, worker_started_at TEXT, last_failure_error TEXT, max_runtime_seconds INTEGER,
  last_heartbeat_at INTEGER, current_run_id INTEGER, skills TEXT, model_override TEXT,
  provider_override TEXT, reasoning_effort TEXT, max_retries INTEGER,
  goal_mode INTEGER NOT NULL DEFAULT 0, session_id TEXT, block_kind TEXT,
  block_recurrences INTEGER NOT NULL DEFAULT 0);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
  PRIMARY KEY (parent_id, child_id));
CREATE TABLE task_comments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
  step_key TEXT, status TEXT NOT NULL, claim_lock TEXT, worker_pid INTEGER,
  max_runtime_seconds INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT,
  summary TEXT, metadata TEXT, error TEXT);
CREATE TABLE task_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
  filename TEXT NOT NULL, stored_path TEXT NOT NULL, content_type TEXT,
  size INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT, created_at INTEGER NOT NULL);
CREATE TABLE kanban_notify_subs (task_id TEXT NOT NULL, platform TEXT NOT NULL,
  chat_id TEXT NOT NULL);
"""
PR41 = "https://github.com/acme/widget/pull/41"
PR42 = "https://github.com/acme/widget/pull/42"


class Legacy(NamedTuple):
    root: Path
    repo: Path
    wal: tuple[Path, ...]


def hdb(path: Path, held: list[sqlite3.Connection], **tables: list[dict[str, Any]]) -> None:
    """A legacy board database: WAL, never checkpointed, kept open so `-wal`/`-shm` stay."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.executescript(DDL)
    for table, rows in tables.items():
        for row in rows:
            cols = ", ".join(row)
            marks = ", ".join("?" * len(row))
            c.execute(f"INSERT INTO {table}({cols}) VALUES ({marks})", tuple(row.values()))
    held.append(c)


def card(tid: str, status: str, **kw: Any) -> dict[str, Any]:
    return {"id": tid, "title": f"card {tid}", "status": status, "created_at": 1000, **kw}


def ev(
    task: str, kind: str, at: int, payload: Any = None, run: int | None = None
) -> dict[str, Any]:
    body = payload if payload is None else json.dumps(payload)
    return {"task_id": task, "run_id": run, "kind": kind, "payload": body, "created_at": at}


def run_row(task: str, start: int, status: str, outcome: str | None, **kw: Any) -> dict[str, Any]:
    return {"task_id": task, "status": status, "outcome": outcome, "started_at": start, **kw}


@pytest.fixture(autouse=True)
def gateway_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importer, "gateway_running", lambda: False)


@pytest.fixture
def legacy(env: Path) -> Iterator[Legacy]:
    root = env / "legacy"
    repo = env / "repos" / "legacy"
    repo.mkdir(parents=True)
    held: list[sqlite3.Connection] = []
    boards = root / "kanban" / "boards"

    d1, d2 = "t_d0000001", "t_d0000002"
    hdb(
        root / "kanban.db",
        held,
        tasks=[
            card(
                d1,
                "done",
                assignee="smith",
                completed_at=2000,
                result="all shipped",
                workspace_kind="dir",
                workspace_path=str(repo),
                skills='["python"]',
                model_override="sonnet",
            ),
            card(d2, "archived"),
        ],
        task_runs=[
            run_row(
                d1,
                1000,
                "completed",
                "review_requested",
                id=1,
                profile="smith",
                ended_at=1100,
                summary=f"opened {PR41}, then {PR42}",
            ),
            run_row(
                d1,
                1200,
                "completed",
                "completed",
                id=2,
                profile="warden",
                ended_at=1300,
                metadata='{"a": 1}',
            ),
            run_row(d1, 900, "rate_limited", "rate_limited", id=3, profile="smith", ended_at=950),
            run_row(d1, 1400, "gave_up", "gave_up", id=4, profile="smith", ended_at=1450),
        ],
        task_events=[
            ev(d1, "created", 1000),
            ev(d1, "rate_limited", 950, {"reason": "quota"}, run=3),
            ev(d1, "claimed", 1000, {"run_id": 1, "lock": "h:1"}, run=1),
            ev(d1, "claimed", 1200, {"run_id": 2, "source_status": "review", "actor": "warden"}, 2),
            ev(d1, "edited", 1500),
            ev(d1, "heartbeat", 1600, {"run_id": 99}, run=99),
            ev("t_dead0000", "created", 1),
        ],
        task_comments=[
            {"task_id": d1, "author": "alice", "body": "see .../pull/40", "created_at": 1050},
            {"task_id": "t_dead0000", "author": "bob", "body": "orphan", "created_at": 5},
        ],
        task_links=[
            {"parent_id": d1, "child_id": d2},
            {"parent_id": d1, "child_id": "t_ffffffff"},
            {"parent_id": d2, "child_id": d2},
        ],
    )

    a1, a2, a3, a4, a5 = (f"t_a000000{i}" for i in range(1, 6))
    alpha = boards / "alpha"
    attach = alpha / "attachments" / a4
    attach.mkdir(parents=True)
    (attach / "notes.txt").write_bytes(b"hello")
    (attach / "rel.txt").write_bytes(b"relative")
    (env / "outside.txt").write_bytes(b"secret")
    hdb(
        alpha / "kanban.db",
        held,
        tasks=[
            card(a1, "running", assignee="smith", started_at=3000, claim_lock="h:2", worker_pid=5),
            card(a2, "scheduled"),
            card(a3, "blocked", block_kind="dependency"),
            card(a4, "review", workspace_kind="worktree", workspace_path=str(repo / "gone")),
            card(a5, "ready", assignee="smith", workspace_kind="scratch", workspace_path="/x"),
        ],
        task_runs=[
            run_row(a1, 3000, "running", None, id=1, profile="smith"),
            run_row(a3, 1000, "reclaimed", "reclaimed", id=2, profile="smith", ended_at=1010),
            run_row(a3, 1020, "stale", None, id=3, profile="smith"),
        ],
        task_events=[
            ev(a1, "claimed", 3000, {"run_id": 1, "lock": "h:2"}, run=1),
            ev(a2, "scheduled", 1000, {"reason": "wait for Monday"}),
            ev(a3, "reclaimed", 1010, None, run=2),
        ],
        task_comments=[{"task_id": a4, "author": "bob", "body": "hello", "created_at": 1000}],
        task_attachments=[
            {
                "task_id": a4,
                "filename": "notes.txt",
                "stored_path": str(attach / "notes.txt"),
                "content_type": "text/plain",
                "size": 5,
                "uploaded_by": "bob",
                "created_at": 1000,
            },
            {
                "task_id": a4,
                "filename": "passwd",
                "stored_path": "../../../../outside.txt",
                "size": 6,
                "created_at": 1001,
            },
            {
                "task_id": a4,
                "filename": "ghost.txt",
                "stored_path": "attachments/gone.txt",
                "size": 1,
                "created_at": 1002,
            },
            {
                "task_id": a4,
                "filename": "rel.txt",
                "stored_path": f"attachments/{a4}/rel.txt",
                "size": 8,
                "created_at": 1003,
            },
        ],
    )
    meta = {
        "slug": "alpha",
        "name": "Alpha",
        "default_workdir": str(repo),
        "created_at": 1700000000,
    }
    (alpha / "board.json").write_text(json.dumps({**meta, "archived": False, "icon": "x"}))

    hdb(boards / "beta" / "kanban.db", held, tasks=[card("t_b0000001", "todo")])
    meta_beta = {"slug": "beta", "default_workdir": str(env / "nowhere"), "created_at": 1700000001}
    (boards / "beta" / "board.json").write_text(json.dumps(meta_beta))
    hdb(boards / "gamma" / "kanban.db", held, tasks=[card("t_c0000001", "done")])
    meta_gamma = {"slug": "gamma", "default_workdir": str(repo), "archived": True}
    (boards / "gamma" / "board.json").write_text(json.dumps(meta_gamma))
    hdb(
        boards / "_archived" / "old-1700000000" / "kanban.db",
        held,
        tasks=[card("t_e0000001", "done")],
    )
    hdb(boards / "Bad_Slug" / "kanban.db", held, tasks=[card("t_f0000001", "todo")])
    hdb(env / "elsewhere" / "kanban.db", held, tasks=[card("t_f0000002", "todo")])
    (boards / "evil").symlink_to(env / "elsewhere")

    side = root / "kanban"
    (side / "grants").mkdir()
    grant = {"allow": ["Bash(git push origin feature/x)"], "approved": "owner, 2026-09-01"}
    (side / "grants" / f"{a4}.json").write_text(json.dumps(grant))
    (side / "grants" / "t_dead0000.json").write_text(json.dumps(grant))
    (side / "grants" / f"{a5}.json").write_text(json.dumps({"allow": [], "approved": "x"}))
    run1 = {
        "board": "default", "task": d1, "run_id": "1", "model": "sonnet", "effort": "high",
        "session_id": "sess-1", "resumed": False, "exit_code": 0, "claude_exit_code": 0,
        "subtype": "success", "is_error": False, "api_error_status": None, "num_turns": 7,
        "total_cost_usd": 0.5, "session_cost_usd": 1.25, "stream": "/streams/t_d0000001.run1.jsonl",
    }  # fmt: skip
    run2 = {**run1, "run_id": 2, "session_id": "sess-2", "total_cost_usd": 0.75, "num_turns": 3}
    lines = [json.dumps(run1), json.dumps({**run1, "run_id": "77"}), "not json", json.dumps(run2)]
    (side / "claude-runs.jsonl").write_text("\n".join(lines))
    resets = int(time.time()) + 3600
    (side / "claude-limit.json").write_text(json.dumps({"resetsAt": resets}))
    (root / "config.yaml").write_text(
        "model:\n  default: x\nkanban:\n"
        "  dispatch_profiles: [atlassian, designer, scout, smith, warden]\n"
        "  auto_decompose: false  # comment\n  max_in_progress: 5\n  default_assignee: ghost\n"
        "other:\n  max_in_progress: 99\n"
    )
    wal = tuple(root / f"kanban.db{s}" for s in ("", "-wal", "-shm"))
    yield Legacy(root, repo, wal)
    for c in held:
        c.close()


def run_import(
    kb: Kb, legacy: Legacy, *extra: str, board: str | None = None
) -> tuple[int, Any, str]:
    pick = ["--board", board] if board else ["--all"]
    args = ["import-hermes", "--from", str(legacy.root), *pick, "--json", *extra]
    code, out, err = kb(*args)
    return code, (json.loads(out) if out.strip().startswith("{") else out), err


def fetch(sql: str, *args: Any) -> list[sqlite3.Row]:
    conn = store.connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def real(kb: Kb, legacy: Legacy) -> Any:
    code, data, err = run_import(kb, legacy, "--force", "--default-workdir", str(legacy.repo))
    assert code == 0, err
    return data


def test_m1_dry_run_reports_counts_and_writes_nothing(kb: Kb, legacy: Legacy) -> None:
    code, data, _ = run_import(kb, legacy, "--dry-run", "--default-workdir", str(legacy.repo))
    assert code == 0 and data["dry_run"] is True
    assert data["counts"] == {
        "boards": 4, "tasks": 9, "task_runs": 7, "task_events": 14, "task_comments": 5,
        "task_links": 1, "task_attachments": 2, "grants": 1,
    }  # fmt: skip
    assert data["statuses"] == {
        "archived": 1, "blocked": 2, "done": 2, "ready": 2, "review": 1, "todo": 1,
    }  # fmt: skip
    assert data["blockers"] == ["card alpha/t_a0000001 is running"]
    assert fetch("SELECT 1 FROM tasks") == [] and fetch("SELECT 1 FROM boards") == []


def test_m1_dry_run_lists_every_dropped_row(kb: Kb, legacy: Legacy) -> None:
    _, data, _ = run_import(kb, legacy, "--dry-run", "--default-workdir", str(legacy.repo))
    text = "\n".join(data["dropped"])
    for needle in (
        "t_dead0000",
        "t_ffffffff",
        "outside",
        "gone.txt",
        "line 2",
        "line 3",
        "Bad_Slug",
    ):
        assert needle in text
    assert "t_d0000002 -> t_d0000002" in text


def test_m2_ids_and_sessions_survive(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    ids = {r["id"] for r in fetch("SELECT id FROM tasks")}
    assert {"t_d0000001", "t_a0000004", "t_b0000001"} <= ids
    run = fetch("SELECT * FROM task_runs WHERE session_id = 'sess-1'")[0]
    assert (run["task_id"], run["model"], run["effort"], run["cost_usd"], run["resumed"]) == (
        "t_d0000001", "sonnet", "high", 0.5, 0,
    )  # fmt: skip
    assert (run["session_cost_usd"], run["num_turns"], run["is_error"]) == (1.25, 7, 0)
    assert run["stream_path"] == "/streams/t_d0000001.run1.jsonl"


def test_run_log_matches_string_and_int_run_ids(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    got = fetch(
        "SELECT started_at, session_id, cost_usd, num_turns FROM task_runs WHERE session_id IS NOT NULL ORDER BY started_at"
    )
    assert [tuple(r) for r in got] == [(1000, "sess-1", 0.5, 7), (1200, "sess-2", 0.75, 3)]


def test_m3_source_files_are_untouched(kb: Kb, legacy: Legacy) -> None:
    before = {p: p.stat().st_mtime_ns for p in legacy.wal}
    sizes = {p: p.stat().st_size for p in legacy.wal}
    assert all(p.exists() for p in legacy.wal)
    run_import(kb, legacy, "--dry-run", "--default-workdir", str(legacy.repo))
    real(kb, legacy)
    assert {p: p.stat().st_mtime_ns for p in legacy.wal} == before
    assert {p: p.stat().st_size for p in legacy.wal} == sizes


def test_m4_non_dispatchable_default_assignee_becomes_null(kb: Kb, legacy: Legacy) -> None:
    data = real(kb, legacy)
    conn = store.connect()
    assert store.cfg(conn, "dispatch.default_assignee") is None
    assert store.cfg(conn, "dispatch.max_in_progress") == 5
    assert store.cfg(conn, "dispatch.roles") == ["designer", "scout", "smith", "warden"]
    conn.close()
    assert any("ghost" in w for w in data["warnings"])
    assert any("atlassian" in w for w in data["warnings"])


def test_m4_block_list_and_dispatchable_default_assignee(kb: Kb, legacy: Legacy) -> None:
    (legacy.root / "config.yaml").write_text(
        "kanban:\n  default_assignee: scout\n  dispatch_profiles:\n    - scout\n    - smith\n"
    )
    real(kb, legacy)
    conn = store.connect()
    assert store.cfg(conn, "dispatch.default_assignee") == "scout"
    assert store.cfg(conn, "dispatch.roles") == ["scout", "smith"]
    conn.close()


def test_m5_grants_arrive_revoked_and_yes_never_activates(kb: Kb, legacy: Legacy) -> None:
    code, data, _ = run_import(
        kb, legacy, "--dry-run", "--yes", "--default-workdir", str(legacy.repo)
    )
    assert data["grants"] == ["t_a0000004: 1 rule(s), imported revoked"]
    assert any("t_dead0000.json" in d for d in data["dropped"])
    assert any("t_a0000005.json" in d for d in data["dropped"])
    real_args = ("--force", "--yes", "--default-workdir", str(legacy.repo))
    assert run_import(kb, legacy, *real_args)[0] == 0
    rows = fetch("SELECT task_id, revoked_at, rules FROM grants")
    assert [r["task_id"] for r in rows] == ["t_a0000004"] and rows[0]["revoked_at"] is not None
    assert json.loads(rows[0]["rules"]) == ["Bash(git push origin feature/x)"]
    assert code == 0


def test_m6_escapes_are_dropped_or_refused(kb: Kb, legacy: Legacy) -> None:
    data = real(kb, legacy)
    assert fetch("SELECT 1 FROM tasks WHERE id IN ('t_f0000001', 't_f0000002')") == []
    assert any("Bad_Slug" in d for d in data["dropped"])
    assert any("evil" in d and "outside" in d for d in data["dropped"])
    names = sorted(r["filename"] for r in fetch("SELECT filename FROM task_attachments"))
    assert names == ["notes.txt", "rel.txt"]
    for bad in ("../etc", "Upper"):
        code, _, err = run_import(kb, legacy, board=bad)
        assert code == 4 and "board slug" in err


def test_m7_orphans_are_dropped_and_listed(kb: Kb, legacy: Legacy) -> None:
    data = real(kb, legacy)
    assert fetch("SELECT 1 FROM task_events WHERE task_id = 't_dead0000'") == []
    assert fetch("SELECT 1 FROM task_comments WHERE task_id = 't_dead0000'") == []
    assert [(r["parent_id"], r["child_id"]) for r in fetch("SELECT * FROM task_links")] == [
        ("t_d0000001", "t_d0000002")
    ]
    drops = "\n".join(data["dropped"])
    assert "task_events default#7" in drops and "task_comments default#2" in drops
    assert fetch("PRAGMA foreign_key_check") == []


def test_runs_are_renumbered_and_events_follow(kb: Kb, legacy: Legacy) -> None:
    data = real(kb, legacy)
    runs = fetch("SELECT id, task_id, n, role, kind, outcome, status FROM task_runs ORDER BY id")
    d1 = [
        (r["n"], r["role"], r["kind"], r["outcome"]) for r in runs if r["task_id"] == "t_d0000001"
    ]
    assert d1 == [
        (1, "smith", "work", "quota"), (2, "smith", "work", "review_requested"),
        (3, "warden", "review", "completed"), (4, "smith", "work", "crashed"),
    ]  # fmt: skip
    assert len({r["id"] for r in runs}) == 7 and {r["status"] for r in runs} == {"ended"}
    review = next(r["id"] for r in runs if r["kind"] == "review")
    claimed = fetch(
        "SELECT run_id, payload FROM task_events WHERE kind = 'claimed' AND actor = 'warden'"
    )
    assert claimed[0]["run_id"] == review and json.loads(claimed[0]["payload"])["run_id"] == review
    beat = fetch("SELECT run_id, payload FROM task_events WHERE kind = 'heartbeat'")[0]
    assert beat["run_id"] is None and json.loads(beat["payload"])["run_id"] is None
    assert any("2 event run reference" in w for w in data["warnings"])


def test_events_are_global_chronological(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    rows = fetch("SELECT id, board, created_at, kind FROM task_events ORDER BY id")
    stamps = [r["created_at"] for r in rows]
    assert stamps == sorted(stamps)
    tie = [r["board"] for r in rows if r["created_at"] == 1000]
    assert tie == sorted(tie) and {"alpha", "default"} <= set(tie)
    edited = next(r for r in rows if r["kind"] == "edited")
    payload = fetch("SELECT payload FROM task_events WHERE id = ?", edited["id"])[0]["payload"]
    assert json.loads(payload) == {"fields": None, "previous": None, "source": "hermes_dashboard"}


def test_status_and_outcome_mapping(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    task = {r["id"]: r for r in fetch("SELECT * FROM tasks")}
    assert task["t_a0000001"]["status"] == "ready"
    assert (task["t_a0000002"]["status"], task["t_a0000002"]["block_kind"]) == (
        "blocked",
        "needs_input",
    )
    assert (task["t_a0000003"]["status"], task["t_a0000003"]["block_kind"]) == (
        "blocked",
        "needs_input",
    )
    notes = fetch("SELECT body FROM task_comments WHERE task_id = 't_a0000002'")
    assert [r["body"] for r in notes] == ["SCHEDULED: wait for Monday"]
    a3 = [r["outcome"] for r in fetch("SELECT outcome FROM task_runs WHERE task_id = 't_a0000003'")]
    assert a3 == ["interrupted", "interrupted"]


def test_running_cards_need_force_and_come_back_ready(kb: Kb, legacy: Legacy) -> None:
    code, _, err = run_import(kb, legacy, "--default-workdir", str(legacy.repo))
    assert code == 4 and "t_a0000001 is running" in err and "nothing was written" in err
    assert fetch("SELECT 1 FROM tasks") == []
    real(kb, legacy)
    run = fetch("SELECT id, status, outcome, ended_at FROM task_runs WHERE task_id = 't_a0000001'")[
        0
    ]
    assert (run["status"], run["outcome"]) == ("ended", "interrupted") and run["ended_at"]
    interrupted = fetch("SELECT run_id, actor FROM task_events WHERE kind = 'interrupted'")
    assert [(r["run_id"], r["actor"]) for r in interrupted] == [(run["id"], importer.AUTHOR)]
    body = fetch("SELECT body FROM task_comments WHERE task_id = 't_a0000001'")[0]["body"]
    assert body.startswith("Imported while running")
    assert fetch("SELECT 1 FROM task_runs WHERE status = 'running'") == []


def test_gateway_running_blocks_unless_forced(
    kb: Kb, legacy: Legacy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importer, "gateway_running", lambda: True)
    code, data, _ = run_import(kb, legacy, "--dry-run", "--default-workdir", str(legacy.repo))
    assert code == 0 and any("gateway is running" in b for b in data["blockers"])
    code, _, err = run_import(kb, legacy, "--default-workdir", str(legacy.repo))
    assert code == 4 and "gateway is running" in err
    assert real(kb, legacy)["dry_run"] is False


def test_card_fields_pr_and_result(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    t = {r["id"]: r for r in fetch("SELECT * FROM tasks")}
    d1 = t["t_d0000001"]
    assert (d1["implementer"], d1["pr_number"], d1["pr_url"]) == ("smith", 42, PR42)
    assert (d1["version"], d1["protocol_violations"], d1["updated_at"]) == (1, 0, 1600)
    assert json.loads(d1["skills"]) == ["python"] and d1["max_runtime_seconds"] == 7200
    results = fetch(
        "SELECT author, body, created_at FROM task_comments WHERE body LIKE '[result]%'"
    )
    assert [(r["author"], r["body"], r["created_at"]) for r in results] == [
        (importer.AUTHOR, "[result] all shipped", 2000)
    ]
    assert (
        t["t_a0000004"]["worktree_state"] == "removed" and t["t_d0000001"]["worktree_state"] is None
    )
    assert (t["t_a0000005"]["workspace_path"], t["t_a0000005"]["board"]) == (None, "alpha")
    assert t["t_a0000005"]["max_runtime_seconds"] == 7200


def test_boards_default_workdir_and_archival(kb: Kb, legacy: Legacy) -> None:
    data = real(kb, legacy)
    boards = {r["slug"]: r for r in fetch("SELECT * FROM boards")}
    assert set(boards) == {"default", "alpha", "beta", "gamma"}
    assert boards["alpha"]["archived_at"] is None and boards["alpha"]["created_at"] == 1700000000
    assert boards["alpha"]["name"] == "Alpha" and boards["default"]["archived_at"] is None
    assert boards["beta"]["archived_at"] and boards["beta"]["default_workdir"] is None
    assert boards["gamma"]["archived_at"]
    assert any("board beta" in w and "cannot dispatch" in w for w in data["warnings"])
    code, data, _ = run_import(kb, legacy, "--dry-run")
    assert any("board default" in w and "--default-workdir" in w for w in data["warnings"])
    assert code == 0


def test_attachment_blobs_are_copied(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    rows = fetch("SELECT filename, stored_path, size FROM task_attachments ORDER BY id")
    home = Path(os.environ["KANBAN_HOME"]) / "attachments" / "t_a0000004"
    assert [Path(r["stored_path"]).parent for r in rows] == [home, home]
    assert [Path(r["stored_path"]).read_bytes() for r in rows] == [b"hello", b"relative"]
    assert all(Path(r["stored_path"]).stat().st_mode & 0o777 == 0o600 for r in rows)
    assert (legacy.root / "kanban" / "boards" / "alpha" / "attachments").is_dir()


def test_future_limit_sets_hard_until(kb: Kb, legacy: Legacy) -> None:
    real(kb, legacy)
    conn = store.connect()
    state = store.state_get(conn, f"account:{Path.home() / '.claude'}")
    conn.close()
    assert state["hard_until"] > time.time() and state["auth_paused"] is None


def test_expired_limit_is_ignored(kb: Kb, legacy: Legacy) -> None:
    (legacy.root / "kanban" / "claude-limit.json").write_text('{"resetsAt": 5}')
    real(kb, legacy)
    assert fetch("SELECT key FROM state WHERE key LIKE 'account:%'") == []


def test_selection_is_required_and_boards_are_not_overwritten(kb: Kb, legacy: Legacy) -> None:
    code, _, err = kb("import-hermes", "--from", str(legacy.root))
    assert code == 2 and "--board SLUG or --all" in err
    code, _, err = run_import(kb, legacy, board="nonesuch")
    assert code == 3 and "nonesuch" in err
    real(kb, legacy)
    code, _, err = run_import(kb, legacy, "--force", board="alpha")
    assert code == 4 and "already exists" in err


def test_archived_boards_only_when_named(kb: Kb, legacy: Legacy) -> None:
    code, data, err = run_import(kb, legacy, "--default-workdir", str(legacy.repo), board="old")
    assert code == 0, err
    assert data["counts"]["tasks"] == 1 and data["counts"]["boards"] == 1
    assert [r["id"] for r in fetch("SELECT id FROM tasks")] == ["t_e0000001"]


def test_id_collisions_fail_loudly(kb: Kb, legacy: Legacy) -> None:
    held: list[sqlite3.Connection] = []
    hdb(
        legacy.root / "kanban" / "boards" / "zeta" / "kanban.db",
        held,
        tasks=[card("t_d0000001", "todo")],
    )
    code, _, err = run_import(kb, legacy, "--force", "--default-workdir", str(legacy.repo))
    for c in held:
        c.close()
    assert code == 4 and "t_d0000001" in err and "'default'" in err and "'zeta'" in err
    assert fetch("SELECT 1 FROM tasks") == []


def test_a_legacy_triage_card_keeps_a_missing_runtime_null(kb: Kb, legacy: Legacy) -> None:
    held: list[sqlite3.Connection] = []
    triage = [card("t_c0000001", "triage"), card("t_c0000002", "triage", max_runtime_seconds=900)]
    ready = card("t_c0000003", "ready", assignee="scout")
    hdb(legacy.root / "kanban" / "boards" / "zeta" / "kanban.db", held, tasks=[*triage, ready])
    code, _, err = run_import(kb, legacy, "--default-workdir", str(legacy.repo), board="zeta")
    for c in held:
        c.close()
    assert code == 0, err
    got = {r["id"]: r["max_runtime_seconds"] for r in fetch("SELECT * FROM tasks")}
    assert got == {"t_c0000001": None, "t_c0000002": 900, "t_c0000003": 1800}


def test_collision_with_the_target_is_refused(kb: Kb, legacy: Legacy) -> None:
    conn = store.connect()
    store.board_create(conn, "mine", default_workdir=str(legacy.repo))
    with store.tx(conn):
        conn.execute(
            "INSERT INTO tasks(id, board, title, status, max_runtime_seconds, created_at, updated_at) "
            "VALUES ('t_d0000001', 'mine', 'x', 'todo', 60, 1, 1)"
        )
    conn.close()
    code, _, err = run_import(kb, legacy, "--default-workdir", str(legacy.repo), board="default")
    assert code == 4 and "'mine'" in err


def test_failed_import_rolls_back_and_removes_blobs(
    kb: Kb, legacy: Legacy, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "state_put", boom)
    with pytest.raises(RuntimeError):
        run_import(kb, legacy, "--force", "--default-workdir", str(legacy.repo))
    assert fetch("SELECT 1 FROM tasks") == [] and fetch("SELECT 1 FROM task_events") == []
    blobs = Path(os.environ["KANBAN_HOME"]) / "attachments"
    assert [p for p in blobs.rglob("*") if p.is_file()] == []


def test_worker_mode_cannot_import(kb: Kb, legacy: Legacy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANBAN_RUN_ID", "1")
    code, _, err = kb("import-hermes", "--from", str(legacy.root), "--all", "--dry-run")
    assert code == 5, err
    assert not (Path(os.environ["KANBAN_HOME"]) / "kanban.db").exists()


def test_text_output_ends_with_decommission_commands(kb: Kb, legacy: Legacy) -> None:
    code, out, _ = kb(
        "import-hermes", "--from", str(legacy.root), "--all", "--force",
        "--default-workdir", str(legacy.repo),
    )  # fmt: skip
    assert code == 0 and out.startswith("imported")
    assert "launchctl bootout gui/$UID/ai.hermes.gateway" in out
    assert "launchctl bootout gui/$UID/kanban-hermes-claude.dashboard" in out


def test_kanban_section_parser() -> None:
    text = "a:\n  max_in_progress: 1\nkanban:\n  k: 'v'  # c\n  xs: [a, \"b\"]\n  ys:\n    - 1\n    - x\n  n:\n    deep: 2\n"
    assert importer.kanban_section(text) == {"k": "v", "xs": ["a", "b"], "ys": [1, "x"], "n": []}


def test_a_write_between_planning_and_applying_aborts_the_import(
    kb: Kb, legacy: Legacy, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_run_log = importer.plan_run_log

    def raced(*args: Any) -> None:
        conn = store.connect()
        try:
            store.board_create(conn, "raced", default_workdir=str(legacy.repo))
        finally:
            conn.close()
        plan_run_log(*args)

    monkeypatch.setattr(importer, "plan_run_log", raced)
    code, _, err = run_import(kb, legacy, "--force", "--default-workdir", str(legacy.repo))
    assert code == 4 and "database changed during planning" in err
    assert [r["slug"] for r in fetch("SELECT slug FROM boards")] == ["raced"]
    assert fetch("SELECT COUNT(*) FROM tasks")[0][0] == 0
