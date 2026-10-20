"""TST-D (dispatcher) plus STO-06 repair and the owner verbs that touch the tick."""

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import dispatcher, gitops, runner, store
from kanban_claude.dispatcher import dispatch_once
from kanban_claude.errors import Refused

from .conftest import FakeProcs, Kb, git

Card = Callable[..., str]
NOW = 2_000_000_000


def tick(conn: Any, procs: FakeProcs, ts: int | None = None, **kw: Any) -> dict[str, Any]:
    return dispatch_once(conn, ts or int(time.time()), procs, **kw)


def run_row(conn: Any, run_id: int) -> Any:
    return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()


def kinds(conn: Any) -> list[str]:
    return [r["kind"] for r in conn.execute("SELECT kind FROM task_events ORDER BY id")]


def to_review(conn: Any, tid: str) -> None:
    run = store.claim(conn, tid)
    assert run
    store.request_review(conn, run, "PR #7", pr_number=7, pr_url="https://example.invalid/p/7")


def test_d1_the_cap_limits_claims(conn: Any, card: Card, procs: FakeProcs) -> None:
    ids = [card(f"c{i}") for i in range(4)]
    first = tick(conn, procs)
    assert len(first["claimed"]) == 2 and first["slots"] == 2
    assert len(procs.spawned) == 2
    again = tick(conn, procs)
    assert again["claimed"] == [] and again["slots"] == 0
    assert sum(store.get_task(conn, i)["status"] == "running" for i in ids) == 2


def set_card(conn: Any, tid: str, **cols: Any) -> None:
    with store.tx(conn):
        for k, v in cols.items():
            conn.execute(f"UPDATE tasks SET {k} = ? WHERE id = ?", (v, tid))


def set_run(conn: Any, run_id: int, **cols: Any) -> None:
    with store.tx(conn):
        for k, v in cols.items():
            conn.execute(f"UPDATE task_runs SET {k} = ? WHERE id = ?", (v, run_id))


def test_d2_ordering_priority_then_review_then_age(conn: Any, card: Card, procs: FakeProcs) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 1)
    old, newer, high = card("old"), card("newer"), card("high")
    set_card(conn, old, created_at=100)
    set_card(conn, newer, created_at=200)
    set_card(conn, high, created_at=300, priority=5)

    def first() -> list[str]:
        return [c["id"] for c in tick(conn, procs, dry_run=True)["claimed"]]

    assert first() == [high]
    set_card(conn, high, priority=0)
    assert first() == [old]
    rev = card("rev")
    to_review(conn, rev)
    set_card(conn, rev, created_at=900)
    assert first() == [rev], "a review goes before ready cards of equal priority"
    set_card(conn, high, priority=9)
    assert first() == [rev], "one slot is held for the review"


def test_d3_failure_limit_gives_up(conn: Any, card: Card, claim: Callable[[str], int]) -> None:
    tid = card("diagnostic")
    for i in range(2):
        run = claim(tid)
        with store.tx(conn):
            store.end_run(conn, run, "timed_out", "retry", actor="runner", event_kind="timed_out")
        assert store.get_task(conn, tid)["status"] == ("ready" if i == 0 else "blocked")
    t = store.get_task(conn, tid)
    assert t["block_kind"] == "gave_up" and "gave_up" in kinds(conn)


def test_d3_a_ready_card_at_the_limit_is_skipped(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET consecutive_failures = 2 WHERE id = ?", (tid,))
    assert tick(conn, procs)["skipped"] == {tid: "failure_limit"}


def test_a_ready_card_on_an_archived_board_is_skipped(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    tid = card()
    store.board_archive(conn, "main")
    report = tick(conn, procs)
    assert report["skipped"] == {tid: "board_archived"} and report["claimed"] == []
    assert procs.spawned == [] and store.get_task(conn, tid)["status"] == "ready"


def test_d4_quota_walls_hold_and_expire(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()
    cfg_dir = store.effective_config_dir(conn, store.get_board(conn, "main"))
    for field in ("hard_until", "soft_until"):
        with store.tx(conn):
            store.account_set(conn, cfg_dir, **{field: NOW + 100})
        held = tick(conn, procs, NOW)
        assert held["skipped"] == {tid: "quota_wall"} and held["claimed"] == []
        freed = tick(conn, procs, NOW + 200)
        assert [c["id"] for c in freed["claimed"]] == [tid]
        assert store.account_get(conn, cfg_dir)[field] is None
        with store.tx(conn):
            store.end_run(
                conn, run_of(conn, tid), "interrupted", "ready", actor="t", event_kind="interrupted"
            )
    assert kinds(conn).count("account_resumed") == 2


def run_of(conn: Any, tid: str) -> int:
    return int(
        conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()[0]
    )


def test_d5_unblock_starts_a_new_run(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    store.block(conn, claim(tid), "needs_input", "which branch?")
    assert store.get_task(conn, tid)["status"] == "blocked"
    assert tick(conn, procs)["claimed"] == []
    store.unblock(conn, tid, "use main")
    again = tick(conn, procs)
    assert [c["id"] for c in again["claimed"]] == [tid]
    assert (
        conn.execute(
            "SELECT n FROM task_runs WHERE id = ?", (again["claimed"][0]["run_id"],)
        ).fetchone()[0]
        == 2
    )


def test_d6_a_live_runner_with_stale_output_is_untouched(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    run = claim(card())
    set_run(conn, run, runner_pid=111, runner_started=1, last_output_at=1)
    procs.live.add(111)
    assert tick(conn, procs)["reconciled"] == []
    assert run_row(conn, run)["status"] == "running"


def test_d6_a_null_pid_is_ended_only_past_the_grace(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    run = claim(card())
    set_run(conn, run, started_at=store.now() - 5)
    assert tick(conn, procs)["reconciled"] == []
    set_run(conn, run, started_at=store.now() - 60)
    done = tick(conn, procs)["reconciled"]
    assert done == [
        {"run_id": run, "task": run_row(conn, run)["task_id"], "outcome": "interrupted"}
    ]


def test_d7_a_dead_runner_is_crashed_and_its_claude_group_killed(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    set_run(conn, run, runner_pid=222, runner_started=1, claude_pid=333, claude_started=1)
    procs.live.add(333)
    assert tick(conn, procs)["reconciled"][0]["outcome"] == "crashed"
    assert procs.killed == [333]
    t = store.get_task(conn, tid)
    assert t["consecutive_failures"] == 1 and t["status"] == "ready" and t["retry_after"]


def test_d8_a_run_older_than_the_boot_is_interrupted(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    set_run(conn, run, runner_pid=222, runner_started=1)
    procs.boot = store.now() + 10
    assert tick(conn, procs)["reconciled"][0]["outcome"] == "interrupted"
    assert store.get_task(conn, tid)["consecutive_failures"] == 0


def test_d9_a_restarted_dispatcher_adopts_running_runners(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 1)
    first, second = card("a"), card("b")
    claimed = tick(conn, procs)["claimed"]
    run = claimed[0]["run_id"]
    set_run(conn, run, runner_pid=444, runner_started=1)
    fresh = FakeProcs()
    fresh.live.add(444)
    report = tick(conn, fresh)
    assert report["reconciled"] == [] and report["claimed"] == [] and report["slots"] == 0
    assert {store.get_task(conn, first)["status"], store.get_task(conn, second)["status"]} == {
        "running",
        "ready",
    }


def test_d10_a_live_previous_run_blocks_the_card(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    set_run(conn, run, runner_pid=555, runner_started=1)
    with store.tx(conn):
        store.end_run(conn, run, "interrupted", "ready", actor="t", event_kind="interrupted")
    procs.live.add(555)
    assert tick(conn, procs)["skipped"] == {tid: "previous_run_alive"}


def test_d11_a_missing_workspace_is_skipped(
    conn: Any, board: str, env: Path, procs: FakeProcs
) -> None:
    work = env / "repos" / "gone"
    work.mkdir()
    tid = str(
        store.create(conn, board, "d", body="b", assignee="smith", workspace=f"dir:{work}")["id"]
    )
    assert [c["id"] for c in tick(conn, procs, dry_run=True)["claimed"]] == [tid]
    work.rmdir()
    assert tick(conn, procs)["skipped"] == {tid: "workspace_invalid"}


def test_d12_a_paused_account_does_not_hold_another(conn: Any, env: Path, procs: FakeProcs) -> None:
    dirs_ = {slug: env / f"claude-{slug}" for slug in ("a", "b")}
    ids = {}
    for slug, cfg_dir in dirs_.items():
        cfg_dir.mkdir()
        work = env / "repos" / slug
        work.mkdir(parents=True)
        store.board_create(conn, slug, default_workdir=str(work), claude_config_dir=str(cfg_dir))
        ids[slug] = str(store.create(conn, slug, "t", body="b", assignee="smith")["id"])
    with store.tx(conn):
        store.account_set(
            conn, str(dirs_["a"]), auth_paused={"since": int(time.time()), "error": "x"}
        )
    report = tick(conn, procs)
    assert report["skipped"] == {ids["a"]: "account_paused"}
    assert [c["id"] for c in report["claimed"]] == [ids["b"]]


def paused_account(conn: Any, since: int) -> str:
    cfg = str(store.effective_config_dir(conn, store.get_board(conn, "main")))
    with store.tx(conn):
        store.account_set(conn, cfg, auth_paused={"since": since, "error": "Not logged in"})
    return cfg


def test_a_paused_login_is_probed_after_5_15_then_60_minutes_and_lifts_when_it_passes(
    conn: Any, card: Card, procs: FakeProcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = card()
    cfg = paused_account(conn, NOW)
    answers: list[str | None] = []
    monkeypatch.setattr(runner, "auth_probe", lambda _c, _d: answers.pop(0))

    def probes() -> int:
        return store.account_get(conn, cfg)["auth_paused"].get("probes", 0)

    answers[:] = ["login failed"]
    dispatcher.reprobe_logins(conn, NOW + 299)
    assert tick(conn, procs, NOW + 299)["skipped"] == {tid: "account_paused"}
    assert answers and probes() == 0
    dispatcher.reprobe_logins(conn, NOW + 300)
    assert not answers and probes() == 1
    answers[:] = ["login failed"]
    dispatcher.reprobe_logins(conn, NOW + 300 + 899)
    assert answers and probes() == 1
    dispatcher.reprobe_logins(conn, NOW + 300 + 900)
    assert not answers and probes() == 2
    answers[:] = ["login failed"]
    dispatcher.reprobe_logins(conn, NOW + 1200 + 3599)
    assert answers
    dispatcher.reprobe_logins(conn, NOW + 1200 + 3600)
    assert not answers and probes() == 3
    assert store.account_get(conn, cfg)["auth_paused"]["next_probe_at"] == NOW + 1200 + 7200
    answers[:] = [None]
    dispatcher.reprobe_logins(conn, NOW + 1200 + 7200)
    report = tick(conn, procs, NOW + 1200 + 7200)
    assert not store.account_get(conn, cfg).get("auth_paused")
    assert [c["id"] for c in report["claimed"]] == [tid]
    assert "account_resumed" in kinds(conn)


def test_the_tick_never_probes_a_paused_login(
    conn: Any, card: Card, procs: FakeProcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    card()
    paused_account(conn, 1)

    def boom(*_a: Any) -> None:
        raise AssertionError("the tick probed the login")

    monkeypatch.setattr(runner, "auth_probe", boom)
    assert tick(conn, procs, NOW, dry_run=True)["claimed"] == []
    assert tick(conn, procs, NOW)["claimed"] == []


def test_d13_the_tick_never_calls_git_or_gh(
    conn: Any, card: Card, repo: Path, monkeypatch: pytest.MonkeyPatch, procs: FakeProcs
) -> None:
    store.board_create(conn, "w", default_workdir=str(repo))
    store.create(
        conn, "w", "wt", body="b", assignee="smith", workspace=f"worktree:{repo}", branch="feat/y"
    )
    card("scratch")
    card("plain", workspace=f"dir:{repo}")

    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the tick spawned a process")

    for target in (subprocess.run, subprocess.Popen, subprocess.check_output, os.system):
        monkeypatch.setattr(
            target.__module__ and sys.modules[target.__module__], target.__name__, boom
        )
    monkeypatch.setattr(gitops, "run", boom)
    report = tick(conn, procs)
    assert len(report["claimed"]) == 2 and not report["skipped"]


def test_a_paused_dispatcher_claims_nothing(conn: Any, card: Card, procs: FakeProcs) -> None:
    card()
    store.pause(conn, "testing")
    report = tick(conn, procs)
    assert report["paused"] and report["claimed"] == [] and procs.spawned == []


def test_a_dry_run_changes_nothing(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    ready = card()
    dead = claim(card("other"))
    before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    set_run(conn, dead, started_at=store.now() - 99)
    report = tick(conn, procs, dry_run=True)
    assert [c["id"] for c in report["claimed"]] == [ready]
    assert run_row(conn, dead)["status"] == "running" and procs.spawned == []
    assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == before


def test_sto06_a_running_card_without_a_run_goes_back(
    conn: Any, card: Card, procs: FakeProcs
) -> None:
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    tick(conn, procs, dry_run=False)
    assert "repaired" in kinds(conn)
    assert store.get_task(conn, tid)["status"] in ("ready", "running")


def test_never_started_runners_trip_the_systemic_pause(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 5)
    for i in range(3):
        run = claim(card(f"c{i}"))
        set_run(conn, run, started_at=store.now() - 60)
    assert len(tick(conn, procs)["reconciled"]) == 3
    paused = store.state_get(conn, "dispatch_paused")
    assert paused and paused["reason"] == "systemic"
    assert all(
        store.get_task(conn, r["task_id"])["consecutive_failures"] == 0
        for r in conn.execute("SELECT task_id FROM task_runs")
    )


def test_a_spawn_failure_ends_the_run(conn: Any, card: Card, procs: FakeProcs) -> None:
    tid = card()

    def bad(run_id: int, log: Path) -> None:
        raise OSError("no fork")

    procs.spawn = bad  # type: ignore[method-assign]
    report = tick(conn, procs)
    assert (
        report["claimed"] and run_row(conn, report["claimed"][0]["run_id"])["outcome"] == "crashed"
    )
    assert store.get_task(conn, tid)["status"] == "ready"


# --- one failure never stops the tick (DSP-02, DSP-04) ----------------------------------------


class ProbeFails(FakeProcs):
    """`ps` itself fails for the listed pids: a timeout, or output that does not parse."""

    def __init__(self, *failing: int) -> None:
        super().__init__()
        self.failing = set(failing)

    def alive(self, pid: int | None, started: int | None) -> bool:
        if pid in self.failing:
            raise subprocess.TimeoutExpired("ps", 10)
        return super().alive(pid, started)


def test_a_failed_liveness_probe_skips_the_run_and_never_kills_it(
    conn: Any, card: Card, claim: Callable[[str], int]
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 3)
    unsure, gone = claim(card("unsure")), claim(card("gone"))
    fresh = card("fresh")
    set_run(conn, unsure, runner_pid=111, runner_started=1, claude_pid=112, claude_started=1)
    set_run(conn, gone, started_at=store.now() - 90)
    probes = ProbeFails(111)
    report = tick(conn, probes)
    assert run_row(conn, unsure)["status"] == "running" and probes.killed == []
    assert [r["run_id"] for r in report["reconciled"]] == [gone]
    claimed = {c["id"] for c in report["claimed"]}
    assert fresh in claimed and run_row(conn, unsure)["task_id"] not in claimed, "the tick went on"


def test_a_card_whose_probe_fails_is_skipped_and_the_others_dispatch(
    conn: Any, card: Card, claim: Callable[[str], int]
) -> None:
    held = card("held")
    held_run = claim(held)
    set_run(conn, held_run, runner_pid=777, runner_started=1)
    with store.tx(conn):
        store.end_run(conn, held_run, "interrupted", "ready", actor="t", event_kind="interrupted")
    fine = card("fine")
    report = tick(conn, ProbeFails(777))
    assert report["skipped"] == {held: "error"}
    assert [c["id"] for c in report["claimed"]] == [fine]


def test_a_claim_that_raises_does_not_stop_the_others(
    conn: Any, card: Card, procs: FakeProcs, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursed, fine = card("cursed"), card("fine")
    real_claim = store.claim

    def claim_fails(c: Any, task_id: str, **kw: Any) -> int | None:
        if task_id == cursed:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(c, task_id, **kw)

    monkeypatch.setattr(store, "claim", claim_fails)
    assert [c["id"] for c in tick(conn, procs)["claimed"]] == [fine]


@pytest.mark.parametrize("error", [sqlite3.IntegrityError("UNIQUE"), Refused("changed")])
def test_a_claim_that_loses_the_race_says_so_in_the_log(
    conn: Any, card: Card, error: Exception, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    tid = card()

    def lose(*_a: Any, **_k: Any) -> None:
        raise error

    monkeypatch.setattr(store, "_cas", lose)
    assert store.claim(conn, tid) is None
    assert tid in caplog.text and "not claimed" in caplog.text


def test_a_run_whose_card_is_not_running_is_ended_without_the_card(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tid = card()
    run = claim(tid)
    set_card(conn, tid, status="blocked")
    set_run(conn, run, started_at=store.now() - 90)
    assert tick(conn, procs)["reconciled"][0]["run_id"] == run
    assert (
        run_row(conn, run)["status"] == "ended" and run_row(conn, run)["outcome"] == "interrupted"
    )
    assert store.get_task(conn, tid)["status"] == "blocked"
    assert tick(conn, procs)["slots"] == 2, "the run no longer holds a slot"


def test_the_boot_time_is_read_only_when_a_dead_runner_needs_it(
    conn: Any, card: Card, claim: Callable[[str], int], procs: FakeProcs
) -> None:
    tick(conn, procs)
    assert procs.boot_calls == 0
    run = claim(card())
    set_run(conn, run, runner_pid=5, runner_started=1)
    tick(conn, procs)
    assert procs.boot_calls == 1


# --- the owner verbs ------------------------------------------------------------------------


def test_pause_resume_and_dry_run_verbs(kb: Kb, card: Card) -> None:
    tid = card()
    assert kb("pause", "--reason", "maintenance")[0] == 0
    code, out, _ = kb("dispatch", "--dry-run", "--json")
    assert code == 0 and json.loads(out)["paused"] is True
    assert kb("resume")[0] == 0
    code, out, _ = kb("dispatch", "--dry-run", "--json")
    assert [c["id"] for c in json.loads(out)["claimed"]] == [tid]
    assert (Path(os.environ["KANBAN_HOME"]) / "nudge").exists()


def test_resume_clears_only_accounts_whose_login_passes(
    kb: Kb, conn: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("good", "bad"):
        with store.tx(conn):
            store.account_set(conn, name, auth_paused={"since": 1, "error": "x"})
    monkeypatch.setattr(
        runner, "auth_probe", lambda _conn, cfg: None if cfg == "good" else "probe failed: boom"
    )
    code, out, _ = kb("resume", "--json")
    data = json.loads(out)
    assert code == 0 and data["accounts_cleared"] == ["good"]
    assert data["accounts_failing"] == {"bad": "probe failed: boom"}
    assert not store.account_get(conn, "good").get("auth_paused")
    assert store.account_get(conn, "bad").get("auth_paused")
    text = kb("resume")[1]
    assert "bad: probe failed: boom" in text and "login still failing" not in text


def test_snapshot_verb_prints_a_path(kb: Kb, origin: Path, repo: Path) -> None:
    code, out, _ = kb("snapshot", str(repo))
    assert code == 0 and Path(out.strip()).is_dir()
    assert (Path(out.strip()) / "a.txt").exists()


def wall_events(conn: Any) -> list[tuple[Any, Any, dict[str, Any]]]:
    return [
        (r["board"], r["task_id"], json.loads(r["payload"]))
        for r in conn.execute("SELECT * FROM task_events WHERE kind = 'quota_wall' ORDER BY id")
    ]


def test_a_quota_wall_is_announced_once_per_wall(conn: Any, card: Card, procs: FakeProcs) -> None:
    card("one"), card("two")
    cfg_dir = store.effective_config_dir(conn, store.get_board(conn, "main"))
    with store.tx(conn):
        store.account_set(conn, cfg_dir, hard_until=NOW + 100)
    tick(conn, procs, NOW, dry_run=True)
    assert wall_events(conn) == [], "a dry run records nothing"
    for later in (0, 10):
        tick(conn, procs, NOW + later)
    card("three")
    tick(conn, procs, NOW + 20)
    assert wall_events(conn) == [(None, None, {"until": NOW + 100, "kind": "hard", "cards": 2})]
    with store.tx(conn):
        store.account_set(conn, cfg_dir, soft_until=NOW + 500)
    for later in (30, 40):
        tick(conn, procs, NOW + later)
    assert [p for *_, p in wall_events(conn)][1:] == [
        {"until": NOW + 500, "kind": "soft", "cards": 3}
    ]


def test_each_account_gets_its_own_wall_event_and_an_idle_wall_is_silent(
    conn: Any, card: Card, procs: FakeProcs, tmp_path: Path
) -> None:
    store.board_create(conn, "other", default_workdir=str(tmp_path), claude_config_dir="acct-b")
    cfg_dir = store.effective_config_dir(conn, store.get_board(conn, "main"))
    with store.tx(conn):
        store.account_set(conn, "acct-b", hard_until=NOW + 300)
    tick(conn, procs, NOW)
    assert wall_events(conn) == [], "nothing waits on the wall"
    store.create(conn, "other", "elsewhere", body="do it", assignee="smith")
    with store.tx(conn):
        store.account_set(conn, cfg_dir, hard_until=NOW + 100)
    card("here")
    tick(conn, procs, NOW)
    assert [p for *_, p in wall_events(conn)] == [
        {"until": NOW + 300, "kind": "hard", "cards": 1},
        {"until": NOW + 100, "kind": "hard", "cards": 1},
    ]


def test_ignore_merge_card_is_dispatched(conn: Any, card: Card, procs: FakeProcs) -> None:
    parent, child = card("p"), card("c")
    store.link(conn, parent, child)
    store.complete(conn, store.claim(conn, parent) or 0, "ok")
    set_card(conn, parent, merge_state="pending")
    store.promote(conn, child, "stuck merge", ignore_merge=True)
    assert [c["id"] for c in tick(conn, procs)["claimed"]] == [child]


@pytest.fixture
def worktree_cards(conn: Any, repo: Path) -> Callable[..., str]:
    store.board_create(conn, "wt", default_workdir=str(repo))

    def make(branch: str, touches: list[str] | None, workdir: Path = repo) -> str:
        got = store.create(
            conn, "wt", branch, body="b", assignee="smith", workspace=f"worktree:{workdir}",
            branch=branch, touches=touches,
        )  # fmt: skip
        return str(got["id"])

    return make


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        (["docs/**"], ["docs/guide.md"], True),
        (["docs"], ["docs/guide.md"], True),
        (["README.md"], ["README.md"], True),
        (["*.md"], ["docs/guide.md"], True),
        (["docs/**"], ["src/**"], False),
        (["src/a.py"], ["src/b.py"], False),
        (["docs/a.md", "src/**"], ["tests/x.py", "src/util.py"], True),
    ],
)
def test_globs_overlap(a: list[str], b: list[str], overlap: bool) -> None:
    assert dispatcher.touches_overlap(a, b) is overlap


def test_overlapping_touches_wait_for_the_running_card(
    conn: Any, worktree_cards: Callable[..., str], procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 4)
    first = worktree_cards("feat/a", ["docs/**"])
    assert store.claim(conn, first)
    second = worktree_cards("feat/b", ["docs/guide.md"])
    free = worktree_cards("feat/c", ["src/**"])
    unknown = worktree_cards("feat/d", None)
    report = tick(conn, procs, memory={})
    assert report["skipped"] == {second: f"overlaps:{first}"}
    assert sorted(c["id"] for c in report["claimed"]) == sorted([free, unknown])
    ev = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'skipped'", (second,)
    ).fetchone()
    assert json.loads(ev["payload"]) == {"reason": f"overlaps:{first}"}


def test_overlapping_cards_ready_together_start_one_at_a_time(
    conn: Any, worktree_cards: Callable[..., str], procs: FakeProcs
) -> None:
    store.config_set(conn, "dispatch.max_in_progress", 4)
    a = worktree_cards("feat/a", ["docs/**"])
    b = worktree_cards("feat/b", ["docs/x.md"])
    report = tick(conn, procs)
    assert [c["id"] for c in report["claimed"]] == [a]
    assert report["skipped"] == {b: f"overlaps:{a}"}


def test_touches_in_another_repo_do_not_overlap(
    conn: Any, worktree_cards: Callable[..., str], procs: FakeProcs, repo: Path, env: Path
) -> None:
    other = env / "repos" / "other"
    other.mkdir()
    git("init", "-b", "main", cwd=other)
    git("remote", "add", "origin", "https://example.invalid/acme/other.git", cwd=other)
    a = worktree_cards("feat/a", ["docs/**"])
    assert store.claim(conn, a)
    b = worktree_cards("feat/b", ["docs/**"], other)
    assert [c["id"] for c in tick(conn, procs)["claimed"]] == [b]
