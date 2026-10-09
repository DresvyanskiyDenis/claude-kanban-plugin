#!/usr/bin/env python3
"""PostToolUse hook: lint the lines a builder just changed and hand the findings back.

Silent for every agent outside BUILDERS and whenever the linter is missing. Exit 2 puts
stderr in front of the model; exit 0 says nothing. Stdlib only, runs on the system python3.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

BUILDERS = {"kanban-claude:smith", "kanban-claude:designer", "kanban-claude:card-helper"}
# unused code, needless branches, commented-out code, swallowed or blind excepts
PY_RULES = "F401,F841,SIM,PIE,RET,C4,ERA,S110,S112,BLE001"
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte")
MAX_HITS = 20
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def run(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=50)
    except (OSError, subprocess.TimeoutExpired):
        return None


def added_lines(diff: str) -> set[int]:
    """Line numbers on the new side of a `git diff -U0`."""
    lines: set[int] = set()
    for m in HUNK.finditer(diff):
        start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
        lines.update(range(start, start + count))
    return lines


def changed_lines(path: str, cwd: str) -> set[int] | None:
    """Lines changed against the merge-base with origin/HEAD; None means lint the whole file."""
    tracked = run(["git", "ls-files", "--error-unmatch", "--", path], cwd)
    base = run(["git", "merge-base", "HEAD", "origin/HEAD"], cwd)
    if not tracked or tracked.returncode or not base or base.returncode:
        return None
    diff = run(["git", "diff", "-U0", base.stdout.strip(), "--", path], cwd)
    return added_lines(diff.stdout) if diff and diff.returncode == 0 else None


def ruff_hits(path: str, cwd: str) -> list[tuple[int, str, str]]:
    r = run(
        ["uvx", "ruff", "check", "--isolated", "--no-cache", "--no-fix", "--select", PY_RULES,
         "--output-format", "json", "--", path],
        cwd,
    )  # fmt: skip
    if r is None or not r.stdout.startswith("["):
        return []
    return [(f["location"]["row"], f["code"], f["message"]) for f in json.loads(r.stdout)]


def eslint_binary(path: str) -> str | None:
    d = os.path.dirname(os.path.abspath(path))
    while True:
        exe = os.path.join(d, "node_modules", ".bin", "eslint")
        if os.path.exists(exe):
            return exe
        if os.path.dirname(d) == d:
            return None
        d = os.path.dirname(d)


def eslint_hits(path: str, cwd: str) -> list[tuple[int, str, str]]:
    exe = eslint_binary(path)
    if exe is None:
        return []
    r = run([exe, "--format", "json", "--", path], cwd)
    if r is None or not r.stdout.startswith("["):
        return []
    return [
        (m["line"], m.get("ruleId") or "eslint", m["message"])
        for f in json.loads(r.stdout)
        for m in f["messages"]
        if m.get("line")
    ]


def main() -> int:
    try:
        event = json.load(sys.stdin)
        path = event["tool_input"]["file_path"]
        cwd = event.get("cwd") or os.getcwd()
    except (ValueError, KeyError, TypeError):
        return 0
    if event.get("agent_type") not in BUILDERS or not os.path.isfile(path):
        return 0
    if path.endswith(".py"):
        hits = ruff_hits(path, cwd)
    elif path.endswith(JS_SUFFIXES):
        hits = eslint_hits(path, cwd)
    else:
        return 0
    lines = changed_lines(path, cwd) if hits else None
    shown = [h for h in hits if lines is None or h[0] in lines][:MAX_HITS]
    if not shown:
        return 0
    print(
        f"Lint findings on the lines you changed in {path}; fix them in your next edit:",
        file=sys.stderr,
    )
    for row, code, message in shown:
        print(f"  {path}:{row}: {code} {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
