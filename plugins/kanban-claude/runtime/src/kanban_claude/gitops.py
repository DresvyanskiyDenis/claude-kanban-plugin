"""Hardened git calls (GIT-11): no hooks, no fsmonitor, no prompts, argv lists only."""

import contextlib
import fcntl
import hashlib
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from .errors import Error
from .paths import dirs, ensure_dir

LOCK = "kanban-provision.lock"
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


class ProvisionError(Error):
    """A workspace that cannot be provided. `infra` marks a fault of the machine, not the card."""

    def __init__(self, message: str, *, infra: bool = False) -> None:
        super().__init__(message)
        self.infra = infra


def _exists(root: str | Path, ref: str) -> bool:
    return run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", root=root)[0] == 0


def _exclude(root: str | Path) -> None:
    """GIT-04: keep `.worktrees/` out of the repo's status, locally and never committed."""
    if run("check-ignore", "-q", ".worktrees/", root=root)[0] == 0:
        return
    rc, out = run("rev-parse", "--path-format=absolute", "--git-path", "info/exclude", root=root)
    if rc != 0:
        raise ProvisionError(f"cannot find the exclude file of {root}: {out}")
    f = Path(out)
    f.parent.mkdir(parents=True, exist_ok=True)
    text = f.read_text() if f.exists() else ""
    f.write_text(text + ("" if not text or text.endswith("\n") else "\n") + ".worktrees/\n")


def _fast_forward(path: Path, start: str) -> None:
    """GIT-03: a clean branch strictly behind `start` moves up; anything else is left alone."""
    if run("status", "--porcelain", root=path)[1]:
        return
    if run("merge-base", "--is-ancestor", "HEAD", start, root=path)[0] == 0:
        run("merge", "--ff-only", start, root=path)


@contextlib.contextmanager
def _serialised(root: str | Path) -> Iterator[None]:
    """One provisioning per repository at a time: `worktree prune` and the exclude file are shared."""
    rc, out = run("rev-parse", "--path-format=absolute", "--git-common-dir", root=root)
    if rc != 0:
        raise ProvisionError(f"cannot find the git directory of {root}: {out}")
    with (Path(out) / LOCK).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def provision(root: str | Path, task_id: str, branch: str, base: str) -> Path:
    """GIT-02..05: the card's worktree `<root>/.worktrees/<id>`, a new branch cut from origin.

    A git call that cannot run at all (timeout, no binary, unwritable files) is the machine's
    fault, not the card's: `infra`.
    """
    try:
        if toplevel(root) is None:
            raise ProvisionError(f"{root} is not a git repository")
        with _serialised(root):
            return _provision(root, task_id, branch, base)
    except ProvisionError:
        raise
    except (Error, OSError) as e:
        raise ProvisionError(str(e), infra=True) from e


def _provision(root: str | Path, task_id: str, branch: str, base: str) -> Path:
    if origin_url(root) is None:
        raise ProvisionError(f"{root} has no origin remote")
    rc, out = run("fetch", "origin", "--prune", root=root, timeout=120)
    if rc != 0:
        raise ProvisionError(f"git fetch origin failed: {out}", infra=True)
    _exclude(root)
    start = f"refs/remotes/origin/{base}"
    if not _exists(root, start):
        raise ProvisionError(f"base branch {base!r} does not exist on origin")
    path = Path(root) / ".worktrees" / task_id
    if path.exists():
        rc, head = run("symbolic-ref", "--short", "HEAD", root=path)
        if rc != 0 or head != branch:
            raise ProvisionError(
                f"{path} is on {head if rc == 0 else 'a detached HEAD'}, not the card branch "
                f"{branch}; it would run this card on another card's branch"
            )
        _fast_forward(path, start)
        return path
    run("worktree", "prune", root=root)
    if _exists(root, f"refs/heads/{branch}"):
        add = ["worktree", "add", "--", str(path), branch]
    elif _exists(root, f"refs/remotes/origin/{branch}"):
        add = ["worktree", "add", "--track", "-b", branch, "--", str(path)]
        add.append(f"refs/remotes/origin/{branch}")
    else:
        add = ["worktree", "add", "--no-track", "-b", branch, "--", str(path), start]
    rc, out = run(*add, root=root, timeout=120)
    if rc != 0:
        raise ProvisionError(f"git worktree add failed for {branch}: {out}")
    if add[-1] == branch:
        _fast_forward(path, start)
    return path


def cleanup(root: str | Path, task_id: str, branch: str | None, *, merged: bool) -> str:
    """GIT-06: remove a finished card's worktree if clean and pushed (or merged); else say why not.

    Returns `removed`, `kept_dirty` or `kept_unpushed`. The local branch goes only when merged.
    """
    path = Path(root) / ".worktrees" / task_id
    if path.is_dir():
        if run("status", "--porcelain", root=path)[1]:
            return "kept_dirty"
        pushed = run("rev-list", "--count", "@{u}..HEAD", root=path) == (0, "0")
        if not (merged or pushed):
            return "kept_unpushed"
        if run("worktree", "remove", "--", str(path), root=root)[0] != 0:
            return "kept_dirty"
    else:
        run("worktree", "prune", root=root)
    if merged and branch:
        run("branch", "-D", "--", branch, root=root)
    return "removed"


def snapshot(repo: str | Path) -> tuple[Path, str | None]:
    """CHK-03: (path, warning). A detached checkout of origin/HEAD, or the working tree itself."""
    top = toplevel(repo)
    if top is None:
        return Path(repo).resolve(), "not a git repository; using the directory as it is"
    rc, out = run("fetch", "-q", "origin", root=top, timeout=120)
    warning = None if rc == 0 else f"git fetch failed ({out}); the snapshot may be stale"
    head = "refs/remotes/origin/HEAD"
    if not _exists(top, head):
        return top, "no origin/HEAD; using the working tree"
    snap = dirs().home / "snapshots" / hashlib.sha256(str(top).encode()).hexdigest()[:16]
    if snap.is_dir():
        rc, out = run("checkout", "--detach", head, root=snap)
    else:
        ensure_dir(snap.parent)
        rc, out = run("worktree", "add", "--detach", "--", str(snap), head, root=top)
    if rc != 0:
        raise Error(f"cannot update the snapshot {snap}: {out}")
    return snap, warning
