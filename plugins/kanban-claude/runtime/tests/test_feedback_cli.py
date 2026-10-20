import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import cli, gitops, housekeeping, jira, store, views
from kanban_claude.errors import NotFound, Refused

from .conftest import Kb, git, push_commit

Card = Callable[..., str]
PR_URL = "https://example.invalid/acme/proj/pull/12"


def row(conn: Any, tid: str) -> Any:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def kinds(conn: Any, tid: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,))
    ]


def claim_run(conn: Any, tid: str) -> int:
    run = store.claim(conn, tid)
    assert run
    return run


def blocked(conn: Any, card: Card, kind: str = "needs_input") -> str:
    tid = card()
    store.block(conn, claim_run(conn, tid), kind, "why")
    return tid


# --- L3: leaving `blocked` drops the block chip ----------------------------------------------

LEAVERS: dict[str, Callable[[Any, str], Any]] = {
    "unblock": lambda c, t: store.unblock(c, t, "answered"),
    "hold": lambda c, t: store.hold(c, t, "parked"),
    "mark_done": lambda c, t: store.mark_done(c, t, "handled elsewhere"),
    "archive": lambda c, t: store.archive(c, t),
}


@pytest.mark.parametrize("leave", sorted(LEAVERS))
def test_l3_leaving_blocked_clears_block_kind_and_recurrences(
    conn: Any, card: Card, leave: str
) -> None:
    tid = blocked(conn, card)
    assert row(conn, tid)["block_kind"] == "needs_input"
    LEAVERS[leave](conn, tid)
    t = row(conn, tid)
    assert (t["block_kind"], t["block_recurrences"]) == (None, 0)


def test_l3_a_stopped_card_shows_no_chip_once_it_runs_again(conn: Any, card: Card) -> None:
    tid = card()
    claim_run(conn, tid)
    store.stop(conn, tid)
    assert row(conn, tid)["block_kind"] == "stopped"
    store.unblock(conn, tid, "go on")
    claim_run(conn, tid)
    t = row(conn, tid)
    assert (t["status"], t["block_kind"]) == ("running", None)


def test_l3_promoting_a_looped_card_clears_the_chip(conn: Any, card: Card) -> None:
    tid = card()
    for _ in range(3):
        store.block(conn, claim_run(conn, tid), "needs_input", "same")
        if row(conn, tid)["status"] == "blocked":
            store.unblock(conn, tid, "again")
    t = row(conn, tid)
    assert (t["status"], t["block_kind"]) == ("triage", "needs_input")
    store.specify(conn, tid, {})
    assert row(conn, tid)["block_kind"] is None


def test_l3_loop_detection_survives_the_cleared_columns(conn: Any, card: Card) -> None:
    tid = card()
    for expected in ("blocked", "blocked", "triage"):
        store.block(conn, claim_run(conn, tid), "needs_input", "same")
        assert row(conn, tid)["status"] == expected
        if expected == "blocked":
            store.unblock(conn, tid, "again")
            assert row(conn, tid)["block_recurrences"] == 0
    assert "block_loop_detected" in kinds(conn, tid)


# --- A1: snapshot --ref ----------------------------------------------------------------------


@pytest.fixture
def refs(origin: Path, repo: Path, env: Path) -> dict[str, str]:
    """origin gains `main` + b.txt, branch `feat/x` + c.txt, and a tag `v1` + d.txt on no branch."""
    main = push_commit(env, "b.txt")
    other = env / "other"
    git("checkout", "-q", "-b", "feat/x", cwd=other)
    (other / "c.txt").write_text("c")
    git("add", "c.txt", cwd=other)
    git("commit", "-q", "-m", "c", cwd=other)
    git("push", "-q", "origin", "feat/x", cwd=other)
    git("checkout", "-q", "--detach", main, cwd=other)
    (other / "d.txt").write_text("d")
    git("add", "d.txt", cwd=other)
    git("commit", "-q", "-m", "d", cwd=other)
    git("tag", "v1", cwd=other)
    git("push", "-q", "origin", "v1", cwd=other)
    return {"main": main}


def files(path: Path) -> set[str]:
    return {p.name for p in path.iterdir() if p.is_file() and p.name != ".git"}


def test_a1_snapshot_ref_checks_out_a_branch_a_tag_or_a_sha(
    refs: dict[str, str], repo: Path
) -> None:
    plain, _ = gitops.snapshot(repo)
    assert files(plain) == {"a.txt", "b.txt"}
    branch, _ = gitops.snapshot(repo, ref="feat/x")
    assert branch != plain and files(branch) == {"a.txt", "b.txt", "c.txt"}
    tag, _ = gitops.snapshot(repo, ref="v1")
    assert files(tag) == {"a.txt", "b.txt", "d.txt"}
    sha, _ = gitops.snapshot(repo, ref=refs["main"])
    assert files(sha) == {"a.txt", "b.txt"}
    assert git("branch", "--show-current", cwd=sha) == ""
    back, _ = gitops.snapshot(repo)
    assert files(back) == {"a.txt", "b.txt"}


def test_a1_two_refs_of_one_repo_get_two_paths_that_keep_their_own_head(
    refs: dict[str, str], repo: Path
) -> None:
    first, _ = gitops.snapshot(repo, ref="feat/x")
    second, _ = gitops.snapshot(repo, ref="v1")
    assert first != second
    assert files(first) == {"a.txt", "b.txt", "c.txt"}
    assert files(second) == {"a.txt", "b.txt", "d.txt"}
    again, _ = gitops.snapshot(repo, ref="feat/x")
    assert again == first and files(first) == {"a.txt", "b.txt", "c.txt"}


def test_a1_an_unknown_ref_after_a_failed_fetch_names_the_fetch_failure(repo: Path) -> None:
    with pytest.raises(NotFound, match="git fetch failed"):
        gitops.snapshot(repo, ref="no/such/ref")


def test_a1_snapshot_refuses_an_unknown_ref_and_an_option_lookalike(
    refs: dict[str, str], repo: Path, kb: Kb
) -> None:
    code, _, err = kb("snapshot", str(repo), "--ref", "no/such/ref")
    assert code == 3 and "no/such/ref" in err
    code, _, err = kb("snapshot", str(repo), "--ref=--force")
    assert code == 2 and "--ref" in err


def test_a1_snapshot_ref_needs_a_git_repository(tmp_path: Path, kb: Kb) -> None:
    code, _, err = kb("snapshot", str(tmp_path), "--ref", "main")
    assert code == 2 and "git repository" in err


def test_a1_cli_prints_the_path_of_the_ref_checkout(
    refs: dict[str, str], repo: Path, kb: Kb
) -> None:
    code, out, _ = kb("snapshot", str(repo), "--ref", "feat/x")
    assert code == 0 and (Path(out.strip()) / "c.txt").exists()
    code, out, _ = kb("snapshot", str(repo))
    assert code == 0 and not (Path(out.strip()) / "c.txt").exists()


# --- A2c: create --grant / --approved --------------------------------------------------------

APPROVED = "owner said go · chat · 2026-01-01"
MAKE = ("create", "Fix it", "--board", "main", "--assignee", "smith", "--body", "do it")


def count(conn: Any, table: str) -> int:
    where = " WHERE task_id IS NOT NULL" if table == "task_events" else ""
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}{where}").fetchone()[0])


def test_a2c_create_with_grants_writes_the_card_and_one_grant(
    conn: Any, board: str, kb: Kb
) -> None:
    code, out, err = kb(
        *MAKE,
        "--grant",
        "Bash(git status)",
        "--grant",
        "Read(docs/a.md)",
        "--approved",
        APPROVED,
        "--json",
    )
    assert code == 0, err
    got = json.loads(out)
    grants = store.grants_list(conn, got["id"])
    assert [(g["rules"], g["approved"]) for g in grants] == [
        (["Bash(git status)", "Read(docs/a.md)"], APPROVED)
    ]
    assert got["grant_id"] == grants[0]["id"]
    assert kinds(conn, got["id"]) == ["created", "grant_added"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--grant", "Bash(*)", "--approved", APPROVED],
        ["--grant", "Bash(git:*)", "--approved", APPROVED],
        ["--grant", "Bash", "--approved", APPROVED],
        ["--grant", "Bash(git status)"],
        ["--grant", "Bash(git status)", "--approved", "  "],
        ["--approved", APPROVED],
    ],
)
def test_a2c_a_bad_grant_refuses_before_anything_is_written(
    conn: Any, board: str, kb: Kb, extra: list[str]
) -> None:
    code, _, err = kb(*MAKE, *extra)
    assert code == 4, err
    assert (count(conn, "tasks"), count(conn, "grants"), count(conn, "task_events")) == (0, 0, 0)


def test_a2c_without_grant_nothing_changes(conn: Any, board: str, kb: Kb) -> None:
    code, out, _ = kb(*MAKE, "--json")
    assert code == 0 and "grant_id" not in json.loads(out)
    assert count(conn, "grants") == 0


def test_a2c_the_card_and_its_grant_are_one_transaction(
    conn: Any, board: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise sqlite3.IntegrityError("grant failed")

    monkeypatch.setattr(store, "_insert_grant", boom)
    with pytest.raises(sqlite3.IntegrityError):
        store.create(
            conn,
            board,
            "x",
            body="b",
            assignee="smith",
            grants=["Bash(git status)"],
            approved=APPROVED,
        )
    assert (count(conn, "tasks"), count(conn, "task_events")) == (0, 0)


def test_a2c_an_idempotent_repeat_adds_no_second_grant_and_says_so(
    conn: Any, board: str, kb: Kb
) -> None:
    grant = ["--grant", "Bash(git status)", "--approved", APPROVED, "--idempotency-key", "k1"]
    assert kb(*MAKE, *grant)[0] == 0
    code, out, err = kb(*MAKE, *grant, "--json")
    got = json.loads(out)
    assert code == 0 and got["existing"] is True and count(conn, "grants") == 1
    assert "grant" in got["warnings"][0] and "warning:" in err


# --- L4: a parent in triage warns ------------------------------------------------------------


def test_l4_create_under_a_triage_parent_warns_on_stderr_and_in_json(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    parent = card("idea", triage=True)
    code, out, err = kb(*MAKE, "--parent", parent, "--json")
    got = json.loads(out)
    msg = f"parent {parent} is in triage; {got['id']} waits until it is done"
    assert code == 0 and got["status"] == "todo"
    assert got["warnings"] == [msg] and f"warning: {msg}" in err


def test_l4_link_to_a_triage_parent_warns_the_same_way(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    parent, child = card("idea", triage=True), card("work")
    code, out, err = kb("link", parent, child, "--board", "main", "--json")
    msg = f"parent {parent} is in triage; {child} waits until it is done"
    assert code == 0 and json.loads(out)["warnings"] == [msg] and f"warning: {msg}" in err


def test_l4_other_parents_stay_quiet(conn: Any, board: str, card: Card, kb: Kb) -> None:
    code, out, err = kb(*MAKE, "--parent", card("ready one"), "--json")
    assert code == 0 and "warnings" not in json.loads(out) and "warning" not in err
    parent, child = card("a"), card("b")
    code, out, err = kb("link", parent, child, "--board", "main", "--json")
    assert code == 0 and "warnings" not in json.loads(out) and "warning" not in err


# --- L5 / S5: why a card waits, and a done card awaiting the owner's merge --------------------

APPROVE = "WARDEN: APPROVED PR #12 @ abcdef1"


def approved_card(conn: Any, card: Card, **kw: Any) -> str:
    """A card whose review was approved: done, with the PR still to be merged."""
    tid = card(**kw)
    store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
    store.complete(conn, claim_run(conn, tid), APPROVE, default_branch="main")
    return tid


def listed(kb: Kb) -> tuple[dict[str, dict[str, Any]], str]:
    code, out, err = kb("list", "--board", "main", "--json")
    assert code == 0, err
    cards = {c["id"]: c for c in json.loads(out)["cards"]}
    return cards, kb("list", "--board", "main")[1]


def shown(kb: Kb, tid: str) -> tuple[dict[str, Any], str]:
    code, out, err = kb("show", tid, "--board", "main", "--json")
    assert code == 0, err
    return json.loads(out)["task"], kb("show", tid, "--board", "main")[1]


def test_l5_a_todo_card_names_the_parent_it_waits_for(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    parent = card("first")
    child = card("second", parents=[parent])
    cards, text = listed(kb)
    assert cards[child]["waiting_for"] == [
        {"id": parent, "status": "ready", "block_kind": None, "merge_state": None}
    ]
    assert cards[parent]["waiting_for"] == []
    assert f"waits for {parent} (ready)" in text
    task, text = shown(kb, child)
    assert task["waiting_for"] == cards[child]["waiting_for"]
    assert f"waits for {parent} (ready)" in text
    assert shown(kb, parent)[0]["waiting_for"] == []


def test_l5_a_blocked_parent_shows_its_block_kind(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    parent = blocked(conn, card, "transient")
    child = card("second", parents=[parent])
    cards, text = listed(kb)
    assert cards[child]["waiting_for"][0]["block_kind"] == "transient"
    assert f"waits for {parent} (blocked: transient)" in text
    assert f"waits for {parent} (blocked: transient)" in shown(kb, child)[1]


def test_l5_every_open_parent_is_named_and_a_finished_one_is_not(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    done, open_a, open_b = card("done"), card("a", triage=True), card("b")
    store.complete(conn, claim_run(conn, done), "ok")
    child = card("child", parents=[done, open_a, open_b])
    cards, text = listed(kb)
    assert [w["id"] for w in cards[child]["waiting_for"]] == sorted([open_a, open_b])
    line = next(ln for ln in text.splitlines() if ln.startswith(child))
    assert open_a in line and open_b in line and done not in line


def test_l5_the_wait_follows_the_store_rule_for_a_parent_still_to_merge(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    parent = card("p")
    child = card("c", parents=[parent])
    store.complete(conn, claim_run(conn, parent), "ok")
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
    for merge, waits in (("pending", True), ("failed", True), ("merged", False), (None, False)):
        conn.execute("UPDATE tasks SET merge_state = ? WHERE id = ?", (merge, parent))
        cards, _ = listed(kb)
        assert (
            bool(cards[child]["waiting_for"]) is waits is (not store.parents_satisfied(conn, child))
        )
    conn.execute("UPDATE tasks SET merge_state = 'pending' WHERE id = ?", (parent,))
    assert f"waits for {parent} (done, merge pending)" in listed(kb)[1]


def test_l5_a_card_that_is_not_todo_never_waits(conn: Any, board: str, card: Card, kb: Kb) -> None:
    parent = card("p", triage=True)
    child = card("c")
    store.link(conn, parent, child)
    store.hold(conn, child, "park it")
    cards, text = listed(kb)
    assert cards[child]["status"] == "triage" and cards[child]["waiting_for"] == []
    assert "waits for" not in text


def test_l5_list_stays_one_query(conn: Any, board: str, card: Card) -> None:
    parents = [card(f"p{i}") for i in range(3)]
    for i in range(5):
        card(f"c{i}", parents=parents)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    cards = views.cards_list(conn, board)
    conn.set_trace_callback(None)
    assert len(cards) == 8 and len([s for s in seen if s.lstrip().startswith("SELECT")]) == 1


def test_s5_a_done_card_awaiting_the_owners_merge_says_so(
    conn: Any, board: str, card: Card, kb: Kb
) -> None:
    waiting = approved_card(conn, card)
    plain = card("plain")
    store.complete(conn, claim_run(conn, plain), "ok")
    cards, text = listed(kb)
    assert cards[waiting]["awaiting_merge"] is True and cards[plain]["awaiting_merge"] is False
    line = next(x for x in text.splitlines() if x.startswith(waiting))
    assert "awaiting merge" in line
    assert "awaiting merge" not in next(x for x in text.splitlines() if x.startswith(plain))
    task, text = shown(kb, waiting)
    assert task["awaiting_merge"] is True and "awaiting merge" in text
    task, text = shown(kb, plain)
    assert task["awaiting_merge"] is False and "awaiting merge" not in text


@pytest.mark.parametrize("end", ["merged", "stacked", "owner_done", "reopened"])
def test_s5_the_flag_ends_when_the_merge_is_settled_another_way(
    conn: Any, board: str, card: Card, kb: Kb, end: str, repo: Path
) -> None:
    if end == "stacked":
        tid = approved_card(
            conn, card, workspace=f"worktree:{repo}", branch="feature/x", base="feature/base"
        )
    elif end == "owner_done":
        tid = card()
        store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
        store.mark_done(conn, tid, "merged by hand")
    else:
        tid = approved_card(conn, card)
    if end == "merged":
        conn.execute("UPDATE tasks SET merge_state = 'merged' WHERE id = ?", (tid,))
    elif end == "reopened":
        store.reopen(conn, tid, "more work")
        store.complete(conn, claim_run(conn, tid), "second pass")
    assert listed(kb)[0][tid]["awaiting_merge"] is False
    assert shown(kb, tid)[0]["awaiting_merge"] is False


# --- A3 / S4: the owner's request-review and request-changes ---------------------------------

SHA = "abc1234def5678abc1234def5678abc1234def56"
PR12 = {
    "url": PR_URL,
    "state": "OPEN",
    "number": 12,
    "headRefName": "feat/x",
    "baseRefName": "main",
    "headRefOid": SHA,
}
Prs = Callable[[dict[str, dict[str, Any]]], None]


def worktree_card(conn: Any, board: str, repo: Path, base: str | None = None) -> str:
    return str(
        store.create(
            conn, board, "Work", body="b", assignee="smith", workspace=f"worktree:{repo}",
            branch="feat/x", base=base,
        )["id"]
    )  # fmt: skip


def test_s3_the_flag_ends_when_the_owner_merges_the_pr_on_github(
    conn: Any, board: str, repo: Path, set_prs: Prs
) -> None:
    tid = worktree_card(conn, board, repo)
    store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
    store.complete(conn, claim_run(conn, tid), APPROVE, default_branch="main")
    conn.execute("UPDATE tasks SET worktree_state = 'removed' WHERE id = ?", (tid,))

    def awaiting() -> bool:
        return next(c for c in views.cards_list(conn, board) if c["id"] == tid)["awaiting_merge"]

    set_prs({"12": PR12})
    housekeeping.sweep(conn)
    assert awaiting() is True
    set_prs({"12": {**PR12, "state": "MERGED"}})
    housekeeping.sweep(conn)
    assert awaiting() is False and row(conn, tid)["merged_at"] is not None


def blocked_in_review(conn: Any, card: Card) -> str:
    """Smith opened a PR, warden's review run then blocked: assignee warden, implementer smith."""
    tid = card()
    store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
    store.block(conn, claim_run(conn, tid), "needs_input", "cannot tell")
    return tid


def test_a3_the_owner_sends_a_blocked_builder_card_to_review(
    conn: Any, board: str, repo: Path, kb: Kb, set_prs: Prs
) -> None:
    tid = worktree_card(conn, board, repo)
    store.block(conn, claim_run(conn, tid), "needs_input", "waiting on the owner")
    set_prs({"12": PR12})
    code, out, err = kb("request-review", tid, "--summary", "PR #12 pushed by hand", "--json")
    assert code == 0, err
    assert json.loads(out)["status"] == "review"
    t = row(conn, tid)
    assert (t["status"], t["assignee"], t["implementer"]) == ("review", "warden", "smith")
    assert (t["pr_number"], t["pr_url"], t["block_kind"]) == (12, PR_URL, None)
    ev = conn.execute(
        "SELECT actor, payload FROM task_events WHERE task_id = ? AND kind = 'review_requested'",
        (tid,),
    ).fetchone()
    assert (ev["actor"], json.loads(ev["payload"])) == (
        store.OWNER,
        {"implementer": "smith", "pr": 12},
    )
    last = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (tid,)
    ).fetchone()
    assert (last["author"], last["body"]) == ("owner", "REVIEW REQUESTED: PR #12 pushed by hand")


def test_a3_a_card_blocked_in_a_review_run_goes_back_to_its_builder_as_implementer(
    conn: Any, card: Card
) -> None:
    tid = blocked_in_review(conn, card)
    assert (row(conn, tid)["assignee"], row(conn, tid)["implementer"]) == ("warden", "smith")
    store.request_review_owner(conn, tid, "PR #12", pr_number=13, pr_url=PR_URL)
    t = row(conn, tid)
    assert (t["status"], t["assignee"], t["implementer"], t["pr_number"]) == (
        "review",
        "warden",
        "smith",
        13,
    )


def test_a3_it_refuses_every_other_status_and_a_card_without_a_builder(
    conn: Any, card: Card
) -> None:
    def refused(tid: str) -> None:
        before = dict(row(conn, tid))
        with pytest.raises(Refused):
            store.request_review_owner(conn, tid, "PR #12", pr_number=12, pr_url=PR_URL)
        assert dict(row(conn, tid)) == before

    refused(card("ready"))
    refused(card("triage", triage=True))
    running = card("running")
    claim_run(conn, running)
    refused(running)
    review = card("review")
    store.request_review(conn, claim_run(conn, review), "PR #12", pr_number=12, pr_url=PR_URL)
    refused(review)
    done = card("done")
    store.complete(conn, claim_run(conn, done), "ok")
    refused(done)
    scout = card("scout", assignee="scout")
    store.block(conn, claim_run(conn, scout), "needs_input", "why")
    refused(scout)


def test_a3_the_pr_is_checked_like_the_workers_and_the_card_stays_blocked(
    conn: Any, board: str, repo: Path, kb: Kb, set_prs: Prs
) -> None:
    tid = worktree_card(conn, board, repo)
    store.block(conn, claim_run(conn, tid), "needs_input", "why")
    for pr, summary in (
        ({**PR12, "state": "MERGED"}, "PR #12"),
        ({**PR12, "headRefName": "feat/other"}, "PR #12"),
        (PR12, "no number here"),
    ):
        set_prs({"12": pr})
        assert kb("request-review", tid, "--summary", summary)[0] == 4
        assert row(conn, tid)["status"] == "blocked"
    set_prs({})
    assert kb("request-review", tid, "--summary", "PR #12")[0] != 0
    assert row(conn, tid)["status"] == "blocked"


def test_a3_a_non_blocked_card_is_refused_before_gh_is_asked(
    conn: Any, board: str, repo: Path, kb: Kb, set_prs: Prs, env: Path
) -> None:
    tid = worktree_card(conn, board, repo)
    set_prs({"12": PR12})
    code, _, err = kb("request-review", tid, "--summary", "PR #12")
    assert code == 4 and "blocked" in err
    assert not (env / "gh.log").exists()


def test_a3_an_inactive_reviewer_is_refused(conn: Any, card: Card) -> None:
    tid = blocked_in_review(conn, card)
    with pytest.raises(Refused, match="reviewer"):
        store.request_review_owner(
            conn, tid, "PR #12", pr_number=12, pr_url=PR_URL, reviewer="nobody"
        )
    assert row(conn, tid)["status"] == "blocked"


def test_a3_the_worker_forms_are_still_for_workers(kb: Kb, conn: Any, card: Card) -> None:
    for verb in ("block", "complete"):
        tid = card()
        args = ["--kind", "needs_input", "--reason", "r"] if verb == "block" else ["--summary", "s"]
        assert kb(verb, tid, *args)[0] == 5


def test_a3_approval_of_an_owner_requested_review_reaches_the_stacked_merge(
    conn: Any, board: str, repo: Path, set_prs: Prs, env: Path
) -> None:
    pr = {**PR12, "baseRefName": "feature/x"}
    set_prs({"12": pr})
    tid = worktree_card(conn, board, repo, base="feature/x")
    store.block(conn, claim_run(conn, tid), "needs_input", "why")
    store.request_review_owner(conn, tid, "PR #12", pr_number=12, pr_url=PR_URL)
    store.complete(
        conn, claim_run(conn, tid), f"WARDEN: APPROVED PR #12 @ {SHA[:7]}", default_branch="main"
    )
    t = row(conn, tid)
    assert (t["status"], t["merge_state"]) == ("done", "pending")
    housekeeping.run_due(conn, store.now() + 100000)
    t = row(conn, tid)
    assert t["merge_state"] == "merged"
    log = [json.loads(x) for x in (env / "gh.log").read_text().splitlines()]
    assert any(c[:2] == ["pr", "merge"] and "--squash" in c for c in log)


def test_a3_approval_into_the_default_branch_awaits_the_owners_merge(conn: Any, card: Card) -> None:
    tid = blocked_in_review(conn, card)
    store.request_review_owner(conn, tid, "PR #12", pr_number=12, pr_url=PR_URL)
    store.complete(conn, claim_run(conn, tid), APPROVE, default_branch="main")
    t = row(conn, tid)
    assert (t["status"], t["merge_state"]) == ("done", None)
    assert kinds(conn, tid)[-1] == "awaiting_owner_merge"
    assert views.cards_list(conn, "main")[0]["awaiting_merge"] is True


def reviewed_and_blocked(conn: Any, card: Card, **kw: Any) -> str:
    tid = card(**kw)
    store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
    store.block(conn, claim_run(conn, tid), "needs_input", "cannot tell")
    return tid


def test_s4_the_owner_sends_a_review_blocked_card_back_to_its_builder(
    conn: Any, card: Card, kb: Kb
) -> None:
    tid = reviewed_and_blocked(conn, card)
    code, out, err = kb("request-changes", tid, "--reason", "rename the flag", "--json")
    assert code == 0, err
    assert json.loads(out)["status"] == "ready"
    t = row(conn, tid)
    assert (t["status"], t["assignee"], t["block_kind"]) == ("ready", "smith", None)
    comment = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (tid,)
    ).fetchone()
    assert (comment["author"], comment["body"]) == ("owner", "CHANGES REQUESTED: rename the flag")
    ev = conn.execute(
        "SELECT actor, payload FROM task_events WHERE task_id = ? AND kind = 'changes_requested'",
        (tid,),
    ).fetchone()
    assert (ev["actor"], json.loads(ev["payload"])) == (store.OWNER, {"reason": "rename the flag"})
    assert "CHANGES REQUESTED: rename the flag" in views.context_md(conn, tid)


def test_s4_the_builders_next_run_sees_the_reason_and_the_card_waits_on_open_parents(
    conn: Any, card: Card
) -> None:
    parent = card("parent", triage=True)
    tid = reviewed_and_blocked(conn, card)
    conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, tid))
    assert store.request_changes_owner(conn, tid, "do it again")["status"] == "todo"
    assert row(conn, tid)["assignee"] == "smith"


def test_s4_it_refuses_a_work_run_block_other_statuses_and_a_card_with_no_implementer(
    conn: Any, card: Card
) -> None:
    def refused(tid: str) -> None:
        before = dict(row(conn, tid))
        with pytest.raises(Refused):
            store.request_changes_owner(conn, tid, "why")
        assert dict(row(conn, tid)) == before

    refused(blocked(conn, card))
    refused(card("ready"))
    review = card("review")
    store.request_review(conn, claim_run(conn, review), "PR #12", pr_number=12, pr_url=PR_URL)
    refused(review)
    orphan = reviewed_and_blocked(conn, card)
    conn.execute("UPDATE tasks SET implementer = NULL WHERE id = ?", (orphan,))
    refused(orphan)
    with pytest.raises(Refused, match="reason"):
        store.request_changes_owner(conn, reviewed_and_blocked(conn, card), " ")


@pytest.mark.parametrize("verb", ["request-review", "request-changes"])
def test_a3_s4_the_owner_forms_wake_the_dispatcher(
    conn: Any,
    board: str,
    card: Card,
    kb: Kb,
    verb: str,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    set_prs: Prs,
) -> None:
    woken: list[int] = []
    monkeypatch.setattr(cli, "nudge", lambda: woken.append(1))
    if verb == "request-review":
        tid = worktree_card(conn, board, repo)
        store.block(conn, claim_run(conn, tid), "needs_input", "why")
        set_prs({"12": PR12})
        assert kb(verb, tid, "--summary", "PR #12")[0] == 0
    else:
        assert kb(verb, reviewed_and_blocked(conn, card), "--reason", "r")[0] == 0
    assert woken == [1]


# --- U3: the tokens a run's result event reports ---------------------------------------------

HAIKU = "claude-haiku-x"
OTHER = "claude-other-x"


def usage_of(i: int, o: int, w: int, r: int) -> dict[str, int]:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_creation_input_tokens": w,
        "cache_read_input_tokens": r,
    }


def per_model(i: int, o: int, w: int, r: int, cost: float) -> dict[str, float]:
    return {
        "inputTokens": i,
        "outputTokens": o,
        "cacheCreationInputTokens": w,
        "cacheReadInputTokens": r,
        "costUSD": cost,
    }


def result_fixture(
    where: Path, name: str, *, cost: float, usage: dict[str, int], models: dict[str, Any]
) -> None:
    """A fixture `fake_claude` plays (it takes an absolute name as it is): one successful result
    carrying `usage` and `modelUsage`."""
    init = {"type": "system", "subtype": "init", "session_id": "s1", "model": HAIKU}
    res = {
        "type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
        "result": "Done.", "total_cost_usd": cost, "session_id": "s1",
        "usage": usage, "modelUsage": models,
    }  # fmt: skip
    (where / f"{name}.jsonl").write_text("\n".join(json.dumps(e) for e in (init, res)) + "\n")


def resumed_twice(conn: Any, card: Card, execute: Any, tmp_path: Path) -> tuple[Any, Any]:
    """One session, two invocations; `modelUsage` and the cost are cumulative, `usage` is not."""
    result_fixture(
        tmp_path, "first", cost=0.5, usage=usage_of(2, 4, 100, 0),
        models={HAIKU: per_model(2, 4, 100, 0, 0.5)},
    )  # fmt: skip
    result_fixture(
        tmp_path, "second", cost=0.8, usage=usage_of(3, 6, 50, 150),
        models={HAIKU: per_model(5, 10, 150, 150, 0.8), OTHER: per_model(1, 1, 0, 0, 0.0)},
    )  # fmt: skip
    tid = card()
    first = execute(tid, str(tmp_path / "first"))
    second = execute(tid, str(tmp_path / "second"))
    assert first["session_id"] == second["session_id"]
    return first, second


def test_u3_a_run_records_the_four_counts_of_its_own_invocation(
    conn: Any, card: Card, execute: Any, tmp_path: Path
) -> None:
    first, second = resumed_twice(conn, card, execute, tmp_path)
    assert tuple(first[k] for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")) == (2, 4, 100, 0)  # fmt: skip
    assert tuple(second[k] for k in ("input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens")) == (3, 6, 50, 150)  # fmt: skip


def test_u3_a_resumed_run_records_only_the_rise_of_the_per_model_usage(
    conn: Any, card: Card, execute: Any, tmp_path: Path
) -> None:
    first, second = resumed_twice(conn, card, execute, tmp_path)
    assert json.loads(first["model_usage"]) == {HAIKU: per_model(2, 4, 100, 0, 0.5)}
    assert json.loads(second["model_usage"]) == {
        HAIKU: per_model(3, 6, 50, 150, 0.3),
        OTHER: per_model(1, 1, 0, 0, 0.0),
    }
    assert json.loads(second["session_model_usage"])[HAIKU] == per_model(5, 10, 150, 150, 0.8)
    assert second["cost_usd"] == pytest.approx(0.3)


def test_u3_the_per_model_rise_agrees_with_the_invocations_own_counts(
    conn: Any, card: Card, execute: Any, tmp_path: Path
) -> None:
    _, second = resumed_twice(conn, card, execute, tmp_path)
    share = json.loads(second["model_usage"])[HAIKU]
    assert (
        share["inputTokens"], share["outputTokens"],
        share["cacheCreationInputTokens"], share["cacheReadInputTokens"],
    ) == (3, 6, 50, 150)  # fmt: skip


def test_u3_a_count_that_fell_is_a_fresh_count_and_stands_whole(
    conn: Any, card: Card, execute: Any, tmp_path: Path
) -> None:
    result_fixture(
        tmp_path, "big", cost=1.0, usage=usage_of(9, 9, 9, 9),
        models={HAIKU: per_model(9, 9, 9, 9, 1.0)},
    )  # fmt: skip
    result_fixture(
        tmp_path, "small", cost=0.4, usage=usage_of(2, 3, 4, 5),
        models={HAIKU: per_model(2, 3, 4, 5, 0.4)},
    )  # fmt: skip
    tid = card()
    execute(tid, str(tmp_path / "big"))
    second = execute(tid, str(tmp_path / "small"))
    assert second["cost_usd"] == 0.4
    assert json.loads(second["model_usage"]) == {HAIKU: per_model(2, 3, 4, 5, 0.4)}


def test_u3_a_result_without_usage_leaves_the_columns_empty(
    conn: Any, card: Card, execute: Any
) -> None:
    run = execute(card(), "success")
    assert [run[k] for k in ("input_tokens", "model_usage", "session_model_usage")] == [None] * 3
    assert run["cost_usd"] == 0.12


def test_u3_runs_shows_the_tokens_and_json_carries_the_per_model_figures(
    conn: Any, card: Card, execute: Any, kb: Kb, tmp_path: Path
) -> None:
    tid = card()
    result_fixture(
        tmp_path, "one", cost=0.5, usage=usage_of(2, 4, 100, 7),
        models={HAIKU: per_model(2, 4, 100, 7, 0.5)},
    )  # fmt: skip
    execute(tid, str(tmp_path / "one"))
    code, out, err = kb("runs", tid)
    assert code == 0, err
    assert "in 2 out 4 cache-write 100 cache-read 7" in out
    code, out, err = kb("runs", tid, "--json")
    run = json.loads(out)["runs"][0]
    assert (run["input_tokens"], run["cache_read_tokens"]) == (2, 7)
    assert run["model_usage"] == {HAIKU: per_model(2, 4, 100, 7, 0.5)}


def test_u3_a_run_without_tokens_prints_no_token_text(
    conn: Any, card: Card, execute: Any, kb: Kb
) -> None:
    tid = card()
    execute(tid, "success")
    out = kb("runs", tid)[1]
    assert "cache-read" not in out and "$0.12" in out


def test_u3_stats_adds_the_tokens_by_model(
    conn: Any, card: Card, execute: Any, kb: Kb, tmp_path: Path
) -> None:
    resumed_twice(conn, card, execute, tmp_path)
    by_model = {m["model"]: m for m in store.stats(conn)["tokens_by_model"]}
    assert by_model[HAIKU] == {
        "model": HAIKU, "runs": 2, "input": 5, "output": 10, "cache_write": 150,
        "cache_read": 150, "cost": 0.8,
    }  # fmt: skip
    assert by_model[OTHER]["runs"] == 1 and by_model[OTHER]["input"] == 1
    out = json.loads(kb("stats", "--json")[1])
    assert [m["model"] for m in out["tokens_by_model"]] == [HAIKU, OTHER]


def test_u3_the_migration_adds_the_columns_to_a_version_2_database(env: Path) -> None:
    home = env / "kb-home"
    home.mkdir(exist_ok=True)
    old = sqlite3.connect(home / "kanban.db")
    old.executescript(store.SCHEMA + store.JIRA_SCHEMA)
    old.execute("INSERT INTO boards(slug, name, created_at) VALUES ('main', 'main', 1)")
    old.execute("PRAGMA user_version = 2")
    old.commit()
    old.close()
    c = store.connect()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(task_runs)")}
    assert {"input_tokens", "cache_write_tokens", "model_usage", "session_model_usage"} <= cols
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(store.MIGRATIONS)
    assert c.execute("SELECT slug FROM boards").fetchone()[0] == "main"
    assert list((home / "backups").glob("kanban-s2-*.db"))


def test_s2_review_verdicts_from_before_the_upgrade_are_not_pushed_to_jira(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = store.MIGRATIONS
    monkeypatch.setattr(store, "MIGRATIONS", full[:3])
    c = store.connect()
    store.board_create(c, "main", default_workdir=str(env))
    tid = str(store.create(c, "main", "Fix it", body="do it", assignee="smith")["id"])
    c.execute(
        "INSERT INTO jira_links(task_id, jira_key, base_title, base_body, card_title, card_body, "
        "synced_status, synced_version, created_at) VALUES (?, 'DEMO-1', '', '', '', '', 'todo', 1, 1)",
        (tid,),
    )
    first_work = claim_run(c, tid)
    store.request_review(c, first_work, "PR #12", pr_number=12, pr_url=PR_URL)
    old_verdict = claim_run(c, tid)
    store.request_changes(c, old_verdict, "the test is missing")
    monkeypatch.setattr(store, "MIGRATIONS", full)
    store.migrate(c, env / "kb-home" / "backups")

    def pending() -> list[int]:
        lk = c.execute("SELECT * FROM jira_links").fetchone()
        return [ref for kind, ref, _ in jira.outbox(c, lk, store.get_task(c, tid)) if kind == "run"]

    assert pending() == [first_work]
    second_work = claim_run(c, tid)
    store.request_review(c, second_work, "PR #12 again", pr_number=12, pr_url=PR_URL)
    new_verdict = claim_run(c, tid)
    store.request_changes(c, new_verdict, "still no test")
    assert pending() == [first_work, second_work, new_verdict]
    c.close()


# --- S3: an owner's reopen reason reaches the next run ---------------------------------------


def comments_of(conn: Any, tid: str) -> list[tuple[str, str]]:
    q = "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id"
    return [(r["author"], r["body"]) for r in conn.execute(q, (tid,))]


def in_review(conn: Any, card: Card) -> str:
    tid = card()
    store.request_review(conn, claim_run(conn, tid), "PR #12", pr_number=12, pr_url=PR_URL)
    return tid


def test_s3_reopen_review_puts_the_reason_in_the_builders_context(conn: Any, card: Card) -> None:
    tid = in_review(conn, card)
    store.reopen_review(conn, tid, "rename the flag")
    assert ("owner", "REOPEN: rename the flag") in comments_of(conn, tid)
    assert "REOPEN: rename the flag" in views.context_md(conn, tid)
    assert row(conn, tid)["assignee"] == "smith"


def test_s3_reopen_puts_the_reason_in_the_builders_context(conn: Any, card: Card) -> None:
    tid = card()
    store.complete(conn, claim_run(conn, tid), "ok")
    store.reopen(conn, tid, "the edge case is still broken")
    assert ("owner", "REOPEN: the edge case is still broken") in comments_of(conn, tid)
    assert "REOPEN: the edge case is still broken" in views.context_md(conn, tid)


def test_s3_the_cli_forms_write_the_comment_too(conn: Any, card: Card, kb: Kb) -> None:
    review, done = in_review(conn, card), card()
    store.complete(conn, claim_run(conn, done), "ok")
    assert kb("reopen-review", review, "--reason", "first")[0] == 0
    assert kb("reopen", done, "--reason", "second")[0] == 0
    assert comments_of(conn, review)[-1] == ("owner", "REOPEN: first")
    assert comments_of(conn, done)[-1] == ("owner", "REOPEN: second")


def test_s3_a_refused_reopen_writes_no_comment(conn: Any, card: Card) -> None:
    tid = card()
    with pytest.raises(Refused):
        store.reopen(conn, tid, "not done yet")
    with pytest.raises(Refused):
        store.reopen_review(conn, tid, "not in review")
    assert comments_of(conn, tid) == []


def test_s3_a_reopen_jira_asked_for_is_not_echoed_back_as_an_owner_comment(
    conn: Any, card: Card
) -> None:
    tid = card()
    store.complete(conn, claim_run(conn, tid), "ok")
    store.reopen(conn, tid, "reopened in Jira", actor="jira", status="triage")
    assert comments_of(conn, tid) == []
    assert row(conn, tid)["status"] == "triage"


def test_s3_hold_stays_an_event_only(conn: Any, card: Card) -> None:
    tid = card()
    store.hold(conn, tid, "wait for the design")
    assert comments_of(conn, tid) == []
