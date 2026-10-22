"""OBS-05: `kanban doctor`. Twenty checks, each alone, so one broken thing never hides the rest."""

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import (
    __version__,
    daemon,
    dispatcher,
    ghops,
    github,
    install,
    jira,
    protection,
    runner,
    service,
    store,
    views,
)
from .errors import Error
from .paths import dirs
from .procs import Procs

MIN_FREE_BYTES = 2 * 1024**3
CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
ENV_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
PLUGIN_KEY = install.PLUGIN_INSTALL.split()[-1]
SETUP = "/kanban-claude:setup"
FIX = "kanban doctor --fix"
JIRA_FIX = "run jira.mcp_command from kanban config get jira.mcp_command by hand and read its error"
RULESET_FIX = (
    "needs admin rights: gh api --hostname <host> --method POST repos/<owner>/<repo>/rulesets "
    "--input - (the ruleset bodies are in the README, section Branch protection)"
)


@dataclass
class Result:
    status: str  # ok | warn | fail | skip
    detail: str
    fix: str | None = None
    auto: str | None = None  # which `--fix` repair covers it


@dataclass
class Ctx:
    conn: sqlite3.Connection
    stamp: dict[str, Any]
    probe: bool
    daemon: dict[str, Any] | None = None  # check 2's lock record, for check 3


def ok(detail: str) -> Result:
    return Result("ok", detail)


def warn(detail: str, fix: str | None = None) -> Result:
    return Result("warn", detail, fix)


def fail(detail: str, fix: str | None = None, auto: str | None = None) -> Result:
    return Result("fail", detail, fix, auto)


CHECKS: list[tuple[int, str, str | None, Callable[[Ctx], Result]]] = []


def check(n: int, name: str, fix: str | None = None) -> Callable[[Callable[[Ctx], Result]], Any]:
    """Register a check; `fix` is what an Error raised inside it points the owner at."""

    def register(fn: Callable[[Ctx], Result]) -> Callable[[Ctx], Result]:
        CHECKS.append((n, name, fix, fn))
        return fn

    return register


def claude_path(c: Ctx) -> str:
    found = store.cfg(c.conn, "claude.bin") or c.stamp.get("claude_bin") or shutil.which("claude")
    if not found:
        raise Error("claude is not on PATH and no setup recorded one")
    return str(found)


def config_dirs(c: Ctx) -> dict[str, list[str]]:
    """Every Claude config directory in use, with the boards that name it (none: the global one)."""
    glob = c.stamp.get("claude_config_dir") or store.cfg(c.conn, "claude.config_dir")
    found: dict[str, list[str]] = {str(Path(glob or "~/.claude").expanduser()): []}
    for b in c.conn.execute("SELECT * FROM boards WHERE archived_at IS NULL ORDER BY slug"):
        d = str(Path(store.effective_config_dir(c.conn, b)).expanduser())
        found.setdefault(d, []).append(b["slug"])
    return found


def claude_in(d: str) -> str:
    """What the owner types to start Claude on the account in config directory `d`."""
    return runner.login_command(runner.with_config_dir({}, d)).removesuffix(" /login")


def describe(d: str, boards: list[str]) -> str:
    return f"{d} (board {', '.join(boards)})" if boards else d


# --- 1-3: the daemon ------------------------------------------------------------------------


@check(1, "daemon service loaded")
def launchagent(c: Ctx) -> Result:
    svc = service.backend()
    if svc.loaded():
        return ok(svc.target())
    return fail(f"{service.LABEL} is not loaded as a {svc.NOUN}", FIX, "agent")


@check(2, "daemon holds its lock")
def daemon_lock(c: Ctx) -> Result:
    c.daemon = daemon.probe()
    if c.daemon is None:
        return fail(
            f"nothing holds {dirs().home / 'daemon.lock'}: the daemon is not running",
            f"read {dirs().home / 'logs' / 'daemon.log'}, then rerun {SETUP}",
        )
    return ok(f"held by pid {c.daemon.get('pid', 'unknown')}")


@check(3, "versions match", SETUP)
def versions(c: Ctx) -> Result:
    if not c.stamp:
        return fail("installed.json is missing or unreadable: setup has not completed", SETUP)
    seen = {"tool": __version__, "installed": c.stamp["version"]}
    if c.daemon:
        seen["daemon"] = str(c.daemon.get("version"))
    if root := os.environ.get("CLAUDE_PLUGIN_ROOT"):
        with contextlib.suppress(OSError, ValueError, KeyError):
            seen["plugin"] = json.loads(
                (Path(root) / ".claude-plugin" / "plugin.json").read_text()
            )["version"]
    detail = ", ".join(f"{k} {v}" for k, v in seen.items())
    return fail(detail, SETUP) if len(set(seen.values())) > 1 else ok(detail)


# --- 4-6: the database ----------------------------------------------------------------------


@check(4, "database integrity", "restore the newest copy in the backups directory")
def quick_check(c: Ctx) -> Result:
    rows = [r[0] for r in c.conn.execute("PRAGMA quick_check")]
    if rows == ["ok"]:
        return ok("PRAGMA quick_check: ok")
    return fail("; ".join(rows[:3]), f"restore the newest copy from {dirs().home / 'backups'}")


@check(5, "schema version")
def schema(c: Ctx) -> Result:
    have, want = c.conn.execute("PRAGMA user_version").fetchone()[0], len(store.MIGRATIONS)
    if have == want:
        return ok(f"schema {have}")
    if have < want:
        return fail(f"schema {have}, this tool expects {want}", "kanban migrate")
    return fail(f"schema {have} is newer than this tool's {want}", f"upgrade the tool: {SETUP}")


@check(6, "running cards and runs agree (STO-06)")
def invariant(c: Ctx) -> Result:
    cards = [
        r[0]
        for r in c.conn.execute(
            "SELECT id FROM tasks t WHERE status = 'running' AND NOT EXISTS "
            "(SELECT 1 FROM task_runs r WHERE r.task_id = t.id AND r.status = 'running')"
        )
    ]
    runs = [
        r[0]
        for r in c.conn.execute(
            "SELECT r.id FROM task_runs r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.status = 'running' AND t.status <> 'running'"
        )
    ]
    if not (cards or runs):
        return ok("every running card has a running run, and the other way round")
    detail = [f"card {i} is running without a run" for i in cards]
    detail += [f"run {i} is running but its card is not" for i in runs]
    return fail("; ".join(detail), FIX, "state")


# --- 7-9: claude ----------------------------------------------------------------------------


@check(7, "claude binary", "kanban setup --claude-bin /absolute/path/to/claude")
def claude(c: Ctx) -> Result:
    path = claude_path(c)
    return ok(f"{path} {install.claude_version(path)}")


@check(8, "config directories exist")
def config_exist(c: Ctx) -> Result:
    dirs_ = config_dirs(c)
    gone = [describe(d, b) for d, b in dirs_.items() if not Path(d).is_dir()]
    if not gone:
        return ok(", ".join(dirs_))
    return fail(
        f"missing: {'; '.join(gone)}",
        "log in there once, which creates it: "
        + runner.login_command(runner.with_config_dir({}, next(iter(dirs_)))),
    )


@check(9, "plugin enabled in every config directory")
def plugin_enabled(c: Ctx) -> Result:
    problems, fixes = [], []
    for d, boards in config_dirs(c).items():
        try:
            enabled = json.loads((Path(d) / "settings.json").read_text())["enabledPlugins"]
        except (OSError, ValueError, KeyError, TypeError):
            enabled = {}
        if not (isinstance(enabled, dict) and enabled.get(PLUGIN_KEY) is True):
            problems.append(f"{describe(d, boards)}: {PLUGIN_KEY} is not enabled at user scope")
            fixes.append(f"{claude_in(d)}, then {install.PLUGIN_INSTALL}")
            continue
        if c.probe:
            env = install.probe_env(c.stamp.get("env") or {}, d)
            try:
                install.check_agent(claude_path(c), env, d)
            except Error as e:
                problems.append(str(e))
            if why := runner.auth_probe(c.conn, d):
                problems.append(f"{d}: {why}")
                fixes.append(runner.login_command(runner.with_config_dir({}, d)))
    if problems:
        return fail("; ".join(problems), "; ".join(dict.fromkeys(fixes)) or None)
    return ok("enabled" + (", the scout resolves and the login passes" if c.probe else ""))


# --- 10-14: the host ------------------------------------------------------------------------


@check(10, "gh logged in per host")
def gh_auth(c: Ctx) -> Result:
    hosts = sorted(
        {s.partition("/")[0] for mine in protection.repos(c.conn).values() for s in mine}
    )
    if not hosts:
        return ok("no board uses a git remote")
    out, tokens, down = [], [], []
    for host in hosts:
        try:
            status = ghops.auth_status(host)
        except Error as e:
            down.append(f"{host}: {e}")
            continue
        out.append(host)
        if (m := re.search(r"Logged in to \S+ account \S+ \(([^)]+)\)", status)) and m[
            1
        ] in ENV_TOKENS:
            tokens.append(host)
    login = f"gh auth login --hostname {(down or tokens or hosts)[0].split(':')[0]}"
    if down:
        return fail("; ".join(down), login)
    if tokens:
        return warn(
            f"{', '.join(tokens)}: logged in through a token variable, not the keychain", login
        )
    return ok(f"logged in to {', '.join(out)}")


@check(11, "uv on PATH", install.TOOL_FIX["uv"])
def uv(c: Ctx) -> Result:
    found = shutil.which("uv")
    return ok(found) if found else fail("uv is not on PATH", install.TOOL_FIX["uv"])


@check(12, "CA variables reach the daemon")
def ca_variables(c: Ctx) -> Result:
    svc = service.backend()
    env = svc.read_env()
    if env is None:
        return Result("skip", f"no {svc.NOUN} {svc.FILE} to compare with")
    lost = [v for v in CA_VARS if v in os.environ and v not in env]
    if lost:
        return warn(
            f"{', '.join(lost)} set in this shell but not in the {svc.FILE}", f"rerun {SETUP}"
        )
    return ok(f"every CA variable of this shell is in the {svc.FILE}")


@check(13, "service PATH resolves the tools")
def plist_tools(c: Ctx) -> Result:
    svc = service.backend()
    entry, env = svc.read_entry(), svc.read_env()
    if entry is None or env is None:
        return Result("skip", f"no {svc.NOUN} {svc.FILE} to read")
    path = env.get("PATH", "")
    lost = [t for t in ("claude", "gh", "git", "uv") if not shutil.which(t, path=path)]
    if not os.access(entry, os.X_OK):
        lost.append(f"kanban ({entry})")
    if lost:
        return fail(f"not found: {', '.join(lost)}", f"rerun {SETUP}")
    if soft := [t for t in ("npx", "node") if not shutil.which(t, path=path)]:
        return warn(
            f"not found: {', '.join(soft)} (only the designer needs them)", f"rerun {SETUP}"
        )
    return ok("claude, gh, git, uv, npx, node and kanban resolve")


@check(14, "free disk space")
def disk(c: Ctx) -> Result:
    d, lows, seen = dirs(), [], []
    for p in (d.home, d.work):
        while not p.exists() and p != p.parent:
            p = p.parent
        free = shutil.disk_usage(p).free
        seen.append(f"{p}: {free / 1024**3:.1f} GB free")
        if free < MIN_FREE_BYTES:
            lows.append(str(p))
    if lows:
        return fail("; ".join(seen), "free some space, or move the work directory")
    return ok("; ".join(seen))


# --- 15-18: state ---------------------------------------------------------------------------


@check(15, "pauses and quota walls")
def pauses(c: Ctx) -> Result:
    notes, now = [], store.now()
    if p := store.state_get(c.conn, "dispatch_paused"):
        extra = f": {p['detail']}" if p.get("detail") else ""
        notes.append(f"dispatch paused ({p['reason']}) since {views.iso(p['since'])}{extra}")
    for row in c.conn.execute("SELECT key FROM state WHERE key LIKE 'account:%' ORDER BY key"):
        d = row["key"].removeprefix("account:")
        acct = store.account_get(c.conn, d)
        if acct.get("auth_paused"):
            notes.append(
                f"{d} is logged out; {runner.login_command(runner.with_config_dir({}, d))}"
            )
        for field, label in (("hard_until", "quota wall"), ("soft_until", "soft limit")):
            if (acct.get(field) or 0) > now:
                notes.append(f"{d}: {label} until {views.iso(acct[field])}")
    if notes:
        return warn("; ".join(notes), "kanban resume")
    return ok("dispatch is running; no account is paused or walled")


@check(16, "kept worktrees")
def kept(c: Ctx) -> Result:
    rows = c.conn.execute(
        "SELECT id, worktree_state, workspace_path FROM tasks "
        "WHERE worktree_state IN ('kept_dirty', 'kept_unpushed') ORDER BY id"
    ).fetchall()
    if not rows:
        return ok("none")
    shown = [
        f"{r['id']} {r['worktree_state']} ({r['workspace_path']}/.worktrees/{r['id']})"
        for r in rows
    ]
    return warn(
        "; ".join(shown[:10]) + (f"; and {len(rows) - 10} more" if len(rows) > 10 else ""),
        "push or discard each one, then git worktree remove it; kanban never deletes them",
    )


def mode_problems() -> list[tuple[Path, int, int]]:
    d = dirs()
    home = d.home
    wanted = [(home, 0o700), (d.work, 0o700)] + [
        (p, 0o600)
        for p in (
            home / "kanban.db", home / "kanban.db-wal", home / "kanban.db-shm",
            install.stamp_path(), service.backend().definition_path(),
            *sorted(p for p in (home / "logs").rglob("*") if p.is_file()),
        )
    ]  # fmt: skip
    wrong = []
    for path, want in wanted:
        with contextlib.suppress(OSError):
            have = stat.S_IMODE(path.stat().st_mode)
            if have != want:
                wrong.append((path, have, want))
    return wrong


@check(17, "file modes (SEC-06)")
def modes(c: Ctx) -> Result:
    wrong = mode_problems()
    if not wrong:
        return ok("directories 0700; database, stamp, logs and the service file 0600")
    return fail("; ".join(f"{p} is {h:04o}, want {w:04o}" for p, h, w in wrong), FIX, "modes")


@check(18, "runners alive for running runs")
def runners(c: Ctx) -> Result:
    procs, ts, dead = Procs(), store.now(), []
    for r in c.conn.execute("SELECT * FROM task_runs WHERE status = 'running'").fetchall():
        young = r["runner_pid"] is None and ts - r["started_at"] < dispatcher.CRASH_GRACE_S
        if not young and not procs.alive(r["runner_pid"], r["runner_started"]):
            dead.append(f"run {r['id']} of {r['task_id']}")
    if dead:
        return fail(f"no runner process for {', '.join(dead)}", FIX, "state")
    return ok("every running run has its runner")


# --- 19-21: GitHub and Jira -----------------------------------------------------------------


@check(19, "branch protection (GIT-08)")
def branch_protection(c: Ctx) -> Result:
    found = protection.refresh(c.conn)
    flagged = sorted({s for slugs in found["boards"].values() for s in slugs})
    notes = [f"{s}: the default branch or feature/* has no protection" for s in flagged]
    if found["unknown"]:
        notes.append(f"could not be read: {', '.join(found['unknown'])}")
    if notes:
        return warn("; ".join(notes), RULESET_FIX)
    return ok(
        "every repo a board uses is protected"
        if protection.repos(c.conn)
        else "no board uses a repo"
    )


@check(20, "Jira pairing")
def jira_pairing(c: Ctx) -> Result:
    boards = [b["slug"] for b in jira.paired(c.conn)]
    command = store.cfg(c.conn, "jira.mcp_command")
    if not boards and not store.cfg(c.conn, "roles.atlassian.enabled"):
        return ok("no board is paired")
    if boards and not store.cfg(c.conn, "jira.assignee"):
        return fail(
            f"{', '.join(boards)} paired, but jira.assignee is not set",
            "kanban config set jira.assignee <user>",
        )
    if not command:
        return fail(
            "jira.mcp_command is not set",
            "add the atlassian MCP server to Claude, then run /kanban-claude:setup",
        )
    if c.probe:
        try:
            missing = sorted(set(jira.TOOLS) - set(jira.probe(command)))
        except Error as e:
            return fail(f"{command[0]} did not start as an MCP server: {e}", JIRA_FIX)
        if missing:
            return fail(f"the server does not list {', '.join(missing)}", JIRA_FIX)
    paused = {
        b: p for b in boards if (p := (store.state_get(c.conn, f"jira:{b}") or {}).get("paused"))
    }
    if paused:
        why = "; ".join(f"{b}: {p['detail']}" for b, p in paused.items())
        return warn(f"sync paused on {why}", "kanban jira sync")
    if store.cfg(c.conn, "jira.claim"):
        me = store.cfg(c.conn, "jira.assignee")
        seen = [
            json.loads(r[0])
            for r in c.conn.execute(
                "SELECT jira_assignee FROM jira_links WHERE jira_assignee IS NOT NULL"
            )
        ]
        if not any(jira.is_me(who, me) for who in seen):
            why = (
                f"none of the {len(seen)} linked issues Jira has assigned matches jira.assignee"
                f" {me!r}, so no linked card that is already assigned will dispatch"
                if seen
                else "no linked issue has reported an assignee yet, so jira.assignee is unconfirmed"
            )
            return warn(
                f"jira.claim is on, but {why}",
                "kanban config set jira.assignee <the account id or email Jira shows for you>",
            )
    probed = ", the server lists the seven tools" if c.probe else ""
    return ok(f"{', '.join(boards) or 'no board'}: assignee set, sync running{probed}")


@check(21, "GitHub pairing")
def github_pairing(c: Ctx) -> Result:
    have = c.conn.execute("PRAGMA user_version").fetchone()[0]
    if have < len(store.MIGRATIONS):
        return warn(f"not checked: the database is at schema {have}; see check 5")
    boards = github.paired(c.conn)
    if not boards:
        return ok("no board is paired with a GitHub repository")
    hosts = sorted({github.split(b["github_repo"])[0] for b in boards})
    needs_project = {github.split(b["github_repo"])[0] for b in boards if b["github_project"]}
    for host in hosts:
        try:
            status = ghops.auth_status(host)
        except Error as e:
            return fail(f"{host}: {e}", f"gh auth login --hostname {host}")
        scopes = re.search(r"Token scopes: (.*)", status)
        if host in needs_project and scopes and "'project'" not in scopes[1]:
            return fail(
                f"{host}: the token has no project scope, which a board with --github-project needs",
                f"gh auth refresh -h {host} -s project",
            )
    if c.probe:
        for b in boards:
            repo = github.Repo(b)
            try:
                github.call(repo.host, repo.path)
                repo.use_project()
            except Error as e:
                return fail(f"{b['slug']}: {e}", f"gh auth status --hostname {repo.host}")
    paused = {
        b["slug"]: p
        for b in boards
        if (p := (store.state_get(c.conn, f"github:{b['slug']}") or {}).get("paused"))
    }
    if paused:
        why = "; ".join(f"{slug}: {p['detail']}" for slug, p in paused.items())
        return warn(f"sync paused on {why}", "kanban github sync")
    probed = ", the repository and project read" if c.probe else ""
    return ok(f"{', '.join(b['slug'] for b in boards)}: gh logged in to {', '.join(hosts)}{probed}")


# --- 22: the roles' own MCP servers ---------------------------------------------------------


@check(22, "designer's browser MCP")
def designer_mcp(c: Ctx) -> Result:
    """Claude applies a repo's `disabledMcpServers` to the card worktrees inside it, even to the
    servers a role brings in `mcp/<role>.json`."""
    if "designer" not in store.active_roles(c.conn):
        return ok("designer is not dispatched")
    names = runner.json_section(runner.MCP_DIR / "designer.json", "mcpServers")
    repos, found, fixes = protection.repos(c.conn), [], []
    for d, boards in config_dirs(c).items():
        projects = runner.json_section(runner.account_json(d), "projects")
        for board in boards:
            for top in repos.get(board, {}).values():
                project = projects.get(str(top))
                disabled = project.get("disabledMcpServers") if isinstance(project, dict) else None
                if off := [n for n in names if isinstance(disabled, list) and n in disabled]:
                    found.append(f"{board}: {top} turns off {', '.join(off)}")
                    fixes.append(
                        f"cd {top} && {claude_in(d)}, then /mcp and enable {', '.join(off)}"
                    )
    if found:
        why = "; ".join(found)
        return warn(f"{why}, so designer checks pages with a Playwright script", "; ".join(fixes))
    return ok(f"no board's repo turns off {', '.join(names)}")


# --- running them, and --fix ----------------------------------------------------------------


def evaluate(c: Ctx) -> list[tuple[int, str, Result]]:
    c.daemon = None
    out = []
    for n, name, fix, fn in CHECKS:
        try:
            r = fn(c)
        except Error as e:
            r = fail(str(e), fix)
        except Exception as e:  # a check that crashes failed; it must not kill the doctor
            r = fail(f"{type(e).__name__}: {e}", fix)
        out.append((n, name, r))
    return out


def restore_agent(c: Ctx) -> str:
    d = dirs()
    svc = service.backend()
    definition = svc.definition_path()
    if definition.exists():
        svc.bootstrap(definition)
    elif c.stamp.get("env"):
        definition = svc.load(install.kanban_entry(), c.stamp["env"], d.home, d.work)
    else:
        raise Error(f"the {svc.FILE} is gone and installed.json cannot rebuild it; run {SETUP}")
    with contextlib.suppress(Error):
        install.wait_for_daemon()
    return f"loaded {service.LABEL} from {definition}"


def repair(c: Ctx, results: list[tuple[int, str, Result]]) -> tuple[list[str], list[str]]:
    """Only what is safe: file modes, a service that is not loaded, the STO-06 mismatch through reconcile."""
    wanted = {r.auto for _, _, r in results if r.status != "ok" and r.auto}
    fixed: list[str] = []
    unfixed: list[str] = []
    if "modes" in wanted:
        for path, _, want in mode_problems():
            os.chmod(path, want)
            fixed.append(f"chmod {want:04o} {path}")
    if "agent" in wanted:
        try:
            fixed.append(restore_agent(c))
        except Error as e:
            unfixed.append(str(e))
    if "state" in wanted:
        dispatcher.repair_running(c.conn)
        ended = dispatcher.reconcile(c.conn, store.now(), Procs())
        fixed.append(f"repaired running cards; ended {len(ended)} run(s) whose runner is gone")
    return fixed, unfixed


def report(
    results: list[tuple[int, str, Result]], fixed: list[str], unfixed: list[str]
) -> tuple[dict[str, Any], str]:
    counts = {s: sum(r.status == s for _, _, r in results) for s in ("ok", "warn", "fail", "skip")}
    data = {
        "ok": counts["fail"] == 0,
        "checks": [
            {"n": n, "name": name, "status": r.status, "detail": r.detail, "fix": r.fix}
            for n, name, r in results
        ],
        "fixed": fixed,
        "unfixed": unfixed,
    }
    label = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skip": "skip"}
    lines = []
    for n, name, r in results:
        lines.append(f"{label[r.status]} {n:2}  {name}: {r.detail}")
        if r.fix and r.status in ("warn", "fail"):
            lines.append(f"         fix: {r.fix}")
    lines += [f"fixed: {f}" for f in fixed] + [f"not fixed: {u}" for u in unfixed]
    lines.append(
        f"{len(results)} checks: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail"
    )
    return data, "\n".join(lines)


def run(*, probe: bool, fix: bool) -> tuple[dict[str, Any], str]:
    db = dirs().home / "kanban.db"
    if not db.exists():
        broken = Result("fail", f"no database in {dirs().home}: kanban is not set up", SETUP)
        return report([(0, "set up", broken)], [], [])
    try:
        conn = store.connect(upgrade=False)
    except sqlite3.DatabaseError as e:
        broken = Result(
            "fail",
            f"{db} cannot be opened: {e}",
            f"restore the newest copy from {dirs().home / 'backups'}",
        )
        return report([(0, "database opens", broken)], [], [])
    try:
        c = Ctx(conn, install.read_stamp() or {}, probe)
        results = evaluate(c)
        fixed: list[str] = []
        unfixed: list[str] = []
        if fix:
            fixed, unfixed = repair(c, results)
            if fixed or unfixed:
                results = evaluate(c)
        return report(results, fixed, unfixed)
    finally:
        conn.close()
