import os
import plistlib
import stat
from pathlib import Path

import pytest

from kanban_claude import launchd, service
from kanban_claude.errors import Error

from .conftest import Calls

KANBAN = "/opt/tools/bin/kanban"


def rendered(env: dict[str, str] | None = None) -> dict[str, object]:
    home, work = Path("/data/kanban"), Path("/data/work")
    return plistlib.loads(launchd.render(KANBAN, env or {"PATH": "/usr/bin"}, home, work))


def test_the_plist_carries_the_keys_of_ins_06() -> None:
    plist = rendered({"PATH": "/opt/tools/bin:/usr/bin", "LANG": "C"})
    assert plist == {
        "Label": "kanban-claude.daemon",
        "ProgramArguments": [KANBAN, "daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Standard",
        "LimitLoadToSessionType": "Aqua",
        "AbandonProcessGroup": True,
        "WorkingDirectory": "/data/kanban",
        "EnvironmentVariables": {
            "PATH": "/opt/tools/bin:/usr/bin",
            "LANG": "C",
            "KANBAN_HOME": "/data/kanban",
            "KANBAN_WORK": "/data/work",
        },
        "StandardOutPath": "/data/kanban/logs/daemon.log",
        "StandardErrorPath": "/data/kanban/logs/daemon.log",
    }


def test_the_captured_environment_never_takes_a_secret_like_name() -> None:
    shell = {
        "GH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s",
        "DB_PASSWORD": "p", "SSH_AUTH_SOCK": "/sock", "github_token": "t2", "API_KEY_FILE": "f",
        "SSL_CERT_FILE": "/ca.pem", "LANG": "en_US.UTF-8", "HOME": "/h", "EDITOR": "vi",
    }  # fmt: skip
    got = service.capture_env(shell, [], ["/opt/bin"])
    assert got == {
        "PATH": "/opt/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "SSL_CERT_FILE": "/ca.pem",
        "LANG": "en_US.UTF-8",
    }


def test_only_the_names_the_owner_lists_pass_the_secret_scrub() -> None:
    shell = {"MY_TOKEN": "t", "OTHER_SECRET": "s"}
    assert service.capture_env(shell, ["MY_TOKEN"], [])["MY_TOKEN"] == "t"
    assert "OTHER_SECRET" not in service.capture_env(shell, ["MY_TOKEN"], [])


def test_proxies_and_ca_bundles_are_captured_in_both_cases_and_userinfo_is_kept() -> None:
    shell = {
        "HTTPS_PROXY": "http://u:p@proxy:3128", "no_proxy": "localhost", "NODE_EXTRA_CA_CERTS": "/c",
        "REQUESTS_CA_BUNDLE": "/r", "http_proxy": "http://proxy:3128",
    }  # fmt: skip
    got = service.capture_env(shell, [], [])
    assert {k: v for k, v in got.items() if k != "PATH"} == shell


def test_the_path_lists_each_tool_directory_once_before_the_system_ones() -> None:
    got = service.capture_env({}, [], ["/a", "/b", "/a", "/usr/bin"])
    assert got["PATH"] == "/a:/b:/usr/bin:/bin:/usr/sbin:/sbin"


def test_a_proxy_password_is_masked_in_every_message() -> None:
    assert service.mask("via http://user:pa55@proxy:3128/x") == "via http://***@proxy:3128/x"
    assert service.mask("no credentials: https://example.invalid/a@b") == (
        "no credentials: https://example.invalid/a@b"
    )


def test_the_plist_is_written_0600_under_the_users_launch_agents(env: Path) -> None:
    path = launchd.install_plist(b"<plist/>")
    assert path == Path.home() / "Library" / "LaunchAgents" / "kanban-claude.daemon.plist"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    assert launchd.install_plist(b"<new/>") == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_bytes() == b"<new/>"
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_bootstrap_loads_the_agent_in_the_users_gui_domain(
    launchctl_calls: Calls, agent_loaded: object
) -> None:
    path = launchd.install_plist(launchd.render(KANBAN, {}, Path("/h"), Path("/w")))
    assert not launchd.loaded()
    launchd.bootstrap(path)
    assert launchd.loaded()
    domain = f"gui/{os.getuid()}"
    assert launchctl_calls() == [
        ["print", f"{domain}/kanban-claude.daemon"],
        ["bootstrap", domain, str(path)],
        ["print", f"{domain}/kanban-claude.daemon"],
    ]


def test_bootout_of_an_agent_that_is_not_loaded_is_not_an_error() -> None:
    launchd.bootout()
    assert not launchd.loaded()


def test_bootout_unloads_and_kickstart_restarts(launchctl_calls: Calls) -> None:
    launchd.bootstrap(launchd.install_plist(launchd.render(KANBAN, {}, Path("/h"), Path("/w"))))
    launchd.kickstart()
    assert launchctl_calls()[-1] == ["kickstart", "-k", f"gui/{os.getuid()}/kanban-claude.daemon"]
    launchd.bootout()
    assert not launchd.loaded()


def test_a_launchctl_failure_is_reported_with_its_own_words() -> None:
    path = launchd.install_plist(launchd.render(KANBAN, {}, Path("/h"), Path("/w")))
    launchd.bootstrap(path)
    with pytest.raises(Error, match="Input/output error"):
        launchd.bootstrap(path)
    launchd.bootout()
    with pytest.raises(Error, match="Could not find service"):
        launchd.kickstart()
