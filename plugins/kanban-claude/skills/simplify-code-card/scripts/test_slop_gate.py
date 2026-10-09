"""Slop gate tests. A fake `uvx` stands in for scb-check, so nothing is downloaded."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import slop_gate as g

SCRIPT = Path(g.__file__).resolve()

FAKE_SCB = """import json, sys
text = open(sys.argv[2]).read()
dup = sum(1 for line in text.splitlines() if "dup" in line)
print(json.dumps({"verbosity_flagged_loc": 0, "clone_loc": dup, "total_mass": len(text.splitlines()),
                  "total_cog_mass": 0, "high_cc_mass": 0, "high_cog_mass": 0}))
sys.exit(1 if dup else 0)
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "scb.py").write_text(FAKE_SCB)
    uvx = bin_dir / "uvx"
    uvx.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "{bin_dir}/uvx.args"\nshift 3\n'
        f'exec "{sys.executable}" "{bin_dir}/scb.py" "$@"\n'
    )
    uvx.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def gate(env: dict[str, str], *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, env=env
    )


def test_only_existing_files_scb_check_reads_are_kept(tmp_path: Path):
    py, md = tmp_path / "a.py", tmp_path / "a.md"
    py.write_text("x = 1\n")
    md.write_text("hi\n")
    assert g.supported([str(py), str(md), str(tmp_path / "gone.py")]) == [str(py)]


def test_regressions_are_increases_in_gated_metrics_only():
    before = {"clone_loc": 2, "total_mass": 10.0, "verbosity": 0.1}
    assert g.regressions(before, {"clone_loc": 2, "total_mass": 9.0, "verbosity": 0.9}) == []
    assert g.regressions(before, {"clone_loc": 3, "total_mass": 10.0}) == ["clone_loc: 2 -> 3"]


def test_find_lists_supported_files_and_exits_3_for_none(env: dict[str, str], tmp_path: Path):
    py, md = tmp_path / "a.py", tmp_path / "a.md"
    py.write_text("x = 1\n")
    md.write_text("hi\n")
    r = gate(env, "find", py, md)
    assert (r.returncode, r.stdout.split()) == (0, [str(py)])
    assert gate(env, "find", md).returncode == 3


def test_compare_passes_when_the_code_shrinks_and_fails_when_it_gets_worse(
    env: dict[str, str], tmp_path: Path
):
    f, snap = tmp_path / "a.py", tmp_path / "before.json"
    f.write_text("x = 1\ny = 2\n")
    assert gate(env, "snapshot", snap, f).returncode == 0
    assert gate(env, "compare", snap, f).returncode == 0
    f.write_text("x = 1\n")
    assert gate(env, "compare", snap, f).returncode == 0
    f.write_text("x = 1\ndup = 2\ndup = 3\n")
    r = gate(env, "compare", snap, f)
    assert r.returncode == 1
    assert "clone_loc: 0 -> 2" in r.stdout


def test_compare_counts_a_file_the_change_added(env: dict[str, str], tmp_path: Path):
    old, new, snap = tmp_path / "a.py", tmp_path / "b.py", tmp_path / "before.json"
    old.write_text("x = 1\n")
    assert gate(env, "snapshot", snap, old).returncode == 0
    new.write_text("y = 1\n")
    r = gate(env, "compare", snap, old, new)
    assert r.returncode == 1 and "total_mass" in r.stdout


def test_no_supported_file_exits_3(env: dict[str, str], tmp_path: Path):
    md = tmp_path / "a.md"
    md.write_text("hi\n")
    assert gate(env, "snapshot", tmp_path / "s.json", md).returncode == 3


def test_scb_check_runs_from_an_exact_pinned_release(tmp_path: Path, env: dict[str, str]):
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    gate(env, "snapshot", tmp_path / "b.json", src)
    args = (Path(env["PATH"].split(os.pathsep)[0]) / "uvx.args").read_text().split()
    assert args[:2] == ["--from", g.SCB_CHECK]
    assert re.fullmatch(r"scb-check==\d+\.\d+\.\d+", g.SCB_CHECK)
