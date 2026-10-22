"""The systemd user unit (INS-06): render the unit, load it with `systemctl --user`."""

import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from .errors import Error
from .paths import write_private
from .service import LABEL, mask

NOUN = "systemd unit"
FILE = "unit file"
UNIT = f"{LABEL}.service"


def esc(value: str) -> str:
    """`%` starts a specifier in a unit file."""
    return value.replace("%", "%%")


def quote(value: str) -> str:
    return '"' + esc(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote(word: str) -> str:
    return re.sub(r"\\(.)", r"\1", word[1:-1]).replace("%%", "%")


def render(kanban: str, env: Mapping[str, str], home: Path, work: Path) -> bytes:
    """`KillMode=process` is launchd's `AbandonProcessGroup`: the detached runners outlive a restart."""
    log = esc(str(home / "logs" / "daemon.log"))
    lines = []
    for name, value in {**env, "KANBAN_HOME": str(home), "KANBAN_WORK": str(work)}.items():
        if "\n" in value or "\r" in value:
            raise Error(f"{name} holds a line break; it cannot go into the unit file")
        lines.append(f"Environment={quote(f'{name}={value}')}")
    return "\n".join(
        [
            "[Unit]",
            f"Description={LABEL}",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={quote(kanban)} daemon",
            "Restart=on-failure",
            "RestartSec=10",
            "KillMode=process",
            f"WorkingDirectory={esc(str(home))}",
            f"StandardOutput=append:{log}",
            f"StandardError=append:{log}",
            *lines,
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    ).encode()


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT


definition_path = unit_path


def install_unit(content: bytes) -> Path:
    """Write the unit 0600 (it may carry proxy credentials) and swap it in whole."""
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, content)
    return path


def read_values(key: str) -> list[str] | None:
    """The right-hand sides of every `key=` line in the unit file, or None when it cannot be read."""
    try:
        text = unit_path().read_text()
    except OSError:
        return None
    return [ln.removeprefix(f"{key}=") for ln in text.splitlines() if ln.startswith(f"{key}=")]


def read_env() -> dict[str, str] | None:
    values = read_values("Environment")
    if values is None:
        return None
    pairs = (unquote(v).partition("=") for v in values)
    return {name: value for name, _, value in pairs}


def read_entry() -> str | None:
    values = read_values("ExecStart")
    if values is None:
        return None
    words = shlex.split(values[0]) if values else []
    return words[0].replace("%%", "%") if words else ""


def target() -> str:
    return UNIT


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"systemctl --user {' '.join(args)} failed: {e}") from e


def checked(*args: str) -> None:
    p = systemctl(*args)
    if p.returncode != 0:
        detail = mask(p.stderr.strip() or p.stdout.strip())
        raise Error(f"systemctl --user {' '.join(args)} failed: {detail}")


def loaded() -> bool:
    """False on a host without systemctl, so uninstall and doctor work after `setup --no-daemon`."""
    if not shutil.which("systemctl"):
        return False
    return systemctl("is-enabled", UNIT).returncode == 0


def bootstrap(definition: Path) -> None:
    """Enable and start the unit; systemd finds it by name, so `definition` (launchd's argument) is unused."""
    checked("daemon-reload")
    checked("enable", "--now", UNIT)


def bootout() -> None:
    """Stop and disable the unit; `disable --now` returns once it has stopped."""
    if loaded():
        checked("disable", "--now", UNIT)


def load(entry: str, env: Mapping[str, str], home: Path, work: Path) -> Path:
    """Write the unit for `env` and load it, replacing a loaded one; returns the unit file."""
    unit = install_unit(render(entry, env, home, work))
    bootout()
    bootstrap(unit)
    return unit


def kickstart() -> None:
    checked("restart", UNIT)


def restart() -> None:
    """Run the new code: restart the loaded unit, or load the unit file that is on disk."""
    if loaded():
        kickstart()
    elif unit_path().exists():
        bootstrap(unit_path())
    else:
        raise Error(
            f"the systemd unit {UNIT} is not installed; repair it with: kanban doctor --fix"
        )


def linger_off(user: str) -> bool:
    """True only when logind says the user's units die at logout; any other answer is not a warning."""
    try:
        p = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True, text=True, timeout=10,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.stdout.strip() == "Linger=no"
