"""GIT-08: branch protection is read from both endpoints, either counts, and never gates dispatch."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import housekeeping, protection, store

from .conftest import Calls, git

SLUG = "example.invalid/acme/proj"
RULES = "repos/acme/proj/rules/branches/main"
CLASSIC = "repos/acme/proj/branches/main"
Api = Callable[[dict[str, Any]], None]


@pytest.fixture
def used(conn: Any, repo: Path) -> Path:
    store.board_create(conn, "main", default_workdir=str(repo))
    return repo


def state(conn: Any) -> dict[str, Any]:
    return store.state_get(conn, protection.STATE_KEY)  # type: ignore[no-any-return]


def test_a_ruleset_counts_as_protection(conn: Any, used: Path, set_api: Api) -> None:
    set_api({RULES: [{"type": "pull_request"}], CLASSIC: {"protected": False}})
    assert protection.refresh(conn)["boards"] == {}


def test_classic_protection_counts_even_when_the_ruleset_list_is_empty(
    conn: Any, used: Path, set_api: Api
) -> None:
    set_api({RULES: [], CLASSIC: {"protected": True}})
    assert protection.refresh(conn)["boards"] == {}


def test_a_repo_with_neither_is_listed_for_its_board_and_the_list_is_persisted(
    conn: Any, used: Path, set_api: Api
) -> None:
    set_api({RULES: [], CLASSIC: {"protected": False}})
    got = protection.refresh(conn)
    assert got["boards"] == {"main": [SLUG]} and got["unknown"] == []
    assert state(conn)["boards"] == {"main": [SLUG]} and state(conn)["checked_at"] > 0


def test_a_repo_whose_protection_cannot_be_read_is_unknown_and_not_flagged(
    conn: Any, used: Path, set_api: Api
) -> None:
    set_api({RULES: []})
    got = protection.refresh(conn)
    assert got["boards"] == {} and got["unknown"] == [SLUG]


def test_an_existing_feature_branch_is_checked_too(conn: Any, used: Path, set_api: Api) -> None:
    git("update-ref", "refs/remotes/origin/feature/x", "HEAD", cwd=used)
    set_api({RULES: [{"type": "pull_request"}], "repos/acme/proj/rules/branches/feature/x": [],
             "repos/acme/proj/branches/feature/x": {"protected": False}})  # fmt: skip
    assert protection.refresh(conn)["boards"] == {"main": [SLUG]}


def test_a_repo_two_boards_share_is_asked_once(
    conn: Any, used: Path, set_api: Api, env: Path
) -> None:
    store.board_create(conn, "second", default_workdir=str(used))
    set_api({RULES: [], CLASSIC: {"protected": False}})
    got = protection.refresh(conn)
    assert got["boards"] == {"main": [SLUG], "second": [SLUG]}
    assert len([c for c in Calls(env / "gh.log")() if c[0] == "api"]) == 2


def test_boards_without_a_remote_have_nothing_to_check(conn: Any, board: str) -> None:
    assert protection.refresh(conn) == {
        "checked_at": state(conn)["checked_at"],
        "boards": {},
        "unknown": [],
    }


def test_the_housekeeping_wakeup_refreshes_it_and_then_waits_six_hours(
    conn: Any, used: Path, set_api: Api, env: Path
) -> None:
    set_api({RULES: [], CLASSIC: {"protected": False}})
    housekeeping.run_due(conn, 1_000_000)
    assert state(conn)["boards"] == {"main": [SLUG]}
    set_api({RULES: [{"type": "pull_request"}]})
    housekeeping.run_due(conn, 1_000_000 + protection.PROTECTION_S - 1)
    assert state(conn)["boards"] == {"main": [SLUG]}
    housekeeping.run_due(conn, 1_000_000 + protection.PROTECTION_S)
    assert state(conn)["boards"] == {}


def test_a_worktree_card_in_another_repo_counts_for_its_board(
    conn: Any, used: Path, env: Path, set_api: Api
) -> None:
    other = env / "other"
    git("init", "-q", "-b", "main", str(other), cwd=env)
    git("remote", "add", "origin", "https://example.invalid/acme/other.git", cwd=other)
    git("commit", "-q", "--allow-empty", "-m", "x", cwd=other)
    store.create(
        conn, "main", "Other", body="b", assignee="smith", workspace=f"worktree:{other}",
        branch="feat/o",
    )  # fmt: skip
    set_api({"repos/acme/other/rules/branches/main": [], "repos/acme/other/branches/main": {"protected": False}})  # fmt: skip
    assert protection.refresh(conn)["boards"]["main"] == ["example.invalid/acme/other"]
