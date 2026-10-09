"""Lint hook tests. A fake `uvx` hands ruff to the one in this venv, so nothing is downloaded."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import lint_on_edit as hook
import pytest

SCRIPT = Path(hook.__file__).resolve()
RUFF = Path(sys.executable).parent / "ruff"
OLD = "import os\n\n\ndef f():\n    return 1\n"
NEW = OLD + "\n\ndef g():\n    unused = 1\n    return 2\n"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=ENV)


ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "mod.py").write_text(OLD)
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(tmp_path / "origin.git"))
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(tmp_path / "work"))
    return tmp_path / "work"


@pytest.fixture
def fake_uvx(tmp_path: Path) -> Path:
    if not RUFF.exists():
        pytest.skip("ruff is not installed in this environment")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text(f'#!/bin/sh\nshift\nexec "{RUFF}" "$@"\n')
    uvx.chmod(0o755)
    return bin_dir


def run_hook(
    cwd: Path, path: Path, agent: str, path_dirs: list[Path]
) -> subprocess.CompletedProcess[str]:
    event = {
        "agent_type": agent,
        "cwd": str(cwd),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(path)},
    }
    git_bin = cwd.parent / "gitbin"
    git_bin.mkdir(exist_ok=True)
    (git_bin / "git").unlink(missing_ok=True)
    (git_bin / "git").symlink_to(shutil.which("git") or "git")
    env = {**ENV, "PATH": os.pathsep.join([*map(str, path_dirs), str(git_bin)])}
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=cwd,
        env=env,
    )


def test_hunk_parser_reads_added_line_numbers():
    diff = "@@ -1,0 +2,2 @@ x\n+a\n+b\n@@ -9 +11 @@\n+c\n@@ -20,3 +22,0 @@\n-d\n"
    assert hook.added_lines(diff) == {2, 3, 11}


def test_builders_are_the_namespaced_agents():
    builders = set(hook.BUILDERS)
    assert builders == {
        "kanban-claude:smith",
        "kanban-claude:designer",
        "kanban-claude:card-helper",
    }


def test_reports_only_findings_on_changed_lines(clone: Path, fake_uvx: Path):
    (clone / "mod.py").write_text(NEW)
    r = run_hook(clone, clone / "mod.py", "kanban-claude:smith", [fake_uvx])
    assert r.returncode == 2
    assert "F841" in r.stderr
    assert "F401" not in r.stderr


def test_whole_file_for_an_untracked_file(clone: Path, fake_uvx: Path):
    (clone / "fresh.py").write_text(OLD)
    r = run_hook(clone, clone / "fresh.py", "kanban-claude:designer", [fake_uvx])
    assert r.returncode == 2
    assert "F401" in r.stderr


def test_clean_edit_is_silent(clone: Path, fake_uvx: Path):
    (clone / "mod.py").write_text(OLD + "\n\ndef g():\n    return 2\n")
    r = run_hook(clone, clone / "mod.py", "kanban-claude:smith", [fake_uvx])
    assert (r.returncode, r.stderr) == (0, "")


@pytest.mark.parametrize("agent", ["kanban-claude:warden", "kanban-claude:scout", ""])
def test_other_agents_are_never_linted(clone: Path, fake_uvx: Path, agent: str):
    (clone / "mod.py").write_text(NEW)
    r = run_hook(clone, clone / "mod.py", agent, [fake_uvx])
    assert (r.returncode, r.stderr) == (0, "")


def test_missing_ruff_is_silent(clone: Path):
    (clone / "mod.py").write_text(NEW)
    r = run_hook(clone, clone / "mod.py", "kanban-claude:smith", [])
    assert (r.returncode, r.stderr) == (0, "")


def test_garbage_stdin_is_silent():
    r = subprocess.run(
        [sys.executable, str(SCRIPT)], input="not json", text=True, capture_output=True
    )
    assert (r.returncode, r.stderr) == (0, "")
