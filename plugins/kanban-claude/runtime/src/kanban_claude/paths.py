"""Directories and modes (spec 5.5, SEC-06)."""

import contextlib
import logging
import logging.handlers
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


def add_file_log(logger: logging.Logger, path: Path) -> logging.Handler:
    """Log `logger` at INFO to a rotating file (10 MiB, 3 backups); its directory is made 0700 and the
    file 0600, also when launchd created it first under its own umask (as `StandardOutPath`)."""
    ensure_dir(path.parent)
    path.touch(mode=0o600)
    path.chmod(0o600)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def write_private(path: Path, content: bytes) -> None:
    """Write `path` 0600 from the first byte and swap it in whole, so no reader sees it half-written."""
    tmp = path.with_name(path.name + ".new")
    tmp.unlink(missing_ok=True)
    with os.fdopen(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
        f.write(content)
    tmp.replace(path)


def run_files(board: str, task: str, n: int) -> tuple[Path, Path]:
    """The runner log and the raw stream sidecar of run `n` (RUN-18); derived, never read back."""
    home = dirs().home
    return (
        home / "runs" / board / task / f"run-{n}.log",
        home / "streams" / board / task / f"run-{n}.jsonl",
    )


def card_tmp(task: str) -> Path:
    """TMPDIR of a card's workers: one per card, so a resumed run finds its files and no card
    writes into another's. Under KANBAN_WORK, because the workers' `Edit(KANBAN_HOME/**)` deny
    also blocks the Write tool."""
    return dirs().work / "tmp" / task


def nudge() -> None:
    """Wake the dispatcher's tick early (DSP-01); a failure only costs one tick interval."""
    with contextlib.suppress(OSError):
        (dirs().home / "nudge").touch()
