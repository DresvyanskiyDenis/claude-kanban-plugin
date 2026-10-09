"""TST-C: outcome classification (RUN-12), from the stream fixtures and through a real runner."""

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import classify, store
from kanban_claude.dispatcher import dispatch_once

from .conftest import HERE, Execute, FakeProcs

Card = Callable[..., str]
FAR = 1_900_000_000
NOW = 1_800_000_000


def judge(fixture: str, **kw: Any) -> classify.Verdict:
    stream = classify.Stream()
    for line in (HERE / "fixtures" / f"{fixture}.jsonl").read_text().splitlines():
        stream.feed(json.loads(line.replace("__COST__", "0.12")))
    args: dict[str, Any] = {
        "exit_code": 0, "timed_out": False, "now": NOW, "requeue_s": 300,
    }  # fmt: skip
    return classify.classify(stream, **{**args, **kw})


def test_c1_a_rejected_event_is_a_quota_hit_with_its_reset() -> None:
    assert judge("rejected", exit_code=1) == (
        "quota",
        "You've hit your session limit · resets 3pm",
        FAR,
    )


@pytest.mark.parametrize("exit_code", [0, 1])
def test_c2_a_refusal_is_quota_even_on_exit_0(exit_code: int) -> None:
    v = judge("refusal", exit_code=exit_code)
    assert v.outcome == "quota" and v.retry_after == NOW + 300


def test_c3_a_high_utilisation_is_recorded_but_is_not_a_failure() -> None:
    stream = classify.Stream()
    for line in (HERE / "fixtures" / "utilisation.jsonl").read_text().splitlines():
        stream.feed(json.loads(line.replace("__COST__", "0.1")))
    assert stream.five_hour == (0.92, FAR) and not stream.rejected
    assert judge("utilisation").outcome == "protocol_violation"


def test_c4_a_401_is_auth_lost() -> None:
    assert judge("auth401", exit_code=1).outcome == "auth_lost"


def test_c5_the_budget_fuse_is_its_own_outcome() -> None:
    assert judge("budget", exit_code=1).outcome == "budget_exceeded"


def test_c6_auth_words_beyond_the_first_line_are_not_auth() -> None:
    assert judge("prose").outcome == "protocol_violation"


def test_the_last_result_wins_and_the_rest_of_the_table() -> None:
    stream = classify.Stream()
    for line in (HERE / "fixtures" / "two_results.jsonl").read_text().splitlines():
        stream.feed(json.loads(line.replace("__COST__", "0.9")))
    assert len(stream.results) == 2 and stream.result and stream.result["result"] == "last"
    assert judge("success", timed_out=True, exit_code=-15).outcome == "timed_out"
    crashed = judge("success", exit_code=2, stderr="boom")
    assert crashed.outcome == "crashed" and "exit" in crashed.error


def test_the_rate_limit_event_is_read_in_the_shape_claude_emits() -> None:
    """`rate_limit_info.unifiedWindows.five_hour`: a fraction and epoch seconds; the seven-day window is not the wall."""
    s = classify.Stream()
    assert s.feed(json.loads((HERE / "fixtures" / "rejected.jsonl").read_text().splitlines()[1]))
    assert s.rejected and s.reset == FAR and s.five_hour == (1.0, FAR)
    assert (s.limit or {})["rateLimitType"] == "five_hour"
    t = classify.Stream()
    t.feed(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed",
                "unifiedWindows": {"seven_day": {"utilization": 0.99, "resetsAt": FAR}},
            },
        }
    )
    assert t.five_hour is None and not t.rejected


# --- through the runner ---------------------------------------------------------------------


def account(conn: Any, card_id: str) -> dict[str, Any]:
    board = store.get_board(conn, store.get_task(conn, card_id)["board"])
    return store.account_get(conn, store.effective_config_dir(conn, board))


def test_c1_a_quota_hit_holds_the_account_and_is_not_counted(
    conn: Any, card: Card, execute: Execute, procs: FakeProcs
) -> None:
    tid = card()
    run = execute(tid, "rejected", exit_code=1)
    t = store.get_task(conn, tid)
    assert run["outcome"] == "quota" and t["status"] == "ready" and t["consecutive_failures"] == 0
    assert t["retry_after"] == FAR and account(conn, tid)["hard_until"] == FAR
    assert dispatch_once(conn, int(time.time()), procs)["skipped"] == {tid: "quota_wall"}


def test_c2_a_refusal_on_exit_0_is_quota_not_a_finished_report(
    conn: Any, card: Card, execute: Execute
) -> None:
    tid = card()
    run = execute(tid, "refusal")
    assert run["outcome"] == "quota" and store.get_task(conn, tid)["status"] == "ready"


def test_c3_the_soft_wall_is_set_from_the_utilisation(
    conn: Any, card: Card, execute: Execute
) -> None:
    tid = card()
    run = execute(tid, "utilisation", terminal=["complete", tid, "--summary", "done"])
    acct = account(conn, tid)
    assert run["outcome"] == "completed" and acct["soft_until"] == FAR
    assert acct["last_rate_limit"]["five_hour"] == 0.92


def test_c4_a_401_pauses_the_account_until_resume(
    conn: Any, card: Card, execute: Execute, procs: FakeProcs
) -> None:
    tid = card()
    run = execute(tid, "auth401", exit_code=1)
    assert run["outcome"] == "auth_lost" and store.get_task(conn, tid)["consecutive_failures"] == 0
    assert account(conn, tid)["auth_paused"]["error"].startswith("Invalid API key")
    assert dispatch_once(conn, int(time.time()), procs)["skipped"] == {tid: "account_paused"}


def test_c5_the_budget_fuse_is_a_counted_failure(conn: Any, card: Card, execute: Execute) -> None:
    tid = card()
    run = execute(tid, "budget", exit_code=1)
    t = store.get_task(conn, tid)
    assert run["outcome"] == "budget_exceeded" and t["consecutive_failures"] == 1
    assert run["subtype"] == "error_max_budget_usd" and run["cost_usd"] == 0.31


def test_c6_auth_prose_in_a_successful_card_is_not_auth(
    conn: Any, card: Card, execute: Execute
) -> None:
    done, silent = card("done"), card("silent")
    assert (
        execute(done, "prose", terminal=["complete", done, "--summary", "ok"])["outcome"]
        == "completed"
    )
    assert execute(silent, "prose")["outcome"] == "protocol_violation"
    assert not account(conn, silent).get("auth_paused")
    t = store.get_task(conn, silent)
    assert t["protocol_violations"] == 1 and t["consecutive_failures"] == 0 and t["retry_after"]


def test_c7_cost_is_the_delta_of_the_session_total_across_resumes(
    conn: Any, card: Card, execute: Execute
) -> None:
    tid = card()
    first = execute(tid, "success", FAKE_CLAUDE_COST="0.50")
    second = execute(tid, "success", FAKE_CLAUDE_COST="0.80")
    assert (first["cost_usd"], first["session_cost_usd"]) == (0.5, 0.5)
    assert second["cost_usd"] == pytest.approx(0.3) and second["session_cost_usd"] == 0.8
    assert first["session_id"] == second["session_id"]


def test_the_last_of_several_results_is_what_the_run_records(
    conn: Any, card: Card, execute: Execute, env: Path
) -> None:
    run = execute(card(), "two_results")
    assert run["num_turns"] == 5 and run["cost_usd"] == 0.12
    assert len(Path(run["stream_path"]).read_text().splitlines()) == 4
