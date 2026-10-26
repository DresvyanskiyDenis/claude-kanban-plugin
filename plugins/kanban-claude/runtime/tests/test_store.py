import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import store, views
from kanban_claude.errors import Error, NotFound, Refused, Stale

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


def claim_run(conn: Any, tid: str) -> int:
    run = store.claim(conn, tid)
    assert run
    return run


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
    run = claim_run(conn, tid)
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


VERSIONED: dict[str, tuple[str, Callable[..., Any]]] = {
    "hold": ("ready", lambda c, t, v: store.hold(c, t, "r", expect_version=v)),
    "unblock": ("blocked", lambda c, t, v: store.unblock(c, t, "r", expect_version=v)),
    "reopen_review": ("review", lambda c, t, v: store.reopen_review(c, t, "r", expect_version=v)),
    "reopen": ("done", lambda c, t, v: store.reopen(c, t, "r", expect_version=v)),
    "mark_done": ("review", lambda c, t, v: store.mark_done(c, t, "r", expect_version=v)),
    "specify": ("triage", lambda c, t, v: store.specify(c, t, {}, expect_version=v)),
    "archive": ("ready", lambda c, t, v: store.archive(c, t, expect_version=v)),
    "assign": ("ready", lambda c, t, v: store.assign(c, t, "designer", expect_version=v)),
}  # fmt: skip


@pytest.mark.parametrize("verb", sorted(VERSIONED))
def test_a_stale_version_stops_a_guarded_write_and_the_current_one_passes(
    conn: Any, card: Card, verb: str
) -> None:
    src, fn = VERSIONED[verb]
    tid = reach(conn, card, src)
    before = row(conn, tid)
    with pytest.raises(Stale) as e:
        fn(conn, tid, before["version"] + 1)
    assert e.value.exit == 4 and isinstance(e.value, Refused)
    assert tuple(row(conn, tid)) == tuple(before)
    fn(conn, tid, before["version"])
    assert row(conn, tid)["version"] > before["version"]


def test_a_stale_version_stops_a_manual_promote(conn: Any, card: Card) -> None:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    run = claim_run(conn, parent)
    store.complete(conn, run, "ok")
    v = row(conn, child)["version"]
    with pytest.raises(Stale):
        store.promote(conn, child, "r", expect_version=v + 1)
    assert status(conn, child) == "todo"
    store.promote(conn, child, "r", expect_version=v)
    assert status(conn, child) == "ready"


def test_a_stale_version_wins_over_a_status_that_also_changed(conn: Any, card: Card) -> None:
    tid = card()
    v = row(conn, tid)["version"]
    assert store.claim(conn, tid)
    with pytest.raises(Stale):
        store.hold(conn, tid, "r", expect_version=v)
    with pytest.raises(Refused) as e:
        store.hold(conn, tid, "r", expect_version=row(conn, tid)["version"])
    assert not isinstance(e.value, Stale)


def test_tx_inside_a_transaction_joins_it_and_the_outer_one_decides(conn: Any, card: Card) -> None:
    tid = card()
    with pytest.raises(Refused), store.tx(conn):
        store.hold(conn, tid, "r")
        assert status(conn, tid) == "triage"
        raise Refused("undo")
    assert status(conn, tid) == "ready" and not conn.in_transaction
    with store.tx(conn):
        store.hold(conn, tid, "r")
        assert conn.in_transaction
    assert status(conn, tid) == "triage" and not conn.in_transaction


def test_tx_rolls_back_when_the_commit_itself_fails(conn: Any) -> None:
    conn.execute("CREATE TABLE p (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE c (pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    with pytest.raises(sqlite3.IntegrityError), store.tx(conn):
        conn.execute("INSERT INTO c VALUES (1)")
    assert not conn.in_transaction
    with store.tx(conn):
        conn.execute("INSERT INTO p VALUES (2)")
    other = store.connect(upgrade=False)
    try:
        assert other.execute("SELECT id FROM p").fetchall()[0][0] == 2
        assert other.execute("SELECT count(*) FROM c").fetchone()[0] == 0
    finally:
        other.close()


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
    run = claim_run(conn, tid)
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


def test_warden_work_run_request_changes_blocks_for_the_owner(conn: Any, card: Card) -> None:
    tid = card(assignee="warden")
    run = claim_run(conn, tid)
    assert store.request_changes(conn, run, "fix W1") == {"id": tid, "status": "blocked"}
    t = row(conn, tid)
    assert (t["status"], t["block_kind"], t["assignee"]) == ("blocked", "needs_input", "warden")
    comments = [r["body"] for r in conn.execute("SELECT body FROM task_comments")]
    assert comments == ["BLOCKED: CHANGES REQUESTED: fix W1"]
    assert store.request_changes(conn, run, "fix W1") == {"noop": True}


def test_builder_work_run_request_changes_is_refused(conn: Any, card: Card) -> None:
    run = claim_run(conn, card())
    with pytest.raises(Refused, match="review runs"):
        store.request_changes(conn, run, "nope")


def test_s1_review_approval_complete(conn: Any, card: Card) -> None:
    tid = reach(conn, card, "review")
    run2 = claim_run(conn, tid)
    with pytest.raises(Refused):
        store.complete(conn, run2, "looks fine to me")
    store.complete(conn, run2, APPROVED, default_branch="main")
    t = row(conn, tid)
    assert t["status"] == "done"
    assert t["merge_state"] is None
    assert "awaiting_owner_merge" in kinds(conn, tid)


def test_review_approval_into_non_default_base_sets_merge_pending(
    conn: Any, card: Card, repo: Path
) -> None:
    tid = card(workspace=f"worktree:{repo}", branch="feature/x", base="feature/base")
    run = claim_run(conn, tid)
    review_request(conn, run)
    run2 = claim_run(conn, tid)
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
    run = claim_run(conn, parent)
    store.complete(conn, run, "ok")
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (parent,))
    assert not store.parents_satisfied(conn, child)
    assert store.parents_satisfied(conn, child, ignore_merge=True)
    store.promote(conn, child, "r", ignore_merge=True)
    assert status(conn, child) == "ready"
    conn.execute("UPDATE tasks SET merge_state = 'merged' WHERE id = ?", (parent,))
    assert store.parents_satisfied(conn, child)


def test_unblock_returns_review_card_to_review(conn: Any, card: Card) -> None:
    tid = reach(conn, card, "review")
    run2 = claim_run(conn, tid)
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
        run = claim_run(conn, tid)
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
        run = claim_run(conn, tid)
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
    run = claim_run(conn, tid)
    with pytest.raises(Refused, match="request-review"):
        store.complete(conn, run, "all done", commits_ahead=2)
    assert status(conn, tid) == "running"
    store.complete(conn, run, "all done", commits_ahead=0)
    scout = card("scout card", assignee="scout")
    run = claim_run(conn, scout)
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
    before = len(store.MIGRATIONS)
    monkeypatch.setattr(
        store, "MIGRATIONS", [*store.MIGRATIONS, "ALTER TABLE tasks ADD COLUMN x TEXT;"]
    )
    c = store.connect()
    assert c.execute("PRAGMA user_version").fetchone()[0] == before + 1
    assert c.execute("SELECT x FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is None
    backups = list((env / "kb-home" / "backups").glob(f"kanban-s{before}-*.db"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == before
    assert old.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    old.close()
    assert store.migrate(c, env / "kb-home" / "backups") == before + 1
    assert len(list((env / "kb-home" / "backups").glob("*.db"))) == 1
    c.close()


def test_jira_migration_keeps_a_version_1_database_and_adds_the_link_tables(env: Path) -> None:
    home = env / "kb-home"
    home.mkdir(exist_ok=True)
    old = sqlite3.connect(home / "kanban.db")
    old.executescript(store.SCHEMA)
    old.execute("INSERT INTO boards(slug, name, created_at) VALUES ('main', 'main', 1)")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    c = store.connect()
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    assert dict(c.execute("SELECT * FROM boards").fetchone())["jira_project"] is None
    assert c.execute("SELECT COUNT(*) FROM jira_links").fetchone()[0] == 0
    assert list((home / "backups").glob("kanban-s1-*.db"))
    c.close()


def test_migration_refuses_newer_database(env: Path, conn: Any) -> None:
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(Error, match="newer"):
        store.connect()


def test_database_file_modes(env: Path, conn: Any) -> None:
    assert (env / "kb-home" / "kanban.db").stat().st_mode & 0o777 == 0o600
    assert (env / "kb-home").stat().st_mode & 0o777 == 0o700


def end(conn: Any, run: int, outcome: str, **kw: Any) -> bool:
    return store.end_run(conn, run, outcome, "retry", actor="runner", event_kind="run_ended", **kw)


def test_failure_rule_counts_then_gives_up(
    conn: Any, card: Card, clock: Callable[[int], None]
) -> None:
    tid = card()
    run = claim_run(conn, tid)
    assert end(conn, run, "timed_out", error="took too long")
    t = row(conn, tid)
    assert (t["status"], t["consecutive_failures"], t["retry_after"]) == (
        "ready",
        1,
        store.now() + 60,
    )
    run = claim_run(conn, tid)
    assert end(conn, run, "budget_exceeded", error="e" * 800)
    t = row(conn, tid)
    assert t["status"] == "blocked" and t["block_kind"] == "gave_up"
    assert len(t["last_failure_error"]) == 500
    assert "gave_up" in kinds(conn, tid)


def test_uncounted_outcomes_and_card_retry_limit(
    conn: Any, card: Card, clock: Callable[[int], None]
) -> None:
    tid = card(max_retries=3)
    for outcome in ("interrupted", "auth_lost", "quota"):
        run = claim_run(conn, tid)
        assert end(conn, run, outcome)
        assert row(conn, tid)["consecutive_failures"] == 0
    for _ in range(2):
        run = claim_run(conn, tid)
        end(conn, run, "crashed")
        clock(1000)
    assert status(conn, tid) == "ready"


def test_review_run_failure_returns_to_review(
    conn: Any, card: Card, clock: Callable[[int], None]
) -> None:
    tid = reach(conn, card, "review")
    run2 = claim_run(conn, tid)
    end(conn, run2, "interrupted")
    assert status(conn, tid) == "review"


def test_protocol_violation_streak(conn: Any, card: Card, clock: Callable[[int], None]) -> None:
    tid = card()
    for expected in ("ready", "ready", "blocked"):
        run = claim_run(conn, tid)
        end(conn, run, "protocol_violation", error="no terminal verb")
        assert status(conn, tid) == expected
    assert row(conn, tid)["protocol_violations"] == 3


def test_systemic_crashes_pause_dispatch_without_counting(
    conn: Any, board: str, card: Card, clock: Callable[[int], None]
) -> None:
    ids = [card(f"c{i}") for i in range(3)]
    for tid in ids:
        run = claim_run(conn, tid)
        end(conn, run, "crashed", error="spawn exploded")
        clock(10)
    assert store.state_get(conn, "dispatch_paused")["reason"] == "systemic"
    assert [row(conn, t)["consecutive_failures"] for t in ids] == [1, 1, 0]
    assert store.resume(conn) is True
    assert store.state_get(conn, "dispatch_paused") is None


def test_transient_outcomes_back_off_uncounted_then_surface_as_failures(
    conn: Any, card: Card, clock: Callable[[int], None]
) -> None:
    tid = card()
    for streak in range(store.TRANSIENT_RETRIES):
        run = claim_run(conn, tid)
        assert end(conn, run, ("network", "stalled")[streak % 2], error="x")
        t = row(conn, tid)
        wait = min(store.TRANSIENT_COOLDOWN_S * 2**streak, store.TRANSIENT_COOLDOWN_MAX_S)
        assert (t["status"], t["consecutive_failures"], t["retry_after"]) == (
            "ready", 0, store.now() + wait,
        )  # fmt: skip
        clock(wait)
    run = claim_run(conn, tid)
    end(conn, run, "network", error="x")
    assert row(conn, tid)["consecutive_failures"] == 1
    assert store.state_get(conn, "dispatch_paused") is None


def test_another_outcome_restarts_the_transient_streak(
    conn: Any, card: Card, clock: Callable[[int], None]
) -> None:
    tid = card()
    for outcome in ("network", "network", "interrupted", "network"):
        end(conn, claim_run(conn, tid), outcome)
        clock(3600)
    assert row(conn, tid)["retry_after"] == store.now() - 3600 + store.TRANSIENT_COOLDOWN_S


def test_sec5_second_end_run_after_stop_is_a_noop(conn: Any, card: Card) -> None:
    tid = card()
    run = claim_run(conn, tid)
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
    assert r["outcome"] == "terminated"


def test_sec5_terminal_verb_then_stop_and_double_verb(conn: Any, card: Card) -> None:
    tid = card()
    run = claim_run(conn, tid)
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
    claim_run(conn, tid)
    with pytest.raises(Refused, match="reclaim"):
        store.assign(conn, tid, "designer")
    store.assign(conn, tid, "designer", reclaim=True)
    t = row(conn, tid)
    assert (t["status"], t["assignee"]) == ("ready", "designer")
    assert conn.execute("SELECT outcome FROM task_runs").fetchone()[0] == "terminated"


def test_reopen_demotes_ready_children_and_comments_on_others(conn: Any, card: Card) -> None:
    parent, ready_child, done_child = card("p"), card("c1"), card("c2")
    run = claim_run(conn, done_child)
    store.complete(conn, run, "ok")
    run = claim_run(conn, parent)
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


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("dispatch.tick_seconds", 0),
        ("dispatch.tick_seconds", -5),
        ("dispatch.failure_limit", 0),
        ("retention.stream_days", 0),
        ("retention.stream_days", -1),
        ("worker.max_turns", 0),
        ("worker.max_turns", -3),
        ("worker.max_turns", "ten"),
        ("worker.max_turns", True),
        ("worker.max_turns", 2.5),
    ],
)
def test_config_set_refuses_values_outside_the_bounds(conn: Any, key: str, value: Any) -> None:
    before = store.cfg(conn, key)
    with pytest.raises(Refused, match=key):
        store.config_set(conn, key, value)
    assert store.cfg(conn, key) == before


@pytest.mark.parametrize(
    ("key", "value"),
    [("dispatch.tick_seconds", 1), ("worker.max_turns", 1)],
)
def test_config_set_accepts_the_edges_of_the_bounds(conn: Any, key: str, value: Any) -> None:
    store.config_set(conn, key, value)
    assert store.cfg(conn, key) == value


def test_worker_max_turns_can_be_unset(conn: Any) -> None:
    store.config_set(conn, "worker.max_turns", 5)
    store.config_set(conn, "worker.max_turns", None)
    assert store.cfg(conn, "worker.max_turns") is None


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


def test_a_partial_approval_into_a_feature_base_goes_to_the_owner(
    conn: Any, card: Card, repo: Path
) -> None:
    tid = card(workspace=f"worktree:{repo}", branch="feature/x", base="feature/base")
    review_request(conn, claim_run(conn, tid))
    store.complete(
        conn, claim_run(conn, tid), f"{APPROVED} PARTIAL REVIEW: no tests", default_branch="main"
    )
    t = row(conn, tid)
    assert (t["merge_state"], t["owner_merge"], t["merge_next_at"]) == ("pending", 1, None)
    assert kinds(conn, tid)[-1] == "awaiting_owner_merge" and "merge_pending" not in kinds(
        conn, tid
    )
    store.reopen(conn, tid, "redo")
    assert row(conn, tid)["owner_merge"] == 0


def test_a_clean_approval_into_a_feature_base_is_not_an_owner_merge(
    conn: Any, card: Card, repo: Path
) -> None:
    tid = card(workspace=f"worktree:{repo}", branch="feature/x", base="feature/base")
    review_request(conn, claim_run(conn, tid))
    store.complete(conn, claim_run(conn, tid), APPROVED, default_branch="main")
    assert row(conn, tid)["owner_merge"] == 0


def test_merge_method_is_stored_on_create_and_edit_and_validated(conn: Any, card: Card) -> None:
    tid = card(merge_method="merge")
    assert row(conn, tid)["merge_method"] == "merge"
    store.edit(conn, tid, {"merge_method": "rebase"})
    assert row(conn, tid)["merge_method"] == "rebase"
    with pytest.raises(Refused, match="merge-method"):
        store.edit(conn, tid, {"merge_method": "fast-forward"})
    with pytest.raises(Refused, match="merge-method"):
        card(merge_method="octopus")
    assert row(conn, card())["merge_method"] is None


def unmerged_parent_and_child(conn: Any, card: Card) -> tuple[str, str]:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    store.complete(conn, claim_run(conn, parent), "ok")
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (parent,))
    return parent, child


def test_promote_ignore_merge_survives_to_the_claim(conn: Any, card: Card) -> None:
    _, child = unmerged_parent_and_child(conn, card)
    store.promote(conn, child, "r", ignore_merge=True)
    assert row(conn, child)["ignore_merge"] == 1
    assert store.claim(conn, child), "the claim must honour the waiver the promote recorded"
    assert "claim_rejected" not in kinds(conn, child)


def test_promote_without_the_flag_leaves_the_claim_strict(conn: Any, card: Card) -> None:
    _, child = unmerged_parent_and_child(conn, card)
    with pytest.raises(Refused):
        store.promote(conn, child, "r")
    assert row(conn, child)["ignore_merge"] == 0


def test_promote_auto_honours_the_waiver(conn: Any, card: Card) -> None:
    _, child = unmerged_parent_and_child(conn, card)
    assert not store.promote_auto(conn, child)
    conn.execute("UPDATE tasks SET ignore_merge = 1 WHERE id = ?", (child,))
    assert store.promote_auto(conn, child)


def test_reopen_and_hold_clear_the_waiver(conn: Any, card: Card) -> None:
    _, child = unmerged_parent_and_child(conn, card)
    store.promote(conn, child, "r", ignore_merge=True)
    store.complete(conn, claim_run(conn, child), "ok")
    store.reopen(conn, child, "again")
    assert row(conn, child)["ignore_merge"] == 0
    assert store.claim(conn, child) is None, "a reopened card waits for its merged parent again"
    assert status(conn, child) == "todo"
    store.promote(conn, child, "r", ignore_merge=True)
    store.hold(conn, child, "r")
    assert row(conn, child)["ignore_merge"] == 0


def test_scratch_card_with_a_branch_is_refused(conn: Any, card: Card, repo: Path) -> None:
    with pytest.raises(Refused, match="--workspace worktree"):
        card(branch="feat/x")
    with pytest.raises(Refused, match="--workspace worktree"):
        card(workspace=None, branch="feat/x")
    assert card(triage=True, branch="feat/x"), (
        "a triage card may name the branch before its workspace"
    )


PR_DELIVERABLE = "## Goal\ng\n\n## Deliverable\nPR against host/acme/proj:main, docs only.\n\n## Out of scope\nx\n"


@pytest.mark.parametrize("assignee", ["smith", "designer"])
def test_scratch_builder_card_whose_deliverable_is_a_pr_is_refused(
    conn: Any, card: Card, assignee: str
) -> None:
    with pytest.raises(Refused, match=r"--workspace worktree:.* --branch feature/<slug>"):
        card(body=PR_DELIVERABLE, assignee=assignee)
    tid = card(body=PR_DELIVERABLE, assignee=assignee, triage=True)
    with pytest.raises(Refused, match="request-review"):
        store.specify(conn, tid, {})


def test_scratch_card_without_a_pr_deliverable_is_still_fine(
    conn: Any, card: Card, repo: Path
) -> None:
    assert card(body="## Deliverable\nA file at /abs/report.md\n\n## Context\nthe PR is #4\n")
    assert card(body=PR_DELIVERABLE, assignee="scout"), "a scout writes no PR, whatever it reads"
    store.board_create(conn, "wt", default_workdir=str(repo))
    assert store.create(
        conn, "wt", "w", body=PR_DELIVERABLE, assignee="smith",
        workspace=f"worktree:{repo}", branch="feat/x",
    )  # fmt: skip


def test_base_defaults_to_the_only_worktree_parent_base(conn: Any, board: str, repo: Path) -> None:
    store.board_create(conn, "wt", default_workdir=str(repo))
    ws = f"worktree:{repo}"

    def make(**kw: Any) -> str:
        kw.setdefault("workspace", ws)
        return str(store.create(conn, "wt", "c", body="b", assignee="smith", **kw)["id"])

    parent = make(branch="feat/p", base="release/1")
    scratch = str(store.create(conn, "wt", "s", body="b", assignee="smith")["id"])
    child = make(branch="feat/c", parents=[parent, scratch])
    assert row(conn, child)["base_branch"] == "release/1"
    explicit = make(branch="feat/d", base="main", parents=[parent])
    assert row(conn, explicit)["base_branch"] == "main"
    other = make(branch="feat/e", base="release/2")
    two = make(branch="feat/f", parents=[parent, other])
    assert row(conn, two)["base_branch"] is None, "two worktree parents: no guess"


def test_touches_are_stored_as_a_json_list_and_edit_can_clear_them(conn: Any, card: Card) -> None:
    tid = card(touches=["docs/**", "README.md"])
    assert json.loads(row(conn, tid)["touches"]) == ["docs/**", "README.md"]
    assert row(conn, card())["touches"] is None
    store.edit(conn, tid, {"touches": ["src/*.py"]})
    assert json.loads(row(conn, tid)["touches"]) == ["src/*.py"]
    store.edit(conn, tid, {"touches": [""]})
    assert row(conn, tid)["touches"] is None
    for bad in ("docs/**", [3], [" "]):
        with pytest.raises(Refused, match="touches"):
            store.edit(conn, tid, {"touches": bad})


def spend(conn: Any, tid: str, usd: float, *, started_at: int | None = None) -> None:
    """A finished run on the card that cost `usd`, then back to ready."""
    run = claim_run(conn, tid)
    store.block(conn, run, "needs_input", "why")
    conn.execute(
        "UPDATE task_runs SET cost_usd = ?, started_at = ? WHERE id = ?",
        (usd, started_at or store.now(), run),
    )
    store.unblock(conn, tid, "go")


def test_card_cap_blocks_the_claim_with_the_numbers_and_the_key(conn: Any, card: Card) -> None:
    tid = card()
    spend(conn, tid, 16.5)
    assert store.claim(conn, tid) is None
    t = row(conn, tid)
    assert (t["status"], t["block_kind"]) == ("blocked", "capability")
    said = conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,)).fetchall()
    msg = said[-1]["body"]
    assert "$16.50" in msg and "$15.00" in msg and "budget.max_usd_per_card" in msg
    assert "blocked" in kinds(conn, tid)


def test_card_cap_under_the_limit_claims(conn: Any, card: Card) -> None:
    tid = card()
    spend(conn, tid, 14.0)
    assert store.claim(conn, tid)


def test_card_cap_raised_or_off_lets_the_card_run(conn: Any, card: Card) -> None:
    tid = card()
    spend(conn, tid, 20.0)
    assert store.claim(conn, tid) is None
    store.config_set(conn, "budget.max_usd_per_card", 25)
    store.unblock(conn, tid, "raised")
    assert store.claim(conn, tid)
    other = card("other")
    spend(conn, other, 99.0)
    store.config_set(conn, "budget.max_usd_per_card", 0)
    assert store.claim(conn, other), "0 turns the per-card cap off"


def _cost(conn: Any, run: int, usd: float) -> None:
    conn.execute("UPDATE task_runs SET cost_usd = ? WHERE id = ?", (usd, run))


def test_a_cap_hit_at_the_review_claim_unblocks_back_to_review(conn: Any, card: Card) -> None:
    tid, other = card(), card("other")
    for t in (tid, other):
        run = claim_run(conn, t)
        _cost(conn, run, 16.0)
        store.request_review(conn, run, "PR #1", pr_number=1, pr_url=PR_URL)
        assert store.claim(conn, t) is None, "the review claim is over the $15 cap"
    store.request_changes_owner(conn, other, "the owner may still send it back")
    store.config_set(conn, "budget.max_usd_per_card", 25)
    store.unblock(conn, tid, "cap raised")
    t = row(conn, tid)
    assert (t["status"], t["assignee"]) == ("review", "warden"), (
        "back to ready would run warden as a work run and skip the review gate"
    )


def test_a_cap_hit_at_the_work_claim_after_changes_unblocks_to_ready(conn: Any, card: Card) -> None:
    tid = card()
    store.request_review(conn, claim_run(conn, tid), "PR #1", pr_number=1, pr_url=PR_URL)
    review = claim_run(conn, tid)
    _cost(conn, review, 16.0)
    store.request_changes(conn, review, "fix the thing")
    assert store.claim(conn, tid) is None
    store.config_set(conn, "budget.max_usd_per_card", 25)
    store.unblock(conn, tid, "cap raised")
    t = row(conn, tid)
    assert (t["status"], t["assignee"]) == ("ready", "smith"), (
        "back to review would leave smith stuck on role_not_dispatchable"
    )


def test_board_day_cap_counts_only_today_on_this_board(
    conn: Any, board: str, card: Card, repo: Path
) -> None:
    store.config_set(conn, "budget.max_usd_per_board_day", 75)
    old, today, fresh = card("old"), card("today"), card("fresh")
    spend(conn, old, 14.0, started_at=store.now() - 3 * 86400)
    spend(conn, today, 14.0)
    assert store.claim(conn, fresh), (
        "$14 today is under the $75 cap; yesterday's spend is not counted"
    )
    store.complete(conn, _running(conn, fresh), "ok")
    for i in range(5):
        spend(conn, card(f"burn{i}"), 14.0)
    late = card("late")
    assert store.claim(conn, late) is None
    t = row(conn, late)
    assert (t["status"], t["block_kind"]) == ("blocked", "capability")
    msg = conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (late,)).fetchone()[0]
    assert "budget.max_usd_per_board_day" in msg and "$75.00" in msg and "$84.00" in msg
    store.board_create(conn, "other", default_workdir=str(repo))
    elsewhere = str(store.create(conn, "other", "e", body="b", assignee="smith")["id"])
    assert store.claim(conn, elsewhere), "another board's spend does not count"


def _running(conn: Any, tid: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? AND status = 'running'", (tid,)
        ).fetchone()[0]
    )


@pytest.mark.parametrize("off", [None, 0])
def test_board_day_cap_off_states(conn: Any, card: Card, off: Any) -> None:
    store.config_set(conn, "budget.max_usd_per_board_day", 10)
    store.config_set(conn, "budget.max_usd_per_board_day", off)
    tid = card()
    spend(conn, tid, 14.0)
    assert store.claim(conn, tid)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("budget.max_usd_per_board_day", "lots"),
        ("budget.max_usd_per_board_day", True),
        ("budget.max_usd_per_board_day", -1),
        ("budget.max_usd_per_card", "lots"),
        ("budget.max_usd_per_card", -1),
        ("budget.max_usd_per_card", None),
    ],
)
def test_budget_caps_refuse_non_numbers_and_negatives(conn: Any, key: str, value: Any) -> None:
    with pytest.raises(Refused, match=key):
        store.config_set(conn, key, value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("effort.smith", "turbo"),
        ("effort.smith", 5),
        ("effort.default", "turbo"),
        ("effort.review", "turbo"),
        ("models.default", "bad model!"),
        ("models.review", "bad model!"),
    ],
)
def test_effort_and_model_keys_refuse_what_claude_would_reject(
    conn: Any, key: str, value: Any
) -> None:
    """A bad value reaches every run's argv: each run then fails, or Popen raises TypeError."""
    with pytest.raises(Refused):
        store.config_set(conn, key, value)
    store.config_set(conn, "effort.smith", "max")
    store.config_set(conn, "effort.smith", None)


def test_link_ends_the_waiver_for_the_new_parent(conn: Any, card: Card) -> None:
    _, child = unmerged_parent_and_child(conn, card)
    store.promote(conn, child, "r", ignore_merge=True)
    newer = card("newer")
    store.complete(conn, claim_run(conn, newer), "ok")
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (newer,))
    store.link(conn, newer, child)
    assert row(conn, child)["ignore_merge"] == 0
    assert status(conn, child) == "todo"


def test_advisor_config_takes_inherit_off_or_ab_only(conn: Any) -> None:
    assert store.cfg(conn, "advisor.smith") == "inherit"
    for policy in ("off", "ab", "inherit"):
        store.config_set(conn, "advisor.warden", policy)
        assert store.cfg(conn, "advisor.warden") == policy
    for bad in ("on", "yes", True, 1):
        with pytest.raises(Refused):
            store.config_set(conn, "advisor.smith", bad)
    for role in ("smith", "designer", "scout", "warden", "atlassian"):
        assert f"advisor.{role}" in store.DEFAULTS


def test_the_measure_columns_arrive_on_an_old_board_and_old_runs_stay_unmeasured(env: Path) -> None:
    home = env / "kb-home"
    home.mkdir(exist_ok=True)
    old = sqlite3.connect(home / "kanban.db")
    old.executescript(store.SCHEMA)
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    c = store.connect()
    cols = {r[1] for r in c.execute("PRAGMA table_info(task_runs)")}
    assert {"advisor", "advisor_calls", "cache_write_1h_tokens", "cache_write_5m_tokens",
            "api_ms", "harness", "params"} <= cols  # fmt: skip
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    c.close()
