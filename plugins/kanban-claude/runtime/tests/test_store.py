import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import store, views
from kanban_claude.errors import Error, NotFound, Refused

Card = Callable[..., str]
PR_URL = "https://example.invalid/acme/proj/pull/1"
APPROVED = "WARDEN: APPROVED PR #1 @ abcdef1"


def status(conn: Any, tid: str) -> str:
    return str(conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()[0])


def row(conn: Any, tid: str) -> Any:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def kinds(conn: Any, tid: str) -> list[str]:
    q = "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id"
    return [r[0] for r in conn.execute(q, (tid,))]


def review_request(conn: Any, run: int) -> None:
    store.request_review(conn, run, "PR #1", pr_number=1, pr_url=PR_URL)


def reach(conn: Any, card: Card, target: str) -> str:
    if target == "triage":
        return card(triage=True)
    tid = card()
    if target == "ready":
        return tid
    if target == "todo":
        store.link(conn, card("parent"), tid)
        return tid
    run = store.claim(conn, tid)
    assert run
    if target == "running":
        return tid
    if target == "blocked":
        store.block(conn, run, "needs_input", "why")
    elif target == "review":
        review_request(conn, run)
    else:
        store.complete(conn, run, "ok")
    if target == "archived":
        store.archive(conn, tid)
    return tid


ALL = ["triage", "todo", "ready", "running", "review", "blocked", "done", "archived"]
OWNER_VERBS: dict[str, tuple[Callable[[Any, str], Any], set[str]]] = {
    "hold": (lambda c, t: store.hold(c, t, "r"), {"todo", "ready", "blocked"}),
    "unblock": (lambda c, t: store.unblock(c, t, "r"), {"blocked"}),
    "reopen_review": (lambda c, t: store.reopen_review(c, t, "r"), {"review"}),
    "reopen": (lambda c, t: store.reopen(c, t, "r"), {"done"}),
    "mark_done": (lambda c, t: store.mark_done(c, t, "r"), {"review", "blocked"}),
    "specify": (lambda c, t: store.specify(c, t, {}), {"triage"}),
    "stop": (lambda c, t: store.stop(c, t), {"running"}),
    "merge_retry": (lambda c, t: store.merge_retry(c, t), set()),
}


@pytest.mark.parametrize("src", ALL)
@pytest.mark.parametrize("verb", sorted(OWNER_VERBS))
def test_s1_s2_owner_verbs_legal_and_illegal(conn: Any, card: Card, verb: str, src: str) -> None:
    fn, legal = OWNER_VERBS[verb]
    tid = reach(conn, card, src)
    if src in legal:
        before = len(kinds(conn, tid))
        fn(conn, tid)
        assert len(kinds(conn, tid)) > before
    else:
        with pytest.raises(Refused) as e:
            fn(conn, tid)
        assert e.value.exit == 4


@pytest.mark.parametrize("src", ALL)
def test_s2_promote_only_from_todo_with_satisfied_parents(conn: Any, card: Card, src: str) -> None:
    tid = reach(conn, card, src)
    with pytest.raises(Refused):
        store.promote(conn, tid, "r")


def test_s1_promote_manual_and_auto(conn: Any, card: Card) -> None:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    assert status(conn, child) == "todo"
    assert store.promote_auto(conn, child) is False
    store.claim(conn, parent)
    run = conn.execute("SELECT id FROM task_runs WHERE task_id = ?", (parent,)).fetchone()[0]
    store.complete(conn, run, "ok")
    assert store.promote_auto(conn, child) is True
    assert kinds(conn, child)[-1] == "promoted"
    other = card("c2")
    store.link(conn, parent, other)
    assert status(conn, other) == "ready"
    store.hold(conn, other, "wait")
    assert status(conn, other) == "triage"


def test_s1_worker_path_review_and_changes(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    review_request(conn, run)
    t = row(conn, tid)
    assert (t["status"], t["assignee"], t["implementer"], t["pr_number"]) == (
        "review",
        "warden",
        "smith",
        1,
    )
    assert store.claim(conn, tid)
    run2 = conn.execute("SELECT id FROM task_runs WHERE status = 'running'").fetchone()[0]
    store.request_changes(conn, run2, "fix the thing")
    t = row(conn, tid)
    assert (t["status"], t["assignee"]) == ("ready", "smith")
    comments = [r["body"] for r in conn.execute("SELECT body FROM task_comments")]
    assert comments == ["CHANGES REQUESTED: fix the thing"]
    assert "changes_requested" in kinds(conn, tid)


def test_s1_review_approval_complete(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    review_request(conn, run)
    run2 = store.claim(conn, tid)
    assert run2
    with pytest.raises(Refused):
        store.complete(conn, run2, "looks fine to me")
    store.complete(conn, run2, APPROVED, default_branch="main")
    t = row(conn, tid)
    assert t["status"] == "done"
    assert t["merge_state"] is None
    assert "awaiting_owner_merge" in kinds(conn, tid)


def test_review_approval_into_non_default_base_sets_merge_pending(conn: Any, card: Card) -> None:
    tid = card(branch="feature/x", base="feature/base")
    run = store.claim(conn, tid)
    assert run
    review_request(conn, run)
    run2 = store.claim(conn, tid)
    assert run2
    store.complete(conn, run2, APPROVED, default_branch="main")
    t = row(conn, tid)
    assert (t["merge_state"], t["merge_attempts"]) == ("pending", 0)
    assert "merge_pending" in kinds(conn, tid)
    conn.execute("UPDATE tasks SET merge_state = 'failed', merge_attempts = 3 WHERE id = ?", (tid,))
    store.merge_retry(conn, tid)
    t = row(conn, tid)
    assert (t["merge_state"], t["merge_attempts"], t["status"]) == ("pending", 0, "done")


def test_parents_satisfied_requires_merge(conn: Any, card: Card) -> None:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    run = store.claim(conn, parent)
    assert run
    store.complete(conn, run, "ok")
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (parent,))
    assert not store.parents_satisfied(conn, child)
    assert store.parents_satisfied(conn, child, ignore_merge=True)
    store.promote(conn, child, "r", ignore_merge=True)
    assert status(conn, child) == "ready"
    conn.execute("UPDATE tasks SET merge_state = 'merged' WHERE id = ?", (parent,))
    assert store.parents_satisfied(conn, child)


def test_unblock_returns_review_card_to_review(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    review_request(conn, run)
    run2 = store.claim(conn, tid)
    assert run2
    store.block(conn, run2, "needs_input", "need a call")
    assert status(conn, tid) == "blocked"
    store.unblock(conn, tid, "done that")
    assert status(conn, tid) == "review"
    assert conn.execute("SELECT body FROM task_comments ORDER BY id DESC").fetchone()[0] == (
        "UNBLOCK: done that"
    )


def test_block_loop_goes_to_triage_on_third_same_kind(conn: Any, card: Card) -> None:
    tid = card()
    for expected in ("blocked", "blocked", "triage"):
        run = store.claim(conn, tid)
        assert run
        store.block(conn, run, "needs_input", "same reason")
        assert status(conn, tid) == expected
        if expected == "blocked":
            store.unblock(conn, tid, "again")
    assert "block_loop_detected" in kinds(conn, tid)
    t = row(conn, tid)
    assert t["block_recurrences"] == 2


def test_block_kind_change_resets_recurrences(conn: Any, card: Card) -> None:
    tid = card()
    for kind in ("needs_input", "needs_input", "capability"):
        run = store.claim(conn, tid)
        assert run
        store.block(conn, run, kind, "why")
        store.unblock(conn, tid, "go")
    assert row(conn, tid)["block_recurrences"] == 0


def test_s3_assignee_validation(conn: Any, board: str, card: Card) -> None:
    with pytest.raises(Refused):
        store.create(conn, board, "x", body="b", assignee="nobody")
    with pytest.raises(Refused, match="no assignee given") as missing:
        store.create(conn, board, "x", body="b")
    assert "None" not in str(missing.value)
    with pytest.raises(Refused):
        store.config_set(conn, "dispatch.default_assignee", "nobody")
    store.config_set(conn, "dispatch.default_assignee", "scout")
    assert status(conn, str(store.create(conn, board, "x", body="b")["id"])) == "ready"
    with pytest.raises(Refused):
        store.config_set(conn, "dispatch.roles", ["smith"])
    with pytest.raises(Refused):
        store.assign(conn, card(), "nobody")
    with pytest.raises(Refused):
        store.config_set(conn, "no.such.key", 1)


def test_s4_empty_body(conn: Any, board: str, card: Card) -> None:
    with pytest.raises(Refused):
        store.create(conn, board, "x", body="", assignee="smith")
    tid = card(body="keep me")
    with pytest.raises(Refused):
        store.edit(conn, tid, {"body": ""})
    assert row(conn, tid)["body"] == "keep me"
    with pytest.raises(Refused, match="body is required"):
        store.edit(conn, tid, {"body": ""}, allow_empty_body=True)
    parked = card(body="parked", triage=True)
    store.edit(conn, parked, {"body": ""}, allow_empty_body=True)
    ev = conn.execute("SELECT payload FROM task_events WHERE kind = 'edited'").fetchone()[0]
    assert "parked" in ev


def test_s5_idempotency_key(conn: Any, board: str, card: Card) -> None:
    first = store.create(conn, board, "x", body="b", assignee="smith", idempotency_key="k1")
    again = store.create(conn, board, "other", body="b", assignee="smith", idempotency_key="k1")
    assert again["id"] == first["id"] and again["existing"] is True
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    store.archive(conn, str(first["id"]))
    fresh = store.create(conn, board, "x", body="b", assignee="smith", idempotency_key="k1")
    assert fresh["id"] != first["id"]


def test_s6_complete_with_commits_refused_for_builders(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    with pytest.raises(Refused, match="request-review"):
        store.complete(conn, run, "all done", commits_ahead=2)
    assert status(conn, tid) == "running"
    store.complete(conn, run, "all done", commits_ahead=0)
    scout = card("scout card", assignee="scout")
    run = store.claim(conn, scout)
    assert run
    store.complete(conn, run, "report", commits_ahead=5)
    assert status(conn, scout) == "done"
    with pytest.raises(NotFound):
        store.complete(conn, 10_000, "x")


def test_s7_link_cycles_and_guards(conn: Any, card: Card) -> None:
    a, b, c = card("a"), card("b"), card("c")
    store.link(conn, a, b)
    store.link(conn, b, c)
    with pytest.raises(Refused, match="cycle"):
        store.link(conn, c, a)
    with pytest.raises(Refused):
        store.link(conn, a, a)
    assert store.link(conn, a, b).get("noop")
    free = card("free")
    store.claim(conn, free)
    with pytest.raises(Refused, match="running"):
        store.link(conn, a, free)
    store.unlink(conn, a, b)
    assert status(conn, b) == "ready"
    store.unlink(conn, b, c)
    assert status(conn, c) == "ready"


def test_link_open_parent_demotes_ready_child(conn: Any, card: Card) -> None:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    assert status(conn, child) == "todo"
    assert "dependency_wait" in kinds(conn, child)


def test_s8_one_active_run(conn: Any, card: Card) -> None:
    tid = card()
    results: list[int | None] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        c = store.connect()
        barrier.wait()
        results.append(store.claim(c, tid))
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r is not None for r in results) == [False, True]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_runs(task_id, n, kind, status, started_at) "
            "VALUES (?, 99, 'work', 'running', 1)",
            (tid,),
        )


def test_s9_migration_with_backup(
    env: Path, conn: Any, card: Card, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = card()
    conn.close()
    monkeypatch.setattr(
        store, "MIGRATIONS", [*store.MIGRATIONS, "ALTER TABLE tasks ADD COLUMN x TEXT;"]
    )
    c = store.connect()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    assert c.execute("SELECT x FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None
    backups = list((env / "kb-home" / "backups").glob("kanban-s1-*.db"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1
    assert old.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    old.close()
    assert store.migrate(c, env / "kb-home" / "backups") == 2
    assert len(list((env / "kb-home" / "backups").glob("*.db"))) == 1
    c.close()


def test_migration_refuses_newer_database(env: Path, conn: Any) -> None:
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(Error, match="newer"):
        store.connect()


def test_database_file_modes(env: Path, conn: Any) -> None:
    assert (env / "kb-home" / "kanban.db").stat().st_mode & 0o777 == 0o600
    assert (env / "kb-home").stat().st_mode & 0o777 == 0o700


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    t = [1_000_000]
    monkeypatch.setattr(store, "now", lambda: t[0])
    return t


def end(conn: Any, run: int, outcome: str, **kw: Any) -> bool:
    return store.end_run(conn, run, outcome, "retry", actor="runner", event_kind="run_ended", **kw)


def test_failure_rule_counts_then_gives_up(conn: Any, card: Card, clock: list[int]) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    assert end(conn, run, "timed_out", error="took too long")
    t = row(conn, tid)
    assert (t["status"], t["consecutive_failures"], t["retry_after"]) == ("ready", 1, 1_000_060)
    run = store.claim(conn, tid)
    assert run
    assert end(conn, run, "budget_exceeded", error="e" * 800)
    t = row(conn, tid)
    assert t["status"] == "blocked" and t["block_kind"] == "gave_up"
    assert len(t["last_failure_error"]) == 500
    assert "gave_up" in kinds(conn, tid)


def test_uncounted_outcomes_and_card_retry_limit(conn: Any, card: Card, clock: list[int]) -> None:
    tid = card(max_retries=3)
    for outcome in ("interrupted", "auth_lost", "quota"):
        run = store.claim(conn, tid)
        assert run
        assert end(conn, run, outcome)
        assert row(conn, tid)["consecutive_failures"] == 0
    for _ in range(2):
        run = store.claim(conn, tid)
        assert run
        end(conn, run, "crashed")
        clock[0] += 1000
    assert status(conn, tid) == "ready"


def test_review_run_failure_returns_to_review(conn: Any, card: Card, clock: list[int]) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    review_request(conn, run)
    run2 = store.claim(conn, tid)
    assert run2
    end(conn, run2, "interrupted")
    assert status(conn, tid) == "review"


def test_protocol_violation_streak(conn: Any, card: Card, clock: list[int]) -> None:
    tid = card()
    for expected in ("ready", "ready", "blocked"):
        run = store.claim(conn, tid)
        assert run
        end(conn, run, "protocol_violation", error="no terminal verb")
        assert status(conn, tid) == expected
    assert row(conn, tid)["protocol_violations"] == 3


def test_systemic_crashes_pause_dispatch_without_counting(
    conn: Any, board: str, card: Card, clock: list[int]
) -> None:
    ids = [card(f"c{i}") for i in range(3)]
    for tid in ids:
        run = store.claim(conn, tid)
        assert run
        end(conn, run, "crashed", error="spawn exploded")
        clock[0] += 10
    assert store.state_get(conn, "dispatch_paused")["reason"] == "systemic"
    assert [row(conn, t)["consecutive_failures"] for t in ids] == [1, 1, 0]
    assert store.resume(conn) is True
    assert store.state_get(conn, "dispatch_paused") is None


def test_sec5_second_end_run_after_stop_is_a_noop(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    store.stop(conn, tid)
    t = row(conn, tid)
    assert (t["status"], t["block_kind"]) == ("blocked", "stopped")
    assert end(conn, run, "completed") is False
    with pytest.raises(Refused, match="run ended"):
        store.complete(conn, run, "late")
    with pytest.raises(Refused, match="run ended"):
        store.block(conn, run, "needs_input", "late")
    assert status(conn, tid) == "blocked"
    r = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert (r["outcome"], r["stop_requested"]) == ("terminated", 1)


def test_sec5_terminal_verb_then_stop_and_double_verb(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    store.complete(conn, run, "done")
    assert store.complete(conn, run, "done").get("noop")
    with pytest.raises(Refused, match="run ended"):
        store.block(conn, run, "needs_input", "after")
    with pytest.raises(Refused):
        store.stop(conn, tid)
    assert status(conn, tid) == "done"
    assert kinds(conn, tid).count("completed") == 1


def test_reassign_reclaim_ends_run_and_resets_counters(conn: Any, card: Card) -> None:
    tid = card()
    run = store.claim(conn, tid)
    assert run
    with pytest.raises(Refused, match="reclaim"):
        store.assign(conn, tid, "designer")
    store.assign(conn, tid, "designer", reclaim=True)
    t = row(conn, tid)
    assert (t["status"], t["assignee"]) == ("ready", "designer")
    assert conn.execute("SELECT outcome FROM task_runs").fetchone()[0] == "terminated"


def test_reopen_demotes_ready_children_and_comments_on_others(conn: Any, card: Card) -> None:
    parent, ready_child, done_child = card("p"), card("c1"), card("c2")
    run = store.claim(conn, done_child)
    assert run
    store.complete(conn, run, "ok")
    run = store.claim(conn, parent)
    assert run
    store.complete(conn, run, "ok")
    store.link(conn, parent, ready_child)
    assert status(conn, ready_child) == "ready"
    store.reopen(conn, parent, "found a bug")
    assert status(conn, parent) == "ready"
    assert status(conn, ready_child) == "todo"
    assert "dependency_wait" in kinds(conn, ready_child)


def test_archive_and_rm(conn: Any, card: Card, env: Path) -> None:
    tid = card()
    store.attach(conn, tid, "../../etc/passwd", b"hello")
    store.archive(conn, tid)
    assert status(conn, tid) == "archived"
    store.archive(conn, tid, rm=True)
    with pytest.raises(NotFound):
        store.get_task(conn, tid)
    assert conn.execute("SELECT COUNT(*) FROM task_attachments").fetchone()[0] == 0
    assert not (env / "kb-home" / "attachments" / tid).exists()
    assert conn.execute("SELECT kind FROM task_events ORDER BY id DESC").fetchone()[0] == "deleted"


def test_attach_sanitizes_name_and_caps_size(conn: Any, card: Card) -> None:
    tid = card()
    res = store.attach(conn, tid, "../we ird/na$me.txt", b"x")
    assert res["filename"] == "na_me.txt"
    assert Path(res["path"]).stat().st_mode & 0o777 == 0o600
    with pytest.raises(Refused):
        store.attach(conn, tid, "big", b"0" * (store.MAX_ATTACH_BYTES + 1))


def test_edit_cas_and_running_guard(conn: Any, card: Card) -> None:
    tid = card()
    v = row(conn, tid)["version"]
    store.edit(conn, tid, {"title": "new"}, expect_version=v)
    with pytest.raises(Refused, match="changed"):
        store.edit(conn, tid, {"title": "newer"}, expect_version=v)
    store.claim(conn, tid)
    with pytest.raises(Refused, match="running"):
        store.edit(conn, tid, {"title": "x"})


def test_every_mutation_writes_an_event(conn: Any, card: Card) -> None:
    tid = card()
    n = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    store.comment(conn, tid, "hi")
    store.edit(conn, tid, {"priority": 3})
    store.hold(conn, tid, "r")
    store.assign(conn, tid, "scout")
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] >= n + 4


def test_grants_import_shape_and_wildcards(conn: Any, card: Card) -> None:
    tid = card()
    with pytest.raises(Refused):
        store.grant_add(conn, tid, ["Bash(*)"], "words · src · date")
    with pytest.raises(Refused):
        store.grant_add(conn, tid, ["Bash(git:*)"], "words · src · date")
    store.grant_add(conn, tid, ["Bash(npm publish)"], "words · src · date")
    assert store.grants_list(conn, tid)[0]["revoked_at"] is None
    store.grant_revoke(conn, tid)
    assert store.grants_list(conn, tid)[0]["revoked_at"] is not None


def test_config_validation(conn: Any) -> None:
    store.config_set(conn, "runtime.smith", "3h")
    assert store.cfg(conn, "runtime.smith") == 10800
    with pytest.raises(Refused):
        store.config_set(conn, "dispatch.max_in_progress", 0)
    with pytest.raises(Refused):
        store.config_set(conn, "dispatch.max_in_progress", "two")
    with pytest.raises(Refused):
        store.config_set(conn, "dispatch.reviewer", "nobody")


def test_specify_keeps_an_explicit_runtime_and_fills_a_missing_one(conn: Any, card: Card) -> None:
    explicit = card(triage=True, max_runtime="3h")
    bare = card(triage=True)
    assert row(conn, explicit)["max_runtime_seconds"] == 10800
    assert row(conn, bare)["max_runtime_seconds"] is None
    assert "max runtime: -" in views.context_md(conn, bare)
    store.specify(conn, explicit, {"assignee": "smith"})
    store.specify(conn, bare, {"assignee": "scout"})
    assert row(conn, explicit)["max_runtime_seconds"] == 10800
    assert row(conn, bare)["max_runtime_seconds"] == store.cfg(conn, "runtime.scout")


def test_a_triage_card_with_an_assignee_gets_that_roles_runtime_on_specify(
    conn: Any, card: Card
) -> None:
    tid = card(triage=True, assignee="warden")
    store.specify(conn, tid, {})
    assert row(conn, tid)["max_runtime_seconds"] == store.cfg(conn, "runtime.warden")


def test_create_and_specify_write_the_status_in_one_statement(conn: Any, card: Card) -> None:
    parent = card("parent")
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    child = card(parents=[parent])
    conn.set_trace_callback(None)
    assert status(conn, child) == "todo"
    assert [q for q in seen if q.startswith("UPDATE tasks")] == []

    triage = card(triage=True, parents=[parent])
    seen.clear()
    conn.set_trace_callback(seen.append)
    store.specify(conn, triage, {})
    conn.set_trace_callback(None)
    assert status(conn, triage) == "todo"
    assert len([q for q in seen if q.startswith("UPDATE tasks")]) == 1


def test_no_sandbox_settings_remain(conn: Any) -> None:
    assert "deny_read" not in [r[1] for r in conn.execute("PRAGMA table_info(boards)")]
    for key in ("worker.disabled_plugins", "worker.allowed_domains", "worker.allow_write"):
        with pytest.raises(Refused):
            store.config_set(conn, key, [])
