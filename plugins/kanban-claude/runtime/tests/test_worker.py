"""TST-L2 (worker verbs bound to the run), REV-01 (PR check) and SEC3 against `worker.act`."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import store, worker
from kanban_claude.errors import Error

from .conftest import git

Prs = Callable[[dict[str, dict[str, Any]]], None]
PR = {
    "url": "https://example.invalid/acme/proj/pull/7",
    "state": "OPEN",
    "headRefName": "feat/x",
    "baseRefName": "main",
}


def ask(conn: Any, run: int, verb: str, **args: Any) -> dict[str, Any]:
    try:
        return {"exit": 0, "message": "ok", "data": worker.act(conn, run, verb, args)}
    except Error as e:
        return {"exit": e.exit, "message": str(e), "data": None}


def test_show_context_bound_to_the_run_card(conn: Any, wt: tuple[str, int]) -> None:
    tid, run = wt
    shown = ask(conn, run, "show", id=tid)
    assert shown["exit"] == 0 and shown["data"]["task"]["id"] == tid
    assert "Work" in ask(conn, run, "context")["data"]["text"]
    other = ask(conn, run, "show", id="t_other")
    assert other["exit"] == 5


def test_comment_author_is_the_run_role_and_binds_the_card(conn: Any, wt: tuple[str, int]) -> None:
    tid, run = wt
    assert ask(conn, run, "comment", text="hello")["exit"] == 0
    row = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
    ).fetchone()
    assert (row["author"], row["body"]) == ("smith", "hello")
    assert ask(conn, run, "comment", id="t_x", text="hi")["exit"] == 5
    assert ask(conn, run, "comment")["exit"] == 2


def test_verbs_after_the_run_ended_are_refused_unless_repeated(
    conn: Any, wt: tuple[str, int]
) -> None:
    tid, run = wt
    assert ask(conn, run, "block", kind="needs_input", reason="which db?")["exit"] == 0
    again = ask(conn, run, "block", kind="needs_input", reason="which db?")
    assert again["exit"] == 0 and again["data"] == {"noop": True}
    for route, body in (
        ("comment", {"text": "late"}),
        ("complete", {"summary": "done"}),
        ("attach", {"filename": "a", "data": b""}),
        ("show", {}),
        ("context", {}),
    ):
        got = ask(conn, run, route, **body)
        assert got["exit"] == 4 and "run ended" in got["message"]


def test_complete_by_a_builder_with_unpushed_work_is_refused(
    conn: Any, wt: tuple[str, int], repo: Path
) -> None:
    tid, run = wt
    git("checkout", "-b", "feat/x", cwd=repo)
    (repo / "b.txt").write_text("b")
    git("add", "b.txt", cwd=repo)
    git("commit", "-m", "work", cwd=repo)
    got = ask(conn, run, "complete", summary="done")
    assert got["exit"] == 4 and "request-review" in got["message"]
    assert store.get_task(conn, tid)["status"] == "running"


def test_request_review_checks_the_pr(
    conn: Any, wt: tuple[str, int], set_prs: Prs, env: Path
) -> None:
    tid, run = wt
    set_prs({"7": PR})
    got = ask(conn, run, "request-review", summary="done, PR #7")
    assert got["exit"] == 0, got
    t = store.get_task(conn, tid)
    assert (t["status"], t["pr_number"], t["pr_url"]) == ("review", 7, PR["url"])
    calls = [json.loads(x) for x in (env / "gh.log").read_text().splitlines()]
    view = next(c for c in calls if c[:2] == ["pr", "view"])
    assert view[view.index("-R") + 1] == "example.invalid/acme/proj"


@pytest.mark.parametrize(
    "change",
    [
        {"state": "MERGED"},
        {"headRefName": "feat/other"},
        {"baseRefName": "develop"},
        {"url": "https://evil.invalid/acme/proj/pull/7"},
    ],
)
def test_request_review_refuses_a_mismatching_pr(
    conn: Any, wt: tuple[str, int], set_prs: Prs, change: dict[str, str]
) -> None:
    tid, run = wt
    set_prs({"7": {**PR, **change}})
    got = ask(conn, run, "request-review", summary="PR #7")
    assert got["exit"] == 4, got
    assert store.get_task(conn, tid)["status"] == "running"


def test_request_review_needs_a_pr_and_reports_gh_failure(
    conn: Any, wt: tuple[str, int], set_prs: Prs
) -> None:
    _, run = wt
    assert ask(conn, run, "request-review", summary="no number")["exit"] == 4
    set_prs({})
    got = ask(conn, run, "request-review", summary="PR #9")
    assert got["exit"] == 1 and "gh" in got["message"]


def test_sec3_worker_inputs_are_validated(conn: Any, wt: tuple[str, int], set_prs: Prs) -> None:
    tid, run = wt
    assert ask(conn, run, "block", kind="not-a-kind", reason="r")["exit"] == 4
    assert ask(conn, run, "block", kind=["needs_input"], reason="r")["exit"] != 0
    set_prs({"7": {**PR, "url": "http://example.invalid/acme/proj/pull/7"}})
    assert ask(conn, run, "request-review", summary="PR #7")["exit"] == 4
    set_prs({"7": {**PR, "url": "https://evil.invalid/acme/proj/pull/7"}})
    assert ask(conn, run, "request-review", summary="PR #7")["exit"] == 4
    assert store.get_task(conn, tid)["status"] == "running"


def test_attach_and_the_text_cap(conn: Any, wt: tuple[str, int]) -> None:
    tid, run = wt
    got = ask(conn, run, "attach", filename="../report.md", data=b"scout")
    assert got["exit"] == 0, got
    row = conn.execute("SELECT filename, uploaded_by FROM task_attachments").fetchone()
    assert (row["filename"], row["uploaded_by"]) == ("report.md", "smith")
    assert ask(conn, run, "comment", text="x" * (worker.MAX_TEXT + 1))["exit"] == 4
    assert ask(conn, run, "comment", text="x" * worker.MAX_TEXT)["exit"] == 0


def test_an_unknown_run_counts_as_ended(conn: Any, wt: tuple[str, int]) -> None:
    got = ask(conn, wt[1] + 100, "show")
    assert got["exit"] == 4 and "run ended" in got["message"]


@pytest.mark.parametrize(
    ("verb", "args"), [("comment", {"text": "late"}), ("attach", {"filename": "a", "data": b"x"})]
)
def test_a_write_racing_the_end_of_its_run_is_refused(
    conn: Any,
    wt: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    args: dict[str, Any],
) -> None:
    tid, run = wt
    real_text = worker._text

    def end_then_read(body: dict[str, Any], key: str) -> str:
        if store.get_task(conn, tid)["status"] == "running":
            store.stop(conn, tid)
        return real_text(body, key)

    monkeypatch.setattr(worker, "_text", end_then_read)
    got = ask(conn, run, verb, **args)
    assert got["exit"] == 4 and "run ended" in got["message"]
    for table in ("task_comments", "task_attachments"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
