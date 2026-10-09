"""`gh` helpers. The repository always comes from the board's origin, never from a worker (GIT-11)."""

import json
import subprocess
from pathlib import Path
from typing import Any

from . import gitops
from .errors import Error


def gh(*args: str, timeout: int = 60) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"gh failed: {e}") from e
    if p.returncode != 0:
        raise Error(f"gh {args[0]} failed: {p.stderr.strip()[:300]}")
    return p.stdout


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


def repo_default_branch(repo: str) -> str:
    try:
        return str(
            json.loads(gh("repo", "view", "-R", repo, "--json", "defaultBranchRef"))[
                "defaultBranchRef"
            ]["name"]
        )
    except (ValueError, KeyError, TypeError) as e:
        raise Error("gh returned unreadable JSON for the repository") from e


def merge(repo: str, number: int, head_oid: str) -> None:
    """REV-05: squash, only if the head is still the approved commit; remote branch deleted."""
    gh("pr", "merge", str(number), "-R", repo, "--squash", "--match-head-commit", head_oid,
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
