"""0.4.0 per-run measurement: the `Measure` accumulator, `record_measure`, the backfill and the
new `stats` keys."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kanban_claude import measure, store

from .conftest import Execute

Card = Callable[..., str]
Claim = Callable[[str], int]


def asst(mid: str, *blocks: dict[str, Any], one_h: int = 0, five_m: int = 0) -> dict[str, Any]:
    usage = {
        "cache_creation": {"ephemeral_1h_input_tokens": one_h, "ephemeral_5m_input_tokens": five_m}
    }
    return {"type": "assistant", "message": {"id": mid, "content": list(blocks), "usage": usage}}


def tool(bid: str, name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": bid, "name": name, "input": args}


def feed(*events: dict[str, Any]) -> measure.Measure:
    m = measure.Measure()
    for ev in events:
        m.feed(ev)
    return m


def test_one_api_message_streamed_as_several_events_is_one_turn_and_counted_once() -> None:
    bash = tool("b1", "Bash", command="ls")
    m = feed(
        asst("m1", {"type": "text", "text": "x"}, one_h=100, five_m=5),
        asst("m1", bash, one_h=100, five_m=5),
        asst("m1", bash, one_h=100, five_m=5),  # the same block again
        asst("m2", tool("b2", "Bash", command="pwd"), one_h=7),
    )
    h = m.harness()
    assert h["turns"] == 2 and h["tools"] == {"Bash": 2}
    assert (m.cache_1h, m.cache_5m) == (107, 5)


def test_advisor_skills_mcp_and_agents_are_told_apart() -> None:
    m = feed(
        asst(
            "m1",
            {"type": "server_tool_use", "id": "s1", "name": "advisor", "input": {}},
            tool("b1", "Skill", skill="kanban-code-review"),
            tool("b2", "mcp__context7__query-docs", libraryId="x"),
            tool("b3", "mcp__context7__resolve-library-id"),
            tool("b4", "Agent", subagent_type="Explore"),
            tool("b5", "Task", subagent_type="Explore"),
        )
    )
    h = m.harness()
    assert m.advisor_calls == 1
    assert h["skills"] == {"kanban-code-review": 1}
    assert h["mcp"] == {"context7": 2}
    assert h["agents"] == {"Explore": 2}


def test_hooks_compactions_and_tool_errors() -> None:
    err = {"type": "tool_result", "tool_use_id": "b1", "is_error": True}
    m = feed(
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_name": "SessionStart:startup",
            "outcome": "success",
        },
        {
            "type": "system",
            "subtype": "hook_response",
            "hook_name": "SessionStart:startup",
            "outcome": "error",
        },
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "user", "message": {"content": [err]}},
        {
            "type": "user",
            "message": {"content": [err, {"type": "tool_result", "tool_use_id": "b2"}]},
        },
    )
    h = m.harness()
    assert h["hooks"] == {"SessionStart:startup": {"ok": 1, "failed": 1}}
    assert h["compactions"] == 1 and h["tool_errors"] == 1


def test_results_denials_add_up_subagents_take_the_largest_and_the_last_reason_wins() -> None:
    m = feed(
        {
            "type": "result",
            "duration_api_ms": 0,
            "permission_denials": [1],
            "terminal_reason": "a",
            "subagent_stats": {"spawned": 1, "failed": 0},
        },
        {
            "type": "result",
            "duration_api_ms": 9000,
            "permission_denials": [],
            "terminal_reason": None,
            "subagent_stats": {"spawned": 3, "failed": 1},
        },
        {
            "type": "result",
            "duration_api_ms": 4000,
            "permission_denials": [1, 2],
            "terminal_reason": "completed",
            "subagent_stats": {"spawned": 2, "failed": 0},
        },
    )
    h = m.harness()
    assert m.api_ms == 9000  # cumulative over the session, so the largest result
    assert h["permission_denials"] == 3
    assert h["subagents"] == {"spawned": 3, "failed": 1}
    assert h["terminal_reason"] == "completed"


def test_a_stream_with_no_result_has_no_api_time() -> None:
    assert feed(asst("m1")).api_ms is None


def test_a_malformed_event_never_raises() -> None:
    m = feed(
        {"type": "assistant", "message": "x"}, {"type": "result", "permission_denials": "x"}, {}
    )
    assert m.harness()["turns"] == 0


def test_init_params_keep_the_shape_of_the_init_event() -> None:
    m = feed({"type": "system", "subtype": "init", "model": "m", "permissionMode": "auto",
              "claude_code_version": "2.1", "tools": ["Bash"], "plugins": [{"name": "kanban-claude"}],
              "mcp_servers": [{"name": "c7", "status": "connected", "command": "secret"}]})  # fmt: skip
    p = m.init_params()
    assert p["model"] == "m" and p["plugins"] == ["kanban-claude"]
    assert p["mcp_servers"] == [{"name": "c7", "status": "connected"}]


def test_init_params_take_an_init_event_of_any_shape() -> None:
    m = feed({"type": "system", "subtype": "init", "mcp_servers": None, "tools": 5, "plugins": 5,
              "skills": "x", "agents": {"a": 1}})  # fmt: skip
    p = m.init_params()
    assert [p[k] for k in ("tools", "mcp_servers", "plugins", "skills", "agents")] == [[]] * 5


def test_the_param_env_allowlist_holds_no_secret_name() -> None:
    assert not [
        k for k in measure.PARAM_ENV if any(w in k for w in ("TOKEN", "KEY", "SECRET", "PASS"))
    ]
    assert "PATH" not in measure.PARAM_ENV


# --- through the store ------------------------------------------------------------------------


def stream(*events: dict[str, Any]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\nnot json, stderr merged in\n"


def result(api_ms: int) -> dict[str, Any]:
    return {"type": "result", "duration_api_ms": api_ms, "permission_denials": []}


def test_a_launched_and_finished_run_records_advisor_params_and_harness(
    conn: Any, card: Card, execute: Execute
) -> None:
    run = execute(card())
    assert run["advisor"] == "inherit"
    assert run["harness"] is not None and json.loads(run["harness"])["turns"] >= 0
    params = json.loads(run["params"])
    assert params["argv"][params["argv"].index("-p") + 1] == "<prompt>"
    assert "PATH" not in params["env"] and params["env"]["KANBAN_RUN_ID"] == str(run["id"])
    assert Path(params["prompt_path"]).read_text().strip()
    assert params["init"]["model"] == "claude-sonnet-x"


def test_api_time_of_a_resumed_run_is_the_rise_over_the_sessions_earlier_runs(
    conn: Any, card: Card, execute: Execute
) -> None:
    first = execute(card())
    conn.execute("UPDATE task_runs SET api_ms = 0 WHERE id = ?", (first["id"],))
    one = execute(card())
    two = execute(card())
    sid = "shared-session"
    for r in (one, two):
        conn.execute("UPDATE task_runs SET session_id = ? WHERE id = ?", (sid, r["id"]))
    conn.execute("UPDATE task_runs SET api_ms = 5000 WHERE id = ?", (one["id"],))
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (two["id"],)).fetchone()
    store.record_measure(conn, row, feed(result(8000)))
    assert (
        conn.execute("SELECT api_ms FROM task_runs WHERE id = ?", (two["id"],)).fetchone()[0]
        == 3000
    )
    store.record_measure(conn, row, feed(result(2000)))  # below the floor: a fresh count
    assert (
        conn.execute("SELECT api_ms FROM task_runs WHERE id = ?", (two["id"],)).fetchone()[0]
        == 2000
    )


def test_backfill_fills_old_runs_from_their_sidecars_and_is_idempotent(
    conn: Any, card: Card, execute: Execute, tmp_path: Path
) -> None:
    ok, lost = execute(card()), execute(card())
    side = tmp_path / "old.jsonl"
    side.write_text(
        stream(
            asst(
                "m1",
                {"type": "server_tool_use", "id": "s", "name": "advisor", "input": {}},
                one_h=9,
            ),
            result(1200),
        )
    )
    conn.execute("UPDATE task_runs SET harness = NULL, advisor = NULL, advisor_calls = 0, api_ms = NULL, "
                 "stream_path = ? WHERE id = ?", (str(side), ok["id"]))  # fmt: skip
    conn.execute("UPDATE task_runs SET harness = NULL, advisor = NULL, stream_path = ? WHERE id = ?",
                 (str(tmp_path / "gone.jsonl"), lost["id"]))  # fmt: skip
    assert measure.backfill(conn) == {"filled": 1, "skipped": 1}
    got = conn.execute("SELECT * FROM task_runs WHERE id = ?", (ok["id"],)).fetchone()
    assert (got["advisor"], got["advisor_calls"], got["cache_write_1h_tokens"], got["api_ms"]) == (
        "inherit",
        1,
        9,
        1200,
    )
    assert measure.backfill(conn) == {
        "filled": 0,
        "skipped": 1,
    }  # the lost sidecar is retried, the rest is done
    assert (
        conn.execute("SELECT harness FROM task_runs WHERE id = ?", (lost["id"],)).fetchone()[0]
        is None
    )


def test_backfill_leaves_a_run_that_has_not_ended_alone(
    conn: Any, card: Card, claim: Claim, tmp_path: Path
) -> None:
    run = claim(card())
    side = tmp_path / "live.jsonl"
    side.write_text(stream(result(1)))
    conn.execute("UPDATE task_runs SET stream_path = ? WHERE id = ?", (str(side), run))
    assert measure.backfill(conn) == {"filled": 0, "skipped": 0}


def test_backfill_skips_a_run_it_cannot_measure_and_fills_the_rest(
    conn: Any, card: Card, execute: Execute, tmp_path: Path, monkeypatch: Any
) -> None:
    bad, good = execute(card()), execute(card())
    for run in (bad, good):
        side = tmp_path / f"{run['id']}.jsonl"
        side.write_text(stream(result(1)))
        conn.execute("UPDATE task_runs SET harness = NULL, stream_path = ? WHERE id = ?",
                     (str(side), run["id"]))  # fmt: skip
    real = store.record_measure

    def flaky(c: Any, run: Any, m: Any, **kw: Any) -> Any:
        if run["id"] == bad["id"]:
            raise TypeError("a stream nobody expected")
        return real(c, run, m, **kw)

    monkeypatch.setattr(store, "record_measure", flaky)
    assert measure.backfill(conn) == {"filled": 1, "skipped": 1}
    got = conn.execute("SELECT harness FROM task_runs WHERE id = ?", (good["id"],)).fetchone()[0]
    assert got is not None


def test_stats_new_keys_on_a_measured_board(conn: Any, card: Card, execute: Execute) -> None:
    run = execute(card())
    store.record_measure(
        conn, run,
        feed(asst("m1", tool("b1", "Skill", skill="s1"), tool("b2", "mcp__c7__q"), one_h=4),
             {"type": "system", "subtype": "hook_response", "hook_name": "h", "outcome": "success"},
             {"type": "result", "duration_api_ms": 2500, "permission_denials": [1], "terminal_reason": None}),
    )  # fmt: skip
    s = store.stats(conn, days=1)
    assert s["unmeasured_runs"] == 0
    assert s["skills"] == [{"name": "s1", "calls": 1, "runs": 1}]
    assert s["mcp"] == [{"name": "c7", "calls": 1, "runs": 1}]
    assert s["harness_totals"]["hooks_ok"] == 1 and s["harness_totals"]["permission_denials"] == 1
    assert s["harness_totals"]["by_terminal_reason"] == {"none": 1}
    assert s["runtime_by_role"][0]["role"] == "smith" and s["runtime_by_role"][0]["api_s"] == 2.5
    assert s["advisor_arms"][0]["advisor"] == "inherit"
    assert s["guards"]["first_pass_rate"] is None and s["per_approved_card"]["cards"] == 0
    conn.execute("UPDATE task_runs SET harness = NULL")
    assert store.stats(conn, days=1)["unmeasured_runs"] == 1


def test_stats_guards_and_cost_per_approved_card(conn: Any, card: Card, claim: Claim) -> None:
    def review(tid: str, verb: str, cost: float) -> None:
        run = claim(tid)
        conn.execute("UPDATE task_runs SET kind = 'review', terminal_verb = ?, status = 'ended', "
                     "ended_at = started_at + 5, cost_usd = ? WHERE id = ?", (verb, cost, run))  # fmt: skip
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))

    a, b = card("a"), card("b")
    review(a, "complete", 1.0)  # approved first time
    review(b, "request-changes", 2.0)
    review(b, "complete", 3.0)  # approved on the second round
    s = store.stats(conn, days=1)
    assert s["guards"] == {
        "reviews": 3,
        "first_pass_approvals": 1,
        "send_backs": 1,
        "first_pass_rate": 0.5,
    }
    assert s["per_approved_card"] == {"cards": 2, "cost": 6.0, "avg_cost": 3.0}
