"""Jira as the shared source of truth for a team: two people, each with their own `KANBAN_HOME`, their
own database and their own Atlassian login, against ONE fake Jira. What works is a passing test; what
a team needs and the plugin does not do yet is an `xfail(strict=True)` that names the gap. Dispatch
is shared through the assignee only with `jira.claim` on, so those tests turn it on; the ones without
pin what the default does."""

import json
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, jira, store

from .conftest import HERE, FakeProcs
from .fake_jira_mcp import Fake

KEY = "DEMO-100"  # the first key the fake hands out
TEAM_JQL = 'project = DEMO AND labels = "kanban-claude"'
TEAM_CLAIM_JQL = TEAM_JQL + " AND (assignee is EMPTY OR assignee = currentUser())"
PR = "https://example.invalid/acme/demo/pull/7"


class Person:
    """One teammate: a home, a database, a Jira login. `db` switches the process to them first, so
    the paths and the fake server see this person and nobody else."""

    def __init__(
        self, name: str, root: Path, fake: Fake, clock: Callable[[int], None],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        self.name, self.fake, self.clock, self.mp = name, fake, clock, monkeypatch
        self.email = f"{name.split()[0].lower()}@example.com"
        self.report: dict[str, Any] = {}
        self.home, self.work = root / "home", root / "work"
        self.procs = FakeProcs()
        plain = root / "plain"
        plain.mkdir(parents=True)
        self.conn = store.connect(self.home)
        store.config_set(self.db, "jira.assignee", self.email)
        store.config_set(
            self.db, "jira.mcp_command", [sys.executable, str(HERE / "fake_jira_mcp.py")]
        )
        store.board_create(self.db, "main", default_workdir=str(plain), jira_project="DEMO")

    @property
    def db(self) -> sqlite3.Connection:
        self.mp.setenv("KANBAN_HOME", str(self.home))
        self.mp.setenv("KANBAN_WORK", str(self.work))
        self.mp.setenv("FAKE_JIRA_USER", self.name)
        return self.conn

    def sync(self) -> list[str]:
        """One pass, then a minute for the boards and for the server."""
        out = jira.sync_all(self.db)
        self.clock(60)
        self.fake.advance(60)
        return out

    def card(self, title: str = "Fix the login", body: str = "do it", **kw: Any) -> str:
        kw.setdefault("assignee", "smith")
        return str(store.create(self.db, "main", title, body=body, **kw)["id"])

    def task(self, tid: str) -> sqlite3.Row:
        return store.get_task(self.db, tid)

    def link(self, tid: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM jira_links WHERE task_id = ?", (tid,)).fetchone()
        assert row, f"{tid} has no link"
        return row  # type: ignore[no-any-return]

    def only_card(self) -> str:
        [row] = self.db.execute("SELECT id FROM tasks").fetchall()
        return str(row["id"])

    def comments(self, tid: str) -> list[tuple[str, str]]:
        rows = self.db.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,)
        )
        return [(r["author"], r["body"]) for r in rows]

    def kinds(self, like: str) -> list[str]:
        return [
            r[0] for r in self.db.execute("SELECT kind FROM task_events WHERE kind LIKE ?", (like,))
        ]

    def dispatch(self) -> list[str]:
        """One dispatcher tick of this person's daemon; the ids of the cards it claimed."""
        self.report = dispatcher.dispatch_once(self.db, store.now(), self.procs)
        return [c["id"] for c in self.report["claimed"]]

    def skipped(self) -> dict[str, str]:
        """Why the last tick left each ready card alone."""
        return dict(self.report["skipped"])

    def assignments(self) -> list[Any]:
        """The assignees this person's login set, in order."""
        return [
            json.loads(c["args"]["fields"])["assignee"]
            for c in self.fake.calls("jira_update_issue")
            if c["user"] == self.name and "assignee" in c["args"].get("fields", "")
        ]

    def run(self, tid: str) -> int:
        run = store.claim(self.db, tid)
        assert run
        return run


class Team:
    def __init__(self, fake: Fake, ann: Person, bob: Person) -> None:
        self.fake, self.ann, self.bob, self.jql = fake, ann, bob, TEAM_JQL

    def share_query(self) -> None:
        """Both boards ask for the whole project's kanban issues, not only their own."""
        for p in (self.ann, self.bob):
            store.board_edit(p.db, "main", jira_jql=self.jql)

    def claim_on(self, jql: str = TEAM_CLAIM_JQL) -> None:
        """Both boards claim what they run. The default query asks for the issues nobody holds as well
        as their own, so an issue a teammate holds never reaches this board; `TEAM_JQL` lets it, to
        test two boards that hold one issue."""
        self.jql = jql
        for p in (self.ann, self.bob):
            store.config_set(p.db, "jira.claim", True)
        self.share_query()

    def unassigned(self, key: str = "DEMO-1") -> str:
        """The product owner files an issue; both boards import it into triage, no one holds it."""
        self.fake.add(key, "Backlog item", "from the product owner", labels=["kanban-claude"])
        for p in (self.ann, self.bob):
            p.sync()
        return key

    def shared_issue(self) -> tuple[str, str]:
        """Ann's card exported, Bob's board holding the issue as a triage card; both linked."""
        self.share_query()
        mine = self.ann.card()
        self.ann.sync()
        self.bob.sync()
        return mine, self.bob.only_card()

    def hand_over(self) -> tuple[str, str]:
        """Ann exports under the default query, then the issue is reassigned to Bob in Jira."""
        mine = self.ann.card()
        self.ann.sync()
        self.fake.edit(KEY, assignee=self.bob.name)
        self.bob.sync()
        return mine, self.bob.only_card()

    def approve(self, p: Person, tid: str) -> None:
        store.specify(p.db, tid, {"assignee": "smith"})


@pytest.fixture
def team(tmp_path: Path, clock: Callable[[int], None], monkeypatch: pytest.MonkeyPatch) -> Team:
    monkeypatch.setenv("FAKE_JIRA_STATE", str(tmp_path / "jira.json"))
    fake = Fake(tmp_path / "jira.json")
    ann, bob = (
        Person(n, tmp_path / n.split()[0].lower(), fake, clock, monkeypatch)
        for n in ("Ann Example", "Bob Example")
    )
    return Team(fake, ann, bob)


def jira_comments(fake: Fake, key: str = KEY) -> list[tuple[str, str]]:
    return [(c["author"]["display_name"], c["body"]) for c in fake.issue(key)["comments"]]


# --- 1. import --------------------------------------------------------------------------------


def test_1_the_default_query_keeps_a_teammates_issue_off_my_board(team: Team) -> None:
    team.ann.card()
    team.ann.sync()
    team.bob.sync()
    assert team.fake.issue(KEY)["assignee"]["display_name"] == "Ann Example"
    assert team.bob.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_1_an_unassigned_issue_belongs_to_nobody_until_jira_assigns_it(team: Team) -> None:
    team.fake.add("DEMO-1", "Backlog item", "from the product owner", labels=["kanban-claude"])
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert team.bob.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    team.fake.edit("DEMO-1", assignee=team.ann.name)
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.task(team.ann.only_card())["status"] == "triage"
    assert team.bob.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_1_an_issue_handed_over_in_jira_arrives_in_triage_and_is_not_dispatchable(
    team: Team,
) -> None:
    team.hand_over()
    t = team.bob.task(team.bob.only_card())
    assert (t["status"], t["assignee"], t["title"], t["body"]) == (
        "triage",
        None,
        "Fix the login",
        "do it",
    )
    assert team.bob.link(t["id"])["jira_key"] == KEY
    assert team.bob.dispatch() == []
    team.approve(team.bob, t["id"])
    assert team.bob.dispatch() == [t["id"]]


def test_1_a_team_query_mirrors_each_persons_new_card_into_the_other_boards_triage(
    team: Team,
) -> None:
    team.share_query()
    team.ann.card("Ann's idea")
    team.bob.card("Bob's idea")
    for p in (team.ann, team.bob, team.ann):
        p.sync()

    def board(p: Person) -> dict[str, str]:
        rows = p.db.execute("SELECT title, status FROM tasks").fetchall()
        return {r["title"]: r["status"] for r in rows}

    assert board(team.ann) == {"Ann's idea": "ready", "Bob's idea": "triage"}
    assert board(team.bob) == {"Ann's idea": "triage", "Bob's idea": "ready"}


# --- 2. duplicate dispatch ----------------------------------------------------------------------

GAP = "multi-user gap: "


def test_2_without_the_claim_both_boards_start_a_run_on_one_issue(team: Team) -> None:
    mine, theirs = team.shared_issue()
    team.approve(team.bob, theirs)
    assert [*team.ann.dispatch(), *team.bob.dispatch()] == [mine, theirs]


@pytest.mark.parametrize(("how", "holder"), [("hand_over", "bob"), ("shared_issue", "ann")])
def test_2_with_the_claim_the_assignee_is_the_one_board_that_dispatches_the_issue(
    team: Team, how: str, holder: str
) -> None:
    team.claim_on(TEAM_JQL)
    mine, theirs = getattr(team, how)()
    team.approve(team.bob, theirs)
    team.ann.sync()
    team.bob.sync()
    assert team.ann.task(mine)["status"] == team.bob.task(theirs)["status"] == "ready"
    ann, bob = team.ann.dispatch(), team.bob.dispatch()
    assert (ann, bob) == (([mine], []) if holder == "ann" else ([], [theirs]))
    skipped = team.bob.skipped() if holder == "ann" else team.ann.skipped()
    assert set(skipped.values()) == {"claimed_elsewhere"}


def test_2_a_card_the_board_exported_is_born_claimed_and_dispatches_without_an_extra_call(
    team: Team,
) -> None:
    team.claim_on()
    mine = team.ann.card()
    team.ann.sync()
    assert team.ann.dispatch() == [mine]
    assert team.ann.assignments() == []


def test_2_a_review_card_linked_before_the_claim_was_on_is_refreshed_and_not_stuck_unclaimed(
    team: Team,
) -> None:
    mine, _ = team.shared_issue()
    ann = team.ann
    store.request_review(ann.db, ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    for _ in range(4):  # until the issue is older than the window of recently updated issues
        ann.sync()
    store.config_set(ann.db, "jira.claim", True)
    with store.tx(ann.db):
        ann.db.execute("UPDATE jira_links SET jira_assignee = NULL")
    ann.dispatch()
    assert ann.skipped() == {mine: "jira_unclaimed"}
    ann.sync()
    ann.dispatch()
    assert mine not in ann.skipped()
    assert ann.assignments() == []


def test_2_a_review_card_on_an_issue_nobody_holds_is_claimed_like_a_ready_one(team: Team) -> None:
    team.claim_on()
    team.unassigned()
    card = team.bob.only_card()
    team.approve(team.bob, card)
    team.bob.sync()
    store.request_review(team.bob.db, team.bob.run(card), "PR #7", pr_number=7, pr_url=PR)
    team.fake.edit("DEMO-1", assignee=None)
    team.bob.sync()
    assert team.fake.issue("DEMO-1")["assignee"]["email"] == "bob@example.com"
    team.bob.dispatch()
    assert card not in team.bob.skipped()


def test_2_an_unlinked_card_dispatches_with_the_claim_on(team: Team) -> None:
    team.claim_on()
    mine = team.ann.card()
    assert team.ann.dispatch() == [mine]


def test_2_a_ready_card_on_an_unassigned_issue_waits_for_the_pass_that_claims_it(
    team: Team,
) -> None:
    team.claim_on()
    team.unassigned()
    card = team.bob.only_card()
    assert team.bob.assignments() == []
    team.approve(team.bob, card)
    assert team.bob.dispatch() == []
    assert team.bob.skipped() == {card: "jira_unclaimed"}
    team.bob.sync()
    assert team.fake.issue("DEMO-1")["assignee"]["email"] == "bob@example.com"
    assert team.bob.assignments() == ["bob@example.com"]
    assert team.bob.dispatch() == [card]


def test_2_a_board_that_reads_the_claim_of_another_does_not_overwrite_it(team: Team) -> None:
    team.claim_on()
    key = team.unassigned()
    a_card, b_card = team.ann.only_card(), team.bob.only_card()
    team.approve(team.ann, a_card)
    team.approve(team.bob, b_card)
    team.ann.sync()
    assert team.fake.issue(key)["assignee"]["email"] == "ann@example.com"
    assert team.bob.link(b_card)["jira_assignee"] is None
    assert team.bob.dispatch() == [] and team.bob.skipped() == {b_card: "jira_unclaimed"}
    team.bob.sync()
    assert team.fake.issue(key)["assignee"]["email"] == "ann@example.com"
    assert team.bob.assignments() == []
    assert team.bob.dispatch() == [] and team.bob.skipped() == {b_card: "claimed_elsewhere"}
    assert team.ann.dispatch() == [a_card]


def test_2_two_boards_that_read_the_issue_unassigned_both_write_and_the_read_back_decides(
    team: Team,
) -> None:
    team.claim_on()
    key = team.unassigned()
    a_card, b_card = team.ann.only_card(), team.bob.only_card()
    team.approve(team.ann, a_card)
    team.approve(team.bob, b_card)
    team.fake.mode(stale_read=[team.bob.name])  # Bob's pass read the issue before Ann's write
    team.ann.sync()
    team.bob.sync()
    assert team.fake.issue(key)["assignee"]["email"] == "bob@example.com"
    assert team.ann.assignments() == ["ann@example.com"]
    assert team.bob.assignments() == ["bob@example.com"]
    assert team.ann.dispatch() == [a_card] and team.bob.dispatch() == [b_card]
    team.ann.sync()
    assert "Bob Example" in team.ann.link(a_card)["conflict"]
    assert team.ann.kinds("jira_conflict") == ["jira_conflict"]
    assert team.bob.link(b_card)["conflict"] is None


def test_2_a_claim_that_fails_leaves_the_card_waiting_and_the_next_pass_retries_it(
    team: Team,
) -> None:
    team.claim_on()
    key = team.unassigned()
    card = team.bob.only_card()
    team.approve(team.bob, card)
    team.fake.mode(fail={"jira_update_issue:assignee": "the Assign Issues permission is missing"})
    team.bob.sync()
    assert "Assign Issues" in team.bob.link(card)["error"]
    assert not team.fake.issue(key).get("assignee")
    assert team.bob.task(card)["status"] == "ready"
    assert team.bob.dispatch() == [] and team.bob.skipped() == {card: "jira_unclaimed"}
    team.fake.mode(fail={})
    team.bob.sync()
    assert team.bob.link(card)["error"] is None
    assert team.bob.dispatch() == [card]


def test_2_an_update_the_server_ignores_is_an_error_and_not_a_claim(team: Team) -> None:
    team.claim_on()
    team.unassigned()
    card = team.bob.only_card()
    team.approve(team.bob, card)
    team.fake.mode(ignore_assignee=True)
    team.bob.sync()
    assert "still unassigned" in team.bob.link(card)["error"]
    assert team.bob.dispatch() == [] and team.bob.skipped() == {card: "jira_unclaimed"}


def test_2_a_display_name_as_jira_assignee_never_matches_so_the_board_dispatches_nothing(
    team: Team,
) -> None:
    team.claim_on()
    store.config_set(team.ann.db, "jira.assignee", team.ann.name)
    mine = team.ann.card()
    team.ann.sync()
    assert team.fake.issue(KEY)["assignee"]["display_name"] == "Ann Example"
    assert team.ann.dispatch() == [] and team.ann.skipped() == {mine: "claimed_elsewhere"}


def test_2_a_card_running_on_an_issue_jira_gives_to_another_is_flagged_and_never_stopped(
    team: Team,
) -> None:
    team.claim_on(TEAM_JQL)
    mine, _ = team.shared_issue()
    run = team.ann.run(mine)
    team.fake.edit(KEY, assignee=team.bob.name)
    for _ in range(2):
        team.ann.sync()
    assert team.ann.task(mine)["status"] == "running"
    assert (
        team.ann.link(mine)["conflict"]
        == "assigned to Bob Example in Jira while the card is running"
    )
    assert team.ann.kinds("jira_conflict") == ["jira_conflict"]
    store.complete(team.ann.db, run, "finished anyway")
    team.ann.sync()
    assert team.ann.link(mine)["conflict"] is None
    assert team.fake.issue(KEY)["status"]["name"] == "To Do"


# --- 2b. who is "me" ----------------------------------------------------------------------------

CLOUD = {
    "account_id": "acc-ann", "display_name": "Ann Example", "name": "Ann Example",
    "email": "ann@example.com", "avatar_url": None,
}  # fmt: skip
SERVER = {
    "account_id": None, "display_name": "Ann Example", "name": "aexample",
    "email": "ann@example.com", "avatar_url": None, "key": "JIRAUSER10000",
}  # fmt: skip
NOBODY = {"display_name": "Unassigned"}


@pytest.mark.parametrize(
    ("who", "me", "expected"),
    [
        (CLOUD, "acc-ann", True),
        (CLOUD, "ANN@example.com", True),
        (CLOUD, " ann@example.com ", True),
        (CLOUD, "Ann Example", False),
        (CLOUD, "bob@example.com", False),
        ({**CLOUD, "email": None}, "ann@example.com", False),
        ({**CLOUD, "email": None}, "acc-ann", True),
        (SERVER, "aexample", True),
        (SERVER, "AExample", True),
        (SERVER, "ann@example.com", True),
        (SERVER, "Ann Example", False),
        (SERVER, "JIRAUSER10000", False),
        (CLOUD, None, False),
        (CLOUD, "", False),
        ({}, "ann@example.com", False),
        (None, "ann@example.com", False),
    ],
)
def test_jira_assignee_matches_an_account_id_an_email_or_a_username_never_a_display_name(
    who: dict[str, Any] | None, me: str | None, expected: bool
) -> None:
    assert jira.is_me(who, me) is expected


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        ({"assignee": CLOUD}, CLOUD),
        ({"assignee": SERVER}, SERVER),
        ({"assignee": NOBODY}, {}),
        ({"assignee": {"display_name": "Unassigned", "name": "unassigned"}}, {"display_name": "Unassigned", "name": "unassigned"}),
        ({"assignee": {"display_name": "Somebody"}}, {"display_name": "Somebody"}),
        ({"assignee": "Ann Example"}, None),
        ({"assignee": None}, None),
        ({"assignee": {}}, None),
        ({}, None),
    ],
)  # fmt: skip
def test_the_assignee_of_an_issue_is_nobody_only_when_jira_says_unassigned(
    issue: jira.Issue, expected: dict[str, Any] | None
) -> None:
    assert jira.assignee_of(issue) == expected


# --- 3. decisions travel ------------------------------------------------------------------------


def test_3_owner_comments_and_run_outcomes_reach_jira_as_the_documented_texts(team: Team) -> None:
    mine, _ = team.shared_issue()
    ann = team.ann
    store.block(ann.db, ann.run(mine), "needs_input", "which environment?")
    ann.sync()
    store.unblock(ann.db, mine, "use staging")
    store.comment(ann.db, mine, "worker-side note", author="smith")
    store.request_review(ann.db, ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    ann.sync()
    store.complete(ann.db, ann.run(mine), "WARDEN: APPROVED PR #7 @ abcdef1")
    ann.sync()
    assert jira_comments(team.fake) == [
        ("Ann Example", "kanban blocked: which environment?"),
        ("Ann Example", "UNBLOCK: use staging"),
        ("Ann Example", f"kanban ready for review: PR #7\nPR: {PR}"),
        ("Ann Example", f"kanban review approved: WARDEN: APPROVED PR #7 @ abcdef1\nPR: {PR}"),
    ]
    assert team.fake.issue(KEY)["status"]["name"] == "Done"
    local = ann.comments(mine)
    assert ("smith", "BLOCKED: which environment?") in local
    assert ("smith", "worker-side note") in local


def test_3_a_review_verdict_reaches_jira_under_a_label_of_its_own(team: Team) -> None:
    mine, theirs = team.shared_issue()
    ann = team.ann
    store.request_review(ann.db, ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    store.request_changes(ann.db, ann.run(mine), "the login test is missing")
    ann.sync()
    assert [body for _, body in jira_comments(team.fake)] == [
        f"kanban ready for review: PR #7\nPR: {PR}",
        f"kanban review changes requested: the login test is missing\nPR: {PR}",
    ]
    team.bob.sync()
    assert team.bob.comments(theirs)[-1] == (
        "jira:Ann Example",
        f"kanban review changes requested: the login test is missing\nPR: {PR}",
    )


def test_3_a_blocked_review_has_its_own_label_and_a_verdict_crosses_once(team: Team) -> None:
    mine, _ = team.shared_issue()
    ann = team.ann
    store.request_review(ann.db, ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    store.block(ann.db, ann.run(mine), "needs_input", "no access to the CI logs")
    for _ in range(2):
        ann.sync()
    bodies = [body for _, body in jira_comments(team.fake)]
    assert bodies[1:] == [f"kanban review blocked: no access to the CI logs\nPR: {PR}"]


def test_3_the_owners_unblock_and_reopen_comments_are_pushed(team: Team) -> None:
    mine, _ = team.shared_issue()
    ann = team.ann
    store.block(ann.db, ann.run(mine), "needs_input", "which environment?")
    ann.sync()
    store.unblock(ann.db, mine, "use staging")
    ann.sync()
    store.request_review(ann.db, ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    ann.sync()
    store.reopen_review(ann.db, mine, "rework the error path")
    ann.sync()
    store.complete(ann.db, ann.run(mine), "shipped")
    ann.sync()
    store.reopen(ann.db, mine, "it regressed")
    ann.sync()
    sent = [body.split("\n")[0] for _, body in jira_comments(team.fake)]
    assert sent == [
        "kanban blocked: which environment?",
        "UNBLOCK: use staging",
        "kanban ready for review: PR #7",
        "REOPEN: rework the error path",
        "kanban completed: shipped",
        "REOPEN: it regressed",
    ]


def test_3_a_comment_crosses_once_in_each_direction_and_never_echoes_back(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.comment(team.ann.db, mine, "Ann: take the v2 endpoint")
    for p in (team.ann, team.bob):
        p.sync()
    store.comment(team.bob.db, theirs, "Bob: agreed")
    for p in (team.bob, team.ann, team.bob, team.ann):
        p.sync()
    assert jira_comments(team.fake) == [
        ("Ann Example", "Ann: take the v2 endpoint"),
        ("Bob Example", "Bob: agreed"),
    ]
    assert team.ann.comments(mine) == [
        ("owner", "Ann: take the v2 endpoint"),
        ("jira:Bob Example", "Bob: agreed"),
    ]
    assert team.bob.comments(theirs) == [
        ("jira:Ann Example", "Ann: take the v2 endpoint"),
        ("owner", "Bob: agreed"),
    ]


# --- 4. concurrent edits ------------------------------------------------------------------------


def test_4_two_boards_edit_one_field_the_first_to_sync_wins_and_the_loser_keeps_its_text(
    team: Team,
) -> None:
    mine, theirs = team.shared_issue()
    store.edit(team.ann.db, mine, {"body": "Ann's plan"})
    store.edit(team.bob.db, theirs, {"body": "Bob's plan"})
    team.bob.sync()
    team.ann.sync()
    assert team.fake.issue(KEY)["description"] == "Bob's plan"
    assert team.ann.task(mine)["body"] == "Bob's plan"
    [(_, note)] = [c for c in team.ann.comments(mine) if c[1].startswith("[jira] conflict")]
    assert "Ann's plan" in note
    assert team.ann.kinds("jira_conflict") == ["jira_conflict"]
    assert team.bob.kinds("jira_conflict") == []


def test_4_two_boards_edit_different_fields_and_both_edits_survive(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.edit(team.ann.db, mine, {"title": "Fix the login page"})
    store.edit(team.bob.db, theirs, {"body": "Bob's plan"})
    for p in (team.ann, team.bob, team.ann):
        p.sync()
    issue = team.fake.issue(KEY)
    assert (issue["summary"], issue["description"]) == ("Fix the login page", "Bob's plan")
    for p, tid in ((team.ann, mine), (team.bob, theirs)):
        assert (p.task(tid)["title"], p.task(tid)["body"]) == ("Fix the login page", "Bob's plan")


def test_4_a_teammates_text_edit_puts_my_approved_card_back_in_triage(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.edit(team.bob.db, theirs, {"body": "Bob's plan"})
    team.bob.sync()
    team.ann.sync()
    assert team.ann.task(mine)["status"] == "triage"
    assert (team.ann.task(mine)["body"], team.ann.task(mine)["assignee"]) == ("Bob's plan", "smith")
    assert ("jira", "[jira] body changed; the card is back in triage") in team.ann.comments(mine)


def test_4_a_running_card_keeps_its_text_until_the_run_ends(team: Team) -> None:
    mine, _ = team.shared_issue()
    run = team.ann.run(mine)
    team.fake.edit(KEY, description="changed by a teammate")
    team.ann.sync()
    assert team.ann.task(mine)["body"] == "do it"
    store.complete(team.ann.db, run, "done")
    team.ann.sync()
    assert team.ann.task(mine)["body"] == "changed by a teammate"


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason=GAP + "jira_update_issue has no precondition: a Jira edit made after the pass read the issue is overwritten",
)  # fmt: skip
def test_4_a_jira_edit_between_my_read_and_my_write_is_not_overwritten_silently(
    team: Team, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, _ = team.shared_issue()
    store.edit(team.ann.db, mine, {"body": "Ann's plan"})
    real, fired = jira.fetch, []

    def fetch_then_teammate_edits(mcp: jira.Mcp, key: str) -> jira.Issue:
        issue = real(mcp, key)
        if not fired:
            fired.append(True)
            team.fake.edit(key, description="Bob's plan, typed in Jira just now")
        return issue

    monkeypatch.setattr(jira, "fetch", fetch_then_teammate_edits)
    team.ann.sync()
    kept = "Bob's plan" in team.fake.issue(KEY)["description"]
    flagged = any(c[1].startswith("[jira] conflict") for c in team.ann.comments(mine))
    assert kept or flagged, "Bob's edit is gone from Jira and nobody was told"


# --- 5. status changed in Jira --------------------------------------------------------------------


def test_5_done_in_jira_closes_the_idle_card_on_every_board(team: Team) -> None:
    mine, theirs = team.shared_issue()
    team.approve(team.bob, theirs)
    team.fake.edit(KEY, status="Done")
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.task(mine)["status"] == team.bob.task(theirs)["status"] == "done"
    assert ("jira", "[jira] closed in Jira") in team.bob.comments(theirs)


def test_5_done_in_jira_flags_a_running_card_and_lets_the_run_finish(team: Team) -> None:
    mine, theirs = team.shared_issue()
    team.approve(team.bob, theirs)
    run = team.ann.run(mine)
    team.fake.edit(KEY, status="Done")
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.task(mine)["status"] == "running"
    assert "running" in team.ann.link(mine)["conflict"]
    assert team.bob.task(theirs)["status"] == "done"
    store.complete(team.ann.db, run, "finished anyway")
    team.ann.sync()
    assert team.ann.task(mine)["status"] == "done"
    assert team.ann.link(mine)["conflict"] is None
    assert team.fake.issue(KEY)["status"]["name"] == "Done"


def test_5_a_teammate_finishing_the_issue_closes_my_idle_twin_of_it(team: Team) -> None:
    mine, theirs = team.shared_issue()
    team.approve(team.bob, theirs)
    store.complete(team.ann.db, team.ann.run(mine), "shipped")
    team.ann.sync()
    team.bob.sync()
    assert team.fake.issue(KEY)["status"]["name"] == "Done"
    assert team.bob.task(theirs)["status"] == "done"


def test_5_back_to_todo_in_jira_reopens_a_done_card_into_triage_on_every_board(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.complete(team.ann.db, team.ann.run(mine), "shipped")
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.task(mine)["status"] == team.bob.task(theirs)["status"] == "done"
    team.fake.edit(KEY, status="To Do")
    for p in (team.ann, team.bob):
        p.sync()
    assert team.ann.task(mine)["status"] == team.bob.task(theirs)["status"] == "triage"
    assert team.ann.dispatch() == team.bob.dispatch() == []


def test_5_without_the_claim_a_teammates_board_moves_a_running_issue_back_to_todo(
    team: Team,
) -> None:
    mine, theirs = team.shared_issue()
    team.ann.run(mine)
    team.ann.sync()
    assert team.fake.issue(KEY)["status"]["name"] == "In Progress"
    team.approve(team.bob, theirs)
    team.bob.sync()
    assert team.fake.issue(KEY)["status"]["name"] == "To Do"


def test_5_with_the_claim_only_the_board_that_holds_the_issue_moves_its_status(team: Team) -> None:
    team.claim_on(TEAM_JQL)
    mine, theirs = team.shared_issue()
    team.ann.run(mine)
    team.ann.sync()
    assert team.fake.issue(KEY)["status"]["name"] == "In Progress"
    team.approve(team.bob, theirs)
    team.bob.sync()
    assert team.fake.issue(KEY)["status"]["name"] == "In Progress"
    assert [c["user"] for c in team.fake.calls("jira_transition_issue")] == ["Ann Example"]


# --- 6. history ---------------------------------------------------------------------------------


def test_6_bob_reads_anns_run_summaries_and_the_pr_as_jira_authored_comments(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.block(team.ann.db, team.ann.run(mine), "needs_input", "which environment?")
    team.ann.sync()
    store.unblock(team.ann.db, mine, "use staging")
    store.request_review(team.ann.db, team.ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    team.ann.sync()
    team.bob.sync()
    assert team.bob.comments(theirs) == [
        ("jira:Ann Example", "kanban blocked: which environment?"),
        ("jira:Ann Example", "UNBLOCK: use staging"),
        ("jira:Ann Example", f"kanban ready for review: PR #7\nPR: {PR}"),
    ]


def test_6_the_structured_run_history_stays_on_anns_board(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.request_review(team.ann.db, team.ann.run(mine), "PR #7", pr_number=7, pr_url=PR)
    team.ann.sync()
    team.bob.sync()
    assert team.ann.task(mine)["pr_url"] == PR
    bob = team.bob.task(theirs)
    assert (bob["status"], bob["pr_url"], bob["implementer"]) == ("triage", None, None)
    assert team.bob.db.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    assert "claimed" not in {r[0] for r in team.bob.db.execute("SELECT kind FROM task_events")}


# --- 7. locks and identity --------------------------------------------------------------------------


def test_7_the_jira_lock_is_per_home_so_it_does_not_coordinate_two_people(team: Team) -> None:
    mine, _ = team.shared_issue()
    team.ann.db  # noqa: B018 - switches the process to Ann's home before her lock is taken
    with jira.lock():
        with pytest.raises(jira.Busy):
            jira.sync_all(team.ann.db)
        assert team.bob.sync() == ["main"]
    assert team.ann.home != team.bob.home
    assert (team.ann.home / "jira.lock").exists() and (team.bob.home / "jira.lock").exists()
    assert team.ann.sync() == ["main"]
    assert team.ann.task(mine)["status"] == "ready"


def test_7_every_comment_is_written_under_the_login_of_the_board_that_sends_it(team: Team) -> None:
    mine, theirs = team.shared_issue()
    store.comment(team.ann.db, mine, "from Ann's board")
    store.comment(team.bob.db, theirs, "from Bob's board")
    for p in (team.ann, team.bob):
        p.sync()
    sent = [(c["user"], c["args"]["body"]) for c in team.fake.calls("jira_add_comment")]
    assert sent == [("Ann Example", "from Ann's board"), ("Bob Example", "from Bob's board")]
    assert jira_comments(team.fake) == [
        ("Ann Example", "from Ann's board"),
        ("Bob Example", "from Bob's board"),
    ]
    assert {c["user"] for c in team.fake.writes() if c["tool"] == "jira_create_issue"} == {
        "Ann Example"
    }
