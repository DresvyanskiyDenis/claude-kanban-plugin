"""The runner: one process per run. It provisions the workspace, supervises Claude, ends the run (RUN)."""

import collections
import contextlib
import hashlib
import json
import os
import queue
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from importlib import resources
from pathlib import Path
from string import Template
from typing import IO, Any

from . import classify, ghops, gitops, housekeeping, measure, store, validate
from .errors import Error
from .gitops import ProvisionError
from .paths import card_tmp, dirs, ensure_dir, nudge, run_files, write_private
from .procs import signal_group, start_time

KILL_GRACE_S = 30
WALL_WAIT_S = 120
QUIET_POLL_S = 5
READER_JOIN_S = 3
INFRA_COOLDOWN_S = 300
INFRA_RETRIES = 3
BILLING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
STRIPPED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_ENABLE_TODO_TOOLS")
MCP_DIR = Path(__file__).parent / "mcp"
OWN_PLUGIN = "kanban-claude"
OWN_DEPENDENCIES = ("frontend-design",)  # plugin.json "dependencies"; a test keeps the two equal
OWN_AGENTS = (
    "atlassian",
    "card-checker",
    "card-helper",
    "designer",
    "review-helper",
    "scout",
    "smith",
    "warden",
)
BUILTIN_AGENTS = ("Explore", "Plan", "general-purpose", "statusline-setup", "claude")
HELPERS = {
    "smith": "card-helper",
    "designer": "card-helper",
    "scout": "card-helper",
    "warden": "review-helper",
}
# the floor under `tools.<role>`: a config list cannot give these roles a file writer
NO_FILE_WRITES = {
    "warden": ("Edit", "Write", "NotebookEdit"),
    "atlassian": ("Edit", "Write", "NotebookEdit"),
    "scout": ("Edit", "NotebookEdit"),
}
ROLE_SERVERS = {"atlassian": ("atlassian",)}
# mcp-atlassian enforces ENABLED_TOOLS when it lists and when it calls a tool
ATLASSIAN_TOOLS = (
    "jira_get_issue", "jira_search", "jira_get_transitions", "jira_get_project_issues",
    "jira_get_all_projects", "jira_get_link_types", "confluence_search", "confluence_get_page",
    "confluence_get_comments", "jira_create_issue", "jira_update_issue", "jira_add_comment",
    "jira_transition_issue", "jira_create_issue_link", "confluence_create_page",
    "confluence_update_page", "confluence_add_comment",
)  # fmt: skip
# D1: an allowlist of one, so an agent type the deny list does not name (a user agent) stays out;
# agent_id is set only inside a subagent, and a helper inherits the role's Agent tool
# A PreToolUse hook that exits 1 lets the tool run, so any failure to read the event blocks (exit 2).
SPAWN_GUARD = """import json, sys
a = sys.argv[1]
try:
    e = json.load(sys.stdin)
    ok = bool(a) and e["tool_input"]["subagent_type"] == a and "agent_id" not in e
except Exception:
    ok = False
if not ok:
    print("kanban: only the role spawns, and only " + (a or "no subagent"), file=sys.stderr)
    sys.exit(2)
"""
EOF = object()
Row = sqlite3.Row


def session_id(board: str, task: str, role: str) -> str:
    """RUN-04. The `hermes-kanban:` prefix is kept so cards imported earlier still resume their sessions."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-kanban:{board}/{task}/{role}"))


def was_resumed(config_dir: str, sid: str) -> bool:
    return any(Path(config_dir).glob(f"projects/*/{sid}.jsonl"))


def is_review(conn: sqlite3.Connection, run: Row) -> bool:
    return bool(run["kind"] == "review" or run["role"] == store.cfg(conn, "dispatch.reviewer"))


def model_effort(conn: sqlite3.Connection, t: Row, run: Row) -> tuple[str, str]:
    """RUN-06: review runs always use the review model; both flags are always passed. The effort
    is the card's own, else `effort.<role>`, else the kind default (`effort.review` for a review,
    which ignores the card's own), else `effort.default`."""
    key = f"effort.{run['role']}"
    role_effort = store.cfg(conn, key) if key in store.DEFAULTS else None
    if is_review(conn, run):
        return store.cfg(conn, "models.review"), role_effort or store.cfg(conn, "effort.review")
    effort = t["reasoning_effort"]
    if effort not in validate.EFFORTS:
        effort = role_effort or store.cfg(conn, "effort.default")
    return t["model_override"] or store.cfg(conn, "models.default"), effort


def advisor_arm(conn: sqlite3.Connection, run: Row) -> str:
    """D1: the advisor arm of this run, `inherit`, `on` or `off`, from `advisor.<role>`. An `ab`
    role is split by a stable hash of role and card (salted, so smith's and warden's arms on one
    card are independent, and a card keeps its arm across reruns)."""
    key = f"advisor.{run['role']}"
    policy = store.cfg(conn, key) if key in store.DEFAULTS else "inherit"
    if policy != "ab":
        return str(policy)
    digest = hashlib.sha256(f"{run['role']}:{run['task_id']}".encode()).digest()
    return "off" if digest[0] & 1 else "on"


def json_section(path: Path, key: str) -> dict[str, Any]:
    try:
        section = json.loads(path.read_text()).get(key)
    except (OSError, ValueError, AttributeError):
        return {}
    return section if isinstance(section, dict) else {}


def plugins_off(conn: sqlite3.Connection, config_dir: str) -> dict[str, bool]:
    """P2-1: every plugin the account's own settings enable, switched off for the worker, but
    kanban-claude, what it depends on and `worker.plugin_allowlist` (names or full ids)."""
    keep = {OWN_PLUGIN, *OWN_DEPENDENCIES, *store.cfg(conn, "worker.plugin_allowlist")}
    enabled = json_section(Path(config_dir).expanduser() / "settings.json", "enabledPlugins")
    off = {k: False for k, v in enabled.items() if v and not {k, k.partition("@")[0]} & keep}
    return {**off, "cc-plugin-you-should-know@builtin": False}


def role_denies(role: str) -> list[str]:
    """D1: a role may spawn only its helper, and a read-only role may not write files."""
    deny = list(NO_FILE_WRITES.get(role, ()))
    if role in HELPERS:
        agents = [*BUILTIN_AGENTS, *(f"{OWN_PLUGIN}:{a}" for a in OWN_AGENTS)]
        deny += [f"Agent({a})" for a in agents if a != f"{OWN_PLUGIN}:{HELPERS[role]}"]
    return deny


def settings_json(
    home: Path, rules: list[str], role: str = "", off: dict[str, bool] | None = None
) -> str:
    """RUN-06: guard rails, the role's denies, the active grants and the plugin switches."""
    deny = [
        "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(gh pr merge:*)",
        "Bash(kanban grant:*)", f"Edit(/{home}/**)", *role_denies(role),
    ]  # fmt: skip
    plugins = off or {"cc-plugin-you-should-know@builtin": False}
    helper = f"{OWN_PLUGIN}:{HELPERS[role]}" if role in HELPERS else ""
    guard = shlex.join([sys.executable, "-c", SPAWN_GUARD, helper])
    hooks = {
        "PreToolUse": [{"matcher": "Agent|Task", "hooks": [{"type": "command", "command": guard}]}]
    }
    return json.dumps(
        {"permissions": {"deny": deny, "allow": rules}, "enabledPlugins": plugins, "hooks": hooks}
    )


def account_json(config_dir: str) -> Path:
    """The account's `.claude.json`: beside Claude's own default directory, else inside the directory."""
    base, home = Path(config_dir).expanduser(), Path.home()
    return (
        home / ".claude.json"
        if base.resolve() == (home / ".claude").resolve()
        else base / ".claude.json"
    )


def mcp_config(
    conn: sqlite3.Connection, role: str, workspace: Path, config_dir: str
) -> dict[str, Any]:
    """P2-1 step 2, for `--strict-mcp-config`: the role's own servers plus the allowlisted ones,
    copied from the account's `.claude.json` (user scope), else from the repo's `.mcp.json`."""
    user = json_section(account_json(config_dir), "mcpServers")
    # a repo's .mcp.json is untrusted: it supplies only a name the account does not define
    known = {**json_section(workspace / ".mcp.json", "mcpServers"), **user}
    # a role's own server is its alone, and such a role gets nothing else
    owned = {s for names in ROLE_SERVERS.values() for s in names}
    shared = [n for n in store.cfg(conn, "worker.mcp_allowlist") if n not in owned]
    allowed = [] if role in ROLE_SERVERS else shared
    servers = {n: known[n] for n in (*allowed, *ROLE_SERVERS.get(role, ())) if n in known}
    if "atlassian" in servers:
        entry = servers["atlassian"]
        env = {**entry.get("env", {}), "ENABLED_TOOLS": ",".join(ATLASSIAN_TOOLS)}
        servers["atlassian"] = {**entry, "env": env}
    return {"mcpServers": {**servers, **json_section(MCP_DIR / f"{role}.json", "mcpServers")}}


def base_env(conn: sqlite3.Connection) -> dict[str, str]:
    """RUN-07: the daemon's environment minus Claude's own markers and, unless the owner lists
    them, the variables that would move the worker onto metered API billing. Nothing else."""
    drop = {*STRIPPED_ENV, *BILLING_ENV} - set(store.cfg(conn, "worker.env_passthrough"))
    return {k: v for k, v in os.environ.items() if k not in drop}


def with_config_dir(env: dict[str, str], config_dir: str) -> dict[str, str]:
    """RUN-07: Claude's own default directory is left unset. On macOS the login keychain entry
    of an explicitly set `CLAUDE_CONFIG_DIR` is keyed by that path, so naming `~/.claude` loses the login."""
    env = {k: v for k, v in env.items() if k != "CLAUDE_CONFIG_DIR"}
    if Path(config_dir).expanduser().resolve() != (Path.home() / ".claude").resolve():
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return env


def worker_env(
    conn: sqlite3.Connection,
    t: Row,
    run_id: int,
    workspace: Path,
    config_dir: str,
    advisor: str = "inherit",
) -> dict[str, str]:
    env = with_config_dir(base_env(conn), config_dir)
    exe = shutil.which("kanban")
    env["PATH"] = os.pathsep.join(
        [str(Path(exe).parent if exe else Path(sys.executable).parent), env.get("PATH", "")]
    )
    d = dirs()
    env.update(
        CLAUDE_CODE_WEB_FETCH_AGENT="0", CLAUDE_SKIP_SOUL="1", KANBAN_TASK=t["id"],
        KANBAN_BOARD=t["board"], KANBAN_RUN_ID=str(run_id), KANBAN_WORKSPACE=str(workspace),
        KANBAN_HOME=str(d.home), KANBAN_WORK=str(d.work), TMPDIR=str(card_tmp(t["id"])),
        # claude's Bash default of 120 s would cut `kanban wait-ci` (up to 540 s) off
        BASH_DEFAULT_TIMEOUT_MS="600000",
        # `-p` otherwise stops background tasks 10 min after the last turn and exits 0 (a
        # protocol_violation); 0 waits for them, and worker.stall_s ends one that hangs
        CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="0",
    )  # fmt: skip
    if t["workspace_kind"] != "scratch" and (host := origin_host(workspace)):
        env.setdefault("GH_HOST", host)
    if advisor == "off":
        env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] = "1"
    elif advisor == "on":  # an explicit arm, also against a daemon started with the switch set
        env.pop("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", None)
    return env


def origin_host(workspace: Path) -> str | None:
    """The host of the workspace's origin when it is not github.com: `gh` in a worker would
    otherwise go to github.com (the daemon always passes `-R HOST/OWNER/REPO`)."""
    url = gitops.origin_url(workspace)
    try:
        host = gitops.repo_slug(url).split("/")[0] if url else None
    except Error:
        return None
    return None if host == "github.com" else host


def login_command(env: dict[str, str]) -> str:
    """What the owner types to log in the account that `env` selects."""
    if "CLAUDE_CONFIG_DIR" in env:
        return f"CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']} claude /login"
    return "claude /login"


def probe_claude(conn: sqlite3.Connection, config_dir: str) -> dict[str, Any]:
    """One cheap `claude -p` under the account's own login; its parsed JSON result."""
    env = with_config_dir(base_env(conn), config_dir)
    claude = store.cfg(conn, "claude.bin") or shutil.which("claude") or "claude"
    p = subprocess.run(
        [claude, "-p", "ok", "--model", "haiku", "--effort", "low", "--tools", "",
         "--max-turns", "1", "--output-format", "json"],
        capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL,
    )  # fmt: skip
    out: dict[str, Any] = json.loads(p.stdout)
    return out


def auth_probe(conn: sqlite3.Connection, config_dir: str) -> str | None:
    """OBS-03: None when the login passes, else why not: only a login failure counts as one;
    a probe that could not run says so."""
    try:
        out = probe_claude(conn, config_dir)
        line = classify.first_line(out)
        if out.get("api_error_status") not in (401, 403) and not classify.AUTH_RE.match(line):
            return None
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as e:
        return f"probe failed: {e}"
    env = with_config_dir(base_env(conn), config_dir)
    return f"login failed ({line or out.get('api_error_status')}); run: {login_command(env)}"


def api_probe(conn: sqlite3.Connection, config_dir: str) -> str | None:
    """None when a model call goes through (API reachable, login good), else why not."""
    try:
        out = probe_claude(conn, config_dir)
        if not out.get("is_error"):
            return None
        return classify.first_line(out) or f"api error {out.get('api_error_status')}"
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError) as e:
        return f"probe failed: {e}"


def skill_exists(config_dir: str, name: str) -> bool:
    plugin, _, skill = name.rpartition(":")
    base = Path(config_dir)
    if not plugin:
        return (base / "skills" / skill / "SKILL.md").is_file()
    return any(base.glob(f"plugins/cache/*/{plugin}/*/skills/{skill}/SKILL.md"))


def build_prompt(
    conn: sqlite3.Connection, t: Row, workspace: Path, config_dir: str, resumed: bool, review: bool
) -> str:
    """RUN-05: the protocol with the terminal calls of the run kind, and the card's skills that
    exist here. Grants are not repeated: `kanban context` lists them, also those added later."""

    def prompt(name: str) -> str:
        return resources.files("kanban_claude").joinpath("prompts", name).read_text()

    text = (
        prompt("resume.md" if resumed else "protocol.md")
        .replace(
            "$TERMINALS", prompt("terminals-review.md" if review else "terminals-work.md").rstrip()
        )
        .replace("$RULES", prompt("rules.md"))
    )
    out = Template(text).safe_substitute(
        KANBAN_TASK=t["id"], KANBAN_BOARD=t["board"], KANBAN_WORKSPACE=str(workspace)
    )
    skills = [s for s in json.loads(t["skills"]) if skill_exists(config_dir, s)]
    if skills:
        out += f"Load and follow these skills for this card: {', '.join(skills)}.\n"
    return out


def build_argv(
    claude: str, prompt: str, role: str, task: str, model: str, effort: str, settings: str,
    budget: float, turns: int | None, sid: str, resumed: bool, mcp: str, tools: list[str],
) -> list[str]:  # fmt: skip
    argv = [
        claude, "-p", prompt, "--agent", f"{OWN_PLUGIN}:{role}", "--model", model,
        "--effort", effort, "--permission-mode", "auto", "--permission-prompts", "none",
        "--settings", settings, "--strict-mcp-config", "--mcp-config", mcp,
        "--output-format", "stream-json", "--verbose", "--no-chrome",
        "--name", f"kanban {task} {role}", "--max-budget-usd", f"{budget:g}",
        "--tools", ",".join(tools),
    ]  # fmt: skip
    if turns:
        argv += ["--max-turns", str(turns)]
    return [*argv, "--resume" if resumed else "--session-id", sid]


def launch_params(argv: list[str], env: dict[str, str], prompt_path: Path) -> dict[str, Any]:
    """How a run was launched, for `task_runs.params`: the argv without the prompt text (it is in
    the file), and only the env keys the runner sets itself."""
    shown = [*argv]
    shown[shown.index("-p") + 1] = "<prompt>"
    return {
        "argv": shown,
        "env": {k: env[k] for k in measure.PARAM_ENV if k in env},
        "prompt_path": str(prompt_path),
    }


def run_line(run: Row) -> str:
    """RUN-14."""
    secs = (run["ended_at"] or int(time.time())) - run["started_at"]
    model, effort = run["model"] or "-", run["effort"] or "-"
    exit_code = "-" if run["claude_exit_code"] is None else run["claude_exit_code"]
    return (
        f"[run] {run['role']} · {model}/{effort} · {run['outcome']} · "
        f"exit {exit_code} · {run['num_turns'] or 0} turns · {secs}s · "
        f"${run['cost_usd'] or 0:.2f} · {(run['session_id'] or '-')[:8]}"
    )


class Runner:
    def __init__(self, conn: sqlite3.Connection, run_id: int) -> None:
        self.conn = conn
        self.run_id = run_id
        self.stream = classify.Stream()
        self.measure = measure.Measure()
        self.q: queue.SimpleQueue[object] = queue.SimpleQueue()
        self.tail: collections.deque[str] = collections.deque(maxlen=40)
        self.proc: subprocess.Popen[str] | None = None
        self.sidecar: IO[str] | None = None
        self.config_dir = ""
        self.signalled = False
        self.timed_out = False
        self.stalled = False
        self.stall_s = 0
        self.progress_at = 0.0
        self.bg_tasks = False
        self.api_retrying = False
        self.killed = False
        self.eof = False
        self.term_at: float | None = None
        self.silence_at: float | None = None
        self.reap_at: float | None = None

    def row(self) -> Row:
        row: Row = self.conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (self.run_id,)
        ).fetchone()
        return row

    def task(self) -> Row:
        return store.get_task(self.conn, self.row()["task_id"])

    def go(self) -> int:
        run = self.row()
        t = self.task()
        self.config_dir = run["config_dir"] or store.effective_config_dir(
            self.conn, store.get_board(self.conn, t["board"])
        )
        if not self.register(run, t):
            return 0
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGHUP, self.on_signal)
        if self.account_blocked():
            return 0
        try:
            workspace = self.provision(t)
        except ProvisionError as e:
            print(f"[kanban] provision failed: {e}", flush=True)
            fields = {"metadata": store.dumps({"infrastructure": True})} if e.infra else None
            streak = store.infra_streak(self.conn, t["id"], self.run_id, INFRA_RETRIES)
            self.end(
                "provision_failed", error=str(e), counted=not e.infra or streak >= INFRA_RETRIES,
                run_fields=fields, retry_after=int(time.time()) + INFRA_COOLDOWN_S if e.infra else None,
            )  # fmt: skip
            return 0
        if self.signalled or self.row()["status"] != "running":
            self.end("interrupted", error="stopped before claude started")
            return 0
        try:
            return self.launch(t, workspace)
        finally:
            self.mcp_path().unlink(missing_ok=True)

    def mcp_path(self) -> Path:
        """The run's MCP config: it holds the allowlisted servers' credentials, so it is private
        and `go` removes it when the run ends."""
        return Path(self.row()["log_path"]).with_suffix(".mcp.json")

    def on_signal(self, signum: int, frame: object) -> None:
        self.signalled = True
        self.q.put(None)

    def register(self, run: Row, t: Row) -> bool:
        """RUN-01 step 1: the runner is the only writer of its pid."""
        log, stream = run_files(t["board"], t["id"], run["n"])
        pid = os.getpid()
        started = start_time(pid) or int(time.time())
        with store.tx(self.conn):
            cur = self.conn.execute(
                "UPDATE task_runs SET runner_pid = ?, runner_started = ?, log_path = ?, stream_path = ? "
                "WHERE id = ? AND status = 'running' AND runner_pid IS NULL",
                (pid, started, str(log), str(stream), self.run_id),
            )
            if cur.rowcount == 1:
                store.event(self.conn, t, "runner_registered", "runner", {"pid": pid}, self.run_id)
        return cur.rowcount == 1

    def account_blocked(self) -> bool:
        """RUN-03: the dispatcher's gate, closed again between claim and start."""
        acct = store.account_get(self.conn, self.config_dir)
        ts = int(time.time())
        until = max(acct.get("hard_until") or 0, acct.get("soft_until") or 0)
        if until > ts:
            self.end("quota", error=f"the account is held until {until}", retry_after=until)
            return True
        if acct.get("auth_paused"):
            self.end("auth_lost", error="the account's login is paused; run: kanban resume")
            return True
        return False

    def provision(self, t: Row) -> Path:
        kind, path = t["workspace_kind"], t["workspace_path"]
        if kind == "scratch":
            return ensure_dir(Path(path))
        if not path or not Path(path).is_dir():
            raise ProvisionError(f"workspace {path} is not a directory")
        if kind == "dir":
            return Path(path)
        if not t["branch_name"]:
            raise ProvisionError("a worktree card needs a branch")
        base = t["base_branch"] or ghops.default_branch(path)
        if not base:
            raise ProvisionError(f"cannot find the default branch of {path}; pass --base")
        wt = gitops.provision(path, t["id"], t["branch_name"], base)
        with store.tx(self.conn):
            self.conn.execute("UPDATE tasks SET worktree_state = 'active' WHERE id = ?", (t["id"],))
        return wt

    def launch(self, t: Row, workspace: Path) -> int:
        run = self.row()
        model, effort = model_effort(self.conn, t, run)
        sid = session_id(t["board"], t["id"], run["role"])
        resumed = was_resumed(self.config_dir, sid)
        budget = t["budget_usd"] or store.cfg(self.conn, "budget.max_usd_per_run")
        rules = [
            r
            for g in store.grants_list(self.conn, t["id"])
            if not g["revoked_at"]
            for r in g["rules"]
        ]
        mcp = self.mcp_path()
        ensure_dir(mcp.parent)
        fd = os.open(mcp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(mcp_config(self.conn, run["role"], workspace, self.config_dir), f)
        prompt = build_prompt(
            self.conn, t, workspace, self.config_dir, resumed, is_review(self.conn, run)
        )
        arm = advisor_arm(self.conn, run)
        argv = build_argv(
            store.cfg(self.conn, "claude.bin") or shutil.which("claude") or "claude", prompt,
            run["role"], t["id"],
            model, effort,
            settings_json(dirs().home, rules, run["role"], plugins_off(self.conn, self.config_dir)),
            budget, store.cfg(self.conn, "worker.max_turns"), sid, resumed, str(mcp),
            store.cfg(self.conn, f"tools.{run['role']}"),
        )  # fmt: skip
        env = worker_env(self.conn, t, self.run_id, workspace, self.config_dir, arm)
        prompt_path = Path(run["log_path"]).with_suffix(".prompt.md")
        write_private(prompt_path, prompt.encode())
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET session_id = ?, resumed = ?, model = ?, effort = ?, "
                "budget_usd = ?, config_dir = ?, advisor = ?, params = ? WHERE id = ?",
                (
                    sid,
                    int(resumed),
                    model,
                    effort,
                    budget,
                    self.config_dir,
                    arm,
                    store.dumps(launch_params(argv, env, prompt_path)),
                    self.run_id,
                ),
            )
        print(
            f"[kanban] {run['role']} {t['id']} model={model} effort={effort} session={sid} "
            f"resume={resumed} cwd={workspace} stream={run['stream_path']}",
            flush=True,
        )
        if store.cfg(self.conn, "worker.keep_awake") and shutil.which("caffeinate"):
            with contextlib.suppress(OSError):
                subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])  # noqa: S603
        ensure_dir(Path(run["stream_path"]).parent)
        ensure_dir(card_tmp(t["id"]))
        fd = os.open(run["stream_path"], os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            self.proc = subprocess.Popen(
                argv, cwd=workspace, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                process_group=0, text=True, errors="replace", bufsize=1,
            )  # fmt: skip
        except OSError as e:
            os.close(fd)
            self.end("crashed", error=f"cannot start claude: {e}")
            return 1
        self.sidecar = os.fdopen(fd, "a")
        started = start_time(self.proc.pid) or int(time.time())
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET claude_pid = ?, claude_started = ? WHERE id = ?",
                (self.proc.pid, started, self.run_id),
            )
        self.stall_s = store.cfg(self.conn, "worker.stall_s")
        self.progress_at = time.monotonic()
        self.supervise(time.monotonic() + (run["max_runtime_seconds"] or store.TRIAGE_RUNTIME_S))
        self.finish()
        return 0

    # --- supervision (RUN-08 .. RUN-11) -----------------------------------------------------

    def pump(self, pipe: IO[str]) -> None:
        for line in pipe:
            self.q.put(line)
        self.q.put(EOF)

    def stop_claude(self) -> None:
        if self.term_at is None and self.proc is not None:
            signal_group(self.proc.pid, signal.SIGTERM)
            self.term_at = time.monotonic()

    def timers(self, t: float, deadline: float) -> None:
        grace = store.cfg(self.conn, "worker.terminal_grace_s")
        if self.signalled or (self.silence_at and t >= self.silence_at):
            self.stop_claude()
        if t >= deadline and self.term_at is None:
            self.timed_out = True
            self.stop_claude()
        if self.stall_armed() and t >= self.progress_at + self.stall_s:
            self.stalled = True
            self.stop_claude()
        r = self.conn.execute(
            "SELECT status, terminal_verb FROM task_runs WHERE id = ?", (self.run_id,)
        ).fetchone()
        if r["terminal_verb"]:
            self.reap_at = self.reap_at or t + grace
        elif r["status"] != "running":
            self.stop_claude()
        if self.reap_at and t >= self.reap_at:
            self.stop_claude()
        if self.term_at is not None and not self.killed and t >= self.term_at + KILL_GRACE_S:
            self.killed = True
            signal_group(self.proc.pid if self.proc else None, signal.SIGKILL)

    def stall_armed(self) -> bool:
        """The no-progress watchdog runs unless claude waits on background tasks: that wait is quiet
        by design (`-p` ceiling 0). A main thread retrying the API meanwhile is an outage, not a wait.
        shortcut: a background task that hangs for good is left to the runtime cap."""
        return bool(
            self.stall_s and self.term_at is None and (not self.bg_tasks or self.api_retrying)
        )

    def wake_in(self, t: float, deadline: float) -> float:
        due = [t + QUIET_POLL_S]
        if self.term_at is None:
            due.append(deadline)
        elif not self.killed:
            due.append(self.term_at + KILL_GRACE_S)
        due += [x for x in (self.silence_at, self.reap_at) if x]
        if self.stall_armed():
            due.append(self.progress_at + self.stall_s)
        return max(0.05, min(due) - t)

    def supervise(self, deadline: float) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        threading.Thread(target=self.pump, args=(self.proc.stdout,), daemon=True).start()
        join_by: float | None = None
        while True:
            t = time.monotonic()
            self.timers(t, deadline)
            if self.proc.poll() is not None:
                if join_by is None:
                    signal_group(self.proc.pid, signal.SIGKILL)
                    join_by = t + READER_JOIN_S
                if self.eof or t > join_by:
                    break
            wait = self.wake_in(t, deadline)
            try:
                item = self.q.get(timeout=min(wait, join_by - t) if join_by else wait)
            except queue.Empty:
                continue
            if item is EOF:
                self.eof = True
            elif isinstance(item, str):
                self.on_line(item)

    def on_line(self, line: str) -> None:
        assert self.sidecar is not None
        self.sidecar.write(line if line.endswith("\n") else line + "\n")
        self.sidecar.flush()
        t = time.monotonic()
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        kind = self.stream.feed(ev) if isinstance(ev, dict) else None
        if isinstance(ev, dict):
            try:
                self.measure.feed(ev)
            except Exception:  # noqa: BLE001 - counting must never end a paid run
                traceback.print_exc()
        if isinstance(ev, dict):
            if ev.get("type") == "system" and ev.get("subtype") == "background_tasks_changed":
                if self.bg_tasks != bool(ev.get("tasks")):
                    self.progress_at = t  # the quiet wait was not stalled; time it afresh
                self.bg_tasks = bool(ev.get("tasks"))
            elif ev.get("type") == "system" and ev.get("subtype") == "api_retry":
                if not self.api_retrying:
                    self.progress_at = t
                self.api_retrying = True
            if classify.is_progress(ev):
                self.progress_at, self.api_retrying = t, False
        if not isinstance(ev, dict):
            self.tail.append(line.rstrip())
        if self.silence_at is not None or (kind == "limit" and self.stream.rejected):
            self.silence_at = t + WALL_WAIT_S
        if kind == "init":
            self.record_init(ev)
        elif kind == "limit":
            self.record_limit()

    def record_init(self, ev: dict[str, Any]) -> None:
        with store.tx(self.conn):
            self.conn.execute(
                "UPDATE task_runs SET claude_version = ?, permission_mode = ?, model = COALESCE(?, model) "
                "WHERE id = ?",
                (
                    ev.get("claude_code_version") or ev.get("claudeCodeVersion"),
                    ev.get("permissionMode") or ev.get("permission_mode"),
                    ev.get("model"),
                    self.run_id,
                ),
            )

    def record_limit(self) -> None:
        """RUN-09: the latest utilisation always; the hard and soft walls when they apply."""
        st, ts = self.stream, int(time.time())
        five = st.five_hour
        fields: dict[str, Any] = {
            "last_rate_limit": {
                "at": ts, "status": (st.limit or {}).get("status"),
                "five_hour": five[0] if five else None, "resets_at": five[1] if five else None,
            }
        }  # fmt: skip
        with store.tx(self.conn):
            acct = store.account_get(self.conn, self.config_dir)
            if st.rejected:
                wall = st.reset or ts + store.cfg(self.conn, "quota.requeue_s")
                fields["hard_until"] = max(acct.get("hard_until") or 0, wall)
            if five and five[1] and five[0] >= store.cfg(self.conn, "quota.soft_wall"):
                fields["soft_until"] = max(acct.get("soft_until") or 0, five[1])
            store.account_set(self.conn, self.config_dir, **fields)

    # --- the tail (RUN-12 .. RUN-16) --------------------------------------------------------

    def end(self, outcome: str, *, error: str, **kwargs: Any) -> bool:
        with store.tx(self.conn):
            ended = store.end_run(
                self.conn, self.run_id, outcome, "retry", actor="runner", event_kind=outcome,
                error=error, **kwargs,
            )  # fmt: skip
        self.afterwards()
        return ended

    def finish(self) -> None:
        assert self.proc is not None
        code = self.proc.returncode
        st = self.stream
        res = st.result or {}
        run = self.row()
        verdict = classify.classify(
            st, exit_code=code, timed_out=self.timed_out,
            now=int(time.time()), stalled_s=self.stall_s if self.stalled else 0,
            requeue_s=store.cfg(self.conn, "quota.requeue_s"), stderr="\n".join(self.tail),
        )  # fmt: skip
        if self.signalled and not run["terminal_verb"]:
            verdict = classify.Verdict("interrupted", "the runner was signalled")
        elif verdict.outcome == "auth_lost" and not self.login_confirmed_lost():
            verdict = classify.Verdict("network", f"{verdict.error} (a fresh login probe passed)")
        with store.tx(self.conn):
            store.record_result(self.conn, run, code, res)
            try:
                store.record_measure(self.conn, run, self.measure)
            except Exception:  # noqa: BLE001 - counting must never cost a paid run its result
                traceback.print_exc()
            ended = store.end_run(
                self.conn, self.run_id, verdict.outcome, "retry", actor="runner",
                event_kind=verdict.outcome, retry_after=verdict.retry_after, error=verdict.error,
            )  # fmt: skip
            detail = (
                f"classified={verdict.outcome}: {verdict.error}" if ended else "run already ended"
            )
            print(f"[kanban] exit={code} {detail}", flush=True)
        if ended and verdict.outcome == "auth_lost":
            with store.tx(self.conn):
                store.account_set(
                    self.conn,
                    self.config_dir,
                    auth_paused={"since": int(time.time()), "error": verdict.error},
                )
                store.event(
                    self.conn, None, "account_paused", "runner",
                    {
                        "config_dir": self.config_dir, "task": run["task_id"], "error": verdict.error,
                        "login": login_command(with_config_dir({}, self.config_dir)),
                    },
                )  # fmt: skip
        self.afterwards()

    def login_confirmed_lost(self) -> bool:
        """A 401/403 in a stream can be a proxy or a token race: pause the account only when a
        fresh probe under the same login fails too (a probe that cannot run counts as failed)."""
        if auth_probe(self.conn, self.config_dir) is None:
            print(
                "[kanban] auth_lost not confirmed: the login probe passed, account stays open",
                flush=True,
            )
            return False
        return True

    def afterwards(self) -> None:
        """The `[run]` line, worktree cleanup for a finished card, and a wake-up for the tick."""
        t = self.task()
        line = run_line(self.row())
        print(line, flush=True)
        store.comment(self.conn, t["id"], line, author="kanban", actor="runner")
        if t["status"] in ("done", "archived") and t["workspace_kind"] == "worktree":
            housekeeping.cleanup_card(self.conn, t)
        nudge()

    def crash(self, error: str) -> None:
        if self.proc is not None and self.proc.poll() is None:
            signal_group(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            self.end("crashed", error=f"runner error: {error}")


def run(run_id: int) -> int:
    """`kanban run RUN_ID`, started by the dispatcher (DSP-08)."""
    os.umask(0o077)
    runner = Runner(store.connect(), run_id)
    try:
        return runner.go()
    except Exception as e:
        traceback.print_exc()
        runner.crash(f"{type(e).__name__}: {e}")
        return 1
