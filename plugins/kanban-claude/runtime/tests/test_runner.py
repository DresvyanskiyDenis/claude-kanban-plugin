"""TST-R: the runner (RUN), through a real `kanban run` process against the fake `claude`."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import runner, store
from kanban_claude.gitops import ProvisionError
from kanban_claude.paths import dirs
from kanban_claude.procs import start_time

from .conftest import Execute, Go

Card = Callable[..., str]
Claim = Callable[[str], int]


def dumped(env: Path, run_id: int) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((env / "dump" / f"{run_id}.json").read_text())
    return data


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


@pytest.fixture
def dump(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (env / "dump").mkdir()
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(env / "dump"))
    return env / "dump"


def dead(pid: int, wait: float = 5) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_r1_the_argv_is_exactly_the_contract(
    conn: Any, card: Card, execute: Execute, env: Path, dump: Path
) -> None:
    tid = card()
    store.grant_add(conn, tid, ["Bash(npm publish)"], "owner, chat, today")
    run = execute(tid, terminal=["complete", tid, "--summary", "ok"])
    seen = dumped(env, run["id"])
    argv = seen["argv"]
    assert argv[0] == "-p" and "kanban context" in argv[1] and "npm publish" not in argv[1]
    assert flag(argv, "--agent") == "kanban-claude:smith"
    assert (flag(argv, "--model"), flag(argv, "--effort")) == ("sonnet", "xhigh")
    assert (flag(argv, "--permission-mode"), flag(argv, "--permission-prompts")) == ("auto", "none")
    assert flag(argv, "--output-format") == "stream-json" and "--verbose" in argv
    assert "--no-chrome" in argv and flag(argv, "--max-budget-usd") == "30"
    assert flag(argv, "--name") == f"kanban {tid} smith"
    assert flag(argv, "--session-id") == runner.session_id("main", tid, "smith")
    for banned in ("--bare", "--plugin-dir", "--setting-sources", "-w"):
        assert banned not in argv
    assert "--strict-mcp-config" in argv and "--max-turns" not in argv
    assert flag(argv, "--mcp-config").endswith("run-1.mcp.json")
    settings = json.loads(flag(argv, "--settings"))
    deny = settings["permissions"]["deny"]
    assert deny[:5] == [
        "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(gh pr merge:*)",
        "Bash(kanban grant:*)", f"Edit(/{dirs().home}/**)",
    ]  # fmt: skip
    assert "Agent(kanban-claude:warden)" in deny and "Agent(kanban-claude:card-helper)" not in deny
    assert settings["permissions"]["allow"] == ["Bash(npm publish)"]
    assert seen["env"]["TMPDIR"] == str(dirs().work / "tmp" / tid)
    assert (dirs().work / "tmp" / tid).is_dir()
    assert run["last_output_at"] is None
    assert settings["enabledPlugins"] == {"cc-plugin-you-should-know@builtin": False}
    assert seen["cwd"] == str(dirs().work / "scratch" / tid) or seen["cwd"].endswith(tid)


def test_r1_the_mcp_config_is_strict_and_always_passed_and_the_rest_follows(
    tmp_path: Path,
) -> None:
    def argv(turns: int | None, resumed: bool) -> list[str]:
        return runner.build_argv(
            "claude", "p", "scout", "t_1", "m", "e", "{}", 1.5, turns, "sid", resumed, "/m.json"
        )

    first, again = argv(7, True), argv(None, False)
    assert "--strict-mcp-config" in first and flag(first, "--mcp-config") == "/m.json"
    assert flag(first, "--max-turns") == "7" and flag(first, "--resume") == "sid"
    assert "--max-turns" not in again and flag(again, "--session-id") == "sid"
    assert flag(again, "--max-budget-usd") == "1.5"


def user_config(config_dir: Path, plugins: dict[str, bool]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({"enabledPlugins": plugins}))


def test_p21_every_enabled_user_plugin_is_off_but_ours_its_dependencies_and_the_allowlist(
    conn: Any, tmp_path: Path
) -> None:
    alt = tmp_path / "alt"
    user_config(alt, {
        "ponytail@ponytail": True, "watch@claude-video": True, "off@m": False,
        "kanban-claude@claude-kanban-plugin": True,
        "frontend-design@claude-plugins-official": True,
        "context-bar@claude-code-mods": True,
    })  # fmt: skip
    store.config_set(conn, "worker.plugin_allowlist", ["watch", "context-bar@claude-code-mods"])
    assert runner.plugins_off(conn, str(alt)) == {
        "ponytail@ponytail": False, "cc-plugin-you-should-know@builtin": False,
    }  # fmt: skip
    store.config_set(conn, "worker.plugin_allowlist", [])
    assert set(runner.plugins_off(conn, str(alt))) == {
        "ponytail@ponytail", "watch@claude-video", "context-bar@claude-code-mods",
        "cc-plugin-you-should-know@builtin",
    }  # fmt: skip
    assert runner.plugins_off(conn, str(tmp_path / "no-such-dir")) == {
        "cc-plugin-you-should-know@builtin": False
    }


def test_p21_the_dependencies_kept_on_are_the_ones_the_manifest_declares() -> None:
    manifest = Path(runner.__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
    declared = json.loads(manifest.read_text()).get("dependencies", [])
    assert {d.partition("@")[0] for d in declared} == set(runner.OWN_DEPENDENCIES)


def test_p21_a_run_disables_the_plugins_of_the_accounts_own_settings(
    conn: Any, card: Card, execute: Execute, env: Path, dump: Path
) -> None:
    user_config(Path.home() / ".claude", {"ponytail@ponytail": True})
    run = execute(card())
    settings = json.loads(flag(dumped(env, run["id"])["argv"], "--settings"))
    assert settings["enabledPlugins"] == {
        "ponytail@ponytail": False, "cc-plugin-you-should-know@builtin": False,
    }  # fmt: skip


def test_d1_the_deny_list_leaves_each_role_only_its_helper_and_what_its_job_needs() -> None:
    def deny(role: str) -> list[str]:
        out: list[str] = json.loads(runner.settings_json(Path("/h"), [], role))["permissions"][
            "deny"
        ]
        return out

    for role in ("smith", "designer", "scout"):
        assert "Agent(kanban-claude:card-helper)" not in deny(role)
        assert {"Agent(Explore)", "Agent(general-purpose)", "Agent(kanban-claude:warden)"} <= {
            *deny(role)
        }
    assert "Agent(kanban-claude:review-helper)" in deny("smith")
    assert "Agent(kanban-claude:review-helper)" not in deny("warden")
    assert "Agent(kanban-claude:card-helper)" in deny("warden")
    for role in ("warden", "atlassian"):
        assert {"Edit", "Write"} <= {*deny(role)}
    assert "Edit" in deny("scout") and "Write" not in deny("scout")
    assert "Edit" not in deny("smith") and "Write" not in deny("smith")
    assert not any(d.startswith("Agent(") for d in deny("atlassian"))
    assert deny("") == deny("card-checker")[:5] == deny("smith")[:5]


def write_mcp(path: Path, servers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


def test_p21_only_the_allowlisted_user_servers_and_the_roles_own_reach_the_worker(
    conn: Any, tmp_path: Path
) -> None:
    write_mcp(Path.home() / ".claude.json", {
        "context7": {"command": "npx"}, "memory-mcp": {"url": "https://vault.invalid"},
        "lightpanda": {"command": "lp"}, "atlassian": {"command": "uvx"},
    })  # fmt: skip
    ws = tmp_path / "ws"
    default = str(Path.home() / ".claude")
    got = runner.mcp_config(conn, "smith", ws, default)
    assert got == {"mcpServers": {"context7": {"command": "npx"}, "lightpanda": {"command": "lp"}}}
    assert set(runner.mcp_config(conn, "atlassian", ws, default)["mcpServers"]) == {
        "context7", "lightpanda", "atlassian",
    }  # fmt: skip
    store.config_set(conn, "worker.mcp_allowlist", [])
    assert runner.mcp_config(conn, "smith", ws, default) == {"mcpServers": {}}
    assert set(runner.mcp_config(conn, "designer", ws, default)["mcpServers"]) == {"playwright"}


def test_p21_a_server_comes_from_the_config_dirs_own_claude_json_and_the_repo_fills_the_gaps(
    conn: Any, tmp_path: Path
) -> None:
    alt = tmp_path / "alt"
    write_mcp(alt / ".claude.json", {"context7": {"command": "alt-npx"}})
    write_mcp(Path.home() / ".claude.json", {"lightpanda": {"command": "home-lp"}})
    ws = tmp_path / "ws"
    write_mcp(ws / ".mcp.json", {
        "code-review-graph": {"command": "crg"}, "context7": {"command": "repo-planted"},
        "rogue": {"command": "x"},
    })  # fmt: skip
    got = runner.mcp_config(conn, "smith", ws, str(alt))["mcpServers"]
    assert got == {"context7": {"command": "alt-npx"}, "code-review-graph": {"command": "crg"}}
    (ws / ".mcp.json").write_text("not json")
    assert runner.mcp_config(conn, "smith", ws, str(alt))["mcpServers"] == {
        "context7": {"command": "alt-npx"}
    }


def test_p21_the_mcp_file_is_private_while_claude_runs_and_gone_after(
    conn: Any, card: Card, execute: Execute, env: Path, fake_claude: Path
) -> None:
    write_mcp(Path.home() / ".claude.json", {"context7": {"env": {"KEY": "s3cret"}}})
    spy = env / "bin" / "claude-spy"
    spy.write_text(
        f"#!{sys.executable}\nimport os, sys\n"
        "f = sys.argv[sys.argv.index('--mcp-config') + 1]\n"
        f"open({str(env / 'seen')!r}, 'w').write(f'{{f}}\\n{{os.stat(f).st_mode & 0o777:o}}\\n' + open(f).read())\n"
        f"os.execv({str(fake_claude)!r}, [{str(fake_claude)!r}, *sys.argv[1:]])\n"
    )
    spy.chmod(0o755)
    store.config_set(conn, "claude.bin", str(spy))
    execute(card())
    path, mode, body = (env / "seen").read_text().split("\n", 2)
    assert mode == "600" and json.loads(body)["mcpServers"]["context7"]["env"]["KEY"] == "s3cret"
    assert not Path(path).exists()


def test_r1_review_runs_always_use_the_review_model(conn: Any, card: Card, claim: Claim) -> None:
    tid = card("r", model="haiku", effort="low")
    run = claim(tid)
    t = store.get_task(conn, tid)
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert runner.model_effort(conn, t, row) == ("haiku", "low")
    store.request_review(conn, run, "PR #7", pr_number=7, pr_url="https://example.invalid/p/7")
    review = claim(tid)
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (review,)).fetchone()
    assert row["kind"] == "review"
    assert runner.model_effort(conn, store.get_task(conn, tid), row) == ("opus", "high")


def test_r2_only_the_billing_switches_and_the_claude_markers_are_dropped(
    conn: Any, card: Card, execute: Execute, env: Path, dump: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.config_set(conn, "worker.env_passthrough", ["ANTHROPIC_BASE_URL"])
    for k, v in {
        "ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDECODE": "1",
        "ANTHROPIC_BASE_URL": "https://proxy.invalid", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "SSH_AUTH_SOCK": "/agent.sock", "GITHUB_TOKEN": "gh", "HARMLESS": "yes",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    tid = card()
    run = execute(tid)
    seen = dumped(env, run["id"])["env"]
    for gone in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
    ):
        assert gone not in seen
    assert seen["ANTHROPIC_BASE_URL"] == "https://proxy.invalid"
    assert seen["SSH_AUTH_SOCK"] == "/agent.sock" and seen["GIT_AUTHOR_NAME"] == "t"
    assert seen["GITHUB_TOKEN"] == "gh" and seen["HARMLESS"] == "yes"
    assert seen["KANBAN_RUN_ID"] == str(run["id"]) and seen["KANBAN_TASK"] == tid
    assert seen["KANBAN_BOARD"] == "main" and "CLAUDE_CONFIG_DIR" not in seen
    assert seen["CLAUDE_CODE_WEB_FETCH_AGENT"] == "0"
    assert seen["CLAUDE_SKIP_SOUL"] == "1"
    assert not [k for k in seen if k.startswith("KANBAN_") and re.search("TOKEN|SOCK", k)]


def test_r3_the_session_id_is_stable_and_resumed_from_the_transcript(
    conn: Any, card: Card, execute: Execute, env: Path, dump: Path
) -> None:
    import uuid

    tid = card()
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-kanban:main/{tid}/smith"))
    first = execute(tid)
    assert first["session_id"] == sid and first["resumed"] == 0
    config = Path(first["config_dir"])
    (config / "projects" / "p").mkdir(parents=True)
    (config / "projects" / "p" / f"{sid}.jsonl").write_text("{}\n")
    second = execute(tid)
    argv = dumped(env, second["id"])["argv"]
    assert second["resumed"] == 1 and flag(argv, "--resume") == sid and "--session-id" not in argv
    assert argv[1] != dumped(env, first["id"])["argv"][1]


def test_r4_the_runtime_cap_kills_the_whole_group_including_a_grandchild(
    conn: Any, card: Card, execute: Execute, env: Path
) -> None:
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds = 1 WHERE id = ?", (tid,))
    pidfile = env / "grandchild"
    started = time.monotonic()
    run = execute(tid, FAKE_CLAUDE_SLEEP="300", FAKE_CLAUDE_GRANDCHILD=str(pidfile))
    assert time.monotonic() - started < 60
    assert run["outcome"] == "timed_out" and store.get_task(conn, tid)["consecutive_failures"] == 1
    assert dead(int(pidfile.read_text()))


def test_r4_a_grandchild_that_outlives_claude_is_killed_with_the_group(
    conn: Any, card: Card, execute: Execute, env: Path
) -> None:
    tid = card()
    pidfile = env / "grandchild"
    run = execute(
        tid, terminal=["complete", tid, "--summary", "ok"], FAKE_CLAUDE_GRANDCHILD=str(pidfile)
    )
    assert run["outcome"] == "completed"
    assert dead(int(pidfile.read_text()))


def registered(conn: Any, claim_run: int) -> runner.Runner:
    r = runner.Runner(conn, claim_run)
    r.config_dir = r.row()["config_dir"]
    assert r.register(r.row(), r.task())
    return r


def test_r5_the_cap_follows_the_monotonic_clock(
    conn: Any,
    card: Card,
    claim: Claim,
    fake_claude: Path,
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall clock set a generation back must neither fire the cap early nor stop it firing."""
    tid = card()
    with store.tx(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds = 2 WHERE id = ?", (tid,))
    r = registered(conn, claim(tid))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 1e9)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "20")
    started = time.monotonic()
    assert r.launch(r.task(), env) == 0
    assert r.timed_out and 1.5 < time.monotonic() - started < 15


def test_the_ps_fork_never_runs_inside_the_write_transaction(
    conn: Any,
    card: Card,
    claim: Claim,
    fake_claude: Path,
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inside: list[bool] = []
    real = runner.start_time

    def watched(pid: int) -> int | None:
        inside.append(conn.in_transaction)
        return real(pid)

    monkeypatch.setattr(runner, "start_time", watched)
    r = registered(conn, claim(card()))
    assert r.launch(r.task(), env) == 0
    assert inside == [False, False]


def test_r6_the_terminal_grace_reaps_a_claude_that_lingers(
    conn: Any, card: Card, execute: Execute
) -> None:
    store.config_set(conn, "worker.terminal_grace_s", 1)
    tid = card()
    started = time.monotonic()
    run = execute(tid, terminal=["complete", tid, "--summary", "ok"], FAKE_CLAUDE_SLEEP="300")
    assert time.monotonic() - started < 60
    assert run["outcome"] == "completed" and store.get_task(conn, tid)["status"] == "done"


def test_r7_the_run_line_is_posted_as_a_comment(conn: Any, card: Card, execute: Execute) -> None:
    tid = card()
    execute(tid, terminal=["complete", tid, "--summary", "ok"])
    lines = [
        r["body"] for r in conn.execute("SELECT body FROM task_comments WHERE author = 'kanban'")
    ]
    assert len(lines) == 1
    assert re.fullmatch(
        r"\[run\] smith · claude-sonnet-x/xhigh · completed · exit 0 · 3 turns · \d+s · \$0\.12 · [0-9a-f]{8}",
        lines[0],
    ), lines[0]


def test_the_log_says_when_the_worker_already_ended_the_run(
    card: Card, claim: Claim, go: Go
) -> None:
    tid = card()
    p = go(
        claim(tid),
        FAKE_CLAUDE_FIXTURE="success",
        FAKE_CLAUDE_TERMINAL=json.dumps(["complete", tid, "--summary", "ok"]),
    )
    assert "run already ended" in p.stdout and "classified=" not in p.stdout


def start_runner(conn: Any, run: int) -> subprocess.Popen[str]:
    """`kanban run` as a child process, returned once its claude has a pid."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "kanban_claude", "run", str(run)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if conn.execute("SELECT claude_pid FROM task_runs WHERE id = ?", (run,)).fetchone()[0]:
            break
        time.sleep(0.1)
    return proc


def test_r8_a_database_only_stop_is_enough_for_the_runner_to_take_the_group_down(
    conn: Any, card: Card, claim: Claim, go: Go, env: Path, kb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = card()
    run = claim(tid)
    pidfile = env / "grandchild"
    for k, v in {
        "FAKE_CLAUDE_SLEEP": "300", "FAKE_CLAUDE_GRANDCHILD": str(pidfile),
        "FAKE_CLAUDE_FIXTURE": "success",
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    proc = start_runner(conn, run)
    end = time.monotonic() + 30
    while time.monotonic() < end and not (pidfile.exists() and pidfile.read_text()):
        time.sleep(0.1)
    assert pidfile.exists()
    stopped = time.monotonic()
    assert kb("stop", tid)[0] == 0
    assert proc.wait(timeout=60) == 0
    assert time.monotonic() - stopped < runner.QUIET_POLL_S + 15
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert row["outcome"] == "terminated"
    t = store.get_task(conn, tid)
    assert t["status"] == "blocked" and t["block_kind"] == "stopped"
    assert dead(int(pidfile.read_text()))


def test_stop_signals_no_process_it_only_ends_the_run_in_the_database(
    conn: Any, card: Card, claim: Claim, kb: Any
) -> None:
    tid = card()
    run = claim(tid)
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        conn.execute(
            "UPDATE task_runs SET runner_pid = ?, runner_started = ? WHERE id = ?",
            (sleeper.pid, start_time(sleeper.pid), run),
        )
        assert kb("stop", tid)[0] == 0
        time.sleep(0.5)
        assert sleeper.poll() is None
    finally:
        sleeper.kill()
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert row["outcome"] == "terminated"


def test_a_runner_signalled_by_something_else_is_interrupted_not_a_crash(
    conn: Any, card: Card, claim: Claim, fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = card()
    run = claim(tid)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "300")
    proc = start_runner(conn, run)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=60) == 0
    assert (
        conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run,)).fetchone()[0]
        == "interrupted"
    )
    t = store.get_task(conn, tid)
    assert t["status"] == "ready" and t["consecutive_failures"] == 0


def test_a_missing_workspace_is_a_card_fault(
    conn: Any, card: Card, env: Path, execute: Execute
) -> None:
    work = env / "repos" / "gone"
    work.mkdir()
    tid = card("d", workspace=f"dir:{work}")
    work.rmdir()
    run = execute(tid)
    assert run["outcome"] == "provision_failed" and "not a directory" in run["error"]
    assert store.get_task(conn, tid)["consecutive_failures"] == 1


def test_an_unreachable_origin_is_an_infrastructure_fault_with_a_cooldown(
    conn: Any, repo: Path, execute: Execute
) -> None:
    store.board_create(conn, "w", default_workdir=str(repo))
    tid = str(
        store.create(
            conn,
            "w",
            "wt",
            body="b",
            assignee="smith",
            workspace=f"worktree:{repo}",
            branch="feat/z",
        )["id"]
    )  # the fixture's origin URL is a placeholder that cannot be fetched
    run = execute(tid)
    t = store.get_task(conn, tid)
    assert run["outcome"] == "provision_failed" and json.loads(run["metadata"])["infrastructure"]
    assert t["consecutive_failures"] == 0 and t["retry_after"] >= store.now() + 290


def test_a_worktree_card_runs_in_its_worktree_and_an_unpushed_one_is_kept(
    conn: Any, origin: Path, repo: Path, execute: Execute, env: Path, dump: Path
) -> None:
    store.board_create(conn, "w", default_workdir=str(repo))
    tid = str(
        store.create(
            conn, "w", "wt", body="b", assignee="scout", workspace=f"worktree:{repo}", branch="feat/z"
        )["id"]
    )  # fmt: skip
    run = execute(tid, terminal=["complete", tid, "--summary", "ok"])
    seen = dumped(env, run["id"])
    assert Path(seen["cwd"]).resolve() == (repo / ".worktrees" / tid).resolve()
    assert run["outcome"] == "completed"
    assert (repo / ".worktrees" / tid).is_dir()
    assert store.get_task(conn, tid)["worktree_state"] == "kept_unpushed"
    assert conn.execute("SELECT 1 FROM task_events WHERE kind = 'worktree_kept'").fetchone()


def test_a_quota_wall_set_after_claim_stops_the_runner_before_it_starts(
    conn: Any, card: Card, claim: Claim, go: Go
) -> None:
    tid = card()
    run = claim(tid)
    cfg = conn.execute("SELECT config_dir FROM task_runs WHERE id = ?", (run,)).fetchone()[0]
    with store.tx(conn):
        store.account_set(conn, cfg, hard_until=store.now() + 500)
    assert go(run).returncode == 0
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert row["outcome"] == "quota" and row["claude_pid"] is None


def test_the_run_files_are_the_documented_paths(conn: Any, card: Card, execute: Execute) -> None:
    tid = card()
    run = execute(tid)
    home = dirs().home
    assert run["log_path"] == str(home / "runs" / "main" / tid / "run-1.log")
    assert run["stream_path"] == str(home / "streams" / "main" / tid / "run-1.jsonl")
    assert oct(Path(run["stream_path"]).stat().st_mode & 0o777) == "0o600"


def test_a_crash_inside_the_runner_is_recorded(
    conn: Any, card: Card, claim: Claim, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = claim(card())
    r = runner.Runner(conn, run)

    def boom(_t: Any) -> Path:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(r, "provision", boom)
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)
    monkeypatch.setattr(runner, "Runner", lambda c, i: r)
    assert runner.run(run) == 1
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert row["outcome"] == "crashed" and "disk on fire" in row["error"]


def test_a_non_default_config_dir_is_exported_and_the_default_is_left_to_claude(
    tmp_path: Path,
) -> None:
    inherited = {"CLAUDE_CONFIG_DIR": "/elsewhere", "PATH": "/bin"}
    assert runner.with_config_dir(inherited, str(Path.home() / ".claude")) == {"PATH": "/bin"}
    custom = runner.with_config_dir(inherited, str(tmp_path / "alt"))
    assert custom == {"CLAUDE_CONFIG_DIR": str(tmp_path / "alt"), "PATH": "/bin"}


def test_the_default_config_dir_is_recognised_however_it_is_spelled(tmp_path: Path) -> None:
    home = Path.home()
    (home / ".claude").mkdir()
    (tmp_path / "alias").symlink_to(home / ".claude")
    for spelled in ("~/.claude", f"{home}/.claude/", f"{home}/./.claude", str(tmp_path / "alias")):
        assert runner.with_config_dir({"CLAUDE_CONFIG_DIR": "/x"}, spelled) == {}, spelled


# --- the login probe ------------------------------------------------------------------------


@pytest.fixture
def probe_claude(conn: Any, env: Path) -> Callable[[str], None]:
    """A `claude` that prints `body` (a shell snippet's stdout) for the probe's one call."""

    def install(body: str) -> None:
        path = env / "bin" / "probe-claude"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        store.config_set(conn, "claude.bin", str(path))

    return install


def test_the_auth_probe_names_what_failed(
    conn: Any, probe_claude: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_second_connection(*_: Any, **__: Any) -> None:
        raise AssertionError("the probe opens no connection of its own")

    monkeypatch.setattr(store, "connect", no_second_connection)
    probe_claude("""echo '{"result": "ok"}'""")
    assert runner.auth_probe(conn, str(Path.home() / ".claude")) is None
    probe_claude("""echo '{"result": "Invalid API key", "api_error_status": 401}'""")
    reason = runner.auth_probe(conn, "/somewhere/alt")
    assert reason and reason.startswith("login failed") and "Invalid API key" in reason
    assert "CLAUDE_CONFIG_DIR=/somewhere/alt claude /login" in reason
    probe_claude("echo 'not json'")
    assert str(runner.auth_probe(conn, "/somewhere/alt")).startswith("probe failed: ")
    store.config_set(conn, "claude.bin", "/nonexistent/claude")
    assert str(runner.auth_probe(conn, "/somewhere/alt")).startswith("probe failed: ")


# --- an early stop --------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["signalled", "ended"])
def test_a_stop_during_provisioning_ends_the_run_before_claude_starts(
    conn: Any, card: Card, claim: Claim, monkeypatch: pytest.MonkeyPatch, env: Path, how: str
) -> None:
    tid = card()
    run = claim(tid)
    r = runner.Runner(conn, run)

    def provision(_t: Any) -> Path:
        if how == "signalled":
            r.on_signal(signal.SIGTERM, None)
        else:
            with store.tx(conn):
                conn.execute(
                    "UPDATE task_runs SET status = 'ended', outcome = 'stopped', ended_at = ? WHERE id = ?",
                    (store.now(), run),
                )
        return env

    def launch(*_: Any) -> int:
        raise AssertionError("claude must not start after a stop")

    monkeypatch.setattr(r, "provision", provision)
    monkeypatch.setattr(r, "launch", launch)
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)
    assert r.go() == 0
    row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
    assert row["status"] == "ended" and row["claude_pid"] is None
    assert row["outcome"] == ("interrupted" if how == "signalled" else "stopped")
    line = conn.execute("SELECT body FROM task_comments WHERE task_id = ?", (tid,)).fetchone()[0]
    assert line.startswith("[run]") and row["outcome"] in line


# --- infrastructure failures ----------------------------------------------------------------


def test_infrastructure_failures_stop_being_free_after_a_few_in_a_row(
    conn: Any, card: Card, claim: Claim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad credentials or a missing repository look like infrastructure; they must end in a block."""
    tid = card()

    def provision(_self: Any, _t: Any) -> Path:
        raise ProvisionError("git fetch origin failed: Authentication failed", infra=True)

    monkeypatch.setattr(runner.Runner, "provision", provision)
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)
    limit = store.cfg(conn, "dispatch.failure_limit")
    for attempt in range(1, 20):
        assert runner.Runner(conn, claim(tid)).go() == 0
        t = store.get_task(conn, tid)
        if t["status"] != "ready":
            break
        assert (attempt > runner.INFRA_RETRIES) == bool(t["consecutive_failures"])
    assert t["status"] == "blocked" and t["block_kind"] == "gave_up"
    assert attempt == runner.INFRA_RETRIES + limit


# --- per-card TMPDIR, GH_HOST, effort, prompts ------------------------------------------------


def git_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True)
    for args in (["init"], ["remote", "add", "origin", origin]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    return path


def env_for(conn: Any, tid: str, workspace: Path) -> dict[str, str]:
    return runner.worker_env(conn, store.get_task(conn, tid), 1, workspace, "/cfg")


def test_two_cards_never_share_a_tmpdir(conn: Any, card: Card, tmp_path: Path) -> None:
    one, two = card("one"), card("two")
    a, b = env_for(conn, one, tmp_path), env_for(conn, two, tmp_path)
    assert a["TMPDIR"] != b["TMPDIR"] and a["TMPDIR"] == str(dirs().work / "tmp" / one)


@pytest.mark.parametrize(
    ("origin", "host"),
    [
        ("git@ghe.example.com:acme/proj.git", "ghe.example.com"),
        ("https://ghe.example.com/acme/proj", "ghe.example.com"),
        ("https://github.com/acme/proj.git", None),
        ("/some/local/path.git", None),
    ],
)
def test_gh_host_follows_a_non_github_origin(
    conn: Any, tmp_path: Path, origin: str, host: str | None
) -> None:
    store.board_create(conn, "w", default_workdir=str(tmp_path))
    repo = git_repo(tmp_path / "repo", origin)
    tid = str(
        store.create(conn, "w", "t", body="b", assignee="smith", workspace=f"worktree:{repo}",
                     branch="feat/x")["id"]
    )  # fmt: skip
    assert env_for(conn, tid, repo).get("GH_HOST") == host


def test_an_explicit_gh_host_is_kept(
    conn: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.board_create(conn, "w", default_workdir=str(tmp_path))
    repo = git_repo(tmp_path / "repo", "git@ghe.example.com:acme/proj.git")
    tid = str(
        store.create(conn, "w", "t", body="b", assignee="smith", workspace=f"worktree:{repo}",
                     branch="feat/x")["id"]
    )  # fmt: skip
    monkeypatch.setenv("GH_HOST", "other.example.com")
    assert env_for(conn, tid, repo)["GH_HOST"] == "other.example.com"


def test_effort_is_the_card_then_the_role_then_the_kind_then_the_default(
    conn: Any, card: Card, claim: Claim
) -> None:
    def effort(tid: str, run: int) -> str:
        row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()
        return runner.model_effort(conn, store.get_task(conn, tid), row)[1]

    plain, own = card("plain"), card("own", effort="low")
    run_plain, run_own = claim(plain), claim(own)
    assert effort(plain, run_plain) == "xhigh"
    store.config_set(conn, "effort.smith", "medium")
    assert effort(plain, run_plain) == "medium" and effort(own, run_own) == "low"
    scouting = card("s", assignee="scout")
    assert effort(scouting, claim(scouting)) == "high"
    store.request_review(
        conn, run_plain, "PR #7", pr_number=7, pr_url="https://example.invalid/p/7"
    )
    review = claim(plain)
    assert effort(plain, review) == "high"
    store.config_set(conn, "effort.warden", "max")
    assert effort(plain, review) == "max"


def prompt_for(conn: Any, tid: str, *, resumed: bool, review: bool) -> str:
    return runner.build_prompt(conn, store.get_task(conn, tid), Path("/w"), "/cfg", resumed, review)


@pytest.mark.parametrize("resumed", [False, True])
def test_the_prompt_lists_the_terminal_calls_of_the_run_kind(
    conn: Any, card: Card, resumed: bool
) -> None:
    tid = card()
    work = prompt_for(conn, tid, resumed=resumed, review=False)
    review = prompt_for(conn, tid, resumed=resumed, review=True)
    assert "kanban request-review" in work and "request-changes" not in work
    assert "kanban request-changes" in review and "WARDEN: APPROVED" in review
    assert "--reviewer warden" not in review
    for text in (work, review):
        assert tid in text and "$" not in text.replace("$KANBAN", "")
        assert "kanban wait-ci" in text and "--retry-after <duration>" in text
        assert "--watch" not in text and "sleep 30" not in text and "sleep in" not in text
        assert "does not retry by itself" not in text


def test_the_prompt_does_not_repeat_the_grants(conn: Any, card: Card) -> None:
    tid = card()
    store.grant_add(conn, tid, ["Bash(npm publish)"], "owner, chat, today")
    assert "npm publish" not in prompt_for(conn, tid, resumed=False, review=False)


def test_the_fake_claude_rejects_a_flag_it_does_not_know(tmp_path: Path) -> None:
    fake = Path(__file__).with_name("fake_claude.py")
    for argv, code in (
        (["-p", "x", "--effort", "high", "--verbose"], 0),
        (["-p", "x", "--efort", "high"], 1),
    ):
        p = subprocess.run([sys.executable, str(fake), *argv], capture_output=True, text=True,
                           env={**os.environ, "FAKE_CLAUDE_FIXTURES": str(fake.parent / "fixtures")})  # fmt: skip
        assert p.returncode == code, p.stderr
    assert "--efort" in p.stderr


def test_the_account_paused_event_names_the_card_the_error_and_the_login_command(
    conn: Any, card: Card, execute: Execute
) -> None:
    tid = card()
    execute(tid, "auth401", exit_code=1)
    event = conn.execute("SELECT payload FROM task_events WHERE kind = 'account_paused'").fetchone()
    payload = json.loads(event["payload"])
    assert payload["task"] == tid and payload["error"].startswith("Invalid API key")
    assert payload["login"] == "claude /login" and payload["config_dir"]
