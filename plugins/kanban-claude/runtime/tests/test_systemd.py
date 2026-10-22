"""INS-06 on Linux: the systemd user unit, with a fake `systemctl` and the platform forced to linux."""

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from kanban_claude import launchd, service, systemd
from kanban_claude.errors import Error

from .conftest import Calls, Kb

KANBAN = "/opt/tools/bin/kanban"
UNIT = "kanban-claude.daemon.service"


@pytest.fixture(autouse=True)
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")


def rendered(env: dict[str, str] | None = None) -> str:
    home, work = Path("/data/kanban"), Path("/data/work")
    return systemd.render(KANBAN, env or {"PATH": "/usr/bin"}, home, work).decode()


def test_the_unit_carries_the_directives_of_ins_06() -> None:
    assert rendered({"PATH": "/opt/tools/bin:/usr/bin", "LANG": "C"}) == "\n".join(
        [
            "[Unit]",
            "Description=kanban-claude.daemon",
            "",
            "[Service]",
            "Type=simple",
            'ExecStart="/opt/tools/bin/kanban" daemon',
            "Restart=on-failure",
            "RestartSec=10",
            "KillMode=process",
            "WorkingDirectory=/data/kanban",
            "StandardOutput=append:/data/kanban/logs/daemon.log",
            "StandardError=append:/data/kanban/logs/daemon.log",
            'Environment="PATH=/opt/tools/bin:/usr/bin"',
            'Environment="LANG=C"',
            'Environment="KANBAN_HOME=/data/kanban"',
            'Environment="KANBAN_WORK=/data/work"',
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def test_every_captured_variable_including_path_reaches_the_unit() -> None:
    shell = {
        "HTTPS_PROXY": "http://u:p%40ss@proxy:3128", "no_proxy": 'local host,"quoted"',
        "SSL_CERT_FILE": "/ca\\bundle.pem", "LANG": "en_US.UTF-8", "GH_TOKEN": "never",
    }  # fmt: skip
    captured = service.capture_env(shell, [], ["/opt/bin"])
    assert "PATH" in captured and "GH_TOKEN" not in captured
    text = rendered(captured)
    assert text.count("Environment=") == len(captured) + 2
    assert "p%%40ss" in text, "a percent sign is a systemd specifier and must be doubled"


def test_the_environment_survives_a_write_and_a_read_back(env: Path) -> None:
    captured = {"PATH": "/a:/b", "HTTPS_PROXY": "http://u:p%40ss@proxy", "X": 'a "b" \\c d'}
    systemd.install_unit(systemd.render(KANBAN, captured, Path("/h"), Path("/w")))
    assert systemd.read_env() == {**captured, "KANBAN_HOME": "/h", "KANBAN_WORK": "/w"}
    assert systemd.read_entry() == KANBAN


def test_a_program_path_with_a_space_and_a_percent_is_read_back_whole(env: Path) -> None:
    entry = "/opt/my tools/100%/kanban"
    systemd.install_unit(systemd.render(entry, {}, Path("/h"), Path("/w")))
    assert systemd.read_entry() == entry


def test_without_a_unit_file_there_is_nothing_to_read(env: Path) -> None:
    assert systemd.read_env() is None and systemd.read_entry() is None


@pytest.mark.parametrize("value", ["a\nb", "a\rb"])
def test_a_value_with_a_line_break_is_refused_rather_than_misread_by_systemd(value: str) -> None:
    with pytest.raises(Error, match="FOO holds a line break"):
        rendered({"FOO": value})


def test_the_unit_is_written_0600_under_the_users_systemd_directory(env: Path) -> None:
    path = systemd.install_unit(b"[Unit]\n")
    assert path == Path.home() / ".config" / "systemd" / "user" / UNIT
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    assert systemd.install_unit(b"[new]\n") == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.read_bytes() == b"[new]\n"
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_load_reloads_then_enables_and_starts_the_unit(
    systemctl_calls: Calls, unit_enabled: Callable[[], bool]
) -> None:
    unit = systemd.load(KANBAN, {"PATH": "/usr/bin"}, Path("/h"), Path("/w"))
    assert unit == systemd.unit_path() and unit_enabled() and systemd.loaded()
    assert systemctl_calls()[:4] == [
        ["--user", "is-enabled", UNIT],
        ["--user", "daemon-reload"],
        ["--user", "enable", "--now", UNIT],
        ["--user", "is-enabled", UNIT],
    ]


def test_loading_over_a_loaded_unit_disables_it_first(systemctl_calls: Calls) -> None:
    systemd.load(KANBAN, {}, Path("/h"), Path("/w"))
    before = len(systemctl_calls())
    systemd.load(KANBAN, {}, Path("/h"), Path("/w"))
    verbs = [c[1:] for c in systemctl_calls()[before:]]
    assert verbs == [
        ["is-enabled", UNIT],
        ["disable", "--now", UNIT],
        ["daemon-reload"],
        ["enable", "--now", UNIT],
    ]


def test_bootout_unloads_and_restart_restarts(
    systemctl_calls: Calls, unit_enabled: Callable[[], bool]
) -> None:
    systemd.load(KANBAN, {}, Path("/h"), Path("/w"))
    systemd.restart()
    assert systemctl_calls()[-1] == ["--user", "restart", UNIT]
    systemd.bootout()
    assert not unit_enabled() and not systemd.loaded()
    assert systemctl_calls()[-2:] == [
        ["--user", "disable", "--now", UNIT],
        ["--user", "is-enabled", UNIT],
    ]


def test_bootout_of_a_unit_that_is_not_loaded_is_not_an_error(systemctl_calls: Calls) -> None:
    systemd.bootout()
    assert systemctl_calls() == [["--user", "is-enabled", UNIT]]


def test_restart_of_an_unloaded_unit_loads_the_file_on_disk_or_says_how_to_repair(
    unit_enabled: Callable[[], bool],
) -> None:
    with pytest.raises(Error, match="kanban doctor --fix"):
        systemd.restart()
    systemd.install_unit(systemd.render(KANBAN, {}, Path("/h"), Path("/w")))
    systemd.restart()
    assert unit_enabled()


def test_a_systemctl_failure_names_the_command_the_owner_can_run_by_hand(
    systemctl_calls: Calls,
) -> None:
    with pytest.raises(Error, match=r"systemctl --user enable --now .*failed.*does not exist"):
        systemd.bootstrap(systemd.unit_path())
    with pytest.raises(Error, match=r"systemctl --user restart .*failed.*not found"):
        systemd.kickstart()


def test_without_systemctl_nothing_is_loaded_so_uninstall_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert not systemd.loaded()
    systemd.bootout()


def test_the_backend_follows_the_platform_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert service.backend() is systemd
    monkeypatch.setattr(sys, "platform", "darwin")
    assert service.backend() is launchd


@pytest.mark.parametrize(("answer", "warns"), [("no", True), ("yes", False)])
def test_setup_on_linux_loads_the_unit_and_warns_only_when_lingering_is_off(
    kb: Kb, daemon_up: int, unit_enabled: Callable[[], bool],
    monkeypatch: pytest.MonkeyPatch, answer: str, warns: bool,
) -> None:  # fmt: skip
    monkeypatch.setenv("LOGINCTL_FAKE_LINGER", answer)
    code, _, err = kb("setup")
    assert code == 0, err
    assert unit_enabled() and systemd.unit_path().exists()
    assert ("loginctl enable-linger" in err) is warns
    assert f"systemd unit {service.LABEL} loaded from {systemd.unit_path()}" in err


def test_doctor_on_linux_reads_the_unit_for_the_service_checks(kb: Kb, daemon_up: int) -> None:
    code, _, err = kb("setup")
    assert code == 0, err
    _, out, _ = kb("doctor", "--json")
    checks = {c["n"]: c for c in json.loads(out)["checks"]}
    assert [checks[n]["status"] for n in (1, 12, 13, 17)] == ["ok"] * 4, checks
    assert checks[1]["detail"] == UNIT
    assert os.access(systemd.read_entry() or "", os.X_OK)
