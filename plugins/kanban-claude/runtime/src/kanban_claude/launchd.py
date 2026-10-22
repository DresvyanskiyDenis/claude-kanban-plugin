"""The LaunchAgent (INS-06): render the plist, load it with `launchctl` in the user's GUI domain."""

import os
import plistlib
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import Error
from .paths import write_private
from .service import LABEL, mask

NOUN = "LaunchAgent"
FILE = "plist"
BOOTOUT_WAIT_S = 10.0


def render(kanban: str, env: Mapping[str, str], home: Path, work: Path) -> bytes:
    log = str(home / "logs" / "daemon.log")
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [kanban, "daemon"],
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ProcessType": "Standard",
            "LimitLoadToSessionType": "Aqua",
            "AbandonProcessGroup": True,
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {**env, "KANBAN_HOME": str(home), "KANBAN_WORK": str(work)},
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
    )


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def install_plist(content: bytes) -> Path:
    """Write the plist 0600 (it may carry proxy credentials) and swap it in whole."""
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, content)
    return path


def read_plist() -> dict[str, Any] | None:
    try:
        data = plistlib.loads(plist_path().read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


definition_path = plist_path


def read_env() -> dict[str, str] | None:
    plist = read_plist()
    return None if plist is None else dict(plist.get("EnvironmentVariables") or {})


def read_entry() -> str | None:
    plist = read_plist()
    return None if plist is None else (plist.get("ProgramArguments") or [""])[0]


def target() -> str:
    return f"gui/{os.getuid()}/{LABEL}"


def launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"launchctl {args[0]} failed: {e}") from e


def checked(*args: str) -> None:
    p = launchctl(*args)
    if p.returncode != 0:
        raise Error(f"launchctl {args[0]} failed: {mask(p.stderr.strip() or p.stdout.strip())}")


def loaded() -> bool:
    return launchctl("print", target()).returncode == 0


def bootstrap(plist: Path) -> None:
    checked("bootstrap", f"gui/{os.getuid()}", str(plist))


def bootout() -> None:
    """Unload the agent and wait until launchd has let go of it, so a bootstrap can follow."""
    if not loaded():
        return
    checked("bootout", target())
    deadline = time.monotonic() + BOOTOUT_WAIT_S
    while loaded():
        if time.monotonic() > deadline:
            raise Error(
                f"launchd still holds {LABEL} after bootout; run: launchctl bootout {target()}"
            )
        time.sleep(0.2)


def load(entry: str, env: Mapping[str, str], home: Path, work: Path) -> Path:
    """Write the plist for `env` and load it, replacing a loaded agent; returns the plist."""
    plist = install_plist(render(entry, env, home, work))
    bootout()
    bootstrap(plist)
    return plist


def kickstart() -> None:
    checked("kickstart", "-k", target())


def restart() -> None:
    """Run the new code: kick the loaded agent, or load the plist that is on disk."""
    if loaded():
        kickstart()
    elif plist_path().exists():
        bootstrap(plist_path())
    else:
        raise Error(
            f"the LaunchAgent {LABEL} is not installed; repair it with: kanban doctor --fix"
        )
