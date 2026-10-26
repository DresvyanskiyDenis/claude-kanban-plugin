"""TST-L2 (worker verbs bound to the run), REV-01 (PR check) and SEC3 against `worker.act`."""

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, ghops, store, worker
from kanban_claude.errors import Error
from kanban_claude.paths import run_files

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


def test_request_review_finds_the_pr_by_the_card_not_by_the_summary(
    conn: Any, wt: tuple[str, int], set_prs: Prs, env: Path
) -> None:
    tid, run = wt
    set_prs({"3": {**PR, "state": "MERGED", "url": PR["url"].replace("/7", "/3")}, "7": PR})
    got = ask(conn, run, "request-review", summary="follow-up to PR #3, now done")
    assert got["exit"] == 0, got
    t = store.get_task(conn, tid)
    assert (t["status"], t["pr_number"], t["pr_url"]) == ("review", 7, PR["url"])
    calls = [json.loads(x) for x in (env / "gh.log").read_text().splitlines()]
    listing = next(c for c in calls if c[:2] == ["pr", "list"])
    assert listing[listing.index("-R") + 1] == "example.invalid/acme/proj"
    assert [listing[listing.index(f) + 1] for f in ("--head", "--base", "--state")] == [
        "feat/x", "main", "open",
    ]  # fmt: skip


def link_issue(
    conn: Any, tid: str, repo: str = "example.invalid/acme/proj", number: int = 12
) -> None:
    conn.execute(
        "INSERT INTO github_links(task_id, repo, number, node_id, base_title, base_body, card_title,"
        " card_body, synced_status, synced_version, created_at) VALUES (?,?,?,'n','t','b','t','b','todo',1,0)",
        (tid, repo, number),
    )


@pytest.mark.parametrize(
    "body",
    [
        "Closes acme/proj#12",
        "closes ACME/proj#12.",
        "Fixes acme/proj#12\nGate: x",
        "Resolves #12",
        "Closes https://example.invalid/acme/proj/issues/12",
    ],
)
def test_request_review_takes_a_linked_card_whose_pr_closes_the_issue(
    conn: Any, wt: tuple[str, int], set_prs: Prs, body: str
) -> None:
    tid, run = wt
    link_issue(conn, tid)
    set_prs({"7": {**PR, "body": f"Summary\n\n{body}\n"}})
    got = ask(conn, run, "request-review", summary="done")
    assert got["exit"] == 0, got
    assert store.get_task(conn, tid)["status"] == "review"


@pytest.mark.parametrize(
    "body", ["", "no link here", "Closes acme/proj#123", "Closes acme/other#12", "see acme/proj#12"]
)
def test_request_review_refuses_a_linked_card_whose_pr_lacks_the_closes_line(
    conn: Any, wt: tuple[str, int], set_prs: Prs, body: str
) -> None:
    tid, run = wt
    link_issue(conn, tid)
    set_prs({"7": {**PR, "body": body}})
    got = ask(conn, run, "request-review", summary="done")
    assert got["exit"] == 4 and "`Closes acme/proj#12`" in got["message"], got
    assert "PR #7" in got["message"] and store.get_task(conn, tid)["status"] == "running"


@pytest.mark.parametrize("jira", [False, True])
def test_request_review_needs_no_closes_line_without_a_github_link(
    conn: Any, wt: tuple[str, int], set_prs: Prs, jira: bool
) -> None:
    tid, run = wt
    if jira:  # a Jira-only card has no GitHub issue to close
        conn.execute(
            "INSERT INTO jira_links(task_id, jira_key, base_title, base_body, card_title, card_body,"
            " synced_status, synced_version, created_at) VALUES (?,'ABC-1','t','b','t','b','todo',1,0)",
            (tid,),
        )
    set_prs({"7": {**PR, "body": "no closing line at all"}})
    got = ask(conn, run, "request-review", summary="done")
    assert got["exit"] == 0, got


@pytest.mark.parametrize(
    "change",
    [{"state": "MERGED"}, {"headRefName": "feat/other"}, {"baseRefName": "develop"}],
)
def test_request_review_refuses_when_the_card_has_no_open_pr(
    conn: Any, wt: tuple[str, int], set_prs: Prs, change: dict[str, str]
) -> None:
    tid, run = wt
    set_prs({"7": {**PR, **change}})
    got = ask(conn, run, "request-review", summary="PR #7")
    assert got["exit"] == 4 and "no open PR" in got["message"] and "feat/x" in got["message"], got
    assert store.get_task(conn, tid)["status"] == "running"


def test_request_review_refuses_several_open_prs(
    conn: Any, wt: tuple[str, int], set_prs: Prs
) -> None:
    tid, run = wt
    set_prs({"7": PR, "8": {**PR, "url": PR["url"].replace("/7", "/8")}})
    got = ask(conn, run, "request-review", summary="done")
    assert got["exit"] == 4 and "#7" in got["message"] and "#8" in got["message"], got
    assert store.get_task(conn, tid)["status"] == "running"


def test_request_review_on_a_scratch_card_says_to_block_not_to_complete(
    conn: Any, card: Callable[..., str]
) -> None:
    run = store.claim(conn, card("s"))
    assert run
    got = ask(conn, run, "request-review", summary="PR #5 is open")
    assert got["exit"] == 4 and "kanban block --kind needs_input" in got["message"], got
    assert "skip the warden" in got["message"]


def test_request_review_needs_a_summary(conn: Any, wt: tuple[str, int], set_prs: Prs) -> None:
    _, run = wt
    set_prs({"7": PR})
    assert ask(conn, run, "request-review")["exit"] == 2


def test_sec3_worker_inputs_are_validated(conn: Any, wt: tuple[str, int], set_prs: Prs) -> None:
    tid, run = wt
    assert ask(conn, run, "block", kind="not-a-kind", reason="r")["exit"] == 4
    assert ask(conn, run, "block", kind=["needs_input"], reason="r")["exit"] != 0
    set_prs({"7": {**PR, "url": "http://example.invalid/acme/proj/pull/7"}})
    assert ask(conn, run, "request-review", summary="done")["exit"] == 4
    set_prs({"7": {**PR, "url": "https://evil.invalid/acme/proj/pull/7"}})
    assert ask(conn, run, "request-review", summary="done")["exit"] == 4
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


# --- wait-ci (P1-3) -------------------------------------------------------------------------

NO_CHECKS = "no checks reported on the 'feat/x' branch"
Reply = tuple[int, str, str]


def checks(*rows: tuple[str, str]) -> str:
    return json.dumps([{"name": n, "bucket": b} for n, b in rows])


class FakeGh:
    """`gh` as a scripted replay: each (verb, noun) pair answers its replies in order, the last
    one repeating. `time` is the clock `worker` sees, so a 540 s wait costs no real time."""

    def __init__(self, replies: dict[tuple[str, str], list[Reply]]) -> None:
        self.replies, self.calls, self.t, self.slept = replies, [], 0.0, 0

    def __call__(self, *args: str, timeout: int = 60) -> "subprocess.CompletedProcess[str]":
        self.calls.append(args)
        queue = self.replies[args[0], args[1]]
        rc, out, err = queue.pop(0) if len(queue) > 1 else queue[0]
        return subprocess.CompletedProcess(args, rc, out, err)

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += 1


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeGh]:
    def install(**replies: list[Reply]) -> FakeGh:
        gh = FakeGh({tuple(k.split("_")): v for k, v in replies.items()})  # type: ignore[misc]
        monkeypatch.setattr(ghops, "gh_rc", gh)
        monkeypatch.setattr(worker, "time", gh)
        return gh

    return install


@pytest.mark.parametrize("n", [1, 3])
def test_wait_ci_all_skipped_is_not_a_pass(fake_gh: Callable[..., FakeGh], n: int) -> None:
    fake_gh(pr_checks=[(0, checks(*[(f"c{i}", "skipping") for i in range(n)]), "")])
    state, line = worker.wait_ci("h/o/r", 7)
    assert state == "skipped" and line.startswith("skipped:") and "passed" not in line
    assert worker.CI_EXIT[state] not in (0, worker.CI_EXIT["pending"])


def test_wait_ci_failed_beats_skipped(fake_gh: Callable[..., FakeGh]) -> None:
    fake_gh(pr_checks=[(1, checks(("a", "skipping"), ("b", "fail")), "")])
    assert worker.wait_ci("h/o/r", 7)[0] == "failed"


def test_wait_ci_passes_and_names_the_count(fake_gh: Callable[..., FakeGh]) -> None:
    gh = fake_gh(pr_checks=[(0, checks(("lint", "pass"), ("tests", "skipping")), "")])
    assert worker.wait_ci("h/o/r", 7) == ("passed", "passed: 2 checks")
    assert gh.slept == 0


def test_wait_ci_fails_fast_and_names_the_failing_checks(fake_gh: Callable[..., FakeGh]) -> None:
    rows = (("lint", "fail"), ("tests", "pending"), ("scan", "cancel"), ("fmt", "pass"))
    gh = fake_gh(pr_checks=[(1, checks(*rows), "")])
    assert worker.wait_ci("h/o/r", 7) == ("failed", "failed: lint, scan")
    assert gh.slept == 0


def test_wait_ci_polls_until_the_checks_settle(fake_gh: Callable[..., FakeGh]) -> None:
    pending = (8, checks(("lint", "pass"), ("tests", "pending")), "")
    gh = fake_gh(pr_checks=[pending, pending, (0, checks(("lint", "pass"), ("tests", "pass")), "")])
    assert worker.wait_ci("h/o/r", 7)[0] == "passed"
    assert gh.slept == 2


def test_wait_ci_gives_up_inside_the_budget_and_says_pending(
    fake_gh: Callable[..., FakeGh],
) -> None:
    gh = fake_gh(pr_checks=[(8, checks(("lint", "pass"), ("a", "pending"), ("b", "pending")), "")])
    assert worker.wait_ci("h/o/r", 7) == ("pending", "pending: 2 of 3 checks still running")
    assert 0 < gh.t <= worker.BUDGET_S and gh.t > worker.BUDGET_S - 2 * worker.POLL_S


def test_wait_ci_with_no_checks_yet_falls_back_to_the_runs_of_the_head_commit(
    fake_gh: Callable[..., FakeGh],
) -> None:
    running = [{"name": "ci", "status": "in_progress", "conclusion": ""}]
    done = [{"name": "ci", "status": "completed", "conclusion": "success"}]
    gh = fake_gh(
        pr_checks=[(1, "", NO_CHECKS)],
        pr_view=[(0, json.dumps({"headRefOid": "abc123"}), "")],
        run_list=[(0, json.dumps(running), ""), (0, json.dumps(done), "")],
    )
    assert worker.wait_ci("h/o/r", 7) == ("passed", "passed: 1 check")
    listing = next(c for c in gh.calls if c[:2] == ("run", "list"))
    assert listing[listing.index("--commit") + 1] == "abc123"
    assert gh.slept == 1


def test_wait_ci_failed_run_fails_and_no_run_at_all_stays_pending(
    fake_gh: Callable[..., FakeGh],
) -> None:
    failed = [{"name": "build", "status": "completed", "conclusion": "failure"}]
    fake_gh(
        pr_checks=[(1, "", NO_CHECKS)],
        pr_view=[(0, json.dumps({"headRefOid": "abc"}), "")],
        run_list=[(0, json.dumps(failed), "")],
    )
    assert worker.wait_ci("h/o/r", 7) == ("failed", "failed: build")
    fake_gh(
        pr_checks=[(0, "[]", "")],
        pr_view=[(0, json.dumps({"headRefOid": "abc"}), "")],
        run_list=[(0, "[]", "")],
        **WORKFLOWS_2,
    )
    state, line = worker.wait_ci("h/o/r", 7)
    assert state == "pending" and "no checks reported" in line


WF_API = "api_repos/o/r/actions/workflows"
WF_DIR = "api_repos/o/r/contents/.github/workflows?ref=abc"
WORKFLOWS_2 = {WF_API: [(0, json.dumps({"total_count": 2}), "")]}
NO_WORKFLOWS = {WF_API: [(0, json.dumps({"total_count": 0}), "")]}
NOT_FOUND = (1, "", "gh: Not Found (HTTP 404)")


def no_checks(**extra: list[Reply]) -> dict[str, list[Reply]]:
    return {
        "pr_checks": [(1, "", NO_CHECKS)],
        "pr_view": [(0, json.dumps({"headRefOid": "abc"}), "")],
        "run_list": [(0, "[]", "")],
        **extra,
    }


def test_wait_ci_on_a_repo_without_workflows_says_no_ci_after_the_grace(
    fake_gh: Callable[..., FakeGh],
) -> None:
    gh = fake_gh(**no_checks(**NO_WORKFLOWS, **{WF_DIR: [NOT_FOUND]}))
    state, line = worker.wait_ci("h/o/r", 7)
    assert state == "no_ci" and "passed" not in line and "green" not in line
    assert worker.NO_CI_GRACE_S <= gh.t < worker.NO_CI_GRACE_S + worker.POLL_S
    asked = [c for c in gh.calls if c[0] == "api"]
    assert asked[0][1] == "repos/o/r/actions/workflows" and asked[0][-2:] == ("--hostname", "h")


def test_wait_ci_does_not_declare_no_ci_when_the_pr_adds_the_first_workflow(
    fake_gh: Callable[..., FakeGh],
) -> None:
    gh = fake_gh(**no_checks(**NO_WORKFLOWS, **{WF_DIR: [(0, "[]", "")]}))
    assert worker.wait_ci("h/o/r", 7)[0] == "pending"
    assert gh.t >= worker.NO_CHECKS_CAP_S


def test_wait_ci_with_workflows_caps_the_no_checks_phase_and_stays_pending(
    fake_gh: Callable[..., FakeGh],
) -> None:
    gh = fake_gh(**no_checks(**WORKFLOWS_2))
    state, line = worker.wait_ci("h/o/r", 7)
    assert state == "pending" and "no checks reported" in line
    assert worker.NO_CHECKS_CAP_S <= gh.t < worker.NO_CHECKS_CAP_S + worker.POLL_S
    assert not any(c[0] == "api" and "contents" in c[1] for c in gh.calls)


def test_wait_ci_unknown_workflow_lookup_is_not_no_ci(fake_gh: Callable[..., FakeGh]) -> None:
    fake_gh(**no_checks(**{WF_API: [(1, "", "HTTP 502")]}))
    assert worker.wait_ci("h/o/r", 7)[0] == "pending"


def test_wait_ci_late_checks_still_count_after_the_grace(fake_gh: Callable[..., FakeGh]) -> None:
    fake_gh(**{**no_checks(**WORKFLOWS_2), "pr_checks": [
        (1, "", NO_CHECKS), (1, "", NO_CHECKS), (1, "", NO_CHECKS), (1, "", NO_CHECKS),
        (0, checks(("lint", "pass")), ""),
    ]})  # fmt: skip
    assert worker.wait_ci("h/o/r", 7) == ("passed", "passed: 1 check")


def test_wait_ci_cli_exits_6_for_no_ci(
    kb: Any, wt: tuple[str, int], fake_gh: Callable[..., FakeGh], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_RUN_ID", str(wt[1]))
    fake_gh(**no_checks(**{
        "api_repos/acme/proj/actions/workflows": NO_WORKFLOWS[WF_API],
        "api_repos/acme/proj/contents/.github/workflows?ref=abc": [NOT_FOUND],
    }))  # fmt: skip
    got, out, _ = kb("wait-ci", "--pr", "7")
    assert got == 6 and out.startswith("no_ci") and worker.CI_EXIT["no_ci"] not in (0, 1, 2)


def test_wait_ci_survives_a_gh_error_and_reports_it_when_time_runs_out(
    fake_gh: Callable[..., FakeGh],
) -> None:
    gh = fake_gh(
        pr_checks=[(1, "", "HTTP 502"), (0, checks(("lint", "pass")), "")],
    )
    assert worker.wait_ci("h/o/r", 7)[0] == "passed" and gh.slept == 1
    fake_gh(pr_checks=[(1, "", "HTTP 502")])
    state, line = worker.wait_ci("h/o/r", 7)
    assert state == "pending" and "HTTP 502" in line


def test_wait_ci_verb_defaults_to_the_cards_pr_or_finds_it(
    conn: Any, wt: tuple[str, int], set_prs: Prs, fake_gh: Callable[..., FakeGh]
) -> None:
    tid, run = wt
    gh = fake_gh(
        pr_list=[(0, json.dumps([{"number": 7, "url": PR["url"]}]), "")],
        pr_checks=[(0, checks(("lint", "pass")), "")],
    )
    got = ask(conn, run, "wait-ci")
    assert got["exit"] == 0 and got["data"] == {
        "state": "passed", "summary": "passed: 1 check", "pr": 7,
    }  # fmt: skip
    checked = next(c for c in gh.calls if c[:2] == ("pr", "checks"))
    assert checked[2] == "7" and "example.invalid/acme/proj" in checked
    assert ask(conn, run, "wait-ci", pr=9)["data"]["pr"] == 9
    assert store.get_task(conn, tid)["status"] == "running"


def test_wait_ci_cli_exit_codes(
    kb: Any, wt: tuple[str, int], fake_gh: Callable[..., FakeGh], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KANBAN_RUN_ID", str(wt[1]))
    for reply, code, word in (
        ((0, checks(("lint", "pass")), ""), 0, "passed"),
        ((1, checks(("lint", "fail")), ""), 1, "failed: lint"),
        ((8, checks(("lint", "pending")), ""), 2, "pending"),
    ):
        fake_gh(pr_checks=[reply])
        got, out, _ = kb("wait-ci", "--pr", "7")
        assert (got, word in out) == (code, True)
    monkeypatch.delenv("KANBAN_RUN_ID")
    assert kb("wait-ci")[0] == 5
    assert "worker verb" in kb("wait-ci")[2]


def test_wait_ci_cannot_find_the_pr_is_pending_not_failed(
    conn: Any, wt: tuple[str, int], fake_gh: Callable[..., FakeGh]
) -> None:
    fake_gh(pr_list=[(1, "", "HTTP 502")])
    got = ask(conn, wt[1], "wait-ci")["data"]
    assert got["state"] == "pending" and "cannot look up the PR" in got["summary"]
    fake_gh(pr_list=[(0, "[]", "")])
    assert ask(conn, wt[1], "wait-ci")["exit"] == 4


# --- block --retry-after (P1-6) -------------------------------------------------------------


def test_a_transient_block_with_retry_after_goes_back_to_ready_and_counts_as_a_failure(
    conn: Any, wt: tuple[str, int], clock: Callable[[int], None], procs: Any
) -> None:
    tid, run = wt
    got = ask(conn, run, "block", kind="transient", reason="Zscaler 403", retry_after="30m")
    assert got["exit"] == 0 and got["data"]["status"] == "ready", got
    t = store.get_task(conn, tid)
    assert (t["status"], t["block_kind"], t["consecutive_failures"]) == ("ready", None, 1)
    assert t["retry_after"] == store.now() + 1800
    assert dispatcher.dispatch_once(conn, store.now(), procs, dry_run=True)["skipped"] == {
        tid: "retry_after"
    }
    clock(1800)
    assert dispatcher.dispatch_once(conn, store.now(), procs, dry_run=True)["claimed"] == [
        {"id": tid}
    ]
    kinds = [
        r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (tid,))
    ]
    assert "transient_retry" in kinds and "blocked" not in kinds
    assert (
        "Zscaler 403"
        in conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[
            "body"
        ]
    )


def test_transient_retries_stop_at_the_cards_failure_limit(conn: Any, wt: tuple[str, int]) -> None:
    tid, run = wt
    conn.execute("UPDATE tasks SET max_retries = 2 WHERE id = ?", (tid,))
    assert ask(conn, run, "block", kind="transient", reason="slow", retry_after="1h")["exit"] == 0
    again = store.claim(conn, tid)
    assert again
    got = ask(conn, again, "block", kind="transient", reason="slow", retry_after="1h")
    assert got["exit"] == 0 and got["data"]["status"] == "blocked"
    t = store.get_task(conn, tid)
    assert (t["block_kind"], t["consecutive_failures"]) == ("gave_up", 2)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'gave_up'", (tid,)
        ).fetchone()[0]
        == 1
    )


def test_retry_after_is_only_for_transient_blocks_and_must_be_a_duration(
    conn: Any, wt: tuple[str, int]
) -> None:
    tid, run = wt
    for kind, after in (("needs_input", "30m"), ("transient", "soon"), ("transient", "0m")):
        got = ask(conn, run, "block", kind=kind, reason="r", retry_after=after)
        assert got["exit"] == 4, got
    assert store.get_task(conn, tid)["status"] == "running"
    plain = ask(conn, run, "block", kind="transient", reason="r")
    assert plain["data"]["status"] == "blocked"


# --- gate (0.4.0) -----------------------------------------------------------------------------


def gate(conn: Any, run: int, *command: str, lines: int | None = None) -> dict[str, Any]:
    return ask(conn, run, "gate", command=list(command), lines=lines)


def test_gate_passes_the_exit_code_through_and_logs_beside_the_run(
    conn: Any, wt: tuple[str, int]
) -> None:
    tid, run = wt
    out = gate(conn, run, "echo hello; exit 3")["data"]
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    log = Path(out["log"])
    assert out["exit"] == 3 and log.parent == run_files("main", tid, row["n"])[0].parent
    assert log.name == f"run-{row['n']}.gate-1.log" and "hello" in log.read_text()
    assert re.fullmatch(
        rf"gate: exit 3 after \d+s, 1 lines; full log: {re.escape(str(log))}\nhello", out["text"]
    )
    assert Path(gate(conn, run, "true")["data"]["log"]).name.endswith(".gate-2.log")


def test_gate_one_argument_is_a_shell_string_and_several_are_an_argv(
    conn: Any, wt: tuple[str, int]
) -> None:
    _, run = wt
    piped = gate(conn, run, "false | cat")["data"]
    assert piped["exit"] == 1  # pipefail
    argv = gate(conn, run, "echo", "a  b", "$HOME")["data"]
    assert argv["text"].splitlines()[1] == "a  b $HOME"
    assert gate(conn, run, "no-such-command-x", "arg")["data"]["exit"] == 127


def test_gate_shows_the_failures_first_then_the_tail_and_never_more_than_the_limit(
    conn: Any, wt: tuple[str, int]
) -> None:
    _, run = wt
    script = (
        "for i in $(seq 1 100); do echo line $i; done; "
        "echo 'FAILED test_a'; echo 'FAILED test_a'; for i in $(seq 1 100); do echo more $i; done; "
        "echo '== 1 failed, 5 passed =='; exit 1"
    )
    out = gate(conn, run, script, lines=10)["data"]
    body = out["text"].splitlines()[1:]
    assert len(body) <= 10 and body[-1] == "more 100" and body[0] == "FAILED test_a"
    assert body.count("FAILED test_a") == 1 and "== 1 failed, 5 passed ==" in body[:3]
    assert "--- last lines ---" in body
    assert len(Path(out["log"]).read_text().splitlines()) == 203  # the log keeps everything
    quiet = gate(conn, run, "seq 1 100", lines=5)["data"]["text"].splitlines()
    assert quiet[1:] == ["96", "97", "98", "99", "100"]


def test_gate_refuses_no_command_and_a_zero_limit(conn: Any, wt: tuple[str, int]) -> None:
    _, run = wt
    assert gate(conn, run)["exit"] == 2
    assert gate(conn, run, "true", lines=0)["exit"] == 2
