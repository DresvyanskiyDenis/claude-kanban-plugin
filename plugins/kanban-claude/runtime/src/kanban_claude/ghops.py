"""`gh` helpers. The repository always comes from the board's origin, never from a worker (GIT-11)."""

import json
import subprocess
from pathlib import Path
from typing import Any

from . import gitops
from .errors import Error


def gh_rc(*args: str, timeout: int = 60) -> "subprocess.CompletedProcess[str]":
    """Run `gh`; a non-zero exit is the caller's to read (`pr checks` exits 8 while pending)."""
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"gh failed: {e}") from e


def gh(*args: str, timeout: int = 60) -> str:
    p = gh_rc(*args, timeout=timeout)
    if p.returncode != 0:
        raise Error(f"gh {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout


def auth_status(host: str) -> str:
    """What `gh auth status` says about `host` (stdout or stderr, depending on the gh version)."""
    try:
        p = subprocess.run(
            ["gh", "auth", "status", "--hostname", host], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"gh failed: {e}") from e
    out = (p.stdout + p.stderr).strip()
    if p.returncode != 0:
        raise Error(out[:300] or f"gh auth status failed for {host}")
    return out


def pr_view(
    repo: str, number: int, fields: str = "url,state,headRefName,baseRefName"
) -> dict[str, Any]:
    out = gh("pr", "view", str(number), "-R", repo, "--json", fields)
    try:
        data = json.loads(out)
    except ValueError as e:
        raise Error("gh returned unreadable JSON for the PR") from e
    if not isinstance(data, dict):
        raise Error("gh returned unreadable JSON for the PR")
    return data


def pr_list(repo: str, head: str, base: str) -> list[dict[str, Any]]:
    """The open PRs from `head` into `base`: GitHub allows one, so more than one is a problem."""
    out = gh("pr", "list", "-R", repo, "--head", head, "--base", base, "--state", "open",
             "--json", "number,url")  # fmt: skip
    try:
        data = json.loads(out)
    except ValueError as e:
        raise Error("gh returned unreadable JSON for the PR list") from e
    if not isinstance(data, list):
        raise Error("gh returned unreadable JSON for the PR list")
    return data


def repo_default_branch(repo: str) -> str:
    try:
        return str(
            json.loads(gh("repo", "view", repo, "--json", "defaultBranchRef"))["defaultBranchRef"][
                "name"
            ]
        )
    except (ValueError, KeyError, TypeError) as e:
        raise Error("gh returned unreadable JSON for the repository") from e


METHOD_FLAGS = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}


def merge(repo: str, number: int, head_oid: str, method: str = "squash") -> None:
    """REV-05: merge by `method` (squash unless the card says otherwise), only if the head is still
    the approved commit; remote branch deleted."""
    gh("pr", "merge", str(number), "-R", repo, METHOD_FLAGS[method], "--match-head-commit", head_oid,
       "--delete-branch", timeout=120)  # fmt: skip


def default_branch(root: str | Path) -> str | None:
    """origin/HEAD first (GIT-03), then the host's default branch."""
    local = gitops.local_default_branch(root)
    if local:
        return local
    url = gitops.origin_url(root)
    if not url:
        return None
    try:
        return repo_default_branch(gitops.repo_slug(url))
    except Error:
        return None
