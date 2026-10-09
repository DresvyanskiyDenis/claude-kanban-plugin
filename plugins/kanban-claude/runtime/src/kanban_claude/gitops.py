"""Hardened git calls (GIT-11): no hooks, no fsmonitor, no prompts, argv lists only."""

import os
import re
import subprocess
from pathlib import Path

from .errors import Error

GIT = ("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
_URL = re.compile(
    r"^(?:(?:ssh|https?|git)://(?:[^@/]+@)?|[^@/:]+@)(?P<host>[^/:]+)[:/](?:\d+/)?(?P<path>.+?)(?:\.git)?/?$"
)


def run(*args: str, root: str | Path | None = None, timeout: int = 60) -> tuple[int, str]:
    argv = [*GIT, *(("-C", str(root)) if root else ()), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"git failed: {e}") from e
    return p.returncode, p.stdout.strip() if p.returncode == 0 else p.stderr.strip()


def toplevel(path: str | Path) -> Path | None:
    rc, out = run("rev-parse", "--show-toplevel", root=path)
    return Path(out).resolve() if rc == 0 and out else None


def origin_url(root: str | Path) -> str | None:
    rc, out = run("remote", "get-url", "origin", root=root)
    return out if rc == 0 and out else None


def repo_slug(url: str) -> str:
    """`HOST/OWNER/REPO` for `gh -R`, from any origin URL form."""
    m = _URL.match(url.strip())
    if not m:
        raise Error(f"cannot read a repository from the origin URL {url!r}")
    return f"{m['host']}/{m['path']}"


def valid_branch(name: str) -> bool:
    if not name or name.startswith("-"):
        return False
    rc, out = run("check-ref-format", "--branch", name)
    return rc == 0 and out == name


def local_default_branch(root: str | Path) -> str | None:
    rc, out = run("symbolic-ref", "--short", "refs/remotes/origin/HEAD", root=root)
    return out.removeprefix("origin/") if rc == 0 and out.startswith("origin/") else None


def commits_ahead(root: str | Path, base: str, branch: str) -> int:
    """Commits on `branch` that are not on `origin/<base>`; 0 if the branch does not exist."""
    if run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", root=root)[0] != 0:
        return 0
    rc, out = run(
        "rev-list", "--count", f"refs/remotes/origin/{base}..refs/heads/{branch}", root=root
    )
    if rc != 0:
        raise Error(f"cannot compare {branch} with origin/{base}: {out}")
    return int(out)
