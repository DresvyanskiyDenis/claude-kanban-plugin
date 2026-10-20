import contextlib
import fcntl
import json
import os
import shutil
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import __version__, install, launchd, paths, store

from .conftest import Calls, Kb

OLD = "0.0.1"


Setup = Callable[..., tuple[int, str, str]]


@pytest.fixture
def setup(kb: Kb, daemon_up: int) -> Setup:
    return lambda *args: kb("setup", *args)


def claude_calls(env: Path) -> list[dict[str, Any]]:
    log = env / "claude.log"
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def stamp() -> dict[str, Any]:
    return json.loads((paths.dirs().home / "installed.json").read_text())  # type: ignore[no-any-return]


def cfg(key: str) -> Any:
    with contextlib.closing(store.connect()) as conn:
        return store.cfg(conn, key)


def untouched(launchctl_calls: Calls) -> None:
    assert launchctl_calls() == []
    assert not launchd.plist_path().exists()
    assert not (paths.dirs().home / "installed.json").exists()


def test_setup_loads_the_agent_and_stamps_the_version(
    setup: Setup, agent_loaded: Callable[[], bool], env: Path
) -> None:
    code, out, err = setup()
    assert code == 0, err
    assert agent_loaded()
    plist = launchd.read_plist()
    assert plist and plist["ProgramArguments"] == [str(Path.home() / ".local/bin/kanban"), "daemon"]
    assert stat.S_IMODE(launchd.plist_path().stat().st_mode) == 0o600
    got = stamp()
    assert got["version"] == __version__ and got["schema"] == 1
    assert got["claude_bin"] == str(env / "bin" / "claude")
    assert got["claude_config_dir"] == str(Path.home() / ".claude")
    path = got["env"]["PATH"].split(":")
    assert path[0] == str(env / "bin") and {"/usr/bin", "/bin", "/usr/sbin", "/sbin"} <= set(path)
    raw = (paths.dirs().home / "installed.json").read_text()
    assert (
        '"version":"' in raw
        and stat.S_IMODE(os.stat(paths.dirs().home / "installed.json").st_mode) == 0o600
    )
    assert "is set up" in out
    assert f"using claude at {env / 'bin' / 'claude'}" in err
    assert cfg("claude.bin") == str(env / "bin" / "claude")


def test_the_probes_run_with_the_worker_flags_from_the_work_directory(
    setup: Setup, env: Path
) -> None:
    assert setup()[0] == 0
    probe, agent = claude_calls(env)
    assert probe["argv"] == [
        "-p", "ok", "--model", "opus", "--permission-mode", "auto", "--permission-prompts", "none",
        "--no-session-persistence", "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip
    assert agent["argv"][:6] == [
        "-p",
        "reply ok",
        "--agent",
        "kanban-claude:scout",
        "--max-turns",
        "1",
    ]
    assert agent["argv"][6:10] == ["--permission-mode", "auto", "--permission-prompts", "none"]
    assert agent["argv"][10] == "--settings" and agent["argv"][-3:] == [
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert Path(agent["cwd"]).resolve() == paths.dirs().work.resolve()


def test_the_default_config_directory_is_never_exported_and_another_one_is(
    setup: Setup, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
    assert setup()[0] == 0
    assert all("CLAUDE_CONFIG_DIR" not in c["env"] for c in claude_calls(env))
    assert "CLAUDE_CONFIG_DIR" not in launchd.read_plist()["EnvironmentVariables"]  # type: ignore[index]
    (env / "claude.log").unlink()
    alt = env / "alt-claude"
    assert setup("--claude-config-dir", str(alt))[0] == 0
    assert [c["env"]["CLAUDE_CONFIG_DIR"] for c in claude_calls(env)] == [str(alt)] * 2
    assert stamp()["claude_config_dir"] == str(alt)
    assert cfg("claude.config_dir") == str(alt)


def test_the_probe_sees_the_captured_environment_and_not_the_shell(
    setup: Setup, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("SOME_SHELL_VAR", "x")
    monkeypatch.setenv("SSL_CERT_FILE", "/ca.pem")
    assert setup()[0] == 0
    seen = claude_calls(env)[0]["env"]
    assert "GH_TOKEN" not in seen and "SOME_SHELL_VAR" not in seen
    assert seen["SSL_CERT_FILE"] == "/ca.pem" and seen["HOME"] == str(Path.home())


def test_a_secret_is_never_persisted_and_a_proxy_password_is_never_logged(
    setup: Setup, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k3y")
    monkeypatch.setenv("SSL_CERT_FILE", "/ca.pem")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw4321@proxy.invalid:3128")
    code, out, err = setup()
    assert code == 0
    persisted = (
        launchd.plist_path().read_text(),
        (paths.dirs().home / "installed.json").read_text(),
    )
    for text in persisted:
        assert "t0ken" not in text and "k3y" not in text and "GH_TOKEN" not in text
        assert "SSL_CERT_FILE" in text and "pw4321" in text
    log = (paths.dirs().home / "logs" / "upgrade.log").read_text()
    for text in (out, err, log):
        assert "pw4321" not in text and "t0ken" not in text
    assert "HTTPS_PROXY" in log


def test_setup_names_the_captured_variables_and_never_prints_a_passthrough_value(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    with contextlib.closing(store.connect()) as conn:
        store.config_set(conn, "worker.env_passthrough", ["MY_API_TOKEN"])
    monkeypatch.setenv("MY_API_TOKEN", "s3cr3t-passthrough")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    code, out, err = setup()
    assert code == 0, err
    log = (paths.dirs().home / "logs" / "upgrade.log").read_text()
    for text in (out, err, log):
        assert "s3cr3t-passthrough" not in text
    assert "captured environment:" in err and "MY_API_TOKEN" in err and "HTTPS_PROXY" in err
    assert "proxy.invalid" not in err


def test_rerunning_setup_keeps_what_the_owner_configured(setup: Setup, kb: Kb, env: Path) -> None:
    assert setup()[0] == 0
    assert kb("config", "set", "dispatch.max_in_progress", "5")[0] == 0
    assert kb("config", "set", "models.review", "sonnet")[0] == 0
    other = str(shutil.copy(env / "bin" / "claude", env / "bin" / "claude-other"))
    assert kb("config", "set", "claude.bin", other)[0] == 0
    assert setup()[0] == 0
    assert cfg("dispatch.max_in_progress") == 5 and cfg("models.review") == "sonnet"
    assert cfg("claude.bin") == other
    assert setup("--claude-bin", str(env / "bin" / "claude"))[0] == 0
    assert cfg("claude.bin") == str(env / "bin" / "claude")


def test_setup_refuses_a_claude_below_the_minimum_and_changes_nothing(
    setup: Setup, env: Path, launchctl_calls: Calls, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_VERSION="2.1.258 (Claude Code)")
    code, out, err = setup()
    assert code == 1 and "2.1.259" in err and "claude update" in err
    untouched(launchctl_calls)
    assert claude_calls(env) == []
    knobs(FAKE_CLAUDE_VERSION="2.1.259 (Claude Code)")
    assert setup()[0] == 0


@pytest.mark.parametrize(
    ("missing", "fix"),
    [("uv", "brew install uv"), ("git", "xcode-select --install"), ("gh", "brew install gh")],
)
def test_a_missing_tool_is_named_with_its_fix(
    setup: Setup, missing: str, fix: str, monkeypatch: pytest.MonkeyPatch, env: Path,
    launchctl_calls: Calls,
) -> None:  # fmt: skip
    real = install.shutil.which
    monkeypatch.setattr(
        install.shutil, "which", lambda n, *a, **k: None if n == missing else real(n, *a, **k)
    )
    code, _, err = setup()
    assert code == 1 and missing in err and fix in err
    untouched(launchctl_calls)


def test_setup_needs_macos_and_an_absolute_claude(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _, err = setup("--claude-bin", "claude")
    assert code == 1 and "absolute" in err
    monkeypatch.setattr(install.sys, "platform", "linux")
    code, _, err = setup()
    assert code == 1 and "macOS" in err


def test_a_login_failure_prints_the_login_command_and_installs_nothing(
    setup: Setup, env: Path, launchctl_calls: Calls, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE="auth401")
    code, _, err = setup()
    assert code == 1 and "not logged in" in err and "claude /login" in err
    assert "CLAUDE_CONFIG_DIR" not in err
    untouched(launchctl_calls)
    alt = env / "alt-claude"
    code, _, err = setup("--claude-config-dir", str(alt))
    assert f"CLAUDE_CONFIG_DIR={alt} claude /login" in err


def test_an_organisation_that_disables_auto_mode_stops_setup(
    setup: Setup, env: Path, launchctl_calls: Calls, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE="mode_default")
    code, _, err = setup("--yes")
    assert code == 1 and "auto mode" in err and "workers cannot run" in err
    untouched(launchctl_calls)


def test_a_seat_without_opus_gets_the_sonnet_command_and_yes_applies_it(
    setup: Setup, env: Path, launchctl_calls: Calls, knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE_OPUS="no_model")
    code, _, err = setup()
    assert code == 1 and "kanban config set models.review sonnet" in err
    untouched(launchctl_calls)
    assert cfg("models.review") == "opus"
    assert setup("--yes")[0] == 0
    assert cfg("models.review") == "sonnet"
    assert [c["argv"][3] for c in claude_calls(env)[1:3]] == ["opus", "sonnet"]


def test_an_agent_that_does_not_resolve_says_how_to_enable_the_plugin(
    setup: Setup, env: Path, agent_loaded: Callable[[], bool], knobs: Callable[..., None]
) -> None:
    knobs(FAKE_CLAUDE_FIXTURE_AGENT="no_agent")
    code, _, err = setup()
    assert code == 1 and "/plugin install kanban-claude@claude-kanban-plugin" in err
    assert str(Path.home() / ".claude") in err
    assert agent_loaded() and stamp()["version"] == __version__


def test_no_daemon_installs_no_agent_and_does_not_wait_for_the_daemon(
    kb: Kb, entry_point: None, env: Path, launchctl_calls: Calls
) -> None:
    code, out, err = kb("setup", "--no-daemon")
    assert code == 0, err
    assert launchctl_calls() == [] and not launchd.plist_path().exists()
    assert stamp()["version"] == __version__


def test_a_daemon_that_never_starts_is_reported_with_the_lock_and_the_daemon_log(
    kb: Kb, entry_point: None
) -> None:
    code, _, err = kb("setup")
    assert code == 1 and "daemon.lock" in err and "daemon did not start" in err
    assert "daemon.log" in err


# --- upgrade (INS-09) -----------------------------------------------------------------------


@pytest.fixture
def installed(setup: Setup, kb: Kb, monkeypatch: pytest.MonkeyPatch) -> Setup:
    """Setup done by an older release; the process then runs as the current one, as
    `bootstrap.sh` starts it: with no flags, so the settings come from the stamp."""
    with monkeypatch.context() as old:
        old.setattr(install, "__version__", OLD)
        assert setup()[0] == 0
    return lambda *args: kb("setup", *args)


def test_an_upgrade_migrates_restarts_the_daemon_and_rewrites_the_stamp(
    installed: Setup, env: Path, launchctl_calls: Calls, uv_calls: Calls
) -> None:
    before = len(launchctl_calls())
    code, out, err = installed()
    assert code == 0, err
    assert stamp()["version"] == __version__ and stamp()["claude_bin"] == str(
        env / "bin" / "claude"
    )
    assert launchctl_calls()[before:][-2:] == [
        ["print", f"gui/{os.getuid()}/kanban-claude.daemon"],
        ["kickstart", "-k", f"gui/{os.getuid()}/kanban-claude.daemon"],
    ]
    assert len(claude_calls(env)) == 2
    assert f"{OLD} -> {__version__}" in (paths.dirs().home / "logs" / "upgrade.log").read_text()
    assert upgrade_lock_is_free()


@contextlib.contextmanager
def held_upgrade_lock() -> Iterator[None]:
    fd = os.open(paths.dirs().home / "upgrade.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def upgrade_lock_is_free() -> bool:
    with install.upgrade_lock(paths.dirs().home) as held:
        return held


def test_an_upgrade_while_another_holds_the_lock_exits_zero_and_does_nothing(
    installed: Setup, launchctl_calls: Calls
) -> None:
    before = launchctl_calls()
    with held_upgrade_lock():
        code, out, err = installed()
        assert not upgrade_lock_is_free()
    assert code == 0 and "another" in err + out
    assert stamp()["version"] == OLD and launchctl_calls() == before


def test_a_held_lock_is_never_taken_over_however_old_its_file_is(
    installed: Setup, launchctl_calls: Calls
) -> None:
    before = launchctl_calls()
    with held_upgrade_lock():
        os.utime(paths.dirs().home / "upgrade.lock", (0, 0))
        code, out, err = installed()
    assert code == 0 and "another" in err + out
    assert stamp()["version"] == OLD and launchctl_calls() == before


def test_a_lock_file_left_by_a_crashed_setup_blocks_nothing(installed: Setup) -> None:
    (paths.dirs().home / "upgrade.lock").write_text("")
    assert installed()[0] == 0
    assert stamp()["version"] == __version__ and upgrade_lock_is_free()


def test_a_failed_setup_still_releases_the_lock(setup: Setup, knobs: Callable[..., None]) -> None:
    knobs(FAKE_CLAUDE_FIXTURE="auth401")
    assert setup()[0] == 1
    assert upgrade_lock_is_free()


def test_a_plugin_older_than_the_stamp_is_refused_unless_confirmed(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert setup()[0] == 0
    newer = json.loads((paths.dirs().home / "installed.json").read_text()) | {"version": "99.0.0"}
    (paths.dirs().home / "installed.json").write_text(json.dumps(newer))
    code, _, err = setup()
    assert code == 4 and "99.0.0" in err and "kanban setup --yes" in err
    assert stamp()["version"] == "99.0.0"
    assert setup("--yes")[0] == 0
    assert stamp()["version"] == __version__


def claude_json(path: Path, servers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}))


ATLASSIAN = {
    "command": "uvx",
    "args": ["mcp-atlassian", "--cache", "${HOME}/.cache/jira"],
    "env": {"JIRA_API_TOKEN": "s3cr3t-token"},
}


def test_setup_records_the_atlassian_server_command_and_never_its_env(setup: Setup, kb: Kb) -> None:
    claude_json(Path.home() / ".claude.json", {"atlassian": ATLASSIAN, "other": {"command": "x"}})
    code, out, err = setup()
    assert code == 0, err
    assert cfg("jira.mcp_command") == [
        "uvx",
        "mcp-atlassian",
        "--cache",
        f"{Path.home()}/.cache/jira",
    ]
    assert "atlassian MCP server" in err
    for text in (out, err, kb("config", "list")[1], json.dumps(stamp())):
        assert "s3cr3t-token" not in text
    assert (Path.home() / ".claude.json").read_text().count("s3cr3t-token") == 1


def test_setup_reads_a_non_default_config_directory_and_leaves_the_command_alone_without_one(
    setup: Setup, env: Path
) -> None:
    claude_json(Path.home() / ".claude.json", {"atlassian": ATLASSIAN})
    alt = env / "alt-claude"
    assert setup("--claude-config-dir", str(alt))[0] == 0
    assert cfg("jira.mcp_command") is None
    claude_json(alt / ".claude.json", {"atlassian": {"command": "jira-mcp"}})
    assert setup("--claude-config-dir", str(alt))[0] == 0
    assert cfg("jira.mcp_command") == ["jira-mcp"]


@pytest.mark.parametrize(
    "content",
    ["not json", '{"mcpServers": {"atlassian": "oops"}}', '{"mcpServers": {"atlassian": {"args": []}}}',
     '{"mcpServers": {"atlassian": {"command": "uvx", "args": [1]}}}'],
)  # fmt: skip
def test_an_unreadable_atlassian_entry_changes_nothing(setup: Setup, content: str) -> None:
    (Path.home() / ".claude.json").write_text(content)
    assert setup()[0] == 0
    assert cfg("jira.mcp_command") is None
