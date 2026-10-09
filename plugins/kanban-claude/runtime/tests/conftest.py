import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import cli, store

GH_SCRIPT = """#!{python}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["GH_FAKE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "view"]:
    prs = json.load(open(os.environ["GH_FAKE_PRS"]))
    pr = prs.get(args[2])
    if pr is None:
        sys.stderr.write("no pull requests found\\n")
        sys.exit(1)
    print(json.dumps(pr))
elif args[:2] == ["repo", "view"]:
    print(json.dumps({{"defaultBranchRef": {{"name": "main"}}}}))
else:
    sys.exit(2)
"""


@pytest.fixture(autouse=True)
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Everything lives under tmp_path: HOME, the three kanban dirs, git and gh."""
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("KANBAN_HOME", str(tmp_path / "kb-home"))
    monkeypatch.setenv("KANBAN_WORK", str(tmp_path / "kb-work"))
    for var in ("KANBAN_RUN_ID", "KANBAN_BOARD", "KANBAN_TASK", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.invalid")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(GH_SCRIPT.format(python=sys.executable))
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_FAKE_LOG", str(tmp_path / "gh.log"))
    prs = tmp_path / "prs.json"
    prs.write_text("{}")
    monkeypatch.setenv("GH_FAKE_PRS", str(prs))
    return tmp_path


Kb = Callable[..., tuple[int, str, str]]


@pytest.fixture
def kb(capsys: pytest.CaptureFixture[str]) -> Kb:
    def run(*args: str) -> tuple[int, str, str]:
        try:
            code = cli.main(list(args))
        except SystemExit as e:
            code = int(e.code or 0)
        out, err = capsys.readouterr()
        return code, out, err

    return run


def git(*args: str, cwd: Path) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return p.stdout.strip()


@pytest.fixture
def set_prs(env: Path) -> Callable[[dict[str, dict[str, Any]]], None]:
    def put(prs: dict[str, dict[str, Any]]) -> None:
        (env / "prs.json").write_text(json.dumps(prs))

    return put


@pytest.fixture
def repo(env: Path) -> Path:
    """A clone with a bare origin, `main` pushed and origin/HEAD set."""
    origin = env / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=env)
    seed = env / "seed"
    seed.mkdir()
    git("init", "-b", "main", cwd=seed)
    (seed / "a.txt").write_text("a")
    git("add", "a.txt", cwd=seed)
    git("commit", "-m", "init", cwd=seed)
    git("push", str(origin), "main", cwd=seed)
    proj = env / "repos" / "proj"
    proj.parent.mkdir()
    git("clone", str(origin), str(proj), cwd=env)
    git("remote", "set-head", "origin", "-a", cwd=proj)
    git("remote", "set-url", "origin", "https://example.invalid/acme/proj.git", cwd=proj)
    git("config", "remote.origin.pushurl", str(origin), cwd=proj)
    return proj


@pytest.fixture
def conn() -> Iterator[Any]:
    c = store.connect()
    yield c
    c.close()


@pytest.fixture
def board(conn: Any, env: Path) -> str:
    plain = env / "repos" / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    store.board_create(conn, "main", default_workdir=str(plain))
    return "main"


@pytest.fixture
def card(conn: Any, board: str) -> Callable[..., str]:
    def make(title: str = "A card", **kw: Any) -> str:
        kw.setdefault("body", "do the thing")
        kw.setdefault("assignee", "smith")
        return str(store.create(conn, board, title, **kw)["id"])

    return make


@pytest.fixture
def wt(conn: Any, repo: Path) -> tuple[str, int]:
    """A claimed builder run on a worktree card with the branch `feat/x`."""
    store.board_create(conn, "main", default_workdir=str(repo))
    tid = str(
        store.create(
            conn, "main", "Work", body="b", assignee="smith", workspace=f"worktree:{repo}",
            branch="feat/x",
        )["id"]
    )  # fmt: skip
    run = store.claim(conn, tid)
    assert run
    return tid, run
