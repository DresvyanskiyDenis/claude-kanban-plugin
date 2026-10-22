"""TST-I: what a teammate's install has to get right, apart from setup itself (test_setup.py)."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from kanban_claude import install
from kanban_claude.errors import Error

from .conftest import Calls

PLUGIN = Path(__file__).resolve().parents[2]
BOOTSTRAP = PLUGIN / "skills" / "setup" / "scripts" / "bootstrap.sh"
KANBAN_SCRIPT = """#!/bin/sh
case "$1" in
    --version) echo "kanban ${FAKE_KANBAN_VERSION:-1.2.3}" ;;
    *) echo "$@" >> "$KANBAN_FAKE_LOG" ;;
esac
"""
COMPACT = '{"name":"kanban-claude","version":"1.2.3"}\n'
PRETTY = '{\n  "name": "kanban-claude",\n  "version": "1.2.3",\n  "description": "x"\n}\n'


@pytest.fixture
def plugin(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plugin root as the marketplace cache would hold it, and the `kanban` uv would install."""
    root = env / "cache" / "kanban-claude"
    scripts = root / "skills" / "setup" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "bootstrap.sh").write_text(BOOTSTRAP.read_text())
    (root / "runtime").mkdir()
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(COMPACT)
    entry = Path.home() / ".local" / "bin" / "kanban"
    entry.parent.mkdir(parents=True)
    entry.write_text(KANBAN_SCRIPT)
    entry.chmod(0o755)
    monkeypatch.setenv("KANBAN_FAKE_LOG", str(env / "kanban.log"))
    return root


def bootstrap(plugin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    script = plugin / "skills" / "setup" / "scripts" / "bootstrap.sh"
    return subprocess.run(["sh", str(script), *args], capture_output=True, text=True, check=False)


def test_bootstrap_installs_the_shipped_runtime_and_hands_over_to_its_setup(
    plugin: Path, env: Path, uv_calls: Calls
) -> None:
    p = bootstrap(plugin, "--no-daemon", "--yes")
    assert p.returncode == 0, p.stderr
    installs = [c for c in uv_calls() if c[:2] == ["tool", "install"]]
    assert installs == [["tool", "install", "--force", "--quiet", f"{plugin}/runtime"]]
    assert (env / "kanban.log").read_text().split() == ["setup", "--no-daemon", "--yes"]


@pytest.mark.parametrize("manifest", [COMPACT, PRETTY], ids=["compact", "pretty-printed"])
def test_bootstrap_reads_the_manifest_version_in_either_layout(plugin: Path, manifest: str) -> None:
    (plugin / ".claude-plugin" / "plugin.json").write_text(manifest)
    assert bootstrap(plugin).returncode == 0


def test_bootstrap_accepts_a_pre_release_version(
    plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"version":"0.2.0.dev0"}\n')
    monkeypatch.setenv("FAKE_KANBAN_VERSION", "0.2.0.dev0")
    assert bootstrap(plugin).returncode == 0


def test_bootstrap_fails_loud_when_the_installed_version_is_not_the_plugins(
    plugin: Path, env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_KANBAN_VERSION", "9.9.9")
    p = bootstrap(plugin)
    assert p.returncode == 1 and "kanban 9.9.9" in p.stderr and "1.2.3" in p.stderr
    assert not (env / "kanban.log").exists()


def test_bootstrap_without_a_manifest_version_stops_before_installing(
    plugin: Path, uv_calls: Calls
) -> None:
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"kanban-claude"}\n')
    p = bootstrap(plugin)
    assert p.returncode == 1 and "no version" in p.stderr
    assert uv_calls() == []


def test_bootstrap_names_the_fix_when_uv_is_missing(
    plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = bootstrap(plugin)
    assert p.returncode == 1 and "brew install uv" in p.stderr


def test_bootstrap_is_executable() -> None:
    assert BOOTSTRAP.stat().st_mode & stat.S_IXUSR


def test_the_shipped_files_name_no_absolute_user_path() -> None:
    """I1: a plugin cache path or a home directory baked into the package breaks on the next Mac."""
    shipped = [
        p
        for top in ("runtime/src", "skills", "hooks")
        for p in (PLUGIN / top).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    assert shipped
    home_prefix = "/".join(["", "Users", ""])  # spelled out in pieces so this file is not a hit
    assert [str(p) for p in shipped if home_prefix in p.read_text(errors="replace")] == []


def test_check_host_accepts_macos_and_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(install.sys, "platform", platform)
        install.check_host()


def test_check_host_refuses_a_platform_with_no_service_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install.sys, "platform", "win32")
    with pytest.raises(Error, match="macOS and Linux only"):
        install.check_host()


def test_check_host_on_linux_without_systemctl_names_no_daemon_as_the_way_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = shutil.which
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda cmd: None if cmd == "systemctl" else real(cmd))
    with pytest.raises(Error, match="--no-daemon"):
        install.check_host()
    install.check_host(no_daemon=True)
