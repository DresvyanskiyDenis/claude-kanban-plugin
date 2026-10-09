#!/usr/bin/env python3
"""Measure code bloat before and after a simplification, with scb-check.

  slop_gate.py find FILE...              print the files scb-check can read
  slop_gate.py snapshot OUT.json FILE... measure them now
  slop_gate.py compare BEFORE.json FILE... measure again; exit 1 if any gated metric grew

Exit 3 when none of the files is one scb-check supports. Needs `uvx`. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SCB_CHECK = "scb-check==0.2.0"

SUPPORTED = (".py", ".pyw", ".rs", ".js", ".mjs", ".cjs", ".ts", ".zig", ".hs",
             ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx")  # fmt: skip
GATED = ("verbosity_flagged_loc", "clone_loc", "total_mass", "total_cog_mass",
         "high_cc_mass", "high_cog_mass")  # fmt: skip


def supported(files: list[str]) -> list[str]:
    return [f for f in files if f.endswith(SUPPORTED) and os.path.isfile(f)]


def measure(path: str) -> dict[str, float]:
    # ponytail: one scb-check run per file, so clones across files are not seen;
    # measure a copied directory instead if cross-file clones start to matter
    r = subprocess.run(
        ["uvx", "--from", SCB_CHECK, "scb-check", "check", os.path.abspath(path), "--output-format", "json"],
        capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    if r.returncode not in (0, 1):  # 1 only means "findings present"
        sys.exit(f"scb-check failed on {path}: {r.stderr.strip() or r.stdout.strip()}")
    report = json.loads(r.stdout)
    return {m: report[m] for m in GATED}


def totals(files: list[str]) -> dict[str, float]:
    per_file = [measure(f) for f in files]
    return {m: sum(p[m] for p in per_file) for m in GATED}


def regressions(before: dict[str, float], after: dict[str, float]) -> list[str]:
    return [
        f"{m}: {before[m]:g} -> {after[m]:g}"
        for m in GATED
        if m in before and m in after and after[m] > before[m] + 1e-9
    ]


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("find", "snapshot", "compare"):
        print(__doc__, file=sys.stderr)
        return 2
    verb, rest = argv[0], argv[1:]
    out = rest.pop(0) if verb != "find" else None
    files = supported(rest)
    if not files:
        print("no file here is one scb-check supports", file=sys.stderr)
        return 3
    if verb == "find":
        print("\n".join(files))
    elif verb == "snapshot" and out:
        now = totals(files)
        with open(out, "w") as f:
            json.dump({"total": now, "files": files}, f, indent=1)
        print(f"snapshot of {len(files)} file(s) written to {out}")
    elif out:
        with open(out) as f:
            before = json.load(f)["total"]
        now = totals(files)
        worse = regressions(before, now)
        for m in GATED:
            print(
                f"{'FAIL' if any(w.startswith(m + ':') for w in worse) else 'ok  '} {m}: {before[m]:g} -> {now[m]:g}"
            )
        return 1 if worse else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
