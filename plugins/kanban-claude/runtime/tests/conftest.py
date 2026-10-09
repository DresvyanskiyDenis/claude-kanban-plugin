import json
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from kanban_claude import cli, store
from kanban_claude.procs import Procs

HERE = Path(__file__).resolve().parent

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
elif args[:2] == ["pr", "merge"]:
    prs = json.load(open(os.environ["GH_FAKE_PRS"]))
    pr = prs.get(args[2])
    oid = args[args.index("--match-head-commit") + 1] if "--match-head-commit" in args else None
    if os.environ.get("GH_FAKE_MERGE_FAIL") == "landed" and pr and oid == pr.get("headRefOid"):
        pr["state"] = "MERGED"
        json.dump(prs, open(os.environ["GH_FAKE_PRS"], "w"))
        sys.stderr.write("502 Bad Gateway\\n")
        sys.exit(1)
    if os.environ.get("GH_FAKE_MERGE_FAIL") or pr is None or oid != pr.get("headRefOid"):
        sys.stderr.write("merge refused\\n")
        sys.exit(1)
    pr["state"] = "MERGED"
    json.dump(prs, open(os.environ["GH_FAKE_PRS"], "w"))
elif args[:2] == ["repo", "view"]:
    print(json.dumps({{"defaultBranchRef": {{"name": os.environ.get("GH_FAKE_DEFAULT", "main")}}}}))
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
    proj.parent.mkdir(exist_ok=True)
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


class FakeProcs(Procs):
    """The process table as a test sets it: `live` pids are alive, nothing is really spawned."""

    def __init__(self) -> None:
        self.live: set[int] = set()
        self.boot = 0
        self.boot_calls = 0
        self.spawned: list[int] = []
        self.killed: list[int] = []

    def alive(self, pid: int | None, started: int | None) -> bool:
        return pid in self.live

    def boot_time(self) -> int:
        self.boot_calls += 1
        return self.boot

    def killpg(self, pgid: int, sig: int = signal.SIGKILL) -> None:
        self.killed.append(pgid)

    def spawn(self, run_id: int, log: Path) -> None:
        self.spawned.append(run_id)


@pytest.fixture
def procs() -> FakeProcs:
    return FakeProcs()


@pytest.fixture
def fake_claude(env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`claude` on PATH: tests/fake_claude.py with this interpreter as its shebang."""
    path = env / "bin" / "claude"
    body = (HERE / "fake_claude.py").read_text().split("\n", 1)[1]
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURES", str(HERE / "fixtures"))
    return path


Go = Callable[..., "subprocess.CompletedProcess[str]"]


@pytest.fixture
def go(fake_claude: Path) -> Go:
    """Run `kanban run RUN_ID` as its own process, as the dispatcher does."""

    def run(run_id: int, **extra: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "kanban_claude", "run", str(run_id)],
            env={**os.environ, **extra}, capture_output=True, text=True, timeout=90,
        )  # fmt: skip

    return run


@pytest.fixture
def claim(conn: Any) -> Callable[[str], int]:
    def do(task_id: str) -> int:
        t = store.get_task(conn, task_id)
        config_dir = store.effective_config_dir(conn, store.get_board(conn, t["board"]))
        run = store.claim(conn, task_id, config_dir=config_dir)
        assert run
        return run

    return do


@pytest.fixture
def origin(repo: Path, env: Path) -> Path:
    """`repo` with a fetchable origin: the bare repository itself, not the placeholder URL."""
    bare = env / "origin.git"
    git("remote", "set-url", "origin", str(bare), cwd=repo)
    return bare


def push_commit(env: Path, name: str) -> str:
    """A new commit on origin's `main`, made from a second clone; returns its sha."""
    other = env / "other"
    if not other.exists():
        git("clone", str(env / "origin.git"), str(other), cwd=env)
    git("pull", "--ff-only", "origin", "main", cwd=other)
    (other / name).write_text(name)
    git("add", name, cwd=other)
    git("commit", "-m", name, cwd=other)
    git("push", "origin", "main", cwd=other)
    return git("rev-parse", "HEAD", cwd=other)


Execute = Callable[..., Any]


@pytest.fixture
def execute(conn: Any, claim: Callable[[str], int], go: Go) -> Execute:
    """Claim a card, run its runner against a fixture, return the ended run row."""

    def do(
        task_id: str, fixture: str = "success", *, exit_code: int = 0,
        terminal: list[str] | None = None, **env: str,
    ) -> Any:  # fmt: skip
        run = claim(task_id)
        extra = {"FAKE_CLAUDE_FIXTURE": fixture, "FAKE_CLAUDE_EXIT": str(exit_code), **env}
        if terminal:
            extra["FAKE_CLAUDE_TERMINAL"] = json.dumps(terminal)
        p = go(run, **extra)
        assert p.returncode == 0, p.stdout + p.stderr
        return conn.execute("SELECT * FROM task_runs WHERE id = ?", (run,)).fetchone()

    return do
