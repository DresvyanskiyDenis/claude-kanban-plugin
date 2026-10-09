"""GIT-08: branch protection of the repositories the boards use. A warning, never a gate."""

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import ghops, gitops, store
from .errors import Error

PROTECTION_S = 6 * 3600
STATE_KEY = "protection"


def repos(conn: sqlite3.Connection) -> dict[str, dict[str, Path]]:
    """Per board, the repositories its cards use: `host/owner/repo` -> a checkout of it."""
    found: dict[str, dict[str, Path]] = {}
    places = [
        (r["slug"], r["default_workdir"])
        for r in conn.execute("SELECT slug, default_workdir FROM boards WHERE archived_at IS NULL")
    ] + [
        (r["board"], r["workspace_path"])
        for r in conn.execute(
            "SELECT DISTINCT board, workspace_path FROM tasks "
            "WHERE workspace_kind = 'worktree' AND status NOT IN ('done', 'archived')"
        )
    ]
    for board, path in places:
        with contextlib.suppress(Error):
            top = gitops.toplevel(path) if path else None
            url = gitops.origin_url(top) if top else None
            if top and url:
                found.setdefault(board, {})[gitops.repo_slug(url)] = top
    return found


def read(host: str, path: str) -> Any:
    """What `gh api` answers for `path`, or None when it cannot (no rights, no such branch)."""
    try:
        return json.loads(ghops.gh("api", "--hostname", host, path, timeout=20))
    except (Error, ValueError):
        return None


def protected(host: str, repo: str, branch: str) -> bool | None:
    """Rulesets and classic protection are two endpoints; either one counts. None means the
    answer is not known: a repo is only called unprotected when both answered and both said no."""
    rules = read(host, f"repos/{repo}/rules/branches/{branch}")
    classic = read(host, f"repos/{repo}/branches/{branch}")
    if bool(rules) or (isinstance(classic, dict) and classic.get("protected") is True):
        return True
    return False if isinstance(rules, list) and isinstance(classic, dict) else None


def branches(root: Path) -> list[str]:
    """The default branch, and the first remote `feature/*` branch if one exists (a pattern has no
    API path of its own). ponytail: one sample stands for the pattern; list them all when rulesets
    that differ per feature branch turn up."""
    names = []
    if default := ghops.default_branch(root):
        names.append(default)
    rc, out = gitops.run(
        "for-each-ref", "--count=1", "--format=%(refname:short)", "refs/remotes/origin/feature/",
        root=root,
    )  # fmt: skip
    if rc == 0 and out.startswith("origin/"):
        names.append(out.removeprefix("origin/"))
    return names


def check(slug: str, root: Path) -> bool | None:
    host, _, repo = slug.partition("/")
    answers = [protected(host, repo, b) for b in branches(root)]
    if False in answers:
        return False
    return None if None in answers or not answers else True


def refresh(conn: sqlite3.Connection) -> dict[str, Any]:
    """Ask the host about every repo once and persist who is unprotected, per board."""
    verdict: dict[str, bool | None] = {}
    by_board: dict[str, list[str]] = {}
    for board, mine in repos(conn).items():
        for slug, root in mine.items():
            if slug not in verdict:
                verdict[slug] = check(slug, root)
            if verdict[slug] is False:
                by_board.setdefault(board, []).append(slug)
    data = {
        "checked_at": store.now(),
        "boards": by_board,
        "unknown": sorted(s for s, v in verdict.items() if v is None),
    }
    with store.tx(conn):
        store.state_put(conn, STATE_KEY, data)
    return data


def publish(conn: sqlite3.Connection, into: dict[str, list[str]]) -> None:
    """Copy the persisted list into the daemon's map key by key, so the board's request threads
    never see it half-built."""
    boards = (store.state_get(conn, STATE_KEY) or {}).get("boards", {})
    for slug in set(into) - set(boards):
        into.pop(slug, None)
    into.update(boards)
