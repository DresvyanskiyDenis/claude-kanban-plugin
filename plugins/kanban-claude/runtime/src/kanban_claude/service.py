"""The daemon's service manager (INS-06): launchd on macOS, a systemd user unit elsewhere.

What both backends share lives here; `backend()` picks one at call time, so a test can
switch `sys.platform` after import."""

import os
import re
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Protocol

LABEL = "kanban-claude.daemon"
CAPTURED = (
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy", "LANG",
)  # fmt: skip
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
SECRET_NAME = re.compile("TOKEN|KEY|SECRET|PASSWORD|AUTH", re.IGNORECASE)
USERINFO = re.compile(r"(://)[^/@\s]*@")


def darwin() -> bool:
    """A function, so mypy checks both platforms' branches instead of narrowing on the host's."""
    return sys.platform == "darwin"


def mask(text: str) -> str:
    """A proxy URL's `user:pass@` is kept in the service file, because the proxy needs it, and never logged."""
    return USERINFO.sub(r"\1***@", text)


def capture_env(
    shell: Mapping[str, str], passthrough: Iterable[str], tool_dirs: Iterable[str]
) -> dict[str, str]:
    """INS-05 step 3 under SEC-09: the fixed names, plus any the owner lists in
    `worker.env_passthrough`, minus every name that looks like a credential unless listed."""
    listed = set(passthrough)
    path = [*dict.fromkeys(tool_dirs), *SYSTEM_PATH]
    env = {"PATH": os.pathsep.join(dict.fromkeys(path))}
    for name in (*CAPTURED, *listed):
        if name in shell and (name in listed or not SECRET_NAME.search(name)):
            env[name] = shell[name]
    return env


class Backend(Protocol):
    """What `launchd` and `systemd` both provide; `definition_path()` is the plist or the unit file."""

    NOUN: str  # what the owner calls it in a message: "LaunchAgent" or "systemd unit"
    FILE: str  # its definition file: "plist" or "unit file"

    def definition_path(self) -> Path: ...
    def target(self) -> str: ...
    def loaded(self) -> bool: ...
    def load(self, entry: str, env: Mapping[str, str], home: Path, work: Path) -> Path: ...
    def bootstrap(self, definition: Path) -> None: ...
    def bootout(self) -> None: ...
    def restart(self) -> None: ...
    def read_env(self) -> dict[str, str] | None: ...
    def read_entry(self) -> str | None: ...


def backend() -> Backend:
    from . import launchd, systemd

    return launchd if darwin() else systemd
