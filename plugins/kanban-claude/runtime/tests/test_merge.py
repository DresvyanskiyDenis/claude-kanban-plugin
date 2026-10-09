"""TST-V: the merge job (REV-05..08). REV-01 (V9) lives in test_worker.py, next to the verb it guards."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, housekeeping, store

from .conftest import Calls, FakeProcs

Prs = Callable[[dict[str, dict[str, Any]]], None]
SHA = "abc1234def5678abc1234def5678abc1234def56"
PR = {
    "url": "https://example.invalid/acme/proj/pull/7",
    "state": "OPEN",
    "number": 7,
    "headRefName": "feat/x",
    "baseRefName": "feature/x",
    "headRefOid": SHA,
}
CLEAN = f"WARDEN: APPROVED PR #7 @ {SHA[:7]}"


def approved(conn: Any, repo: Path, summary: str = CLEAN, base: str | None = "feature/x") -> str:
    """A card whose review run completed with `summary`: done, with a merge pending for a stacked base."""
    store.board_create(conn, "main", default_workdir=str(repo))
    tid = str(
        store.create(
            conn, "main", "Work", body="b", assignee="smith", workspace=f"worktree:{repo}",
            branch="feat/x", base=base,
        )["id"]
    )  # fmt: skip
    build = store.claim(conn, tid)
    assert build
    store.request_review(conn, build, "PR #7", pr_number=7, pr_url=PR["url"])
    review = store.claim(conn, tid)
    assert review
    store.complete(conn, review, summary, default_branch="main")
    return tid


def merge_calls(env: Path) -> list[list[str]]:
    log = env / "gh.log"
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return [c for c in calls if c[:2] == ["pr", "merge"]]


def comments(conn: Any, tid: str) -> list[str]:
    return [
        r["body"]
        for r in conn.execute(
            "SELECT body FROM task_comments WHERE task_id = ? AND author = 'kanban' ORDER BY id",
            (tid,),
        )
    ]


def later() -> int:
    return store.now() + 100000


def test_v1_a_clean_approval_squash_merges_and_posts_a_comment(
    conn: Any, repo: Path, set_prs: Prs, env: Path
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)
    t = store.get_task(conn, tid)
    assert (t["status"], t["merge_state"]) == ("done", "pending")
    housekeeping.run_due(conn, later())
    t = store.get_task(conn, tid)
    assert t["merge_state"] == "merged" and t["merged_at"]
    call = merge_calls(env)[0]
    assert "--squash" in call and call[call.index("--match-head-commit") + 1] == SHA
    assert "--delete-branch" in call and call[call.index("-R") + 1] == "example.invalid/acme/proj"
    assert comments(conn, tid) == ["[merge] squash-merged PR #7 into feature/x"]


@pytest.mark.parametrize(
    ("change", "summary", "reason"),
    [
        ({}, f"{CLEAN} PARTIAL REVIEW: tests not run", "approval_not_clean"),
        ({"state": "DRAFT"}, CLEAN, "pr_not_open"),
        ({"baseRefName": "release/y"}, CLEAN, "base_changed"),
        ({"headRefName": "feat/other"}, CLEAN, "head_branch_changed"),
        ({"baseRefName": "main"}, CLEAN, "base_is_default"),
        ({"headRefOid": "f" * 40}, CLEAN, "head_moved"),
        ({"state": "CLOSED"}, CLEAN, "closed"),
    ],
)
def test_v1_each_precondition_fails_closed_and_names_itself(
    conn: Any,
    repo: Path,
    set_prs: Prs,
    env: Path,
    change: dict[str, Any],
    summary: str,
    reason: str,
) -> None:
    set_prs({"7": {**PR, **change}})
    tid = approved(conn, repo, summary)
    housekeeping.run_due(conn, later())
    t = store.get_task(conn, tid)
    assert t["merge_state"] == "failed" and t["status"] == "done"
    assert merge_calls(env) == []
    said = comments(conn, tid)[0]
    assert said.startswith(f"[merge] PR #7 into feature/x not merged: {reason}")
    assert ("kanban reopen-review" in said) == (reason != "closed")
    event = conn.execute("SELECT payload FROM task_events WHERE kind = 'merge_failed'").fetchone()
    assert json.loads(event["payload"])["reason"] == reason


def test_v1_an_unknown_default_branch_fails_closed(
    conn: Any, repo: Path, set_prs: Prs, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    monkeypatch.setenv("GH_FAKE_DEFAULT", "")
    tid = approved(conn, repo)
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "failed"
    assert "default_branch_unknown" in comments(conn, tid)[0] and merge_calls(env) == []


def test_v2_a_failing_merge_is_retried_three_times_then_shown_as_failed(
    conn: Any, repo: Path, set_prs: Prs, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    monkeypatch.setenv("GH_FAKE_MERGE_FAIL", "1")
    tid = approved(conn, repo)
    ts = store.now() + 1
    housekeeping.run_due(conn, ts)
    t = store.get_task(conn, tid)
    assert (t["merge_state"], t["merge_attempts"]) == ("pending", 1)
    assert t["merge_next_at"] >= store.now() + housekeeping.MERGE_RETRY_S - 2
    housekeeping.run_due(conn, ts)  # not due again yet
    assert store.get_task(conn, tid)["merge_attempts"] == 1
    for k in (1, 2):
        housekeeping.run_due(conn, ts + 1000 * k)
    t = store.get_task(conn, tid)
    assert t["merge_state"] == "failed"
    assert any("attempt 1 of 3 failed" in c for c in comments(conn, tid))
    assert "failed after 3 attempts" in comments(conn, tid)[-1]


def test_v2_the_pr_is_read_before_every_attempt_so_a_hand_merge_between_attempts_wins(
    conn: Any, repo: Path, set_prs: Prs, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    monkeypatch.setenv("GH_FAKE_MERGE_FAIL", "1")
    tid = approved(conn, repo)
    ts = later()
    housekeeping.run_due(conn, ts)
    assert len(merge_calls(env)) == 1
    set_prs({"7": {**PR, "state": "MERGED"}})
    housekeeping.run_due(conn, ts + 1000)
    assert store.get_task(conn, tid)["merge_state"] == "merged"
    assert len(merge_calls(env)) == 1


def test_v3_a_child_waits_for_merged_with_no_timeout(
    conn: Any, repo: Path, set_prs: Prs, procs: FakeProcs
) -> None:
    set_prs({"7": PR})
    parent = approved(conn, repo)
    child = str(
        store.create(conn, "main", "Next", body="b", assignee="smith", parents=[parent])["id"]
    )
    assert store.get_task(conn, child)["status"] == "todo"
    with store.tx(conn):
        conn.execute("UPDATE tasks SET completed_at = 1 WHERE id = ?", (parent,))
    assert dispatcher.promote(conn) == []
    housekeeping.run_due(conn, later())
    assert dispatcher.promote(conn) == [child]
    assert store.get_task(conn, child)["status"] == "ready"


def test_v4_a_default_base_pr_is_never_merged_by_the_housekeeping(
    conn: Any, repo: Path, set_prs: Prs, env: Path
) -> None:
    set_prs({"7": {**PR, "baseRefName": "main"}})
    tid = approved(conn, repo, base=None)
    t = store.get_task(conn, tid)
    assert (t["status"], t["merge_state"]) == ("done", None)
    assert conn.execute("SELECT 1 FROM task_events WHERE kind = 'awaiting_owner_merge'").fetchone()
    housekeeping.run_due(conn, later())
    assert merge_calls(env) == [] and store.get_task(conn, tid)["merge_state"] is None


def test_v5_a_runner_that_dies_after_the_approval_does_not_lose_the_merge(
    conn: Any, repo: Path, set_prs: Prs, procs: FakeProcs
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)
    with store.tx(conn):
        conn.execute("UPDATE task_runs SET runner_pid = 4242 WHERE task_id = ?", (tid,))
    report = dispatcher.dispatch_once(conn, store.now(), procs)
    assert report["reconciled"] == []
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "merged"


def test_v6_a_pr_merged_by_hand_is_recorded_without_calling_merge(
    conn: Any, repo: Path, set_prs: Prs, env: Path
) -> None:
    set_prs({"7": {**PR, "state": "MERGED"}})
    tid = approved(conn, repo)
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "merged"
    assert merge_calls(env) == []


def test_v7_a_moved_head_fails_and_reopen_review_recovers(
    conn: Any, repo: Path, set_prs: Prs
) -> None:
    set_prs({"7": {**PR, "headRefOid": "f" * 40}})
    tid = approved(conn, repo)
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "failed"
    store.reopen_review(conn, tid, "the head moved after approval")
    t = store.get_task(conn, tid)
    assert (t["status"], t["assignee"], t["merge_state"]) == ("ready", "smith", None)
    assert t["merge_attempts"] == 0


def test_v8_merge_retry_puts_a_failed_merge_back_in_the_queue(
    conn: Any, repo: Path, set_prs: Prs
) -> None:
    set_prs({"7": {**PR, "headRefOid": "f" * 40}})
    tid = approved(conn, repo)
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "failed"
    set_prs({"7": PR})
    store.merge_retry(conn, tid)
    assert store.get_task(conn, tid)["merge_state"] == "pending"
    housekeeping.run_due(conn, later())
    assert store.get_task(conn, tid)["merge_state"] == "merged"


def test_v2_a_merge_that_errored_but_landed_ends_merged_on_the_last_attempt(
    conn: Any, repo: Path, set_prs: Prs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REV-06: a 5xx after the server merged must not leave the card `failed`."""
    set_prs({"7": PR})
    tid = approved(conn, repo)
    ts = later()
    monkeypatch.setenv("GH_FAKE_MERGE_FAIL", "1")
    for k in (0, 1):
        housekeeping.run_due(conn, ts + 1000 * k)
    assert store.get_task(conn, tid)["merge_attempts"] == 2
    monkeypatch.setenv("GH_FAKE_MERGE_FAIL", "landed")
    housekeeping.run_due(conn, ts + 3000)
    t = store.get_task(conn, tid)
    assert t["merge_state"] == "merged" and t["merged_at"]
    assert comments(conn, tid)[-1] == "[merge] squash-merged PR #7 into feature/x"


def test_v2_an_unexpected_error_is_an_attempt_not_a_crash(
    conn: Any, repo: Path, set_prs: Prs, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("surprise")

    monkeypatch.setattr(housekeeping.ghops, "pr_view", boom)
    housekeeping.run_due(conn, later())
    t = store.get_task(conn, tid)
    assert (t["merge_state"], t["merge_attempts"]) == ("pending", 1)
    assert "surprise" in comments(conn, tid)[0]


def test_a_job_that_raises_does_not_stop_the_next_one(
    conn: Any, repo: Path, set_prs: Prs, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    approved(conn, repo)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("surprise")

    monkeypatch.setattr(housekeeping, "merge_one", boom)
    monkeypatch.setattr(housekeeping, "sweep", boom)
    housekeeping.run_due(conn, later())
    assert "retention" in (store.state_get(conn, "housekeeping") or {})


def test_settle_speaks_only_when_it_changed_the_card(conn: Any, repo: Path, set_prs: Prs) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)
    t = store.get_task(conn, tid)
    with store.tx(conn):
        conn.execute("UPDATE tasks SET merge_state = NULL WHERE id = ?", (tid,))
    housekeeping.settle(conn, t, "merged", "merged", {"pr": 7}, "[merge] squash-merged PR #7")
    assert comments(conn, tid) == [] and store.get_task(conn, tid)["merge_state"] is None
    assert not conn.execute("SELECT 1 FROM task_events WHERE kind = 'merged'").fetchone()


@pytest.mark.parametrize("move", ["reopen", "archive"])
def test_a_card_that_left_the_queue_after_selection_is_not_merged(
    conn: Any, repo: Path, set_prs: Prs, env: Path, move: str
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)
    selected = store.get_task(conn, tid)
    if move == "reopen":
        store.reopen(conn, tid, "the owner changed their mind")
    else:
        store.archive(conn, tid)
    housekeeping.merge_one(conn, selected)
    assert merge_calls(env) == []
    assert store.get_task(conn, tid)["merge_state"] != "merged"


def test_a_card_archived_while_a_merge_is_pending_is_left_alone(
    conn: Any, repo: Path, set_prs: Prs, env: Path
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)
    store.archive(conn, tid)
    housekeeping.run_due(conn, later())
    assert [c for c in Calls(env / "gh.log")() if c[0] == "pr"] == []


def test_a_retry_does_not_touch_a_card_that_was_reopened_during_the_attempt(
    conn: Any, repo: Path, set_prs: Prs, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_prs({"7": PR})
    tid = approved(conn, repo)

    def reopen_then_fail(*_a: Any, **_k: Any) -> None:
        store.reopen(conn, tid, "the owner changed their mind")
        raise RuntimeError("surprise")

    monkeypatch.setattr(housekeeping.ghops, "pr_view", reopen_then_fail)
    housekeeping.run_due(conn, later())
    t = store.get_task(conn, tid)
    assert (t["merge_attempts"], t["merge_next_at"]) == (0, None)
    assert comments(conn, tid) == []
