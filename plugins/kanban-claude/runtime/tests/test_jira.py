"""The Jira sync (spec 14.5, TST-J) against tests/fake_jira_mcp.py: a real MCP process per pass."""

import json
import re
import signal
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import housekeeping, jira, store
from kanban_claude.errors import Refused

from .conftest import Kb
from .fake_jira_mcp import Fake

Card = Callable[..., str]
Sync = Callable[[], list[str]]
KEY = "KAN-100"  # the first key the fake hands out


@pytest.fixture
def sync(conn: sqlite3.Connection, jira_fake: Fake, clock: Callable[[int], None]) -> Sync:
    """One pass, then a minute for the board and for the server."""

    def run() -> list[str]:
        out = jira.sync_all(conn)
        clock(60)
        jira_fake.advance(60)
        return out

    return run


def link(conn: sqlite3.Connection, tid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jira_links WHERE task_id = ?", (tid,)).fetchone()
    assert row, f"{tid} has no link"
    return row  # type: ignore[no-any-return]


def kinds(conn: sqlite3.Connection, like: str = "jira_%") -> list[str]:
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE kind LIKE ?", (like,))]


def comments(conn: sqlite3.Connection, tid: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,)
    )
    return [(r["author"], r["body"]) for r in rows]


def status(conn: sqlite3.Connection, tid: str) -> str:
    return str(store.get_task(conn, tid)["status"])


def paused(conn: sqlite3.Connection) -> str | None:
    p = (store.state_get(conn, "jira:main") or {}).get("paused")
    return None if p is None else str(p["detail"])


def test_j1_two_passes_after_a_push_write_nothing_despite_the_lossy_round_trip(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("Title", body="- a\n- b")
    sync()
    assert jira_fake.issue(KEY)["description"] == "* a\r\n* b"
    store.edit(conn, tid, {"body": "- a\n- b\n- c"})
    sync()
    [update] = jira_fake.calls("jira_update_issue")
    assert json.loads(update["args"]["fields"]) == {"description": "- a\n- b\n- c"}
    writes, events = len(jira_fake.writes()), kinds(conn)
    updated = store.get_task(conn, tid)["updated_at"]
    sync()
    sync()
    assert len(jira_fake.writes()) == writes
    assert kinds(conn) == events
    assert store.get_task(conn, tid)["updated_at"] == updated
    assert store.get_task(conn, tid)["body"] == "- a\n- b\n- c"


def test_j2_both_changed_a_field_jira_wins_and_the_cards_text_is_kept_in_a_comment(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("Card title", body="card body")
    sync()
    store.edit(conn, tid, {"title": "Card title v2", "body": "card body v2"})
    jira_fake.edit(KEY, summary="Jira title v2")
    sync()
    t = store.get_task(conn, tid)
    assert (t["title"], t["body"]) == ("Jira title v2", "card body v2")
    assert jira_fake.issue(KEY)["summary"] == "Jira title v2"
    assert jira_fake.issue(KEY)["description"] == "card body v2"
    [(author, text)] = [c for c in comments(conn, tid) if c[1].startswith("[jira] conflict")]
    assert author == "jira"
    assert "Card title v2" in text
    assert kinds(conn, "jira_conflict") == ["jira_conflict"]
    writes = len(jira_fake.writes())
    sync()
    assert len(jira_fake.writes()) == writes
    assert store.get_task(conn, tid)["title"] == "Jira title v2"


def test_j2_an_edit_on_one_side_only_crosses_to_the_other(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("Title", body="body")
    sync()
    jira_fake.edit(KEY, description="- from jira\n- second")
    sync()
    assert store.get_task(conn, tid)["body"] == "* from jira\n* second"
    assert not jira_fake.calls("jira_update_issue")
    store.edit(conn, tid, {"title": "Title v2"})
    sync()
    assert jira_fake.issue(KEY)["summary"] == "Title v2"
    assert len(jira_fake.calls("jira_update_issue")) == 1


def test_j3_jira_done_closes_a_ready_card_and_flags_a_running_one(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    ready, busy = card("Ready"), card("Busy")
    sync()
    k_ready, k_busy = link(conn, ready)["jira_key"], link(conn, busy)["jira_key"]
    assert store.claim(conn, busy)
    sync()
    assert jira_fake.issue(k_busy)["status"]["name"] == "In Progress"
    jira_fake.edit(k_ready, status="Done")
    jira_fake.edit(k_busy, status="Done")
    sync()
    assert status(conn, ready) == "done"
    assert ("jira", "[jira] closed in Jira") in comments(conn, ready)
    assert status(conn, busy) == "running"
    assert "running" in link(conn, busy)["conflict"]
    assert kinds(conn, "jira_conflict") == ["jira_conflict"]
    sync()
    assert kinds(conn, "jira_conflict") == ["jira_conflict"]


def test_j3_a_reopened_issue_sends_a_done_card_to_triage_never_ready(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("Closed in Jira")
    sync()
    jira_fake.edit(KEY, status="Done")
    sync()
    assert status(conn, tid) == "done"
    jira_fake.edit(KEY, status="To Do")
    sync()
    assert status(conn, tid) == "triage"
    assert ("jira", "[jira] reopened in Jira") in comments(conn, tid)
    writes = len(jira_fake.writes())
    sync()
    assert len(jira_fake.writes()) == writes
    assert status(conn, tid) == "triage"


def test_j3_a_parent_closed_in_jira_with_a_waiting_child_is_a_conflict_not_a_close(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    parent = card("Parent")
    child = card("Child", parents=[parent])
    assert status(conn, child) == "todo"
    sync()
    jira_fake.edit(link(conn, parent)["jira_key"], status="Done")
    sync()
    assert status(conn, parent) == "ready"
    assert "child" in link(conn, parent)["conflict"]
    assert kinds(conn, "jira_conflict") == ["jira_conflict"]


def test_j4_a_new_matching_issue_becomes_a_triage_card_and_a_new_card_an_issue(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    jira_fake.add("KAN-7", "From Jira", "- do it", labels=["kanban-claude"])
    jira_fake.comment("KAN-7", "context from the issue")
    jira_fake.add("KAN-8", "Already finished", status="Done")
    mine = card("From the board")
    sync()
    [imported] = conn.execute("SELECT * FROM tasks WHERE title = 'From Jira'").fetchall()
    assert imported["status"] == "triage"
    assert imported["body"] == "* do it"
    assert link(conn, imported["id"])["jira_key"] == "KAN-7"
    assert ("jira:Pat Example", "context from the issue") in comments(conn, imported["id"])
    assert not conn.execute("SELECT 1 FROM tasks WHERE title = 'Already finished'").fetchone()
    args = jira_fake.calls("jira_create_issue")[0]["args"]
    assert (args["project_key"], args["issue_type"], args["assignee"]) == (
        "KAN",
        "Task",
        "me@example.com",
    )
    labels = json.loads(args["additional_fields"])["labels"]
    assert labels[0] == "kanban-claude"
    assert f"kanban-{mine}" in labels
    made = jira_fake.issue(link(conn, mine)["jira_key"])
    assert (made["summary"], made["labels"], made["assignee"]) == (
        "From the board",
        labels,
        "me@example.com",
    )
    board_query = 'labels = "kanban-claude" AND assignee = currentUser()'
    assert any(board_query in c["args"]["jql"] for c in jira_fake.calls("jira_search"))
    sync()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
    assert len(jira_fake.calls("jira_create_issue")) == 1


def test_j5_no_direct_transition_is_a_link_error_and_never_a_chain(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.put(workflow={
        "To Do": [["11", "Review it", "Review"]],
        "Review": [["41", "Start", "In Progress"], ["42", "Done", "Done"]],
    })  # fmt: skip
    assert store.claim(conn, tid)
    sync()
    assert "no transition" in link(conn, tid)["error"]
    assert not jira_fake.calls("jira_transition_issue")
    assert kinds(conn, "jira_error") == ["jira_error"]
    asked = len(jira_fake.calls("jira_get_transitions"))
    sync()
    assert len(jira_fake.calls("jira_get_transitions")) == asked + 1
    assert not jira_fake.calls("jira_transition_issue")
    assert kinds(conn, "jira_error") == ["jira_error"]


def test_j5_a_transition_that_needs_a_field_is_refused_and_tried_again_until_it_passes(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.put(needs_field=["In Progress"])
    run = store.claim(conn, tid)
    assert run
    sync()
    assert "needs a value" in link(conn, tid)["error"]
    assert len(jira_fake.calls("jira_transition_issue")) == 1
    assert jira_fake.issue(KEY)["status"]["name"] == "To Do"
    sync()
    assert len(jira_fake.calls("jira_transition_issue")) == 2
    assert kinds(conn, "jira_error") == ["jira_error"]
    jira_fake.put(needs_field=[])
    store.complete(conn, run, "done it")
    sync()
    assert link(conn, tid)["error"] is None
    assert jira_fake.issue(KEY)["status"]["name"] == "Done"
    assert len(jira_fake.calls("jira_transition_issue")) == 3


def test_j5_a_status_push_that_keeps_failing_does_not_hold_back_the_comments(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.put(needs_field=["In Progress", "Done"])
    run = store.claim(conn, tid)
    assert run
    store.comment(conn, tid, "owner note")
    sync()
    store.complete(conn, run, "all done")
    sync()
    assert [c["body"] for c in jira_fake.issue(KEY)["comments"]] == [
        "owner note",
        "kanban completed: all done",
    ]
    assert "needs a value" in link(conn, tid)["error"]
    asked = len(jira_fake.calls("jira_transition_issue"))
    sync()
    assert len(jira_fake.calls("jira_transition_issue")) == asked + 1
    assert len(jira_fake.calls("jira_add_comment")) == 2


def test_j5_a_key_with_403_in_it_is_not_an_authentication_failure(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    jira_fake.put(next=403)
    tid = card("A")
    sync()
    assert link(conn, tid)["jira_key"] == "KAN-403"
    jira_fake.delete("KAN-403")
    store.edit(conn, tid, {"priority": 2})
    sync()
    assert paused(conn) is None
    assert kinds(conn, "jira_unlinked") == ["jira_unlinked"]


def test_j5_the_card_status_is_pushed_only_when_the_card_changes_status(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.edit(KEY, status="In Progress")
    sync()
    store.edit(conn, tid, {"priority": 2})
    sync()
    sync()
    assert not jira_fake.calls("jira_transition_issue")
    assert jira_fake.issue(KEY)["status"]["name"] == "In Progress"
    assert status(conn, tid) == "ready"


def test_j5_a_not_found_from_any_tool_but_the_read_is_an_error_not_an_unlink(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.mode(fail={"jira_get_transitions": f"Issue {KEY} not found"})
    assert store.claim(conn, tid)
    sync()
    assert "not found" in link(conn, tid)["error"]
    assert kinds(conn, "jira_unlinked") == []
    assert link(conn, tid)["jira_key"] == KEY


def test_j6_a_server_that_does_not_start_pauses_the_board_and_dispatch_goes_on(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    good = store.cfg(conn, "jira.mcp_command")
    store.config_set(conn, "jira.mcp_command", ["/nonexistent/atlassian-mcp"])
    assert sync() == []
    since = store.state_get(conn, "jira:main")["paused"]["since"]
    assert "cannot start" in str(paused(conn))
    sync()
    assert store.state_get(conn, "jira:main")["paused"]["since"] == since
    assert kinds(conn, "jira_error") == ["jira_error"]
    assert store.claim(conn, tid)
    store.config_set(conn, "jira.mcp_command", good)
    assert sync() == ["main"]
    st = store.state_get(conn, "jira:main")
    assert "paused" not in st
    assert st["last_pull_at"]


def test_j6_an_authentication_failure_pauses_the_board_until_a_pass_succeeds(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    card("A")
    jira_fake.mode(auth=True)
    sync()
    assert "401" in str(paused(conn))
    assert not conn.execute("SELECT 1 FROM jira_links").fetchone()
    jira_fake.mode(auth=False)
    sync()
    assert paused(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM jira_links").fetchone()[0] == 1


def test_j6_a_failed_initialize_and_a_silent_server_pause_the_board(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    card("A")
    jira_fake.mode(init=True)
    sync()
    assert "initialize failed" in str(paused(conn))
    jira_fake.mode(init=False, hang=True)
    monkeypatch.setattr(jira, "JIRA_CALL_S", 1)
    sync()
    assert "no answer within 1 s" in str(paused(conn))


def test_j6_a_search_the_server_cannot_page_pauses_the_board_instead_of_looping(
    conn: sqlite3.Connection, jira_fake: Fake, sync: Sync
) -> None:
    for i in range(jira.PAGE + 1):
        jira_fake.add(f"KAN-{i + 1}", f"Issue {i}")
    jira_fake.mode(no_total=True)
    assert sync() == []
    assert "cannot be paged" in str(paused(conn))
    assert not conn.execute("SELECT 1 FROM jira_links").fetchone()


def test_j6_a_short_page_needs_no_total(
    conn: sqlite3.Connection, jira_fake: Fake, sync: Sync
) -> None:
    for i in range(3):
        jira_fake.add(f"KAN-{i + 1}", f"Issue {i}")
    jira_fake.mode(no_total=True)
    assert sync() == ["main"]
    assert paused(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM jira_links").fetchone()[0] == 3


def test_j6_a_timeout_after_the_issue_was_made_adopts_it_instead_of_making_another(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    tid = card("A")
    monkeypatch.setattr(jira, "JIRA_CALL_S", 1)
    jira_fake.mode(hang_after="jira_create_issue")
    sync()
    assert "no answer" in str(paused(conn))
    assert not conn.execute("SELECT 1 FROM jira_links").fetchone()
    jira_fake.mode(hang_after=None)
    assert sync() == ["main"]
    assert len(jira_fake.calls("jira_create_issue")) == 1
    assert link(conn, tid)["jira_key"] == KEY
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_j6_a_timeout_after_a_comment_was_added_adopts_it_instead_of_adding_another(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    tid = card("A")
    sync()
    store.comment(conn, tid, "owner note")
    monkeypatch.setattr(jira, "JIRA_CALL_S", 1)
    jira_fake.mode(hang_after="jira_add_comment")
    sync()
    assert "no answer" in str(paused(conn))
    jira_fake.mode(hang_after=None)
    sync()
    assert len(jira_fake.calls("jira_add_comment")) == 1
    assert [c["body"] for c in jira_fake.issue(KEY)["comments"]] == ["owner note"]
    assert conn.execute(
        "SELECT jira_comment_id FROM task_comments WHERE body = 'owner note'"
    ).fetchone()[0]


def test_j6_an_identical_comment_by_someone_else_is_not_taken_for_the_owners(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.comment(KEY, "LGTM", "Pat Example")
    store.comment(conn, tid, "LGTM")
    sync()
    sync()
    assert [(c["author"]["display_name"], c["body"]) for c in jira_fake.issue(KEY)["comments"]] == [
        ("Pat Example", "LGTM"),
        ("Kanban Owner", "LGTM"),
    ]
    assert ("jira:Pat Example", "LGTM") in comments(conn, tid)


def test_j6_a_card_edited_between_the_create_timeout_and_the_adoption_still_reaches_jira(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    tid = card("T")
    monkeypatch.setattr(jira, "JIRA_CALL_S", 1)
    jira_fake.mode(hang_after="jira_create_issue")
    sync()
    jira_fake.mode(hang_after=None)
    store.edit(conn, tid, {"title": "T2"})
    sync()
    sync()
    assert len(jira_fake.calls("jira_create_issue")) == 1
    assert jira_fake.issue(KEY)["summary"] == "T2"
    assert store.get_task(conn, tid)["title"] == "T2"


def test_j6_an_unexpected_crash_in_the_pass_still_reaps_the_server(
    conn: sqlite3.Connection, jira_fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    children: list[subprocess.Popen[bytes]] = []

    def boom(conn: sqlite3.Connection, mcp: jira.Mcp, b: Any) -> None:
        children.append(mcp.proc)
        raise RuntimeError("boom")

    monkeypatch.setattr(jira, "sync_board", boom)
    with pytest.raises(RuntimeError):
        jira.sync_all(conn)
    assert children[0].poll() is not None


def test_closing_the_server_kills_its_process_group_whatever_it_did(
    conn: sqlite3.Connection, jira_fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[int] = []
    real = jira.signal_group

    def spy(pgid: int | None, sig: int) -> None:
        sent.append(sig)
        real(pgid, sig)

    monkeypatch.setattr(jira, "signal_group", spy)
    assert "jira_search" in jira.probe(store.cfg(conn, "jira.mcp_command"))
    assert sent == [signal.SIGKILL]


def test_j7_comments_cross_once_each_way_and_machine_comments_never_go_out(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    store.comment(conn, tid, "owner note")
    store.comment(conn, tid, "[run] 1 started", author="kanban", actor="runner")
    store.comment(conn, tid, "WARDEN: APPROVED PR #1 @ abcdef1", author="warden", actor="warden")
    jira_fake.comment(KEY, "from jira", "Pat Example")
    sync()
    sync()
    assert [c["args"]["body"] for c in jira_fake.calls("jira_add_comment")] == ["owner note"]
    assert comments(conn, tid).count(("jira:Pat Example", "from jira")) == 1
    assert [b for _, b in comments(conn, tid)].count("owner note") == 1
    assert sorted(kinds(conn, "jira_pushed")) == ["jira_pushed"]


def test_j7_a_work_runs_closing_text_is_pushed_once_but_a_review_ledger_never(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    run = store.claim(conn, tid)
    assert run
    store.complete(conn, run, "implemented the thing")
    conn.execute(
        "INSERT INTO task_runs(task_id, n, role, kind, status, summary, started_at, ended_at, terminal_verb) "
        "VALUES (?, 9, 'warden', 'review', 'ended', 'WARDEN: APPROVED PR #1 @ abcdef1', ?, ?, 'complete')",
        (tid, store.now(), store.now()),
    )
    sync()
    sync()
    assert [c["args"]["body"] for c in jira_fake.calls("jira_add_comment")] == [
        "kanban completed: implemented the thing"
    ]


def test_j7_a_comment_pulled_for_an_unlinked_card_is_not_pulled_again_for_its_successor(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.comment(KEY, "from jira")
    sync()
    assert ("jira:Pat Example", "from jira") in comments(conn, tid)
    jira.unlink_card(conn, tid)
    jira.link_card(conn, tid, KEY)
    sync()
    assert link(conn, tid)["error"] is None
    jira.unlink_card(conn, tid)
    successor = jira.import_issue(conn, "main", KEY)["id"]
    sync()
    assert ("jira:Pat Example", "from jira") not in comments(conn, successor)
    assert link(conn, successor)["error"] is None


def test_j8_a_deleted_issue_is_unlinked_and_the_card_is_kept(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira_fake.delete(KEY)
    store.edit(conn, tid, {"title": "A, renamed"})
    assert sync() == ["main"]
    assert not conn.execute("SELECT 1 FROM jira_links").fetchone()
    assert store.get_task(conn, tid)["title"] == "A, renamed"
    assert any("no longer exists in Jira" in b for _, b in comments(conn, tid))
    assert kinds(conn, "jira_unlinked") == ["jira_unlinked"]
    sync()
    assert len(jira_fake.calls("jira_create_issue")) == 1


def test_j9_a_card_that_cannot_be_exported_does_not_hold_back_the_others(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync, clock: Callable[[int], None]
) -> None:
    bad = card("Bad card")
    clock(1)
    good = card("Good card")
    jira_fake.mode(fail={"jira_create_issue:Bad card": "boom"})
    for _ in range(3):
        assert sync() == ["main"]
    assert link(conn, good)["jira_key"]
    assert not conn.execute("SELECT 1 FROM jira_links WHERE task_id = ?", (bad,)).fetchone()
    errors = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'jira_error'", (bad,)
    ).fetchall()
    assert len(errors) == 1
    assert "boom" in errors[0][0]
    jira_fake.mode(fail={})
    sync()
    assert link(conn, bad)["jira_key"]


def test_j9_a_database_error_on_one_link_is_kept_on_it_and_the_others_still_sync(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    one, two = card("One"), card("Two")
    sync()
    before = store.state_get(conn, "jira:main")["last_pull_at"]
    store.edit(conn, one, {"title": "One v2"})
    store.edit(conn, two, {"title": "Two v2"})
    real = jira.sync_link

    def flaky(conn: sqlite3.Connection, mcp: jira.Mcp, lk: sqlite3.Row, issue: Any) -> None:
        if lk["task_id"] == one:
            raise sqlite3.IntegrityError("UNIQUE constraint failed: boom")
        real(conn, mcp, lk, issue)

    monkeypatch.setattr(jira, "sync_link", flaky)
    assert sync() == ["main"]
    assert "boom" in link(conn, one)["error"]
    assert jira_fake.issue(link(conn, two)["jira_key"])["summary"] == "Two v2"
    assert store.state_get(conn, "jira:main")["last_pull_at"] == before


def test_j10_jira_text_on_an_approved_card_sends_it_to_triage_and_a_running_card_waits(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    ready, busy = card("Ready"), card("Busy")
    sync()
    k_ready, k_busy = link(conn, ready)["jira_key"], link(conn, busy)["jira_key"]
    run = store.claim(conn, busy)
    assert run
    sync()
    jira_fake.edit(k_ready, summary="Ready v2")
    jira_fake.edit(k_busy, summary="Busy v2")
    sync()
    assert store.get_task(conn, ready)["title"] == "Ready v2"
    assert status(conn, ready) == "triage"
    assert ("jira", "[jira] title changed; the card is back in triage") in comments(conn, ready)
    assert store.get_task(conn, busy)["title"] == "Busy"
    assert link(conn, busy)["base_title"] == "Busy"
    sync()
    assert store.get_task(conn, busy)["title"] == "Busy"
    store.complete(conn, run, "done it")
    sync()
    assert store.get_task(conn, busy)["title"] == "Busy v2"
    assert link(conn, busy)["base_title"] == "Busy v2"


def test_j10_a_card_touched_without_a_text_change_resends_nothing_and_never_conflicts(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("T", body="- a\n- b")
    sync()
    store.edit(conn, tid, {"priority": 2})
    sync()
    sync()
    assert not jira_fake.calls("jira_update_issue")
    jira_fake.edit(KEY, summary="T v2")
    store.edit(conn, tid, {"priority": 1})
    sync()
    t = store.get_task(conn, tid)
    assert (t["title"], t["body"]) == ("T v2", "- a\n- b")
    assert not [c for c in comments(conn, tid) if c[1].startswith("[jira] conflict")]
    assert not jira_fake.calls("jira_update_issue")


def test_a_pass_reads_a_window_and_pages_by_start_at(
    conn: sqlite3.Connection, jira_fake: Fake, sync: Sync
) -> None:
    for i in range(51):
        jira_fake.add(f"KAN-{i + 1}", f"Issue {i}")
    sync()
    first = [c["args"] for c in jira_fake.calls("jira_search")]
    assert [a["start_at"] for a in first] == [0, 50]
    assert all("updated >=" not in a["jql"] for a in first)
    assert conn.execute("SELECT COUNT(*) FROM jira_links").fetchone()[0] == 51
    jira.sync_all(conn)
    later = [c["args"]["jql"] for c in jira_fake.calls("jira_search")[2:]]
    window = re.compile(r"\) AND updated >= -\d+m$")
    assert window.search(later[0])
    assert [j.startswith("key in (") and bool(window.search(j)) for j in later[2:]] == [True] * 2


def test_the_search_window_leaves_out_what_did_not_change(
    conn: sqlite3.Connection, jira_fake: Fake, sync: Sync
) -> None:
    jira_fake.add("KAN-1", "Quiet")
    sync()
    jira_fake.advance(3600)
    jira_fake.add("KAN-2", "Busy")
    sync()
    seen = [c["args"]["issue_key"] for c in jira_fake.calls("jira_get_issue")]
    assert seen.count("KAN-1") == 1
    assert seen.count("KAN-2") == 1


def test_an_unlinked_card_is_not_exported_again_and_its_issue_does_not_come_back(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    jira.unlink_card(conn, tid)
    sync()
    sync()
    assert len(jira_fake.calls("jira_create_issue")) == 1
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_a_second_sync_while_one_runs_is_refused(conn: sqlite3.Connection, jira_fake: Fake) -> None:
    with jira.lock():
        with pytest.raises(jira.Busy):
            jira.sync_all(conn)
        jira.poll(conn)
    assert not jira_fake.calls()


def test_jira_fields_are_normalised_and_a_missing_id_gets_a_stand_in() -> None:
    assert jira.norm("a  \r\nb\t\n\n") == "a\nb"
    assert jira.comment_id({"id": "9"}) == "9"
    assert jira.comment_id({"id": 7}) == "7"
    assert jira.comment_id({}).startswith("unknown-")
    assert jira.comment_id({}) != jira.comment_id({})


def test_the_board_jql_quotes_the_label_and_a_custom_one_replaces_it(
    conn: sqlite3.Connection, jira_fake: Fake
) -> None:
    store.config_set(conn, "jira.label", 'kanban "x"')
    b = store.get_board(conn, "main")
    assert jira.board_jql(conn, b) == (
        'project = KAN AND labels = "kanban \\"x\\"" AND assignee = currentUser()'
    )
    store.board_edit(conn, "main", jira_jql="project = KAN AND sprint in openSprints()")
    assert jira.board_jql(conn, store.get_board(conn, "main")).startswith(
        "project = KAN AND sprint"
    )


def test_sync_all_does_nothing_without_a_paired_board(conn: sqlite3.Connection, board: str) -> None:
    assert jira.sync_all(conn) == []
    with pytest.raises(Refused, match="not paired"):
        jira.sync_all(conn, only="main")


# --- the store half: pairing, the atlassian role, grants, the pieces the sync reuses ---------------


def test_pairing_needs_the_account_a_valid_key_and_a_pairing_before_a_jql(
    conn: sqlite3.Connection, board: str
) -> None:
    with pytest.raises(Refused, match="kanban config set jira.assignee"):
        store.board_edit(conn, board, jira_project="KAN")
    store.config_set(conn, "jira.assignee", "me@example.com")
    with pytest.raises(Refused, match="upper case"):
        store.board_edit(conn, board, jira_project="kan")
    with pytest.raises(Refused, match="needs a paired board"):
        store.board_edit(conn, board, jira_jql="project = KAN")
    with pytest.raises(Refused, match="cannot be combined"):
        store.board_edit(conn, board, jira_project="KAN", jira_off=True)
    store.board_edit(conn, board, jira_project="KAN", jira_jql="project = KAN AND labels = x")
    store.board_edit(conn, board, jira_project="KAN")
    assert store.get_board(conn, board)["jira_jql"] == "project = KAN AND labels = x"
    store.board_edit(conn, board, jira_project="OPS")
    assert store.get_board(conn, board)["jira_jql"] is None


def test_unpairing_keeps_the_links_and_stops_the_pass(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    store.board_edit(conn, "main", jira_off=True)
    board = store.get_board(conn, "main")
    assert (board["jira_project"], board["jira_jql"]) == (None, None)
    assert link(conn, tid)["jira_key"] == KEY
    assert sync() == []


def test_the_atlassian_role_is_dispatchable_only_with_the_flag_and_the_server(
    conn: sqlite3.Connection, card: Card
) -> None:
    tid = card("A")
    with pytest.raises(Refused, match="non-empty list"):
        store.config_set(conn, "jira.mcp_command", "npx mcp-atlassian")
    with pytest.raises(Refused, match="not a dispatchable role"):
        store.assign(conn, tid, "atlassian")
    store.config_set(conn, "roles.atlassian.enabled", True)
    assert "atlassian" not in store.active_roles(conn)
    store.config_set(conn, "jira.mcp_command", ["npx", "-y", "mcp-atlassian"])
    assert "atlassian" in store.active_roles(conn)
    assert "atlassian" not in store.cfg(conn, "dispatch.roles")
    assert store.assign(conn, tid, "atlassian")["assignee"] == "atlassian"
    store.config_set(conn, "roles.atlassian.enabled", False)
    assert "atlassian" not in store.active_roles(conn)


def test_a_grant_may_name_one_mcp_tool_but_never_a_wildcard(
    conn: sqlite3.Connection, card: Card
) -> None:
    tid = card("A")
    store.grant_add(conn, tid, ["mcp__atlassian__jira_add_comment"], "owner, 2026-10-10")
    for rule in ("mcp__atlassian", "mcp__atlassian__*", "mcp__*__jira_add_comment"):
        with pytest.raises(Refused, match="exact action"):
            store.grant_add(conn, tid, [rule], "owner, 2026-10-10")


def test_a_jira_comment_id_is_stored_once(conn: sqlite3.Connection, card: Card) -> None:
    tid = card("A")
    store.comment(conn, tid, "one", author="jira:Pat", actor="jira", jira_id="7")
    with pytest.raises(sqlite3.IntegrityError):
        store.comment(conn, tid, "two", author="jira:Pat", actor="jira", jira_id="7")


def test_mark_done_and_reopen_take_the_statuses_they_are_told_to_expect(
    conn: sqlite3.Connection, card: Card
) -> None:
    tid = card("A")
    with pytest.raises(Refused):
        store.mark_done(conn, tid, "no")
    store.mark_done(conn, tid, "yes", expected=("ready",))
    with pytest.raises(Refused):
        store.reopen(conn, tid, "no", expected=("archived",))


def test_archiving_with_rm_removes_the_card_and_its_link_and_the_issue_stays_out(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    tid = card("A")
    sync()
    store.archive(conn, tid, rm=True)
    assert not conn.execute("SELECT 1 FROM jira_links").fetchone()
    assert not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone()
    [gone] = conn.execute(
        "SELECT task_id, payload FROM task_events WHERE kind = 'jira_unlinked'"
    ).fetchall()
    assert gone["task_id"] is None
    assert json.loads(gone["payload"])["key"] == KEY
    sync()
    sync()
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert len(jira_fake.calls("jira_create_issue")) == 1


# --- the owner verbs and the daemon's wake-up -------------------------------------------------


def test_the_boards_verb_pairs_and_unpairs_a_board(
    kb: Kb, conn: sqlite3.Connection, board: str, env: Path
) -> None:
    code, _, err = kb("boards", "edit", board, "--jira-project", "KAN")
    assert code == 4
    assert "kanban config set jira.assignee" in err
    store.config_set(conn, "jira.assignee", "me@example.com")
    assert (
        kb("boards", "edit", board, "--jira-project", "KAN", "--jira-jql", "project = KAN")[0] == 0
    )
    assert store.get_board(conn, board)["jira_jql"] == "project = KAN"
    assert (
        kb("boards", "create", "second", "--default-workdir", str(env), "--jira-project", "OPS")[0]
        == 0
    )
    assert store.get_board(conn, "second")["jira_project"] == "OPS"
    assert kb("boards", "edit", board, "--jira-off")[0] == 0
    assert store.get_board(conn, board)["jira_project"] is None
    assert kb("boards", "create", "third", "--default-workdir", str(env), "--jira-off")[0] == 2


def test_import_link_unlink_sync_and_status_from_the_command_line(
    kb: Kb, conn: sqlite3.Connection, jira_fake: Fake, card: Card, clock: Callable[[int], None]
) -> None:
    jira_fake.add("KAN-5", "Imported", "text", status="Done")
    code, out, _ = kb("jira", "import", "KAN-5", "--board", "main")
    assert code == 0
    assert "created for KAN-5" in out
    assert kb("jira", "import", "kan-5", "--board", "main")[0] == 4
    assert kb("jira", "import", "KAN-99", "--board", "main")[0] == 4
    assert kb("jira", "import", "not a key", "--board", "main")[0] == 4

    tid = card("Mine", body="mine")
    jira_fake.add("KAN-6", "Theirs", "their text")
    assert kb("jira", "link", tid, "KAN-6")[0] == 0
    assert kb("jira", "link", tid, "KAN-6")[0] == 4
    code, out, _ = kb("jira", "sync", "--board", "main")
    assert code == 0
    assert out.startswith("main: synced")
    t = store.get_task(conn, tid)
    assert (t["title"], t["body"]) == ("Theirs", "their text")

    jira_fake.mode(auth=True)
    clock(60)
    code, out, err = kb("jira", "status")
    assert code == 0
    assert "main  KAN  last pull" in out
    code, _, err = kb("jira", "sync")
    assert code == 1
    assert "paused" in err
    code, out, _ = kb("jira", "status")
    assert "PAUSED" in out

    assert kb("jira", "unlink", tid)[0] == 0
    assert kb("jira", "unlink", tid)[0] == 4
    assert kb("jira", "bogus")[0] == 2


def test_linking_a_card_keeps_its_text_in_a_conflict_comment_unless_the_texts_agree(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card, sync: Sync
) -> None:
    jira_fake.add("KAN-7", "Jira title", "jira body")
    jira_fake.add("KAN-8", "Same", "same text")
    mine, same = card("Mine", body="important plan"), card("Same", body="same text")
    jira.link_card(conn, mine, "KAN-7")
    jira.link_card(conn, same, "KAN-8")
    sync()
    t = store.get_task(conn, mine)
    assert (t["title"], t["body"]) == ("Jira title", "jira body")
    lost = " ".join(body for _, body in comments(conn, mine) if "conflict" in body)
    assert "Mine" in lost
    assert "important plan" in lost
    assert not [c for c in comments(conn, same) if "conflict" in c[1]]
    assert not jira_fake.calls("jira_update_issue")


def test_the_housekeeping_wake_up_runs_the_pass_every_poll_interval(
    conn: sqlite3.Connection, jira_fake: Fake, card: Card
) -> None:
    ts = 1_800_000_000
    tid = card("A")
    housekeeping.run_due(conn, ts)
    assert link(conn, tid)["jira_key"] == KEY
    searches = len(jira_fake.calls("jira_search"))
    housekeeping.run_due(conn, ts + 60)
    assert len(jira_fake.calls("jira_search")) == searches
    housekeeping.run_due(conn, ts + 120)
    assert len(jira_fake.calls("jira_search")) > searches
