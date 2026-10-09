"""`kanban setup` (INS-04..INS-10): preflight, probe, store, LaunchAgent, stamp, verify, upgrade."""

import argparse
import contextlib
import fcntl
import json
import logging
import logging.handlers
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from . import __version__, classify, gitops, launchd, runner, store, web
from .errors import Error, Refused, Usage
from .paths import add_file_log, dirs, ensure_dir, write_private
from .procs import Procs

MIN_CLAUDE = (2, 1, 259)
HEALTH_WAIT_S = 10.0
PROBE_TIMEOUT_S = 180
STAMP_SCHEMA = 1
LAUNCHD_BASICS = ("HOME", "USER", "LOGNAME", "TMPDIR")
TOOL_FIX = {
    "uv": "brew install uv (or: curl -LsSf https://astral.sh/uv/install.sh | sh)",
    "git": "xcode-select --install",
    "gh": "brew install gh",
}
PLUGIN_INSTALL = "/plugin install kanban-claude@claude-kanban-plugin"
PLUGIN_UNINSTALL = PLUGIN_INSTALL.replace("install", "uninstall")
# a runner notices its stopped row within one poll, then claude exits
STOP_WAIT_S = runner.QUIET_POLL_S + 10.0
WORK_ENTRIES = ("scratch",)
log = logging.getLogger("kanban.install")


def say(message: str) -> None:
    """Progress goes to stderr and to `logs/upgrade.log`, with any proxy password masked."""
    message = launchd.mask(message)
    print(message, file=sys.stderr)
    log.info(message)


def version_key(text: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


# --- stamp, lock, log -----------------------------------------------------------------------


def stamp_path() -> Path:
    return dirs().home / "installed.json"


def read_stamp() -> dict[str, Any] | None:
    try:
        data = json.loads(stamp_path().read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("version"), str) else None


def write_stamp(stamp: dict[str, Any]) -> None:
    """§6.5: data only. It may carry a proxy password, so 0600; the hook reads `version` with sed."""
    write_private(stamp_path(), store.dumps(stamp).encode())


@contextlib.contextmanager
def upgrade_lock(home: Path) -> Iterator[bool]:
    """INS-09: an exclusive flock on `upgrade.lock`; yields False when another setup holds it.

    The kernel drops the lock with its process, so a crash leaves nothing to clear and a live holder is never taken over.
    """
    fd = os.open(home / "upgrade.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except BlockingIOError:
            held = False
        yield held
    finally:
        os.close(fd)


@contextlib.contextmanager
def upgrade_log(home: Path) -> Iterator[None]:
    handler = add_file_log(log, home / "logs" / "upgrade.log")
    try:
        yield
    finally:
        log.removeHandler(handler)
        handler.close()


# --- preflight (INS-05 step 1) --------------------------------------------------------------


def check_host() -> None:
    if sys.platform != "darwin":
        raise Error("kanban-claude runs on macOS only (it needs launchd); setup stopped")
    for tool, fix in TOOL_FIX.items():
        if not shutil.which(tool):
            raise Error(f"{tool} is not on PATH; install it with: {fix}")
    if not shutil.which("npx"):
        say("warning: npx is not on PATH; only the designer needs it")


def claude_version(claude: str) -> str:
    """`claude --version`'s number, or an Error saying why this binary will not do (INS-02)."""
    try:
        p = subprocess.run(
            [claude, "--version"], capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"cannot run {claude} --version: {e}") from e
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", p.stdout)
    if not m:
        raise Error(f"cannot read a version from `{claude} --version`: {p.stdout.strip()[:100]!r}")
    if tuple(int(n) for n in m.groups()) < MIN_CLAUDE:
        raise Error(
            f"claude {m.group(0)} is older than {'.'.join(map(str, MIN_CLAUDE))}, "
            "which the worker permission flags need; update it with: claude update"
        )
    return m.group(0)


def resolve_claude(arg: str | None, conn: sqlite3.Connection) -> str:
    """`--claude-bin`, else the one recorded by an earlier setup, else the first on PATH."""
    if arg:
        if not Path(arg).expanduser().is_absolute():
            raise Error(f"--claude-bin must be an absolute path, got {arg!r}")
        found: str | None = str(Path(arg).expanduser())
    else:
        stored = store.cfg(conn, "claude.bin")
        found = stored if stored and os.access(stored, os.X_OK) else shutil.which("claude")
    if not found:
        raise Error(
            "claude is not on PATH; install Claude Code, or pass --claude-bin /absolute/path/to/claude"
        )
    claude = os.path.abspath(found)
    claude_version(claude)
    return claude


def config_dir_for(a: argparse.Namespace, conn: sqlite3.Connection) -> str:
    """INS-05 step 2: the flag, else what an earlier setup recorded, else this session's, else Claude's own."""
    chosen = (
        a.claude_config_dir
        or store.cfg(conn, "claude.config_dir")
        or os.environ.get("CLAUDE_CONFIG_DIR")
        or "~/.claude"
    )
    return str(Path(chosen).expanduser().absolute())


def probe_env(captured: dict[str, str], config_dir: str) -> dict[str, str]:
    """What a worker would see under launchd: its four basics plus the captured variables, nothing
    else of this shell."""
    basics = {k: os.environ[k] for k in LAUNCHD_BASICS if k in os.environ}
    return runner.with_config_dir({**basics, **captured}, config_dir)


def tool_dirs(claude: str) -> list[str]:
    found = [shutil.which(t) for t in ("gh", "git", "uv", "npx", "node")]
    return [str(Path(p).parent) for p in [claude, *found] if p]


# --- the probe (INS-05 step 4) --------------------------------------------------------------


class Probe(NamedTuple):
    kind: str  # ok, auth, mode, model or other
    detail: str


def probe(claude: str, model: str, env: dict[str, str], cwd: Path) -> Probe:
    """A port of `install.sh:79-94`: the permission flags workers use, under the captured environment."""
    argv = [
        claude, "-p", "ok", "--model", model, "--permission-mode", "auto", "--permission-prompts",
        "none", "--no-session-persistence", "--output-format", "stream-json", "--verbose",
    ]  # fmt: skip
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return Probe("other", f"the probe could not run: {e}")
    stream = classify.Stream()
    for line in p.stdout.splitlines():
        with contextlib.suppress(ValueError):
            ev = json.loads(line)
            if isinstance(ev, dict):
                stream.feed(ev)
    result = stream.result or {}
    first = classify.first_line(result)
    if result.get("api_error_status") in (401, 403) or classify.AUTH_RE.match(first):
        return Probe("auth", first)
    if not result:
        return Probe("other", p.stderr.strip()[-300:] or f"claude exited {p.returncode} silently")
    if stream.init.get("permissionMode") != "auto":
        return Probe("mode", f"permissionMode={stream.init.get('permissionMode')}")
    if result.get("is_error"):
        return Probe("model" if re.search(r"\bmodel\b", first, re.I) else "other", first)
    return Probe("ok", "")


def check_probe(
    claude: str, model: str, env: dict[str, str], config_dir: str, conn: sqlite3.Connection,
    yes: bool,
) -> None:  # fmt: skip
    """Run the probe and turn a failure into the sentence that names the cause and the fix."""
    work = dirs().work
    result = probe(claude, model, env, work)
    if result.kind == "model" and model != "sonnet":
        command = "kanban config set models.review sonnet"
        if not yes:
            raise Error(
                f"no access to the {model} model on this seat ({result.detail}); "
                f"review with sonnet instead: {command} (or rerun setup with --yes)"
            )
        store.config_set(conn, "models.review", "sonnet", actor="setup")
        say(f"no access to {model}; models.review is now sonnet, probing again")
        result = probe(claude, "sonnet", env, work)
    if result.kind == "auth":
        raise Error(
            f"claude is not logged in for {config_dir} ({result.detail}); "
            f"log in with: {runner.login_command(env)}, then rerun setup"
        )
    if result.kind == "mode":
        raise Error(
            f"claude did not start in auto mode ({result.detail}). Your organisation may disable "
            "it, and workers cannot run without it; setup stops"
        )
    if result.kind != "ok":
        raise Error(f"the claude probe failed: {result.detail}")


def check_agent(claude: str, env: dict[str, str], config_dir: str) -> None:
    """INS-05 step 9: the agent resolves the way a worker will start it (INS-03)."""
    d = dirs()
    argv = [
        claude, "-p", "reply ok", "--agent", "kanban-claude:scout", "--max-turns", "1",
        "--permission-mode", "auto", "--permission-prompts", "none",
        "--settings", runner.settings_json(d.home, []), "--output-format", "json",
    ]  # fmt: skip
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, env=env, cwd=d.work, timeout=PROBE_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
        out = json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        raise Error(f"the agent probe could not run: {e}") from e
    if isinstance(out, dict) and not out.get("is_error"):
        return
    why = classify.first_line(out) if isinstance(out, dict) else p.stderr.strip()[:200]
    where = (
        f" (start it as CLAUDE_CONFIG_DIR={config_dir} claude)"
        if "CLAUDE_CONFIG_DIR" in env
        else ""
    )
    raise Error(
        f"the scout agent does not resolve under {config_dir} ({why}). Enable the plugin at user "
        f"scope in the claude that uses that directory{where}: {PLUGIN_INSTALL}, then rerun setup"
    )


def wait_for_board(port: int, home: Path) -> None:
    deadline = time.monotonic() + HEALTH_WAIT_S
    while True:
        try:
            web.verify_board(port, home)
            return
        except Error as e:
            if time.monotonic() >= deadline:
                raise Error(f"{e}; see {home / 'logs' / 'daemon.log'}") from e
        time.sleep(0.25)


def kanban_entry() -> str:
    """The absolute path of the uv tool's `kanban`; a bare name would run whatever is first on PATH."""
    try:
        p = subprocess.run(
            ["uv", "tool", "dir", "--bin"], capture_output=True, text=True, timeout=30
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Error(f"uv tool dir failed: {e}") from e
    entry = Path(p.stdout.strip()) / "kanban"
    if p.returncode != 0 or not entry.is_file():
        raise Error(
            f"the kanban entry point {entry} is missing; install it with /kanban-claude:setup"
        )
    return str(entry)


# --- setup ----------------------------------------------------------------------------------


def setup(a: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if a.port is not None and not 0 < a.port < 65536:
        raise Usage("--port must be between 1 and 65535")
    check_host()
    d = dirs()
    ensure_dir(d.home)
    ensure_dir(d.work)
    with upgrade_lock(d.home) as held, upgrade_log(d.home):
        if not held:
            return {"locked": True}, "another setup or upgrade is running; nothing done"
        conn = store.connect()
        try:
            return run_setup(a, conn)
        finally:
            conn.close()


def run_setup(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str]:
    d = dirs()
    claude = resolve_claude(a.claude_bin, conn)
    old = read_stamp()
    if old:
        before, now = version_key(old["version"]), version_key(__version__)
        if before > now and not a.yes:
            raise Refused(
                f"this plugin ({__version__}) is older than the installed daemon ({old['version']}); "
                "to go back on purpose run: kanban setup --yes"
            )
        if before < now and not (a.claude_bin or a.claude_config_dir or a.port):
            return upgrade(a, conn, old)
    config_dir = config_dir_for(a, conn)
    captured = launchd.capture_env(
        os.environ, store.cfg(conn, "worker.env_passthrough"), tool_dirs(claude)
    )
    say("captured environment: " + " ".join(captured))
    env = probe_env(captured, config_dir)
    model = store.cfg(conn, "models.review")
    say(f"probing {claude} with the worker flags (model {model}, config {config_dir})")
    check_probe(claude, model, env, config_dir, conn, a.yes)
    port = a.port or int(store.cfg(conn, "web.port"))
    with store.tx(conn):
        for key, value in (
            ("claude.bin", claude),
            ("claude.config_dir", config_dir),
            ("web.port", port),
        ):
            if store.cfg(conn, key) != value:
                store.config_set(conn, key, value, actor="setup")
    if not a.no_daemon:
        entry = kanban_entry()
        ensure_dir(d.home / "logs")
        plist = launchd.load(entry, captured, d.home, d.work)
        say(f"LaunchAgent {launchd.LABEL} loaded from {plist}")
    stamp(
        {
            "version": __version__,
            "schema": STAMP_SCHEMA,
            "claude_bin": claude,
            "claude_config_dir": config_dir,
            "port": port,
            "env": captured,
        }  # fmt: skip
    )
    if not a.no_daemon:
        wait_for_board(port, d.home)
        say(f"the board answers on 127.0.0.1:{port}")
    check_agent(claude, env, config_dir)
    say("the scout agent resolves")
    url = f"http://127.0.0.1:{port}/"
    data = {"version": __version__, "port": port, "url": url, "daemon": not a.no_daemon}
    if a.no_daemon:
        return (
            data,
            f"kanban-claude {__version__} is set up without a daemon; start it with: kanban daemon",
        )
    return (
        data,
        f"kanban-claude {__version__} is set up.\nopen the board with: kanban board  ({url})",
    )


def stamp(fields: dict[str, Any]) -> None:
    write_stamp({**fields, "setup_at": datetime.now().astimezone().isoformat(timespec="seconds")})


def upgrade(
    a: argparse.Namespace, conn: sqlite3.Connection, old: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """INS-09 from the new code's side: `bootstrap.sh` has already reinstalled the tool, and
    `store.connect` has backed up and migrated. What is left is the restart and the stamp."""
    say(f"upgrading {old['version']} -> {__version__}")
    say(f"database schema is {conn.execute('PRAGMA user_version').fetchone()[0]}")
    if not a.no_daemon:
        launchd.restart()
        wait_for_board(int(old.get("port") or store.cfg(conn, "web.port")), dirs().home)
        say("the daemon runs the new code")
    stamp({k: v for k, v in old.items() if k != "setup_at"} | {"version": __version__})
    return (
        {"version": __version__, "upgraded_from": old["version"]},
        f"kanban-claude upgraded {old['version']} -> {__version__}",
    )


# --- uninstall (INS-12) ---------------------------------------------------------------------


def tree_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def purge_targets() -> list[Path]:
    """Both data directories. Refused: any that is, or holds, the user's home or the root; a home
    without the board database; a work directory with an entry kanban did not create."""
    home = Path.home().resolve()
    targets = [p.resolve() for p in dirs()]
    for p in targets:
        if p in (p.parent, home, *home.parents):
            raise Refused(f"refusing to delete {p}: it is or contains your home directory")
    data, work = targets
    if data.exists() and not (data / "kanban.db").exists():
        raise Refused(f"refusing to delete {data}: it holds no kanban.db, so it is not board data")
    if work.is_dir() and (
        strange := sorted(c.name for c in work.iterdir() if c.name not in WORK_ENTRIES)
    ):
        raise Refused(
            f"refusing to delete {work}: it holds {', '.join(strange)}, not made by kanban"
        )
    return targets


def at_risk(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Card worktrees that are on disk and hold work no remote has: never removed, only listed."""
    found = []
    for t in conn.execute(
        "SELECT id, workspace_path FROM tasks WHERE workspace_kind = 'worktree' "
        "AND workspace_path IS NOT NULL ORDER BY id"
    ).fetchall():
        path = Path(t["workspace_path"]) / ".worktrees" / t["id"]
        if path.is_dir() and (risk := gitops.worktree_risk(path)):
            found.append({"task": t["id"], "path": str(path), "risk": risk})
    return found


def wait_dead(runners: list[tuple[int, int | None]]) -> list[int]:
    """The runner pids still alive after `STOP_WAIT_S`."""
    procs, deadline = Procs(), time.monotonic() + STOP_WAIT_S
    alive = [pid for pid, started in runners if procs.alive(pid, started)]
    while alive and time.monotonic() < deadline:
        time.sleep(0.2)
        alive = [pid for pid, started in runners if procs.alive(pid, started)]
    return alive


def uninstall(a: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Nothing changes until every refusal has been checked: a half-uninstalled machine is worse."""
    if a.purge and not a.yes:
        raise Usage("--purge deletes the board's data for good; add --yes to confirm")
    purge = purge_targets() if a.purge else []
    notes: list[str] = []
    stopped: list[str] = []
    risky: list[dict[str, str]] = []
    conn = store.connect(upgrade=False) if (dirs().home / "kanban.db").exists() else None
    try:
        active = (
            conn.execute("SELECT id, task_id FROM task_runs WHERE status = 'running'").fetchall()
            if conn
            else []
        )
        if active and not a.force:
            raise Refused(
                f"{len(active)} run(s) are active (run {', '.join(str(r['id']) for r in active)}); "
                "wait for them, or stop them with: kanban uninstall --force"
            )
        launchd.bootout()
        launchd.plist_path().unlink(missing_ok=True)
        if conn:
            runners = []
            for r in active:
                got = store.stop(conn, r["task_id"])
                stopped.append(r["task_id"])
                runners.append((got["runner_pid"], got["runner_started"]))
            if stopped:
                notes.append(f"stopped card {', '.join(stopped)} (blocked as stopped)")
            if left := wait_dead([(pid, started) for pid, started in runners if pid]):
                notes.append(
                    f"runner {', '.join(map(str, left))} is still ending; it finishes alone"
                )
            risky = at_risk(conn)
    finally:
        if conn:
            conn.close()
    try:
        tool = subprocess.run(
            ["uv", "tool", "uninstall", "kanban-claude"], capture_output=True, text=True, timeout=60
        )  # fmt: skip
        failed = tool.stderr.strip()[:200] if tool.returncode != 0 else ""
    except (OSError, subprocess.TimeoutExpired) as e:
        failed = str(e)
    if failed:
        notes.append(f"uv tool uninstall kanban-claude failed ({failed}); run it yourself")
    notes += [f"worktree of card {w['task']} is {w['risk']}: {w['path']} (kept)" for w in risky]
    notes += [f"deleting {p} ({human(tree_size(p)) if p.exists() else 'absent'})" for p in purge]
    for p in purge:
        try:
            shutil.rmtree(p)
        except OSError as e:
            notes.append(f"could not delete {p}: {e}")
    data = {
        "stopped": stopped, "worktrees": risky, "purged": [str(p) for p in purge],
        "next": PLUGIN_UNINSTALL,
    }  # fmt: skip
    return data, "\n".join([*notes, "kanban-claude is uninstalled", PLUGIN_UNINSTALL])
