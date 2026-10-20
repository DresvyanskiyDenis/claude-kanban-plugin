"""Input validation shared by every owner path and the worker verbs (SEC-12, SEC-10)."""

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from . import gitops
from .errors import Refused
from .paths import dirs

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{8}$")
MODEL_RE = re.compile(r"^[a-z0-9][a-z0-9.\-\[\]]{0,63}$")
SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9:\-_]{0,127}$")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_DURATION_RE = re.compile(r"^(\d+)([smh])$")
_UNITS = {"s": 1, "m": 60, "h": 3600}


def slug(value: str) -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise Refused(f"board slug {value!r} must match {SLUG_RE.pattern}")
    return value


def task_id(value: str) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise Refused(f"task id {value!r} must look like t_<8 hex>")
    return value


def model(value: str) -> str:
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        raise Refused(f"model {value!r} must match {MODEL_RE.pattern}")
    return value


def effort(value: str) -> str:
    if value not in EFFORTS:
        raise Refused(f"effort {value!r} must be one of {', '.join(EFFORTS)}")
    return value


def budget(value: object, ceiling: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= ceiling:
        raise Refused(f"budget {value!r} must be > 0 and <= budget.ceiling ({ceiling})")
    return float(value)


def skills(values: object) -> list[str]:
    if not isinstance(values, list | tuple):
        raise Refused("skills must be a list")
    for v in values:
        if not isinstance(v, str) or not SKILL_RE.fullmatch(v):
            raise Refused(f"skill {v!r} must match {SKILL_RE.pattern}")
    return list(values)


def touches(values: object) -> list[str]:
    if not isinstance(values, list | tuple) or not values:
        raise Refused("touches must be a non-empty list of globs; clear it with --touches ''")
    for v in values:
        if not isinstance(v, str) or not v.strip():
            raise Refused(f"touches glob {v!r} must be a non-empty string")
    return [v.strip() for v in values]


def duration(value: str | int) -> int:
    """`30m`, `2h`, `90s` or plain seconds."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    m = _DURATION_RE.fullmatch(str(value))
    if not m or int(m[1]) == 0:
        raise Refused(f"duration {value!r} must look like 30m, 2h or 90s")
    return int(m[1]) * _UNITS[m[2]]


def branch(name: str, default: str | None = None) -> str:
    if not gitops.valid_branch(name):
        raise Refused(f"branch {name!r} is not a valid branch name (git check-ref-format)")
    if name in ("HEAD", default):
        raise Refused(
            f"branch {name!r} is the default branch or HEAD; pass --branch feature/<slug>"
        )
    return name


def pr_url(url: str, hosts: set[str]) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in hosts:
        raise Refused(
            f"pr_url {url!r} must be https on a known git host ({', '.join(sorted(hosts))})"
        )
    return url


def parse_workspace(spec: str) -> tuple[str, str]:
    kind, _, path = spec.partition(":")
    if kind not in ("worktree", "dir") or not os.path.isabs(path):
        raise Refused(f"workspace {spec!r} must be worktree:<abs path> or dir:<abs path>")
    return kind, path


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def workspace(
    kind: str, path: str, *, claude_dirs: tuple[Path, ...] = (), strict: bool = True
) -> str:
    """SEC-10. `strict` adds the existence and git checks done once a card is dispatchable."""
    p = Path(os.path.realpath(path))
    home = Path.home().resolve()
    d = dirs()
    forbidden = [d.home.resolve(), *(c.resolve() for c in claude_dirs)]
    forbidden += [home / ".ssh", home / ".gnupg", home / "Library"]
    if p == home or any(_under(p, f) for f in forbidden):
        raise Refused(f"workspace {path!r} is a protected location; choose a project directory")
    if not strict:
        return str(p)
    if not p.is_dir():
        raise Refused(f"workspace {path!r} does not exist or is not a directory")
    if kind == "worktree" and (gitops.toplevel(p) != p or not gitops.origin_url(p)):
        raise Refused(f"workspace {path!r} must be a git toplevel with an origin remote")
    return str(p)
