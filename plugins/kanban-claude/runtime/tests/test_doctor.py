"""OBS-05: `kanban doctor` against a set-up install under tmp_path; each test breaks one thing."""

import json
import os
import plistlib
import shutil
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kanban_claude import __version__, install, launchd, paths, store, views

from .conftest import PLUGIN, Kb, enable

Report = dict[str, Any]
Doctor = Callable[..., Report]


@pytest.fixture
def doctor(kb: Kb, healthy: int) -> Doctor:
    def run(*args: str) -> Report:
        code, out, _ = kb("doctor", "--json", *args)
        return {**json.loads(out), "code": code}

    return run


def got(report: Report, n: int) -> dict[str, Any]:
    (found,) = [c for c in report["checks"] if c["n"] == n]
    return found


def edit_plist(change: Callable[[dict[str, Any]], None]) -> None:
    plist = launchd.read_plist()
    assert plist is not None
    change(plist)
    launchd.plist_path().write_bytes(plistlib.dumps(plist))


def edit_stamp(**fields: Any) -> None:
    stamp = install.read_stamp()
    assert stamp is not None
    install.write_stamp({**stamp, **fields})


def test_a_healthy_install_passes_all_twenty_checks(doctor: Doctor) -> None:
    report = doctor()
    assert [c["n"] for c in report["checks"]] == list(range(1, 21))
    assert [c["status"] for c in report["checks"]] == ["ok"] * 20, report["checks"]
    assert report["ok"] and report["code"] == 0 and report["fixed"] == []


def test_the_text_report_lists_every_check_and_the_fix_for_a_failed_one(
    kb: Kb, healthy: int
) -> None:
    edit_stamp(version="0.0.1")
    code, out, _ = kb("doctor")
    assert code == 1
    assert "version" in out and "FAIL" in out and "/kanban-claude:setup" in out
    assert "1 fail" in out.splitlines()[-1]


def test_without_a_database_doctor_says_so_and_creates_nothing(kb: Kb, env: Path) -> None:
    code, out, _ = kb("doctor", "--json")
    assert code == 1 and "/kanban-claude:setup" in json.loads(out)["checks"][0]["fix"]
    assert not (env / "kb-home").exists()


def test_a_database_sqlite_cannot_open_is_reported_with_where_the_backups_are(
    kb: Kb, healthy: int
) -> None:
    (paths.dirs().home / "kanban.db").write_bytes(b"not a database" * 100)
    code, out, _ = kb("doctor", "--json")
    first = json.loads(out)["checks"][0]
    assert code == 1 and "backups" in first["fix"] and first["status"] == "fail"


# --- 1, 2, 3 --------------------------------------------------------------------------------


def test_an_unloaded_launchagent_fails_check_1_and_fix_loads_it_again(
    doctor: Doctor, agent_loaded: Callable[[], bool]
) -> None:
    launchd.bootout()
    assert got(doctor(), 1)["status"] == "fail"
    report = doctor("--fix")
    assert got(report, 1)["status"] == "ok" and agent_loaded()
    assert any("loaded" in f for f in report["fixed"])


def test_fix_rebuilds_a_missing_plist_from_the_installed_stamp(
    doctor: Doctor, agent_loaded: Callable[[], bool]
) -> None:
    launchd.bootout()
    launchd.plist_path().unlink()
    report = doctor("--fix")
    plist = launchd.read_plist()
    assert got(report, 1)["status"] == "ok" and agent_loaded()
    assert plist and plist["ProgramArguments"][1] == "daemon"
    assert stat.S_IMODE(launchd.plist_path().stat().st_mode) == 0o600


def test_fix_cannot_rebuild_a_plist_when_the_stamp_has_no_environment(doctor: Doctor) -> None:
    launchd.bootout()
    launchd.plist_path().unlink()
    stamp = install.read_stamp() or {}
    stamp.pop("env")
    install.write_stamp(stamp)
    report = doctor("--fix")
    assert got(report, 1)["status"] == "fail"
    assert report["fixed"] == [] and "/kanban-claude:setup" in report["unfixed"][0]


def test_a_port_nothing_answers_on_fails_check_2(doctor: Doctor) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    edit_stamp(port=free)
    check = got(doctor(), 2)
    assert check["status"] == "fail" and f"nothing answers on 127.0.0.1:{free}" in check["detail"]
    assert "daemon.log" in check["fix"]


def test_a_stale_stamp_or_plugin_fails_the_version_check_naming_each(
    doctor: Doctor, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = env / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    check = got(doctor(), 3)
    assert check["status"] == "fail" and "plugin 9.9.9" in check["detail"]
    assert f"daemon {__version__}" in check["detail"] and "/kanban-claude:setup" in check["fix"]


def test_a_missing_stamp_fails_the_version_check(doctor: Doctor) -> None:
    install.stamp_path().unlink()
    check = got(doctor(), 3)
    assert check["status"] == "fail" and "installed.json" in check["detail"]


# --- 4, 5, 6 --------------------------------------------------------------------------------


def test_a_database_older_than_the_tool_points_at_migrate(doctor: Doctor, conn: Any) -> None:
    conn.execute(f"PRAGMA user_version = {len(store.MIGRATIONS) - 1}")
    check = got(doctor(), 5)
    assert check["status"] == "fail" and check["fix"] == "kanban migrate"


def test_a_database_newer_than_the_tool_points_at_the_upgrade(doctor: Doctor, conn: Any) -> None:
    conn.execute(f"PRAGMA user_version = {len(store.MIGRATIONS) + 1}")
    check = got(doctor(), 5)
    assert check["status"] == "fail" and "/kanban-claude:setup" in check["fix"]


def test_a_running_card_without_a_run_fails_check_6_and_fix_sends_it_back(
    doctor: Doctor, conn: Any, board: str, card: Callable[..., str]
) -> None:
    tid = card()
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    check = got(doctor(), 6)
    assert check["status"] == "fail" and tid in check["detail"]
    report = doctor("--fix")
    assert got(report, 6)["status"] == "ok" and report["fixed"]
    assert store.get_task(conn, tid)["status"] == "ready"


def test_a_running_run_whose_card_is_not_running_fails_check_6_both_ways(
    doctor: Doctor, conn: Any, board: str, card: Callable[..., str], claim: Callable[[str], int]
) -> None:
    tid = card()
    run = claim(tid)
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    check = got(doctor(), 6)
    assert check["status"] == "fail" and f"run {run}" in check["detail"]


def test_a_dead_runner_fails_check_18_and_fix_ends_its_run(
    doctor: Doctor, conn: Any, board: str, card: Callable[..., str], claim: Callable[[str], int]
) -> None:
    tid = card()
    run = claim(tid)
    conn.execute(
        "UPDATE task_runs SET runner_pid = 2147483000, runner_started = ?, started_at = ? WHERE id = ?",
        (int(time.time()) - 60, int(time.time()) - 60, run),
    )
    check = got(doctor(), 18)
    assert check["status"] == "fail" and f"run {run}" in check["detail"]
    report = doctor("--fix")
    assert got(report, 18)["status"] == "ok" and got(report, 6)["status"] == "ok"
    assert (
        conn.execute("SELECT status FROM task_runs WHERE id = ?", (run,)).fetchone()[0] == "ended"
    )


def test_a_run_that_has_only_just_started_is_not_a_dead_runner(
    doctor: Doctor, conn: Any, board: str, card: Callable[..., str], claim: Callable[[str], int]
) -> None:
    claim(card())
    assert got(doctor(), 18)["status"] == "ok"


# --- 7, 8, 9 --------------------------------------------------------------------------------


def test_a_claude_that_will_not_run_fails_check_7(doctor: Doctor, conn: Any) -> None:
    store.config_set(conn, "claude.bin", "/nonexistent/claude")
    check = got(doctor(), 7)
    assert check["status"] == "fail" and "/nonexistent/claude" in check["detail"]
    assert "--claude-bin" in check["fix"]


def test_a_claude_older_than_the_minimum_fails_check_7(
    doctor: Doctor, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_VERSION="2.1.100 (Claude Code)")
    check = got(doctor(), 7)
    assert check["status"] == "fail" and "2.1.100" in check["detail"]


def test_a_healthy_claude_is_reported_with_its_version(doctor: Doctor, env: Path) -> None:
    detail = got(doctor(), 7)["detail"]
    assert str(env / "bin" / "claude") in detail and "2.1.286" in detail


def test_a_board_with_a_missing_config_directory_fails_check_8_naming_the_board(
    doctor: Doctor, conn: Any, env: Path
) -> None:
    store.board_create(
        conn, "alt", default_workdir=str(env), claude_config_dir=str(env / "nowhere")
    )
    check = got(doctor(), 8)
    assert check["status"] == "fail" and "alt" in check["detail"] and "nowhere" in check["detail"]


def test_a_plugin_not_enabled_at_user_scope_fails_check_9_with_the_install_command(
    doctor: Doctor,
) -> None:
    enable(Path.home() / ".claude", on=False)
    check = got(doctor(), 9)
    assert check["status"] == "fail" and "/plugin install " + PLUGIN in check["fix"]
    assert "CLAUDE_CONFIG_DIR" not in check["fix"]


def test_the_fix_for_another_config_directory_names_it(
    doctor: Doctor, conn: Any, env: Path
) -> None:
    alt = env / "alt-claude"
    alt.mkdir()
    store.board_create(conn, "alt", default_workdir=str(env), claude_config_dir=str(alt))
    check = got(doctor(), 9)
    assert check["status"] == "fail" and f"CLAUDE_CONFIG_DIR={alt}" in check["fix"]


def test_probe_starts_the_scout_and_the_auth_probe_in_every_directory(
    doctor: Doctor, env: Path
) -> None:
    assert got(doctor("--probe"), 9)["status"] == "ok"
    calls = [json.loads(x) for x in (env / "claude.log").read_text().splitlines()]
    assert any("kanban-claude:scout" in c["argv"] for c in calls)
    assert any("haiku" in c["argv"] and "--tools" in c["argv"] for c in calls)


def test_probe_fails_check_9_when_the_agent_does_not_resolve(
    doctor: Doctor, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE_AGENT="no_agent")
    check = got(doctor("--probe"), 9)
    assert check["status"] == "fail" and "does not resolve" in check["detail"]


def test_probe_fails_check_9_when_the_login_is_gone(
    doctor: Doctor, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE_AGENT="success", FAKE_CLAUDE_FIXTURE="auth401")
    check = got(doctor("--probe"), 9)
    assert check["status"] == "fail" and "claude /login" in check["fix"]


# --- 10, 11, 12, 13, 14 ---------------------------------------------------------------------


@pytest.fixture
def used(conn: Any, repo: Path) -> Path:
    store.board_create(conn, "main", default_workdir=str(repo))
    return repo


def test_a_host_gh_is_not_logged_in_to_fails_check_10(
    doctor: Doctor, used: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_FAKE_AUTH", "out")
    check = got(doctor(), 10)
    assert check["status"] == "fail" and "example.invalid" in check["detail"]
    assert check["fix"] == "gh auth login --hostname example.invalid"


def test_a_token_variable_login_is_a_warning_with_the_login_command(
    doctor: Doctor, used: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_FAKE_AUTH", "GH_TOKEN")
    check = got(doctor(), 10)
    assert check["status"] == "warn" and check["fix"] == "gh auth login --hostname example.invalid"


def test_a_keychain_login_passes_check_10(doctor: Doctor, used: Path) -> None:
    check = got(doctor(), 10)
    assert check["status"] == "ok" and "example.invalid" in check["detail"]


def test_a_missing_uv_fails_check_11(
    doctor: Doctor, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env / "bin" / "uv").unlink()
    monkeypatch.setenv("PATH", f"{env / 'bin'}:/usr/bin:/bin")
    check = got(doctor(), 11)
    assert check["status"] == "fail" and "brew install uv" in check["fix"]


def test_a_ca_variable_in_the_shell_but_not_in_the_plist_warns(
    doctor: Doctor, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(env / "ca.pem"))
    check = got(doctor(), 12)
    assert check["status"] == "warn" and "NODE_EXTRA_CA_CERTS" in check["detail"]
    edit_plist(lambda p: p["EnvironmentVariables"].update(NODE_EXTRA_CA_CERTS=str(env / "ca.pem")))
    assert got(doctor(), 12)["status"] == "ok"


def test_a_plist_path_that_lost_a_tool_fails_check_13_and_a_lost_npx_only_warns(
    doctor: Doctor, env: Path
) -> None:
    edit_plist(lambda p: p["EnvironmentVariables"].update(PATH="/usr/bin:/bin"))
    check = got(doctor(), 13)
    assert check["status"] == "fail"
    assert all(t in check["detail"] for t in ("gh", "uv", "claude"))

    system = "/usr/bin:/bin"
    (env / "slim").mkdir()
    for tool in ("gh", "uv", "claude"):
        shutil.copy(env / "bin" / tool, env / "slim" / tool)
    edit_plist(lambda p: p["EnvironmentVariables"].update(PATH=f"{env / 'slim'}:{system}"))
    check = got(doctor(), 13)
    assert check["status"] == "warn" and "npx" in check["detail"]


def test_a_missing_kanban_entry_point_fails_check_13(doctor: Doctor) -> None:
    (Path.home() / ".local" / "bin" / "kanban").unlink()
    check = got(doctor(), 13)
    assert check["status"] == "fail" and "kanban" in check["detail"]


def test_under_two_gigabytes_free_fails_check_14(
    doctor: Doctor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil, "disk_usage", lambda p: SimpleNamespace(total=10**12, used=10**12, free=10**9)
    )
    check = got(doctor(), 14)
    assert check["status"] == "fail" and "free" in check["detail"]


# --- 15, 16, 19, 20 -------------------------------------------------------------------------


def test_a_pause_and_a_quota_wall_are_listed_with_their_times(doctor: Doctor, conn: Any) -> None:
    store.pause(conn, "lunch")
    until = int(time.time()) + 3600
    config_dir = str(Path.home() / ".claude")
    with store.tx(conn):
        store.account_set(conn, config_dir, hard_until=until)
    check = got(doctor(), 15)
    assert check["status"] == "warn" and check["fix"] == "kanban resume"
    assert "lunch" in check["detail"] and views.iso(until) in check["detail"]


def test_a_logged_out_account_is_listed_with_the_login_command(doctor: Doctor, conn: Any) -> None:
    alt = str(Path.home() / "alt")
    with store.tx(conn):
        store.account_set(conn, alt, auth_paused={"since": 1, "error": "401"})
    check = got(doctor(), 15)
    assert check["status"] == "warn" and f"CLAUDE_CONFIG_DIR={alt} claude /login" in check["detail"]


def test_kept_worktrees_are_listed(doctor: Doctor, conn: Any, wt: tuple[str, int]) -> None:
    conn.execute("UPDATE tasks SET worktree_state = 'kept_dirty' WHERE id = ?", (wt[0],))
    check = got(doctor(), 16)
    assert (
        check["status"] == "warn" and wt[0] in check["detail"] and "kept_dirty" in check["detail"]
    )


def test_loose_file_modes_fail_check_17_and_fix_tightens_them(doctor: Doctor, env: Path) -> None:
    d = paths.dirs()
    os.chmod(d.home / "kanban.db", 0o644)  # noqa: S103
    os.chmod(d.work, 0o750)  # noqa: S103
    os.chmod(d.home / "installed.json", 0o640)  # noqa: S103
    check = got(doctor(), 17)
    assert check["status"] == "fail" and "kanban.db" in check["detail"]
    report = doctor("--fix")
    assert got(report, 17)["status"] == "ok" and len(report["fixed"]) >= 3
    assert stat.S_IMODE((d.home / "kanban.db").stat().st_mode) == 0o600
    assert stat.S_IMODE(d.work.stat().st_mode) == 0o700


def test_a_repo_without_branch_protection_warns_and_never_fails_the_run(
    doctor: Doctor, used: Path, set_api: Callable[[dict[str, Any]], None]
) -> None:
    set_api(
        {
            "repos/acme/proj/rules/branches/main": [],
            "repos/acme/proj/branches/main": {"protected": False},
        }
    )
    report = doctor()
    check = got(report, 19)
    assert check["status"] == "warn" and "example.invalid/acme/proj" in check["detail"]
    assert "gh api" in check["fix"] and report["code"] == 0


def test_a_protected_repo_passes_check_19(
    doctor: Doctor, used: Path, set_api: Callable[[dict[str, Any]], None]
) -> None:
    set_api({"repos/acme/proj/rules/branches/main": [{"type": "pull_request"}]})
    assert got(doctor(), 19)["status"] == "ok"


def test_an_unreadable_protection_state_warns_as_unknown(doctor: Doctor, used: Path) -> None:
    check = got(doctor(), 19)
    assert check["status"] == "warn" and "could not be read" in check["detail"]


def test_a_paired_board_without_an_assignee_fails_check_20(doctor: Doctor, conn: Any) -> None:
    with store.tx(conn):
        store.state_put(conn, "jira:main", {})
    check = got(doctor(), 20)
    assert check["status"] == "fail" and "jira.assignee" in check["detail"]


def test_a_paused_jira_sync_warns_and_probe_says_the_sync_is_not_there_yet(
    doctor: Doctor, conn: Any
) -> None:
    with store.tx(conn):
        conn.execute(
            "INSERT INTO config(key, value, updated_at) VALUES ('jira.assignee', '\"me\"', 1)"
        )
        store.state_put(conn, "jira:main", {"paused": True})
    assert got(doctor(), 20)["status"] == "warn"
    assert "not built" in got(doctor("--probe"), 20)["detail"]
