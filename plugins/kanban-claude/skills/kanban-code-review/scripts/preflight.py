#!/usr/bin/env python3
"""Review pre-flight: print the checkable facts about a card's PR, one per line.

Usage: preflight.py [PR number or URL]     (the card comes from $KANBAN_TASK)

Each line starts with ok, CHECK, TIER, SIZE or INFO. CHECK lines are for the reviewer to
resolve or turn into findings; nothing here decides a verdict. Stdlib only.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from collections.abc import Callable

OVERSIZED = 2000
DEEP_LINES, LIGHT_LINES = 500, 30
CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|refactor|test|chore|perf|build|ci|style|revert)(\([^)]*\))?!?: \S"
)
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$"
)
SENSITIVE = re.compile(
    r"auth|secret|crypt|token|passw|credential|permission|migrat|\.github/workflows|dockerfile|\.env|(^|/)hooks/",
    re.I,
)
DEP_FILES = re.compile(
    r"(^|/)(requirements[^/]*\.txt|pyproject\.toml|package\.json|Cargo\.toml|go\.mod|Gemfile)$"
)
DEP_LINE = re.compile(r"[=<>~^]=?\s*\d")
SECRETS = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(?i:(?:api[_-]?key|secret|token|passwd|password)\w*\s*[:=]\s*[\"'][^\"'\s]{12,}[\"'])"
)
HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿]")
HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
BORING_HOSTS = {"localhost", "127.0.0.1", "example.com", "example.org", "example.net"}
LEDGER = "REVIEW LEDGER @ "
APPROVED = re.compile(r"WARDEN: APPROVED PR #\d+ @\s*([0-9a-f]{7,40})\b")
PR_URL = re.compile(r"^https?://([^/]+/[^/]+/[^/]+)/pull/(\d+)")


def is_test(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def gate_fact(
    body: str, sha: str, merge_only_since: Callable[[str], bool] = lambda _: False
) -> list[str]:
    gates = [ln for ln in body.splitlines() if ln.lstrip().startswith("Gate:")]
    if not gates:
        return ["CHECK the PR body has no `Gate:` line"]
    named = [t for line in gates for t in re.findall(r"\b[0-9a-f]{7,40}\b", line)]
    if any(sha.startswith(t) for t in named):
        return ["ok the Gate line names the head commit"]
    for tok in named:
        if merge_only_since(tok):
            return [
                f"ok the Gate line names {tok[:7]}, an ancestor of the head {sha[:7]} with only merge commits since; "
                "a base merge needs no new local gate run (CI and the review gate still run on the head)"
            ]
    return [
        f"CHECK the Gate line does not name the head commit {sha[:7]}: {gates[0].strip()[:120]}"
    ]


_SETTLED = ("pass", "fail", "cancel", "pending")  # the buckets `checks_fact` words itself


def checks_fact(checks: list[dict[str, str]]) -> list[str]:
    if not checks:
        return ["INFO no CI checks reported for this PR"]
    bad = [c["name"] for c in checks if c["bucket"] in ("fail", "cancel")]
    pending = [c["name"] for c in checks if c["bucket"] == "pending"]
    other = [f"{c['name']} ({c['bucket']})" for c in checks if c["bucket"] not in _SETTLED]
    out = []
    if bad:
        out.append(f"CHECK CI failing on the head: {', '.join(bad)}")
    if pending:
        out.append(f"CHECK CI still running on the head: {', '.join(pending)}")
    if other:  # skipped, neutral or a bucket this script does not know is not a success
        out.append(f"CHECK CI not green on the head, these checks did not pass: {', '.join(other)}")
    return out or [f"ok CI green on the head ({len(checks)} checks)"]


def git_out(*args: str) -> str | None:
    r = call(["git", *args])
    return r.stdout.strip() if r.returncode == 0 else None


def own_headlines(commits: list[dict[str, str]]) -> list[str]:
    """Headlines of the PR's own commits: the base merges smith makes are left out."""
    own = git_out("rev-list", "--no-merges", "--no-walk=unsorted", *[c["oid"] for c in commits])
    if own is None:  # a commit is not in this clone: judge them all rather than none
        return [c["messageHeadline"] for c in commits]
    return [c["messageHeadline"] for c in commits if c["oid"] in own.split()]


def merge_only_since(old: str, head: str) -> bool:
    """`old` is an ancestor of `head` on its first-parent line and only merges came after it.

    First-parent matters: a base merge brings the base's own commits into `old..head`, but they
    sit on the second parent, so only the branch's own commits are counted.
    """
    full = git_out("rev-parse", "--verify", "--quiet", f"{old}^{{commit}}")
    if (
        not full
        or full == head
        or call(["git", "merge-base", "--is-ancestor", full, head]).returncode
    ):
        return False
    return git_out("rev-list", "--no-merges", "--first-parent", f"{full}..{head}") == ""


def pr_delta(rev: str, base_ref: str) -> str | None:
    """The PR's own change at `rev`: changed lines against its merge-base, without hunk positions."""
    mb = git_out("merge-base", rev, base_ref)
    out = git_out("diff", "--no-ext-diff", "--no-color", "-U0", mb, rev) if mb else None
    if out is None:
        return None
    return "\n".join(ln for ln in out.splitlines() if ln.startswith(("+", "-", "diff --git")))


def merge_only_fact(runs: list[dict[str, str]], head: str, base: str) -> list[str]:
    """INFO when the last approval is followed by merges only and the PR's own diff is unchanged."""
    shas = [
        m[1] for r in runs if r["kind"] == "review" and (m := APPROVED.search(r["summary"] or ""))
    ]
    if not shas or not merge_only_since(shas[-1], head):
        return []
    ref = f"origin/{base}"
    before = pr_delta(shas[-1], ref)
    if before is None or before != pr_delta(head, ref):
        return []
    return [
        f"INFO merge-only since APPROVED @ {shas[-1][:7]}: every commit since then is a merge "
        f"and the PR's own diff against {ref} is unchanged"
    ]


def nonconventional(headlines: list[str]) -> list[str]:
    bad = [h for h in headlines if not CONVENTIONAL.match(h)]
    if not bad:
        return ["ok commit messages are conventional"]
    return [
        f"CHECK {len(bad)} non-conventional commit message(s), minor: {'; '.join(b[:60] for b in bad[:3])}"
    ]


def tier(files: list[dict[str, object]]) -> list[str]:
    code = [f for f in files if not is_test(str(f["path"]))]
    lines = sum(int(str(f["additions"])) + int(str(f["deletions"])) for f in code)
    sensitive = [str(f["path"]) for f in code if SENSITIVE.search(str(f["path"]))]
    if sensitive or lines > DEEP_LINES:
        name, why = "deep", f"sensitive paths: {', '.join(sensitive[:5])}; " if sensitive else ""
    elif lines <= LIGHT_LINES:
        name, why = "light", ""
    else:
        name, why = "standard", ""
    size = f"{lines} non-test changed lines"
    if lines > OVERSIZED:
        return [
            f"TIER {name}: {why}{size}",
            f"SIZE oversized ({size}): review a stated subset and list it as PARTIAL REVIEW",
        ]
    return [f"TIER {name}: {why}{size}", f"SIZE {size}"]


def fix_needs_test(title: str, files: list[dict[str, object]]) -> list[str]:
    if not title.startswith("fix") or any(is_test(str(f["path"])) for f in files):
        return []
    return ["CHECK a fix PR that changes no test file; ask for the failing test that came first"]


def simplify_rules(title: str, files: list[dict[str, object]]) -> list[str]:
    if not title.startswith("refactor: simplify"):
        return []
    add = sum(int(str(f["additions"])) for f in files)
    delete = sum(int(str(f["deletions"])) for f in files)
    touched = [str(f["path"]) for f in files if is_test(str(f["path"]))]
    out = []
    if add >= delete:
        out.append(f"CHECK simplify PR does not shrink the code (+{add}/-{delete})")
    if touched:
        out.append(f"CHECK simplify PR changes test files: {', '.join(touched[:5])}")
    return out or [f"ok simplify PR shrinks the code (+{add}/-{delete}) and leaves tests alone"]


def out_of_scope(card_body: str, paths: list[str]) -> list[str]:
    m = re.search(r"^## Out of scope[ \t]*\n(.*?)(?=^## |\Z)", card_body, re.M | re.S)
    if not m:
        return []
    hits = []
    for tok in re.findall(r"`([^`\n]+)`", m[1]):
        t = tok.strip().rstrip("/")
        hits += [
            f"{p} (`{tok}`)"
            for p in paths
            if t and (p == t or p.startswith(t + "/") or fnmatch.fnmatch(p, t))
        ]
    return (
        [f"CHECK changed paths inside the card's Out of scope: {'; '.join(hits[:8])}"]
        if hits
        else []
    )


def scan_added(diff: str) -> list[str]:
    """Signs worth a human look in the added lines of a unified diff."""
    found: list[str] = []
    path, added, removed = "?", [], []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
        elif line.startswith("+"):
            added.append((path, line[1:]))
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    old_hosts = {h for line in removed for h in HOST.findall(line)}
    new_hosts: dict[str, str] = {}
    for path, line in added:
        if DEP_FILES.search(path) and DEP_LINE.search(line):
            found.append(f"CHECK dependency line added in {path}: {line.strip()[:80]}")
        if (m := SECRETS.search(line)) is not None:
            found.append(f"CHECK secret-like string in {path}: starts {m[0][:4]!r}")
        if (m := HIDDEN.search(line)) is not None:
            found.append(f"CHECK hidden character U+{ord(m[0]):04X} in {path}")
        for host in HOST.findall(line):
            if host not in old_hosts and host not in BORING_HOSTS:
                new_hosts.setdefault(host, path)
    found += [f"CHECK new URL host {h} in {p}" for h, p in list(new_hosts.items())[:8]]
    return found[:20] or [
        "ok added lines show no dependency, secret, hidden-character or new-host signs"
    ]


def last_ledger(comments: list[dict[str, str]]) -> tuple[int, str | None]:
    """(this review's round number, head sha of the previous ledger)."""
    ledgers = [c["body"].lstrip() for c in comments if c["body"].lstrip().startswith(LEDGER)]
    return len(ledgers) + 1, (ledgers[-1].split()[3] if ledgers else None)


def pr_ref(arg: str | None, task: dict[str, object]) -> tuple[str, str | None] | None:
    """(PR number, `host/owner/repo` for -R or None) from the argument, else from the card."""
    for ref in (arg, task.get("pr_url")):
        if m := PR_URL.match(str(ref or "")):
            return m[2], m[1]
    number = arg if arg and arg.isdigit() else task.get("pr_number")
    return (str(number), None) if number else None


def call(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def gh_json(argv: list[str]) -> object:
    r = call(["gh", *argv])
    if r.returncode:
        raise RuntimeError(
            f"gh {' '.join(argv[:2])} failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    return json.loads(r.stdout)


def pr_checks(number: str, scope: list[str]) -> object:
    """Every check on the head, required or not: the review skips its gate re-run only when all of
    them passed. `gh pr checks` exits 1 (failing) or 8 (pending) with the JSON on stdout, and exits
    1 with "no checks reported" instead of printing [] when the repo has no CI at all."""
    r = call(["gh", "pr", "checks", number, *scope, "--json", "name,bucket"])
    try:
        return json.loads(r.stdout)
    except ValueError:
        if "no checks reported" in r.stderr + r.stdout:
            return []
        raise RuntimeError(f"gh pr checks failed: {(r.stderr or r.stdout).strip()}") from None


def main(argv: list[str]) -> int:
    shown = call(["kanban", "show", "--json"])
    if shown.returncode:
        print(
            f"cannot read the card: {shown.stderr.strip() or shown.stdout.strip()}", file=sys.stderr
        )
        return 2
    card = json.loads(shown.stdout)
    task, comments = card["task"], card["comments"]
    ref = pr_ref(argv[0] if argv else None, task)
    if ref is None:
        print("CHECK the card names no PR; pass a PR number or URL")
        return 0
    number, repo = ref
    scope = ["-R", repo] if repo else []
    try:
        pr = gh_json(
            [
                "pr",
                "view",
                number,
                *scope,
                "--json",
                "number,title,body,headRefName,baseRefName,headRefOid,commits,files",
            ]
        )
        assert isinstance(pr, dict)
        checks = pr_checks(number, scope)
        diff = call(["gh", "pr", "diff", number, *scope]).stdout
    except (RuntimeError, ValueError) as e:
        print(f"cannot read the PR: {e}", file=sys.stderr)
        return 2
    sha, files, title = pr["headRefOid"], pr["files"], pr["title"]
    round_no, previous = last_ledger(comments)
    facts = [
        f"INFO PR #{pr['number']} {pr['headRefName']} -> {pr['baseRefName']} @ {sha[:7]}, review round {round_no}"
    ]
    if previous:
        facts.append(
            f"INFO previous ledger was at {previous[:7]}; see what moved: git diff {previous}..{sha}"
        )
    status = [ln for ln in call(["git", "status", "--porcelain"]).stdout.splitlines() if ln]
    facts += [
        f"INFO git status --porcelain: {len(status)} entries; the verdict is void if this changes"
    ]
    if status:
        facts.append("CHECK the working tree is not clean")
    facts += gate_fact(pr["body"] or "", sha, lambda tok: merge_only_since(tok, sha))
    facts += checks_fact(checks)
    facts += merge_only_fact(card["runs"], sha, pr["baseRefName"])
    facts += nonconventional(own_headlines(pr["commits"]))
    facts += tier(files)
    facts += fix_needs_test(title, files)
    facts += simplify_rules(title, files)
    facts += out_of_scope(task.get("body") or "", [f["path"] for f in files])
    facts += scan_added(diff)
    print("\n".join(facts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
