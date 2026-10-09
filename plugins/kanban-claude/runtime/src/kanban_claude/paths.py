"""Directories and modes (spec 5.5, SEC-06)."""

import os
from pathlib import Path
from typing import NamedTuple


class Dirs(NamedTuple):
    home: Path
    work: Path


def dirs() -> Dirs:
    def pick(var: str, default: str) -> Path:
        return Path(os.environ.get(var) or Path.home() / default).expanduser()

    return Dirs(
        pick("KANBAN_HOME", ".local/share/kanban-claude"),
        pick("KANBAN_WORK", ".local/share/kanban-claude-work"),
    )


def ensure_dir(path: Path) -> Path:
    """Create `path` (and parents) as 0700 and force the mode on the leaf."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path
