"""Schema, migrations and every state change (STO, LIF).

Each public domain function validates its guards, writes at least one event and commits
atomically in one `BEGIN IMMEDIATE` (STO-01/02). Network and git validation runs before the
transaction; side effects on processes run after it. Status changes are compare-and-swap.
"""

import contextlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import ghops, validate
from .errors import Error, NotFound, Refused, Stale
from .paths import dirs, ensure_dir

if TYPE_CHECKING:
    from . import measure

log = logging.getLogger("kanban.store")

SCHEMA = """
CREATE TABLE boards (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, default_workdir TEXT, claude_config_dir TEXT,
  created_at INTEGER NOT NULL, archived_at INTEGER);
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, board TEXT NOT NULL REFERENCES boards(slug), title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN
    ('triage','todo','ready','running','review','blocked','done','archived')),
  assignee TEXT, implementer TEXT, priority INTEGER NOT NULL DEFAULT 0,
  workspace_kind TEXT NOT NULL DEFAULT 'scratch'
    CHECK (workspace_kind IN ('worktree','dir','scratch')),
  workspace_path TEXT, branch_name TEXT, base_branch TEXT,
  max_runtime_seconds INTEGER, max_retries INTEGER,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  protocol_violations INTEGER NOT NULL DEFAULT 0, last_failure_error TEXT,
  block_kind TEXT CHECK (block_kind IS NULL OR block_kind IN
    ('needs_input','capability','transient','stopped','gave_up')),
  block_recurrences INTEGER NOT NULL DEFAULT 0,
  model_override TEXT, reasoning_effort TEXT, budget_usd REAL,
  skills TEXT NOT NULL DEFAULT '[]', idempotency_key TEXT, retry_after INTEGER,
  pr_number INTEGER, pr_url TEXT,
  merge_state TEXT CHECK (merge_state IS NULL OR merge_state IN ('pending','merged','failed')),
  merge_attempts INTEGER NOT NULL DEFAULT 0, merge_next_at INTEGER, merged_at INTEGER,
  worktree_state TEXT CHECK (worktree_state IS NULL OR worktree_state IN
    ('active','removed','kept_dirty','kept_unpushed')),
  created_by TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  started_at INTEGER, completed_at INTEGER, version INTEGER NOT NULL DEFAULT 1);
CREATE UNIQUE INDEX idx_tasks_idempotency ON tasks(board, idempotency_key)
  WHERE idempotency_key IS NOT NULL AND status <> 'archived';
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_tasks_board ON tasks(board, status);
CREATE TABLE task_links (
  parent_id TEXT NOT NULL REFERENCES tasks(id), child_id TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (parent_id, child_id), CHECK (parent_id <> child_id));
CREATE INDEX idx_links_child ON task_links(child_id);
CREATE TABLE task_comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE INDEX idx_comments_task ON task_comments(task_id, id);
CREATE TABLE task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, board TEXT, task_id TEXT REFERENCES tasks(id),
  run_id INTEGER, kind TEXT NOT NULL, actor TEXT, payload TEXT, created_at INTEGER NOT NULL);
CREATE INDEX idx_events_task ON task_events(task_id, id);
CREATE INDEX idx_events_run ON task_events(run_id, id);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
  n INTEGER NOT NULL, role TEXT, kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work','review')),
  status TEXT NOT NULL CHECK (status IN ('running','ended')), outcome TEXT, summary TEXT,
  metadata TEXT, error TEXT, exit_code INTEGER, claude_exit_code INTEGER,
  runner_pid INTEGER, runner_started INTEGER, claude_pid INTEGER, claude_started INTEGER,
  session_id TEXT, resumed INTEGER, model TEXT, effort TEXT, budget_usd REAL,
  claude_version TEXT, permission_mode TEXT, config_dir TEXT, max_runtime_seconds INTEGER,
  started_at INTEGER NOT NULL, ended_at INTEGER, last_output_at INTEGER, num_turns INTEGER,
  cost_usd REAL, session_cost_usd REAL, subtype TEXT, is_error INTEGER, api_error_status INTEGER,
  terminal_verb TEXT, stream_path TEXT, log_path TEXT);
CREATE UNIQUE INDEX one_active_run ON task_runs(task_id) WHERE status = 'running';
CREATE INDEX idx_runs_task ON task_runs(task_id, n);
CREATE TABLE task_attachments (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
  filename TEXT NOT NULL, stored_path TEXT NOT NULL, content_type TEXT,
  size INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT, created_at INTEGER NOT NULL);
CREATE TABLE grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id),
  rules TEXT NOT NULL, approved TEXT NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER);
CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# JIR: the pairing, the id of every comment that crossed, and one link per card. A link keeps what
# each side held at the last sync (`base_*` Jira's read-back, `card_*` the card's own text), so a
# round trip that rewrites the markup is never mistaken for an edit, and the card's status and
# version then, so only a change since decides what goes out.
# SQLite cannot add a UNIQUE column, so the comment id gets a unique index of its own.
JIRA_SCHEMA = """
ALTER TABLE boards ADD COLUMN jira_project TEXT;
ALTER TABLE boards ADD COLUMN jira_jql TEXT;
ALTER TABLE task_comments ADD COLUMN jira_comment_id TEXT;
CREATE UNIQUE INDEX idx_comments_jira ON task_comments(jira_comment_id);
CREATE TABLE jira_links (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id), jira_key TEXT NOT NULL UNIQUE,
  base_title TEXT NOT NULL, base_body TEXT NOT NULL, card_title TEXT NOT NULL,
  card_body TEXT NOT NULL, base_jira_status TEXT, jira_updated TEXT, synced_status TEXT NOT NULL,
  synced_version INTEGER NOT NULL, conflict TEXT, error TEXT, created_at INTEGER NOT NULL);
"""

# U3: what a run's result event says it spent, to check `cost_usd` against a price list. The four
# counts are the invocation's own (`usage`); `model_usage` is its share per model, the rise over
# the session's previous `modelUsage`, which `session_model_usage` keeps whole (it is cumulative
# across a resume, as `total_cost_usd` is).
USAGE_SCHEMA = """
ALTER TABLE task_runs ADD COLUMN input_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN output_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN cache_write_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN model_usage TEXT;
ALTER TABLE task_runs ADD COLUMN session_model_usage TEXT;
"""

# JIR claim: who Jira said the issue was assigned to at the last sync, as the server's JSON user
# (NULL: unassigned, or not read yet). Dispatch reads it without a call. Review verdicts now cross
# to Jira too; the ones already given are marked pushed here, once, so an upgrade posts no backlog.
CLAIM_SCHEMA = """
ALTER TABLE jira_links ADD COLUMN jira_assignee TEXT;
INSERT INTO task_events(board, task_id, run_id, kind, actor, created_at)
  SELECT t.board, r.task_id, r.id, 'jira_pushed', 'jira', CAST(strftime('%s', 'now') AS INTEGER)
  FROM task_runs r JOIN tasks t ON t.id = r.task_id JOIN jira_links l ON l.task_id = r.task_id
  WHERE r.kind = 'review' AND r.terminal_verb IS NOT NULL;
"""

# dev8: `ignore_merge` keeps a promote's --ignore-merge for claim and dispatch. `owner_merge` marks
# an approved card whose PR only the owner merges (a partial review): merge_state stays 'pending',
# so children wait, but the merge job skips it. A new merge_state value would need a table rebuild
# for its CHECK. `touches` is a JSON list of the globs a card edits; `merge_method` overrides squash.
MODERNIZE_SCHEMA = """
ALTER TABLE tasks ADD COLUMN ignore_merge INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN owner_merge INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN touches TEXT;
ALTER TABLE tasks ADD COLUMN merge_method TEXT
  CHECK (merge_method IS NULL OR merge_method IN ('squash','merge','rebase'));
"""

# 0.4.0 per-run measurement. `advisor` is the arm a run used (`inherit`, `on` or `off`); the cache
# split, the advisor calls, the API time and the `harness` counts (tools, skills, hooks ...) come
# from the run's own stream; `params` is how it was launched (argv, allowlisted env, the prompt file
# and the init event). NULL `harness` marks a run nobody measured yet.
METRICS_SCHEMA = """
ALTER TABLE task_runs ADD COLUMN advisor TEXT;
ALTER TABLE task_runs ADD COLUMN advisor_calls INTEGER;
ALTER TABLE task_runs ADD COLUMN cache_write_1h_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN cache_write_5m_tokens INTEGER;
ALTER TABLE task_runs ADD COLUMN api_ms INTEGER;
ALTER TABLE task_runs ADD COLUMN harness TEXT;
ALTER TABLE task_runs ADD COLUMN params TEXT;
-- the token columns were the last result's per-turn usage; the run's `model_usage` delta covers the
-- whole run, subagent and advisor models included
UPDATE task_runs SET
  input_tokens = (SELECT COALESCE(SUM(json_extract(j.value, '$.inputTokens')), 0) FROM json_each(model_usage) j),
  output_tokens = (SELECT COALESCE(SUM(json_extract(j.value, '$.outputTokens')), 0) FROM json_each(model_usage) j),
  cache_write_tokens = (SELECT COALESCE(SUM(json_extract(j.value, '$.cacheCreationInputTokens')), 0) FROM json_each(model_usage) j),
  cache_read_tokens = (SELECT COALESCE(SUM(json_extract(j.value, '$.cacheReadInputTokens')), 0) FROM json_each(model_usage) j)
WHERE model_usage IS NOT NULL;
"""

# GitHub sync: the pairing (repo as `host/owner/repo`, an optional search query, an optional Projects v2
# board as `owner/number`), the id of every comment that crossed, and one link per card. A link keeps
# what each side held at the last sync, as `jira_links` does, plus the issue's and the project item's
# node ids. `base_state` is `done` or `open`: the issue's state, the only thing read back from GitHub
# besides the text.
GITHUB_SCHEMA = """
ALTER TABLE boards ADD COLUMN github_repo TEXT;
ALTER TABLE boards ADD COLUMN github_query TEXT;
ALTER TABLE boards ADD COLUMN github_project TEXT;
ALTER TABLE task_comments ADD COLUMN gh_comment_id TEXT;
CREATE UNIQUE INDEX idx_comments_github ON task_comments(gh_comment_id);
CREATE TABLE github_links (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id), repo TEXT NOT NULL, number INTEGER NOT NULL,
  node_id TEXT NOT NULL, project_item_id TEXT, base_title TEXT NOT NULL, base_body TEXT NOT NULL,
  card_title TEXT NOT NULL, card_body TEXT NOT NULL, base_state TEXT, gh_updated TEXT,
  synced_status TEXT NOT NULL, synced_version INTEGER NOT NULL, conflict TEXT, error TEXT,
  created_at INTEGER NOT NULL, UNIQUE (repo, number));
"""

# Forward-only: MIGRATIONS[i] takes user_version i to i + 1 (STO-03).
MIGRATIONS: list[str] = [
    SCHEMA,
    JIRA_SCHEMA,
    USAGE_SCHEMA,
    CLAIM_SCHEMA,
    MODERNIZE_SCHEMA,
    METRICS_SCHEMA,
    GITHUB_SCHEMA,
]
BACKUPS_KEPT = 5

STATUSES = ("triage", "todo", "ready", "running", "review", "blocked", "done", "archived")
NOT_RUNNING = tuple(s for s in STATUSES if s != "running")
WORKER_BLOCK_KINDS = ("needs_input", "capability", "transient")
BUILDERS = ("smith", "designer")
PARTIAL_REVIEW = "PARTIAL REVIEW"  # a warden approval the owner merges (D10), never the daemon
MERGE_METHODS = ("squash", "merge", "rebase")
ADVISOR_POLICIES = ("inherit", "off", "ab")
APPROVED_RE = re.compile(r"WARDEN: APPROVED PR #(\d+) @\s*([0-9a-f]{7,40})\b")
BLOCK_RECURRENCE_LIMIT = 2
PROTOCOL_VIOLATION_LIMIT = 3
RETRY_AFTER_S = 60
SYSTEMIC_CRASH_COUNT = 3
SYSTEMIC_WINDOW_S = 600
MAX_ATTACH_BYTES = 25 * 1024 * 1024
COUNTED_OUTCOMES = ("budget_exceeded", "timed_out", "crashed")
# the machine or the network, not the card: retried with a growing wait, counted only past the cap
TRANSIENT_OUTCOMES = ("network", "stalled")
TRANSIENT_RETRIES = 6
TRANSIENT_COOLDOWN_S = 300
TRANSIENT_COOLDOWN_MAX_S = 1800
TRIAGE_RUNTIME_S = 7200
OWNER = "owner:cli"
RUN_ENDED = "run ended; the owner stopped it or it already finished. Stop working and exit"

DEFAULTS: dict[str, Any] = {
    "claude.bin": None,
    "claude.config_dir": None,
    "dispatch.max_in_progress": 2,
    "dispatch.tick_seconds": 15,
    "dispatch.roles": ["smith", "designer", "scout", "warden"],
    "dispatch.reviewer": "warden",
    "dispatch.default_assignee": None,
    "dispatch.failure_limit": 2,
    "models.default": "sonnet",
    "models.review": "opus",
    "effort.default": "xhigh",
    "effort.review": "high",
    "effort.scout": "high",
    "effort.smith": None,
    "effort.designer": None,
    "effort.warden": None,
    "effort.atlassian": None,
    # D1: `inherit` leaves the advisor as the owner's settings have it, `off` disables it for the
    # role's runs, `ab` flips it per card (stable hash) to measure what it buys
    "advisor.smith": "inherit",
    "advisor.designer": "inherit",
    "advisor.scout": "inherit",
    "advisor.warden": "inherit",
    "advisor.atlassian": "inherit",
    "runtime.scout": 1800,
    "runtime.smith": 7200,
    "runtime.designer": 7200,
    "runtime.warden": 2700,
    "runtime.atlassian": 900,
    # D1: the built-in tools each role's `claude -p` gets (`--tools`); agent files say tools: "*"
    "tools.smith": ["Agent", "Bash", "Read", "Edit", "Write", "Skill", "ToolSearch", "LSP", "WebFetch",
                    "WebSearch", "ListMcpResourcesTool", "ReadMcpResourceTool", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"],
    "tools.designer": ["Agent", "Bash", "Read", "Edit", "Write", "Skill", "ToolSearch", "LSP",
                       "WebFetch", "WebSearch", "ListMcpResourcesTool", "ReadMcpResourceTool",
                       "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"],
    "tools.scout": ["Agent", "Bash", "Read", "Write", "Skill", "ToolSearch", "LSP", "WebFetch",
                    "WebSearch", "ListMcpResourcesTool", "ReadMcpResourceTool", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"],
    "tools.warden": ["Agent", "Bash", "Read", "Skill", "ToolSearch", "LSP", "WebFetch", "WebSearch",
                     "ListMcpResourcesTool", "ReadMcpResourceTool", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"],
    "tools.atlassian": ["Bash", "Read", "Skill", "ToolSearch", "ListMcpResourcesTool",
                        "ReadMcpResourceTool"],
    "budget.max_usd_per_run": 30,
    "budget.ceiling": 100,
    "budget.max_usd_per_card": 15.0,
    "budget.max_usd_per_board_day": None,
    "notify.command": None,
    "quota.soft_wall": 0.9,
    "quota.requeue_s": 300,
    "worker.keep_awake": True,
    "worker.terminal_grace_s": 120,
    # awake seconds with no assistant/tool event before the run is stopped and retried (0 = off);
    # above the 600 s Bash cap and the 540 s `wait-ci` poll
    "worker.stall_s": 1200,
    "worker.max_turns": None,
    "worker.env_passthrough": [],
    "worker.plugin_allowlist": [],
    "worker.mcp_allowlist": ["context7", "lightpanda", "code-review-graph"],
    "retention.stream_days": 30,
    "roles.atlassian.enabled": False,
    "jira.mcp_command": None,
    "jira.assignee": None,
    # ponytail: one switch for every paired board; a boards column if boards pair with different sites
    "jira.claim": False,
    "jira.issue_type": "Task",
    "jira.label": "kanban-claude",
    "jira.poll_s": 120,
    "jira.status_map": {
        "triage": "To Do", "todo": "To Do", "ready": "To Do",
        "running": "In Progress", "review": "In Progress", "done": "Done",
    },
    "github.label": "kanban-claude",
    "github.poll_s": 120,
    # the Status options of a new Projects v2 board; only used when a board has --github-project
    "github.status_map": {
        "triage": "Todo", "todo": "Todo", "ready": "Todo",
        "running": "In Progress", "review": "In Progress", "done": "Done",
    },
}  # fmt: skip

BOUNDS: dict[str, tuple[int, int | None]] = {
    "jira.poll_s": (1, None),
    "github.poll_s": (1, None),
    "dispatch.max_in_progress": (1, None),
    "dispatch.tick_seconds": (1, None),
    "dispatch.failure_limit": (1, None),
    "retention.stream_days": (1, None),
    "worker.max_turns": (1, None),
    "worker.stall_s": (0, None),
}

Row = sqlite3.Row


def now() -> int:
    return int(time.time())


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


# --- connection, transactions, migrations ---------------------------------------------------


def _statements(sql: str) -> Iterator[str]:
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""


def _backup(conn: sqlite3.Connection, directory: Path, version: int) -> None:
    ensure_dir(directory)
    dest = (
        directory / f"kanban-s{version}-{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(2)}.db"
    )
    os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    out = sqlite3.connect(dest)
    try:
        conn.backup(out)
    finally:
        out.close()
    for old in sorted(directory.glob("kanban-s*.db"), key=lambda p: p.stat().st_mtime)[
        :-BACKUPS_KEPT
    ]:
        old.unlink()


def migrate(conn: sqlite3.Connection, backups: Path) -> int:
    """Apply pending migrations after a backup (STO-03). Idempotent (STO-10)."""
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        raise Error(
            f"database schema {version} is newer than this kanban ({len(MIGRATIONS)}); "
            "upgrade the kanban-claude plugin and runtime"
        )
    if version == len(MIGRATIONS):
        return version
    if version > 0:
        _backup(conn, backups, version)
    for target in range(version, len(MIGRATIONS)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("PRAGMA user_version").fetchone()[0] <= target:
                for stmt in _statements(MIGRATIONS[target]):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {target + 1}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


def connect(home: Path | None = None, *, upgrade: bool = True) -> sqlite3.Connection:
    """Open `$KANBAN_HOME/kanban.db` (0600, created before SQLite touches it, SEC-06)."""
    home = ensure_dir(home or dirs().home)
    db = home / "kanban.db"
    with contextlib.suppress(FileExistsError):
        os.close(os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    conn = sqlite3.connect(db, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    if upgrade:
        migrate(conn, home / "backups")
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Inside an open transaction the block joins it, and the outermost `tx` commits or rolls back.

    A COMMIT that fails (a deferred foreign key) leaves the transaction open, so it is rolled back here too.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# --- config and state -----------------------------------------------------------------------


def cfg(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row:
        return json.loads(row["value"])
    return json.loads(json.dumps(DEFAULTS[key]))


def active_roles(conn: sqlite3.Connection, roles: list[str] | None = None) -> list[str]:
    """The roles a card may be assigned to: `dispatch.roles`, plus `atlassian` once it is enabled and
    the server it talks to is known (JIR-01)."""
    roles = cfg(conn, "dispatch.roles") if roles is None else roles
    on = cfg(conn, "roles.atlassian.enabled") and cfg(conn, "jira.mcp_command")
    return [*roles, "atlassian"] if on and "atlassian" not in roles else roles


def config_all(conn: sqlite3.Connection) -> dict[str, Any]:
    return {k: cfg(conn, k) for k in sorted(DEFAULTS)}


def config_set(conn: sqlite3.Connection, key: str, value: Any, *, actor: str = OWNER) -> None:
    if key not in DEFAULTS:
        raise Refused(f"unknown config key {key!r}; list them with: kanban config list")
    default = DEFAULTS[key]
    if key.startswith("runtime."):
        value = validate.duration(value)
    elif default is not None and type(value) is not type(default):
        ok_number = isinstance(default, float) and type(value) is int
        if not ok_number:
            raise Refused(f"{key} must be {type(default).__name__}, got {value!r}")
    if key.startswith("tools.") and not all(isinstance(t, str) for t in value):
        raise Refused(f"{key} must be a list of tool names, got {value!r}")
    if value is not None and key.startswith("effort."):
        validate.effort(value)
    if key.startswith("advisor.") and value not in ADVISOR_POLICIES:
        raise Refused(f"{key} must be one of {', '.join(ADVISOR_POLICIES)}, got {value!r}")
    if value is not None and key.startswith("models."):
        validate.model(value)
    roles = active_roles(conn, value if key == "dispatch.roles" else None)
    if key == "dispatch.roles":
        if not value or not all(isinstance(r, str) for r in value):
            raise Refused("dispatch.roles must be a non-empty list of role names")
        default_assignee = cfg(conn, "dispatch.default_assignee")
        if default_assignee is not None and default_assignee not in roles:
            raise Refused(f"dispatch.default_assignee {default_assignee!r} would leave the roles")
    unset_ok = value is None and key == "dispatch.default_assignee"
    is_role_key = key in ("dispatch.default_assignee", "dispatch.reviewer")
    if is_role_key and not unset_ok and value not in roles:
        raise Refused(f"{key} must be one of dispatch.roles ({', '.join(roles)})")
    if key in BOUNDS and value is not None:
        low, high = BOUNDS[key]
        if type(value) is not int or value < low or (high is not None and value > high):
            span = f"from {low} to {high}" if high else f"of at least {low}"
            raise Refused(f"{key} must be an integer {span}")
    if key == "budget.ceiling" and value <= 0:
        raise Refused("budget.ceiling must be positive")
    if (
        key in ("budget.max_usd_per_card", "budget.max_usd_per_board_day")
        and value is not None
        and (type(value) not in (int, float) or value < 0)
    ):
        raise Refused(f"{key} must be a number of dollars, 0 for off (board_day: or null)")
    if (
        key == "jira.mcp_command"
        and value is not None
        and not (isinstance(value, list) and value and all(isinstance(a, str) for a in value))
    ):
        raise Refused("jira.mcp_command must be a non-empty list of strings (argv)")
    if (
        key == "notify.command"
        and value is not None
        and not (
            (isinstance(value, str) and value.strip())
            or (isinstance(value, list) and value and all(isinstance(a, str) for a in value))
        )
    ):
        raise Refused("notify.command must be a command line or a non-empty list of strings (argv)")
    if key in ("jira.status_map", "github.status_map") and not (
        isinstance(value, dict)
        and all(k in STATUSES and isinstance(v, str) and v for k, v in value.items())
    ):
        raise Refused(f"{key} maps card statuses ({', '.join(STATUSES)}) to status names")
    if key == "github.label" and not value.strip():
        raise Refused("github.label must not be empty")
    with tx(conn):
        config_put(conn, key, value)
        event(conn, None, "config_changed", actor, {"key": key, "value": value})


def config_put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """The bare upsert, for callers already inside a transaction (no validation, no event)."""
    conn.execute(
        "INSERT INTO config(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, json.dumps(value), now()),
    )


def state_get(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def state_put(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# --- events ---------------------------------------------------------------------------------


def event(
    conn: sqlite3.Connection,
    t: Row | dict[str, Any] | None,
    kind: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO task_events(board, task_id, run_id, kind, actor, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            t["board"] if t else None,
            t["id"] if t else None,
            run_id,
            kind,
            actor,
            None if payload is None else dumps(payload),
            now(),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


# What needs the owner. `awaiting_owner_merge` is a partial approval; the owner's own events never count.
ACTIONABLE_EVENTS = (
    "blocked", "block_loop_detected", "gave_up", "merge_failed", "awaiting_owner_merge",
    "account_paused", "quota_wall", "dispatch_paused",
)  # fmt: skip


def actionable(ev: dict[str, Any]) -> bool:
    return ev["kind"] in ACTIONABLE_EVENTS and not str(ev["actor"]).startswith("owner:")


def events_since(
    conn: sqlite3.Connection,
    after: int,
    *,
    kinds: Sequence[str] = (),
    board: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT e.*, t.assignee FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE e.id > ?"
    )
    args: list[Any] = [after]
    if kinds:
        sql += f" AND e.kind IN ({','.join('?' * len(kinds))})"
        args += kinds
    if board:
        sql += " AND (e.board = ? OR e.board IS NULL)"
        args.append(board)
    rows = conn.execute(sql + " ORDER BY e.id LIMIT ?", [*args, limit]).fetchall()
    return [dict(r) for r in rows]


# --- lookups --------------------------------------------------------------------------------


def get_task(conn: sqlite3.Connection, task_id: str) -> Row:
    validate.task_id(task_id)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(f"no card {task_id}; list cards with: kanban list")
    return row  # type: ignore[no-any-return]


def get_board(conn: sqlite3.Connection, slug: str) -> Row:
    row = conn.execute("SELECT * FROM boards WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise NotFound(f"no board {slug!r}; list boards with: kanban boards list")
    return row  # type: ignore[no-any-return]


def _all_done(conn: sqlite3.Connection, parent_ids: Sequence[str], ignore_merge: bool) -> bool:
    open_parent = conn.execute(
        f"SELECT 1 FROM tasks p WHERE p.id IN ({','.join('?' * len(parent_ids))}) "
        "AND NOT (p.status IN ('done', 'archived') "
        "AND (? OR p.merge_state IS NULL OR p.merge_state = 'merged')) LIMIT 1",
        (*parent_ids, ignore_merge),
    ).fetchone()
    return open_parent is None


def parents_satisfied(
    conn: sqlite3.Connection, task_id: str, *, ignore_merge: bool = False
) -> bool:
    """STO-09: every parent is done or archived, and merged unless `ignore_merge` or the card's own
    waiver (a promote with --ignore-merge) says otherwise. Reading the waiver here reaches every caller."""
    q = "SELECT parent_id FROM task_links WHERE child_id = ?"
    parents = [r[0] for r in conn.execute(q, (task_id,))]
    if not parents:
        return True
    waived = conn.execute("SELECT ignore_merge FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _all_done(conn, parents, ignore_merge or bool(waived and waived[0]))


def _cas(
    conn: sqlite3.Connection,
    task_id: str,
    expected: Sequence[str],
    *,
    version: int | None = None,
    **fields: Any,
) -> None:
    """Status compare-and-swap (STO-02), and with `version` the card must still be at it (WEB-09).
    Any rowcount but 1 means the card moved under us; a version mismatch is reported first.
    Moving to any status but `blocked` drops the block chip, unless the caller sets it."""
    if fields.get("status", "blocked") != "blocked":
        fields = {"block_kind": None, "block_recurrences": 0, **fields}
    sets = "".join(f"{k} = ?, " for k in fields)
    guard = "" if version is None else " AND version = ?"
    cur = conn.execute(
        f"UPDATE tasks SET {sets}updated_at = ?, version = version + 1 "
        f"WHERE id = ? AND status IN ({','.join('?' * len(expected))}){guard}",
        [*fields.values(), now(), task_id, *expected, *([] if version is None else [version])],
    )
    if cur.rowcount != 1:
        now_row = conn.execute(
            "SELECT status, version FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if now_row and version is not None and now_row["version"] != version:
            raise Stale(f"card {task_id} changed elsewhere since it was read; re-read it and retry")
        raise Refused(
            f"card {task_id} is {now_row['status'] if now_row else 'gone'}; "
            f"this needs it in {' or '.join(expected)}"
        )


def _comment(
    conn: sqlite3.Connection, t: Row, author: str, body: str, actor: str, jira_id: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO task_comments(task_id, author, body, created_at, jira_comment_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (t["id"], author, body, now(), jira_id),
    )
    assert cur.lastrowid is not None
    event(conn, t, "commented", actor, {"comment_id": cur.lastrowid, "author": author})
    return cur.lastrowid


def _need(text: str | None, what: str, fix: str) -> str:
    if not text or not text.strip():
        raise Refused(f"{what} is required; {fix}")
    return text.strip()


# --- boards ---------------------------------------------------------------------------------


def _jira_fields(
    conn: sqlite3.Connection, current: str | None, project: str | None, jql: str | None, off: bool
) -> dict[str, Any]:
    """JIR-01: `--jira-off` unpairs and keeps the links. A pairing names the account new issues go to."""
    if off:
        if project or jql is not None:
            raise Refused("--jira-off cannot be combined with --jira-project or --jira-jql")
        return {"jira_project": None, "jira_jql": None}
    fields: dict[str, Any] = {}
    if project is not None:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", project):
            raise Refused(f"Jira project key {project!r} must be upper case, like KAN")
        if not cfg(conn, "jira.assignee"):
            raise Refused(
                "set the account new issues go to first: kanban config set jira.assignee ACCOUNT"
            )
        fields["jira_project"] = project
        if project != current:
            fields["jira_jql"] = None
    if jql is not None:
        if not (project or current):
            raise Refused("--jira-jql needs a paired board; pass --jira-project KEY")
        fields["jira_jql"] = jql
    return fields


REPO_RE = re.compile(r"(?:([A-Za-z0-9.-]+)/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def _github_fields(
    current: str | None, repo: str | None, query: str | None, project: str | None, off: bool
) -> dict[str, Any]:
    """The GitHub pairing, with the repo stored as `host/owner/repo`. `--github-off` unpairs and
    keeps the links."""
    if off:
        if repo or query is not None or project is not None:
            raise Refused("--github-off cannot be combined with the other --github-* flags")
        return {"github_repo": None, "github_query": None, "github_project": None}
    fields: dict[str, Any] = {}
    if repo is not None:
        m = REPO_RE.fullmatch(repo)
        if not m:
            raise Refused(f"--github-repo {repo!r} must look like [HOST/]OWNER/REPO")
        fields["github_repo"] = f"{m[1] or 'github.com'}/{m[2]}/{m[3]}"
        if fields["github_repo"] != current:
            fields |= {"github_query": None, "github_project": None}
    if (query is not None or project is not None) and not (repo or current):
        raise Refused("--github-query and --github-project need a paired board; pass --github-repo")
    if query is not None:
        fields["github_query"] = query or None
    if project is not None:
        if project and not re.fullmatch(r"[A-Za-z0-9_.-]+/[1-9][0-9]*", project):
            raise Refused(f"--github-project {project!r} must look like OWNER/NUMBER")
        fields["github_project"] = project or None
    return fields


def _one_tracker(jira_project: str | None, github_repo: str | None) -> None:
    if jira_project and github_repo:
        raise Refused("a board pairs with Jira or with GitHub, not both; unpair one first")


def board_create(
    conn: sqlite3.Connection,
    slug: str,
    *,
    default_workdir: str,
    name: str | None = None,
    claude_config_dir: str | None = None,
    jira_project: str | None = None,
    jira_jql: str | None = None,
    github_repo: str | None = None,
    github_query: str | None = None,
    github_project: str | None = None,
    actor: str = OWNER,
) -> dict[str, Any]:
    validate.slug(slug)
    workdir = validate.workspace("dir", default_workdir)
    with tx(conn):
        if conn.execute("SELECT 1 FROM boards WHERE slug = ?", (slug,)).fetchone():
            raise Refused(f"board {slug!r} already exists; edit it with: kanban boards edit {slug}")
        jira = _jira_fields(conn, None, jira_project, jira_jql, False)
        gh = _github_fields(None, github_repo, github_query, github_project, False)
        _one_tracker(jira.get("jira_project"), gh.get("github_repo"))
        cols = {
            "slug": slug,
            "name": name or slug,
            "default_workdir": workdir,
            "claude_config_dir": claude_config_dir,
            "jira_project": jira.get("jira_project"),
            "jira_jql": jira.get("jira_jql"),
            **gh,  # only the GitHub columns that are set, so a board can still be made before the migration
            "created_at": now(),
        }
        conn.execute(
            f"INSERT INTO boards({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            tuple(cols.values()),
        )
        event(conn, {"board": slug, "id": None}, "board_created", actor, {"slug": slug})
    return {"slug": slug}


def board_edit(
    conn: sqlite3.Connection,
    slug: str,
    *,
    default_workdir: str | None = None,
    name: str | None = None,
    claude_config_dir: str | None = None,
    jira_project: str | None = None,
    jira_jql: str | None = None,
    jira_off: bool = False,
    github_repo: str | None = None,
    github_query: str | None = None,
    github_project: str | None = None,
    github_off: bool = False,
    actor: str = OWNER,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if default_workdir is not None:
        fields["default_workdir"] = validate.workspace("dir", default_workdir)
    if name is not None:
        fields["name"] = name
    if claude_config_dir is not None:
        fields["claude_config_dir"] = claude_config_dir
    if not (
        fields or jira_project or jira_jql is not None or jira_off
        or github_repo or github_query is not None or github_project is not None or github_off
    ):  # fmt: skip
        raise Refused(
            "nothing to change; pass --default-workdir, --name, --claude-config-dir, "
            "--jira-project, --jira-jql, --jira-off, --github-repo, --github-query, "
            "--github-project or --github-off"
        )
    with tx(conn):
        b = get_board(conn, slug)
        fields |= _jira_fields(conn, b["jira_project"], jira_project, jira_jql, jira_off)
        fields |= _github_fields(
            b["github_repo"], github_repo, github_query, github_project, github_off
        )
        _one_tracker(
            fields.get("jira_project", b["jira_project"]),
            fields.get("github_repo", b["github_repo"]),
        )
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE boards SET {sets} WHERE slug = ?", [*fields.values(), slug])
        event(conn, {"board": slug, "id": None}, "board_edited", actor, {"fields": sorted(fields)})
    return {"slug": slug}


def board_archive(conn: sqlite3.Connection, slug: str, *, actor: str = OWNER) -> dict[str, Any]:
    with tx(conn):
        b = get_board(conn, slug)
        if b["archived_at"] is None:
            conn.execute("UPDATE boards SET archived_at = ? WHERE slug = ?", (now(), slug))
            event(conn, {"board": slug, "id": None}, "board_archived", actor, {"slug": slug})
    return {"slug": slug}


def boards_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM boards ORDER BY slug")]


# --- cards: create, specify, edit -----------------------------------------------------------

FIELD_COLUMNS = {
    "title": "title",
    "body": "body",
    "assignee": "assignee",
    "priority": "priority",
    "branch": "branch_name",
    "base": "base_branch",
    "max_runtime": "max_runtime_seconds",
    "max_retries": "max_retries",
    "skills": "skills",
    "model": "model_override",
    "effort": "reasoning_effort",
    "budget": "budget_usd",
    "merge_method": "merge_method",
    "touches": "touches",
}


def claude_dirs(conn: sqlite3.Connection, board: Row | None = None) -> tuple[Path, ...]:
    found = [
        cfg(conn, "claude.config_dir"),
        os.environ.get("CLAUDE_CONFIG_DIR"),
        Path.home() / ".claude",
    ]
    if board is not None:
        found.append(board["claude_config_dir"])
    return tuple(Path(p) for p in found if p)


def effective_config_dir(conn: sqlite3.Connection, board: Row) -> str:
    """STO-08: the board's directory, else the global one, else Claude's own default."""
    found = board["claude_config_dir"] or cfg(conn, "claude.config_dir")
    return str(found or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def account_get(conn: sqlite3.Connection, config_dir: str) -> dict[str, Any]:
    """`hard_until`, `soft_until`, `last_rate_limit`, `auth_paused` of one Claude login (STO-08)."""
    return state_get(conn, f"account:{config_dir}") or {}


def account_set(conn: sqlite3.Connection, config_dir: str, **fields: Any) -> None:
    """Merge `fields` into the account's state, inside the caller's transaction."""
    state_put(conn, f"account:{config_dir}", {**account_get(conn, config_dir), **fields})


def _columns(conn: sqlite3.Connection, fields: dict[str, Any], board: Row) -> dict[str, Any]:
    """Field-level validation (SEC-12) of the provided fields, as DB columns."""
    out: dict[str, Any] = {}
    roles = active_roles(conn)
    for key, v in fields.items():
        if key == "workspace":
            kind, path = validate.parse_workspace(v)
            out["workspace_kind"] = kind
            out["workspace_path"] = validate.workspace(
                kind, path, claude_dirs=claude_dirs(conn, board), strict=False
            )
            continue
        col = FIELD_COLUMNS[key]
        if key == "title":
            v = _need(v, "title", "pass a non-empty title")
        elif key == "assignee" and v is not None and v not in roles:
            raise Refused(f"assignee {v!r} is not a dispatchable role ({', '.join(roles)})")
        elif key == "priority" and not isinstance(v, int):
            raise Refused(f"priority {v!r} must be an integer")
        elif key == "max_runtime":
            v = validate.duration(v)
        elif key == "max_retries" and (not isinstance(v, int) or v < 1):
            raise Refused(f"max-retries {v!r} must be a positive integer")
        elif key == "skills":
            v = dumps(validate.skills(v))
        elif key == "touches":
            v = None if v is None or list(v) == [""] else dumps(validate.touches(v))
        elif key == "model" and v is not None:
            validate.model(v)
        elif key == "effort" and v is not None:
            validate.effort(v)
        elif key == "budget" and v is not None:
            v = validate.budget(v, cfg(conn, "budget.ceiling"))
        elif key in ("branch", "base") and v is not None:
            validate.branch(v)
        elif key == "merge_method" and v not in MERGE_METHODS:
            raise Refused(f"merge-method {v!r} must be one of {', '.join(MERGE_METHODS)}")
        out[col] = v
    return out


def _dispatchable(conn: sqlite3.Connection, t: dict[str, Any], board: Row) -> None:
    """The non-triage checks of lifecycle #1, on a merged card (SEC-10, GIT-01/09, MN-31)."""
    _need(
        t["body"], "body", "pass --body-file - with the card contract, or keep the card in --triage"
    )
    roles = active_roles(conn)
    if not t["assignee"]:
        raise Refused("no assignee given; pass --assignee or keep the card in --triage")
    if t["assignee"] not in roles:
        raise Refused(
            f"assignee {t['assignee']!r} is not a dispatchable role ({', '.join(roles)}); "
            "pass --assignee or keep the card in --triage"
        )
    kind = t["workspace_kind"]
    if kind == "scratch":
        if t["branch_name"]:
            raise Refused(
                "a scratch card has no repo, so a branch cannot apply; "
                "pass --workspace worktree:<repo> with --branch, or leave the branch out"
            )
        return
    validate.workspace(kind, t["workspace_path"], claude_dirs=claude_dirs(conn, board))
    if kind == "worktree":
        if not t["branch_name"]:
            raise Refused("a worktree card needs a branch; pass --branch feature/<slug>")
        default = ghops.default_branch(t["workspace_path"])
        validate.branch(t["branch_name"], default)
        if t["base_branch"]:
            validate.branch(t["base_branch"])


def _new_id(conn: sqlite3.Connection) -> str:
    while True:
        tid = "t_" + secrets.token_hex(4)
        if not conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone():
            return tid


def create(
    conn: sqlite3.Connection,
    board: str,
    title: str,
    *,
    actor: str = OWNER,
    body: str = "",
    assignee: str | None = None,
    workspace: str | None = None,
    branch: str | None = None,
    base: str | None = None,
    priority: int = 0,
    max_runtime: str | int | None = None,
    max_retries: int | None = None,
    idempotency_key: str | None = None,
    parents: Sequence[str] = (),
    skills: Sequence[str] = (),
    model: str | None = None,
    effort: str | None = None,
    budget: float | None = None,
    merge_method: str | None = None,
    triage: bool = False,
    grants: Sequence[str] = (),
    approved: str | None = None,
    touches: Sequence[str] | None = None,
) -> dict[str, Any]:
    if grants:
        approved = _check_grant(grants, approved)
    elif approved:
        raise Refused("--approved belongs to --grant RULE; pass the rule it approves")
    b = get_board(conn, board)
    if b["archived_at"] is not None:
        raise Refused(f"board {board!r} is archived; unarchive is not offered, create a new board")
    if assignee is None and not triage:
        assignee = cfg(conn, "dispatch.default_assignee")
    given: dict[str, Any] = {
        "title": title, "body": body, "assignee": assignee, "priority": priority,
        "branch": branch, "base": base, "max_retries": max_retries, "skills": list(skills),
        "merge_method": merge_method,
        "model": model, "effort": effort, "budget": budget, "touches": touches,
    }  # fmt: skip
    if max_runtime is not None:
        given["max_runtime"] = max_runtime
    if workspace is not None:
        given["workspace"] = workspace
    tid = _new_id(conn)
    cols = _columns(conn, {k: v for k, v in given.items() if v is not None or k == "body"}, b)
    cols.setdefault("workspace_kind", "scratch")
    if cols["workspace_kind"] == "scratch":
        cols["workspace_path"] = str(dirs().work / "scratch" / tid)
    elif cols["workspace_kind"] == "worktree" and "base_branch" not in cols:
        same_repo = [
            pt for pt in (get_task(conn, p) for p in parents)
            if pt["workspace_kind"] == "worktree" and pt["workspace_path"] == cols["workspace_path"]
        ]  # fmt: skip
        if len(same_repo) == 1 and same_repo[0]["base_branch"]:
            cols["base_branch"] = same_repo[0]["base_branch"]
    cols.setdefault("max_runtime_seconds", None if triage else role_runtime(conn, assignee))
    row = {**_EMPTY_CARD, **cols, "id": tid, "board": board, "assignee": assignee}
    if not triage:
        _dispatchable(conn, row, b)
    ts = now()
    with tx(conn):
        if idempotency_key:
            dup = conn.execute(
                "SELECT id, status FROM tasks WHERE board = ? AND idempotency_key = ? AND status <> 'archived'",
                (board, idempotency_key),
            ).fetchone()
            if dup:
                got = {"id": dup["id"], "status": dup["status"], "existing": True}
                if grants:
                    got["warnings"] = [f"card {dup['id']} already exists; the grant was not added"]
                return got
        warnings = []
        for p in parents:
            pt = get_task(conn, p)
            if pt["board"] != board:
                raise Refused(f"parent {p} is on board {pt['board']!r}, not {board!r}")
            if pt["status"] == "triage":
                warnings.append(f"parent {p} is in triage; {tid} waits until it is done")
        status = "triage" if triage else "ready" if _all_done(conn, parents, False) else "todo"
        row.update(
            status=status, idempotency_key=idempotency_key, created_by=actor,
            created_at=ts, updated_at=ts, version=1,
        )  # fmt: skip
        names = list(row)
        conn.execute(
            f"INSERT INTO tasks({','.join(names)}) VALUES ({','.join('?' * len(names))})",
            [row[n] for n in names],
        )
        for p in parents:
            conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (p, tid))
        event(
            conn,
            row,
            "created",
            actor,
            {"status": status, "assignee": assignee, "parents": list(parents)},
        )
        got = {"id": tid, "status": status, "existing": False}
        if grants and approved:
            got["grant_id"] = _insert_grant(conn, row, grants, approved, actor)
    return {**got, "warnings": warnings} if warnings else got


def role_runtime(conn: sqlite3.Connection, role: str | None) -> int:
    key = f"runtime.{role}"
    return int(cfg(conn, key)) if key in DEFAULTS else TRIAGE_RUNTIME_S


_EMPTY_CARD: dict[str, Any] = {
    "title": "", "body": "", "assignee": None, "implementer": None, "priority": 0,
    "workspace_kind": "scratch", "workspace_path": None, "branch_name": None, "base_branch": None,
    "max_retries": None, "model_override": None, "reasoning_effort": None, "budget_usd": None,
    "skills": "[]",
}  # fmt: skip


def specify(
    conn: sqlite3.Connection,
    task_id: str,
    fields: dict[str, Any],
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    t = get_task(conn, task_id)
    if t["status"] != "triage":
        raise Refused(
            f"card {task_id} is {t['status']}; specify applies to triage cards (edit changes others)"
        )
    b = get_board(conn, t["board"])
    cols = _columns(conn, {k: v for k, v in fields.items() if v is not None}, b)
    merged = {**dict(t), **cols}
    if merged["max_runtime_seconds"] is None:
        cols["max_runtime_seconds"] = merged["max_runtime_seconds"] = role_runtime(
            conn, merged["assignee"]
        )
    _dispatchable(conn, merged, b)
    with tx(conn):
        status = "ready" if parents_satisfied(conn, task_id) else "todo"
        _cas(conn, task_id, ["triage"], version=expect_version, **cols, status=status)
        event(conn, t, "specified", actor, {"fields": sorted(cols), "status": status})
    return {"id": task_id, "status": status}


def edit(
    conn: sqlite3.Connection,
    task_id: str,
    fields: dict[str, Any],
    *,
    actor: str = OWNER,
    allow_empty_body: bool = False,
    expect_version: int | None = None,
    assignee: str | None = None,
) -> dict[str, Any]:
    """`assignee` reassigns in the same transaction, after every check that may call git or gh."""
    t = get_task(conn, task_id)
    if t["status"] == "running":
        raise Refused(f"card {task_id} is running; stop it first with: kanban stop {task_id}")
    if "body" in fields and not (fields["body"] or "").strip() and not allow_empty_body:
        raise Refused("refusing to wipe the card body; pass --allow-empty-body to do it on purpose")
    if "assignee" in fields:
        raise Refused("edit does not change the assignee; use: kanban assign")
    b = get_board(conn, t["board"])
    cols = _columns(conn, fields, b)
    if not cols:
        raise Refused("nothing to change")
    if t["status"] in ("todo", "ready"):
        _dispatchable(conn, {**dict(t), **cols, **({"assignee": assignee} if assignee else {})}, b)
    previous = {k: t[k] for k in cols}
    version = t["version"] if expect_version is None else expect_version
    with tx(conn):
        if assignee and not assign(
            conn, task_id, assignee, actor=actor, expect_version=version
        ).get("noop"):
            version += 1  # a changed role is one version bump of its own
        _cas(conn, task_id, NOT_RUNNING, version=version, **cols)
        event(conn, t, "edited", actor, {"fields": sorted(cols), "previous": previous})
    return {"id": task_id, "fields": sorted(cols)}


# --- owner transitions ----------------------------------------------------------------------


def assign(
    conn: sqlite3.Connection,
    task_id: str,
    role: str,
    *,
    reclaim: bool = False,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    roles = active_roles(conn)
    if role not in roles:
        raise Refused(f"assignee {role!r} is not a dispatchable role ({', '.join(roles)})")
    with tx(conn):
        t = get_task(conn, task_id)
        changed = t["assignee"] != role
        reset = {"consecutive_failures": 0, "protocol_violations": 0} if changed else {}
        payload = {"assignee": role, "from": t["assignee"]}
        if t["status"] == "running":
            if not reclaim:
                raise Refused(f"card {task_id} is running; pass --reclaim to stop its run first")
            run = _running_run(conn, task_id)
            end_run(
                conn, run["id"], "terminated", "retry", actor=actor, event_kind="assigned",
                payload={**payload, "reclaim": True}, counted=False,
                card_fields={"assignee": role, **reset},
            )  # fmt: skip
            return {"id": task_id, "assignee": role, "run_id": run["id"]}
        if not changed:
            return {"id": task_id, "assignee": role, "noop": True}
        _cas(conn, task_id, NOT_RUNNING, version=expect_version, assignee=role, **reset)
        event(conn, t, "assigned", actor, payload)
    return {"id": task_id, "assignee": role}


def promote(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    ignore_merge: bool = False,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        if not parents_satisfied(conn, task_id, ignore_merge=ignore_merge):
            raise Refused(
                f"card {task_id} still has an open parent; finish it, unlink it, "
                "or for a stuck merge pass --ignore-merge"
            )
        _cas(
            conn, task_id, ["todo"], version=expect_version, status="ready", retry_after=None,
            ignore_merge=int(ignore_merge),
        )  # fmt: skip
        event(
            conn,
            t,
            "promoted_manual",
            actor,
            {"actor": actor, "reason": reason, "ignore_merge": ignore_merge},
        )
    return {"id": task_id, "status": "ready"}


def promote_auto(conn: sqlite3.Connection, task_id: str, *, actor: str = "dispatcher") -> bool:
    """DSP-03: a todo card whose parents are satisfied becomes ready."""
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] != "todo" or not parents_satisfied(conn, task_id):
            return False
        _cas(conn, task_id, ["todo"], status="ready")
        event(conn, t, "promoted", actor)
    return True


def hold(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        _cas(
            conn, task_id, ["todo", "ready", "blocked"], version=expect_version, status="triage",
            ignore_merge=0,
        )  # fmt: skip
        event(conn, t, "held", actor, {"reason": reason, "from": t["status"]})
    return {"id": task_id, "status": "triage"}


def last_run_kind(conn: sqlite3.Connection, task_id: str) -> str | None:
    row = conn.execute(
        "SELECT kind FROM task_runs WHERE task_id = ? ORDER BY n DESC LIMIT 1", (task_id,)
    ).fetchone()
    return row["kind"] if row else None


def blocked_in_review(conn: sqlite3.Connection, task_id: str) -> bool:
    """Whether a blocked card left review: blocked by a review run, or by a budget cap at a review claim."""
    row = conn.execute(
        "SELECT kind, json_extract(payload, '$.from') AS src FROM task_events WHERE task_id = ? "
        "AND (kind = 'claimed' OR (kind = 'blocked' AND json_extract(payload, '$.from') IS NOT NULL)) "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row and row["kind"] == "blocked":
        return bool(row["src"] == "review")
    return last_run_kind(conn, task_id) == "review"


def unblock(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        if blocked_in_review(conn, task_id):
            target = "review"
        else:
            target = "ready" if parents_satisfied(conn, task_id) else "todo"
        _cas(
            conn, task_id, ["blocked"], version=expect_version, status=target,
            consecutive_failures=0, protocol_violations=0, last_failure_error=None,
            retry_after=None,
        )  # fmt: skip
        _comment(conn, t, "owner", f"UNBLOCK: {reason}", actor)
        event(conn, t, "unblocked", actor, {"reason": reason, "status": target})
    return {"id": task_id, "status": target}


def reopen_review(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    """review -> ready/todo for the implementer; also a done card whose merge failed (REV-05)."""
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        if not t["implementer"]:
            raise Refused(
                f"card {task_id} has no recorded implementer; use: kanban assign {task_id} ROLE"
            )
        merge_reset: dict[str, Any] = {}
        expected = ["review"]
        if t["status"] == "done" and t["merge_state"] == "failed":
            expected = ["done"]
            merge_reset = {
                "merge_state": None,
                "merge_attempts": 0,
                "merge_next_at": None,
                "owner_merge": 0,
                "completed_at": None,
            }
        target = "ready" if parents_satisfied(conn, task_id) else "todo"
        _cas(
            conn, task_id, expected, version=expect_version, status=target,
            assignee=t["implementer"], **merge_reset,
        )  # fmt: skip
        _comment(conn, t, "owner", f"REOPEN: {reason}", actor)
        event(conn, t, "review_reopened", actor, {"reason": reason, "status": target})
    return {"id": task_id, "status": target}


def reopen(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
    expected: Sequence[str] = ("done",),
    status: str = "ready",
) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        _cas(
            conn, task_id, expected, version=expect_version, status=status, completed_at=None,
            merge_state=None, merge_attempts=0, merge_next_at=None, merged_at=None,
            owner_merge=0, retry_after=None, assignee=t["implementer"] or t["assignee"],
            ignore_merge=0,
        )  # fmt: skip
        if actor not in (
            "jira",
            "github",
        ):  # the sync pushes owner comments to the issue it reopened from
            _comment(conn, t, "owner", f"REOPEN: {reason}", actor)
        event(conn, t, "reopened", actor, {"reason": reason})
        children = conn.execute(
            "SELECT c.* FROM task_links l JOIN tasks c ON c.id = l.child_id WHERE l.parent_id = ?",
            (task_id,),
        ).fetchall()
        for c in children:
            if c["status"] == "ready":
                _cas(conn, c["id"], ["ready"], status="todo")
                event(conn, c, "dependency_wait", actor, {"parent": task_id})
            elif c["status"] in ("running", "review", "done"):
                _comment(conn, c, "kanban", f"parent {task_id} was reopened: {reason}", actor)
    return {"id": task_id, "status": status}


def mark_done(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    actor: str = OWNER,
    expect_version: int | None = None,
    expected: Sequence[str] = ("review", "blocked"),
) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        _cas(
            conn, task_id, expected, version=expect_version, status="done", completed_at=now()
        )  # fmt: skip
        event(conn, t, "completed_by_owner", actor, {"reason": reason, "from": t["status"]})
    return {"id": task_id, "status": "done"}


def _reaches(conn: sqlite3.Connection, start: str, target: str) -> bool:
    """DFS down the child edges from `start`: is `target` a descendant?"""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        stack += [
            r[0]
            for r in conn.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (node,))
        ]
    return False


def link(
    conn: sqlite3.Connection, parent: str, child: str, *, actor: str = OWNER
) -> dict[str, Any]:
    with tx(conn):
        p, c = get_task(conn, parent), get_task(conn, child)
        if parent == child:
            raise Refused("a card cannot be its own parent")
        if p["board"] != c["board"]:
            raise Refused(f"{parent} and {child} are on different boards")
        if conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child)
        ).fetchone():
            return {"parent": parent, "child": child, "noop": True}
        if c["status"] == "running":
            raise Refused(f"card {child} is running; stop it first with: kanban stop {child}")
        if _reaches(conn, child, parent):
            raise Refused(f"linking {parent} -> {child} would create a cycle")
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (parent, child))
        event(conn, c, "linked", actor, {"parent": parent, "child": child})
        if c["ignore_merge"]:  # the owner waived the merge of the old parents, not of this one
            conn.execute("UPDATE tasks SET ignore_merge = 0 WHERE id = ?", (child,))
        if c["status"] == "ready" and not parents_satisfied(conn, child):
            _cas(conn, child, ["ready"], status="todo")
            event(conn, c, "dependency_wait", actor, {"parent": parent})
    got: dict[str, Any] = {"parent": parent, "child": child}
    if p["status"] == "triage":
        got["warnings"] = [f"parent {parent} is in triage; {child} waits until it is done"]
    return got


def unlink(
    conn: sqlite3.Connection, parent: str, child: str, *, actor: str = OWNER
) -> dict[str, Any]:
    with tx(conn):
        c = get_task(conn, child)
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child)
        )
        if cur.rowcount == 0:
            return {"parent": parent, "child": child, "noop": True}
        event(conn, c, "unlinked", actor, {"parent": parent, "child": child})
        if c["status"] == "todo" and parents_satisfied(conn, child):
            _cas(conn, child, ["todo"], status="ready")
            event(conn, c, "promoted", actor, {"reason": "unlink"})
    return {"parent": parent, "child": child}


def stop(conn: sqlite3.Connection, task_id: str, *, actor: str = OWNER) -> dict[str, Any]:
    """RUN-17: stop is the row alone; the runner polls it and takes claude's process group down itself."""
    with tx(conn):
        t = get_task(conn, task_id)
        run = _running_run(conn, task_id)
        end_run(
            conn, run["id"], "terminated", "blocked", actor=actor, event_kind="stopped",
            payload={"actor": actor},
            card_fields={
                "block_kind": "stopped", "consecutive_failures": 0, "protocol_violations": 0,
                "last_failure_error": None, "retry_after": None,
            },
        )  # fmt: skip
    return {
        "id": task_id, "status": "blocked", "run_id": run["id"],
        "runner_pid": run["runner_pid"], "runner_started": run["runner_started"],
        "board": t["board"],
    }  # fmt: skip


def archive(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    rm: bool = False,
    actor: str = OWNER,
    expect_version: int | None = None,
) -> dict[str, Any]:
    blobs = dirs().home / "attachments" / task_id
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] == "running":
            raise Refused(f"card {task_id} is running; stop it first with: kanban stop {task_id}")
        if rm:
            # the issue stays out of the board for good: its tombstone outlives the card
            linked = conn.execute(
                "SELECT jira_key FROM jira_links WHERE task_id = ?", (task_id,)
            ).fetchone()
            if linked:
                key = linked["jira_key"]
                event(
                    conn,
                    {"board": t["board"], "id": None},
                    "jira_unlinked",
                    actor,
                    {"key": key, "reason": "deleted"},
                )
            gh_linked = conn.execute(
                "SELECT repo, number FROM github_links WHERE task_id = ?", (task_id,)
            ).fetchone()
            if gh_linked:
                event(
                    conn,
                    {"board": t["board"], "id": None},
                    "github_unlinked",
                    actor,
                    {"issue": f"{gh_linked['repo']}#{gh_linked['number']}", "reason": "deleted"},
                )
            conn.execute(
                "UPDATE task_events SET task_id = NULL WHERE task_id = ? "
                "AND kind IN ('jira_unlinked', 'github_unlinked')",
                (task_id,),
            )
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            for table, col in (
                ("task_attachments", "task_id"), ("task_comments", "task_id"), ("grants", "task_id"),
                ("task_runs", "task_id"), ("task_links", "parent_id"), ("task_links", "child_id"),
                ("jira_links", "task_id"), ("github_links", "task_id"),
            ):  # fmt: skip
                conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            event(
                conn,
                {"board": t["board"], "id": None},
                "deleted",
                actor,
                {"id": task_id, "title": t["title"]},
            )
        elif t["status"] != "archived":
            _cas(
                conn,
                task_id,
                [s for s in STATUSES if s not in ("running", "archived")],
                version=expect_version,
                status="archived",
            )
            event(conn, t, "archived", actor, {"from": t["status"]})
    if rm and blobs.is_dir():
        for f in blobs.iterdir():
            f.unlink()
        blobs.rmdir()
    return {"id": task_id, "status": "deleted" if rm else "archived"}


def merge_retry(conn: sqlite3.Connection, task_id: str, *, actor: str = OWNER) -> dict[str, Any]:
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] != "done" or t["merge_state"] != "failed":
            raise Refused(
                f"card {task_id} has no failed merge to retry (status {t['status']}, merge {t['merge_state']})"
            )
        _cas(
            conn,
            task_id,
            ["done"],
            merge_state="pending",
            merge_attempts=0,
            merge_next_at=now(),
            owner_merge=0,
        )
        event(conn, t, "merge_retry", actor)
    return {"id": task_id, "merge_state": "pending"}


# --- comments and attachments ---------------------------------------------------------------


def _require_running(conn: sqlite3.Connection, run_id: int | None) -> None:
    """Inside the caller's tx: a worker write needs its run still running (CLI-05)."""
    if run_id is None:
        return
    run = conn.execute("SELECT status FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None or run["status"] != "running":
        raise Refused(RUN_ENDED)


def comment(
    conn: sqlite3.Connection,
    task_id: str,
    body: str,
    *,
    author: str = "owner",
    actor: str = OWNER,
    run_id: int | None = None,
    jira_id: str | None = None,
) -> dict[str, Any]:
    body = _need(body, "comment text", "pass the text")
    with tx(conn):
        _require_running(conn, run_id)
        cid = _comment(conn, get_task(conn, task_id), author, body, actor, jira_id)
    return {"id": task_id, "comment_id": cid}


def write_blob(task_id: str, filename: str, data: bytes) -> tuple[Path, str]:
    """Store an attachment blob (0600) under the home directory; returns its path and clean name."""
    clean = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename))[:100].lstrip(".") or "file"
    stored = ensure_dir(dirs().home / "attachments" / task_id) / f"{secrets.token_hex(4)}-{clean}"
    fd = os.open(stored, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    return stored, clean


def attach(
    conn: sqlite3.Connection,
    task_id: str,
    filename: str,
    data: bytes,
    *,
    content_type: str | None = None,
    uploaded_by: str = "owner",
    actor: str = OWNER,
    run_id: int | None = None,
) -> dict[str, Any]:
    if len(data) > MAX_ATTACH_BYTES:
        raise Refused(f"attachment is {len(data)} bytes; the cap is {MAX_ATTACH_BYTES} (25 MiB)")
    get_task(conn, task_id)
    stored, clean = write_blob(task_id, filename, data)
    try:
        with tx(conn):
            _require_running(conn, run_id)
            t = get_task(conn, task_id)
            cur = conn.execute(
                "INSERT INTO task_attachments(task_id, filename, stored_path, content_type, size, "
                "uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, clean, str(stored), content_type, len(data), uploaded_by, now()),
            )
            event(
                conn,
                t,
                "attached",
                actor,
                {"attachment_id": cur.lastrowid, "filename": clean, "size": len(data)},
            )
    except BaseException:
        stored.unlink(missing_ok=True)
        raise
    return {"id": task_id, "filename": clean, "path": str(stored), "size": len(data)}


# --- runs: claim, end_run -------------------------------------------------------------------


def _running_run(conn: sqlite3.Connection, task_id: str) -> Row:
    run = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND status = 'running'", (task_id,)
    ).fetchone()
    if run is None:
        raise Refused(f"card {task_id} has no active run")
    return run  # type: ignore[no-any-return]


_CARD_SPEND = "SELECT COALESCE(SUM(cost_usd), 0) FROM task_runs WHERE task_id = ?"
_DAY_SPEND = (
    "SELECT COALESCE(SUM(r.cost_usd), 0) FROM task_runs r JOIN tasks k ON k.id = r.task_id "
    "WHERE k.board = ? AND date(r.started_at, 'unixepoch', 'localtime') = date(?, 'unixepoch', 'localtime')"
)


def _over_budget(conn: sqlite3.Connection, t: Row) -> str | None:
    """P2-4: why a claim must not start, once the card's spend or the board's spend today passes its
    cap. A cap of 0 or None is off. The day is the local one, as in `stats` (`cost_by_day`)."""
    caps = (
        ("budget.max_usd_per_card", f"card {t['id']}", _CARD_SPEND, (t["id"],)),
        (
            "budget.max_usd_per_board_day",
            f"board {t['board']} today",
            _DAY_SPEND,
            (t["board"], now()),
        ),
    )
    for key, what, sql, args in caps:
        cap = cfg(conn, key)
        if cap and (used := conn.execute(sql, args).fetchone()[0]) > cap:
            fix = f'kanban config set {key} <usd> (0 turns the cap off), then kanban unblock {t["id"]} --reason "cap raised"'
            return f"{what} has spent ${used:.2f}, over {key} ${cap:.2f}; raise it with: {fix}"
    return None


def claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    config_dir: str | None = None,
    actor: str = "dispatcher",
) -> int | None:
    """DSP-06. Returns the new run id, or None when the card was skipped."""
    try:
        with tx(conn):
            t = get_task(conn, task_id)
            source = t["status"]
            if source not in ("ready", "review"):
                return None
            if source == "ready" and not parents_satisfied(conn, task_id):
                _cas(conn, task_id, ["ready"], status="todo")
                event(conn, t, "claim_rejected", actor, {"reason": "parents_not_done"})
                return None
            if over := _over_budget(conn, t):
                _cas(conn, task_id, [source], status="blocked", block_kind="capability")
                _comment(conn, t, "kanban", f"BLOCKED: {over}", actor)
                event(
                    conn,
                    t,
                    "blocked",
                    actor,
                    {"kind": "capability", "recurrences": 0, "from": source},
                )
                return None
            _cas(conn, task_id, [source], status="running", started_at=t["started_at"] or now())
            n = conn.execute(
                "SELECT COALESCE(MAX(n), 0) + 1 FROM task_runs WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO task_runs(task_id, n, role, kind, status, max_runtime_seconds, config_dir, started_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
                (task_id, n, t["assignee"], "review" if source == "review" else "work",
                 t["max_runtime_seconds"], config_dir, now()),
            )  # fmt: skip
            event(
                conn,
                t,
                "claimed",
                actor,
                {"run_id": cur.lastrowid, "source_status": source},
                cur.lastrowid,
            )
            return cur.lastrowid
    except (sqlite3.IntegrityError, Refused) as e:
        log.warning("card %s not claimed: %s", task_id, e)
        return None


def end_run(
    conn: sqlite3.Connection,
    run_id: int,
    outcome: str,
    card_to: str,
    *,
    actor: str,
    event_kind: str,
    payload: dict[str, Any] | None = None,
    card_fields: dict[str, Any] | None = None,
    run_fields: dict[str, Any] | None = None,
    counted: bool | None = None,
    retry_after: int | None = None,
    error: str | None = None,
    summary: str | None = None,
    orphan_ok: bool = False,
) -> bool:
    """RUN-13: the only code that ends a run. Runs inside the caller's BEGIN IMMEDIATE.

    `card_to` is a status, or "retry" for the failure rule of RUN-12 (review for a review run,
    ready otherwise). Returns False, changing nothing, when the run had already ended. A card that
    is not running although its run was refuses the end, unless `orphan_ok`: then only the run ends.
    """
    ts = now()
    run_set = {"status": "ended", "outcome": outcome, "ended_at": ts, **(run_fields or {})}
    if error is not None:
        run_set["error"] = error
    if summary is not None:
        run_set["summary"] = summary
    cur = conn.execute(
        f"UPDATE task_runs SET {', '.join(f'{k} = ?' for k in run_set)} WHERE id = ? AND status = 'running'",
        [*run_set.values(), run_id],
    )
    if cur.rowcount != 1:
        return False
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
    fields = dict(card_fields or {})
    events: list[tuple[str, dict[str, Any]]] = []
    if card_to == "retry":
        card_to, fields, events = _retry_rule(
            conn, t, run, outcome, counted, error, retry_after, fields
        )
    cas = conn.execute(
        f"UPDATE tasks SET {''.join(f'{k} = ?, ' for k in {**fields, 'status': 0})}"
        "updated_at = ?, version = version + 1 WHERE id = ? AND status = 'running'",
        [*({**fields, "status": card_to}).values(), ts, t["id"]],
    )
    if cas.rowcount != 1 and not orphan_ok:
        raise Refused(
            f"card {t['id']} is not running although run {run_id} was; run: kanban doctor --fix"
        )
    event(
        conn,
        t,
        event_kind,
        actor,
        {"run_id": run_id, "outcome": outcome, **(payload or {})},
        run_id,
    )
    for kind, extra in events if cas.rowcount == 1 else ():
        event(conn, t, kind, actor, extra, run_id)
    return True


_MODEL_USAGE_KEYS = (
    "inputTokens", "outputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "costUSD",
)  # fmt: skip


def _model_usage(res: dict[str, Any]) -> dict[str, dict[str, float]] | None:
    """The result's `modelUsage` cut to what a price list needs, or None when it reports none."""
    raw = res.get("modelUsage")
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        str(m): {k: v[k] for k in _MODEL_USAGE_KEYS if isinstance(v.get(k), int | float)}
        for m, v in raw.items()
        if isinstance(v, dict)
    }


def record_result(
    conn: sqlite3.Connection, run: Row, exit_code: int | None, res: dict[str, Any]
) -> None:
    """RUN-15: Claude's exit and result facts. A resumed session reports its whole cost and its
    whole per-model usage, so `cost_usd` and `model_usage` are the rise over the session's previous
    totals (a total below the previous one is a fresh count and stands as it is).
    Runs inside the caller's BEGIN IMMEDIATE."""
    total = res.get("total_cost_usd")
    prev = conn.execute(
        "SELECT session_cost_usd FROM task_runs WHERE session_id = ? AND id < ? "
        "AND session_cost_usd IS NOT NULL ORDER BY id DESC LIMIT 1",
        (run["session_id"], run["id"]),
    ).fetchone()
    floor = prev[0] if prev else 0.0
    seen = _model_usage(res)
    last = conn.execute(
        "SELECT session_model_usage FROM task_runs WHERE session_id = ? AND id < ? "
        "AND session_model_usage IS NOT NULL ORDER BY id DESC LIMIT 1",
        (run["session_id"], run["id"]),
    ).fetchone()
    before = json.loads(last[0]) if last else {}
    share = None
    if seen is not None:
        share = {
            m: {
                k: round(v - b if v >= (b := before.get(m, {}).get(k, 0)) else v, 8)
                for k, v in used.items()
            }
            for m, used in seen.items()
        }
    usage: dict[str, Any] = dict(res["usage"]) if isinstance(res.get("usage"), dict) else {}
    if share is not None:  # the whole run, every model; the result's `usage` is one turn
        for col, key in (
            ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
            ("cache_creation_input_tokens", "cacheCreationInputTokens"),
            ("cache_read_input_tokens", "cacheReadInputTokens"),
        ):  # fmt: skip
            usage[col] = int(sum(u.get(key, 0) for u in share.values()))
    conn.execute(
        "UPDATE task_runs SET claude_exit_code = ?, exit_code = ?, subtype = ?, is_error = ?, "
        "api_error_status = ?, num_turns = ?, session_cost_usd = ?, cost_usd = ?, "
        "input_tokens = ?, output_tokens = ?, cache_write_tokens = ?, cache_read_tokens = ?, "
        "model_usage = ?, session_model_usage = ? WHERE id = ?",
        (
            exit_code, exit_code, res.get("subtype"),
            None if "is_error" not in res else int(bool(res["is_error"])),
            res.get("api_error_status"), res.get("num_turns"), total,
            None if total is None else (total - floor if total >= floor else total),
            usage.get("input_tokens"), usage.get("output_tokens"),
            usage.get("cache_creation_input_tokens"), usage.get("cache_read_input_tokens"),
            None if share is None else json.dumps(share),
            None if seen is None else json.dumps(seen), run["id"],
        ),
    )  # fmt: skip


def record_measure(
    conn: sqlite3.Connection, run: Row, m: "measure.Measure", *, advisor: str | None = None
) -> None:
    """The run's own counts from its stream. `duration_api_ms` is cumulative over the session
    (verified on resumed runs), so `api_ms` is the rise over the session's earlier runs, as the
    cost is; `advisor` fills the arm only where none was recorded (the backfill's `inherit`).
    Runs inside the caller's BEGIN IMMEDIATE."""
    total = m.api_ms
    prev = conn.execute(
        "SELECT COALESCE(SUM(api_ms), 0) FROM task_runs WHERE session_id = ? AND id < ?",
        (run["session_id"], run["id"]),
    ).fetchone()[0]
    old = conn.execute("SELECT params FROM task_runs WHERE id = ?", (run["id"],)).fetchone()[0]
    params = json.loads(old) if old else {}
    if m.init:
        params["init"] = m.init_params()
    conn.execute(
        "UPDATE task_runs SET advisor = COALESCE(advisor, ?), advisor_calls = ?, "
        "cache_write_1h_tokens = ?, cache_write_5m_tokens = ?, api_ms = ?, harness = ?, "
        "params = ? WHERE id = ?",
        (
            advisor, m.advisor_calls, m.cache_1h, m.cache_5m,
            None if total is None else (total - prev if total >= prev else total),
            dumps(m.harness()), dumps(params) if params else None, run["id"],
        ),
    )  # fmt: skip


def infra_streak(conn: sqlite3.Connection, task_id: str, before_run: int, cap: int) -> int:
    """How many of the card's latest runs before this one failed to provision for want of the
    machine (network, credentials, a lock), unbroken by any other outcome; counted up to `cap`."""
    streak = 0
    for (infra,) in conn.execute(
        "SELECT outcome = 'provision_failed' AND json_extract(metadata, '$.infrastructure') "
        "FROM task_runs WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
        (task_id, before_run, cap),
    ):
        if not infra:
            break
        streak += 1
    return streak


def transient_streak(conn: sqlite3.Connection, task_id: str, before_run: int) -> int:
    """How many of the card's latest runs before this one ended `network` or `stalled`, unbroken
    by any other outcome; counted up to `TRANSIENT_RETRIES`."""
    streak = 0
    for (outcome,) in conn.execute(
        "SELECT outcome FROM task_runs WHERE task_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
        (task_id, before_run, TRANSIENT_RETRIES),
    ):
        if outcome not in TRANSIENT_OUTCOMES:
            break
        streak += 1
    return streak


def _retry_rule(
    conn: sqlite3.Connection,
    t: Row,
    run: Row,
    outcome: str,
    counted: bool | None,
    error: str | None,
    retry_after: int | None,
    fields: dict[str, Any],
) -> tuple[str, dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """RUN-12 failure rule, protocol-violation streak and systemic crash pause."""
    ts = now()
    retry_status = "review" if run["kind"] == "review" else "ready"
    events: list[tuple[str, dict[str, Any]]] = []
    count = outcome in COUNTED_OUTCOMES if counted is None else counted
    if outcome == "crashed" or (outcome == "interrupted" and run["runner_pid"] is None):
        recent = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE ended_at >= ? AND "
            "(outcome = 'crashed' OR (outcome = 'interrupted' AND runner_pid IS NULL))",
            (ts - SYSTEMIC_WINDOW_S,),
        ).fetchone()[0]
        if recent >= SYSTEMIC_CRASH_COUNT and state_get(conn, "dispatch_paused") is None:
            detail = (error or "").splitlines()[0] if error else ""
            state_put(
                conn, "dispatch_paused", {"reason": "systemic", "since": ts, "detail": detail}
            )
            event(conn, None, "dispatch_paused", "runner", {"reason": "systemic", "detail": detail})
            count = False
    if outcome in TRANSIENT_OUTCOMES:
        streak = transient_streak(conn, t["id"], run["id"])
        if streak >= TRANSIENT_RETRIES:
            count = True  # a real fault hides behind "the network": let the failure limit end it
        elif retry_after is None:
            retry_after = ts + min(TRANSIENT_COOLDOWN_S * 2**streak, TRANSIENT_COOLDOWN_MAX_S)
    limit_card = t["max_retries"]
    if outcome == "protocol_violation":
        streak = t["protocol_violations"] + 1
        fields["protocol_violations"] = streak
        limit = limit_card or PROTOCOL_VIOLATION_LIMIT
        if streak >= limit:
            fields.update(block_kind="gave_up")
            events.append(("gave_up", {"failures": streak, "effective_limit": limit,
                                       "limit_source": "card" if limit_card else "default",
                                       "error": error, "trigger_outcome": outcome}))  # fmt: skip
            return "blocked", fields, events
    elif count:
        failures = t["consecutive_failures"] + 1
        limit = limit_card or cfg(conn, "dispatch.failure_limit")
        fields["consecutive_failures"] = failures
        fields["last_failure_error"] = (error or "")[:500]
        if failures >= limit:
            fields.update(block_kind="gave_up")
            events.append(("gave_up", {"failures": failures, "effective_limit": limit,
                                       "limit_source": "card" if limit_card else "config",
                                       "error": (error or "")[:500], "trigger_outcome": outcome}))  # fmt: skip
            return "blocked", fields, events
    if retry_after is None and (count or outcome == "protocol_violation"):
        retry_after = ts + RETRY_AFTER_S
    fields["retry_after"] = retry_after
    return retry_status, fields, events


# --- worker terminal verbs ------------------------------------------------------------------


def _verb_run(conn: sqlite3.Connection, run_id: int, verb: str) -> tuple[Row, Row] | None:
    """The run and its card, or None when this verb already ended the run (CLI-05)."""
    run = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise NotFound(f"no run {run_id}")
    if run["status"] != "running":
        if run["terminal_verb"] == verb:
            return None
        raise Refused(RUN_ENDED)
    return run, conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()


def _worker(run: Row) -> str:
    return f"worker:{run['role']}#{run['id']}"


_TERMINAL_CARD = {"retry_after": None, "protocol_violations": 0}


def _last_block(conn: sqlite3.Connection, task_id: str) -> tuple[str | None, int]:
    """The kind and repeat count of the card's latest block, `(None, 0)` once anything else ended
    it (a stop, a give-up, a completion). The card's own columns are cleared when it leaves `blocked`,
    so the loop check (BLOCK_RECURRENCE_LIMIT) reads the history instead."""
    got = conn.execute(
        "SELECT json_extract(payload, '$.kind'), json_extract(payload, '$.recurrences') "
        "FROM task_events WHERE task_id = ? "
        "AND kind IN ('blocked', 'block_loop_detected', 'stopped', 'gave_up', 'completed') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return (got[0], got[1] or 0) if got else (None, 0)


def block(
    conn: sqlite3.Connection,
    run_id: int,
    kind: str,
    reason: str,
    *,
    retry_after_s: int | None = None,
) -> dict[str, Any]:
    """`retry_after_s` (transient only) puts the card back in line after that long instead of
    blocking it. It counts as a failure, so the card's failure limit ends the retries (`gave_up`)."""
    if kind not in WORKER_BLOCK_KINDS:
        raise Refused(f"block kind {kind!r} must be one of {', '.join(WORKER_BLOCK_KINDS)}")
    if retry_after_s is not None and kind != "transient":
        raise Refused("--retry-after goes with --kind transient only")
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        found = _verb_run(conn, run_id, "block")
        if found is None:
            return {"noop": True}
        run, t = found
        if retry_after_s is not None:
            end_run(
                conn, run_id, "blocked", "retry", actor=_worker(run), event_kind="transient_retry",
                payload={"kind": kind, "retry_after_s": retry_after_s}, counted=True,
                retry_after=now() + retry_after_s, error=reason, summary=reason,
                card_fields={"protocol_violations": 0}, run_fields={"terminal_verb": "block"},
            )  # fmt: skip
            to = get_task(conn, t["id"])["status"]
            note = f" (retrying in {retry_after_s}s)" if to != "blocked" else ""
            _comment(conn, t, run["role"], f"BLOCKED: {reason}{note}", _worker(run))
            return {"id": t["id"], "status": to}
        prev = _last_block(conn, t["id"])
        loops = prev[1] + 1 if prev[0] == kind else 0
        looped = loops >= BLOCK_RECURRENCE_LIMIT
        to = "triage" if looped else "blocked"
        end_run(
            conn, run_id, "blocked", to, actor=_worker(run),
            event_kind="block_loop_detected" if looped else "blocked",
            payload={"kind": kind, "recurrences": loops},
            card_fields={**_TERMINAL_CARD, "block_kind": kind, "block_recurrences": loops},
            run_fields={"terminal_verb": "block"}, summary=reason,
        )  # fmt: skip
        _comment(conn, t, run["role"], f"BLOCKED: {reason}", _worker(run))
    return {"id": t["id"], "status": to}


def complete(
    conn: sqlite3.Connection,
    run_id: int,
    summary: str,
    *,
    commits_ahead: int = 0,
    default_branch: str | None = None,
) -> dict[str, Any]:
    summary = _need(summary, "summary", "pass --summary")
    with tx(conn):
        found = _verb_run(conn, run_id, "complete")
        if found is None:
            return {"noop": True}
        run, t = found
        merge: dict[str, Any] = {}
        extra: list[tuple[str, dict[str, Any]]] = []
        if run["kind"] == "review":
            m = APPROVED_RE.search(summary)
            if not m:
                raise Refused(
                    "a review run completes only with 'WARDEN: APPROVED PR #<n> @ <sha>'; "
                    "otherwise use request-changes or block"
                )
            partial = PARTIAL_REVIEW in summary
            if t["base_branch"] and t["base_branch"] != default_branch:
                # A partial review stays 'pending' so children wait, but no merge job is due (D10).
                merge = {
                    "merge_state": "pending", "merge_attempts": 0, "owner_merge": int(partial),
                    "merge_next_at": None if partial else now(),
                }  # fmt: skip
            pr_sha = {"run_id": run_id, "pr": int(m[1]), "sha": m[2]}
            if merge and not partial:
                extra.append(("merge_pending", pr_sha))
            else:
                extra.append(
                    ("awaiting_owner_merge", {**pr_sha, **({"partial": True} if partial else {})})
                )
        elif run["role"] in BUILDERS and commits_ahead > 0:
            raise Refused(
                f"branch {t['branch_name']} has {commits_ahead} commit(s) not on origin/{t['base_branch'] or 'the default branch'}; "
                "open a PR and run: kanban request-review"
            )
        end_run(
            conn, run_id, "completed", "done", actor=_worker(run), event_kind="completed",
            card_fields={
                **_TERMINAL_CARD, "completed_at": now(), "block_kind": None, "block_recurrences": 0,
                "consecutive_failures": 0, "last_failure_error": None, **merge,
            },
            run_fields={"terminal_verb": "complete"}, summary=summary,
        )  # fmt: skip
        for kind, payload in extra:
            event(conn, t, kind, _worker(run), payload, run_id)
    return {"id": t["id"], "status": "done", "merge_state": merge.get("merge_state")}


def request_review(
    conn: sqlite3.Connection,
    run_id: int,
    summary: str,
    *,
    pr_number: int,
    pr_url: str,
    reviewer: str | None = None,
) -> dict[str, Any]:
    summary = _need(summary, "summary", "say what the PR changes and how it was checked")
    reviewer = reviewer or cfg(conn, "dispatch.reviewer")
    if reviewer not in active_roles(conn):
        raise Refused(f"reviewer {reviewer!r} is not a dispatchable role")
    with tx(conn):
        found = _verb_run(conn, run_id, "request-review")
        if found is None:
            return {"noop": True}
        run, t = found
        if run["kind"] != "work" or run["role"] not in BUILDERS:
            raise Refused(
                "request-review is for smith or designer work runs; use complete or request-changes"
            )
        end_run(
            conn, run_id, "review_requested", "review", actor=_worker(run), event_kind="review_requested",
            payload={"implementer": t["assignee"], "pr": pr_number},
            card_fields={
                **_TERMINAL_CARD, "implementer": t["assignee"], "assignee": reviewer,
                "pr_number": pr_number, "pr_url": pr_url,
            },
            run_fields={"terminal_verb": "request-review"}, summary=summary,
        )  # fmt: skip
    return {"id": t["id"], "status": "review", "assignee": reviewer, "pr_number": pr_number}


def request_changes(conn: sqlite3.Connection, run_id: int, reason: str) -> dict[str, Any]:
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        run = conn.execute("SELECT kind, role FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if run and run["kind"] == "work" and run["role"] == cfg(conn, "dispatch.reviewer"):
            # A standalone review card has no builder to send back to, so the owner relays the verdict.
            return block(conn, run_id, "needs_input", f"CHANGES REQUESTED: {reason}")
        found = _verb_run(conn, run_id, "request-changes")
        if found is None:
            return {"noop": True}
        run, t = found
        if run["kind"] != "review":
            raise Refused("request-changes is for review runs; use block or complete")
        if not t["implementer"]:
            raise Refused(
                f"card {t['id']} has no recorded implementer; use block --kind needs_input"
            )
        to = "ready" if parents_satisfied(conn, t["id"]) else "todo"
        end_run(
            conn, run_id, "changes_requested", to, actor=_worker(run), event_kind="changes_requested",
            payload={"reason": reason},
            card_fields={**_TERMINAL_CARD, "assignee": t["implementer"]},
            run_fields={"terminal_verb": "request-changes"}, summary=reason,
        )  # fmt: skip
        _comment(conn, t, run["role"], f"CHANGES REQUESTED: {reason}", _worker(run))
    return {"id": t["id"], "status": to, "assignee": t["implementer"]}


def owner_builder(conn: sqlite3.Connection, t: Row) -> str:
    """The builder of a blocked card: its assignee, or the implementer a blocked review run left behind."""
    if t["status"] != "blocked":
        raise Refused(f"card {t['id']} is {t['status']}; request-review needs it blocked")
    if t["assignee"] in BUILDERS:
        return str(t["assignee"])
    if blocked_in_review(conn, t["id"]) and t["implementer"] in BUILDERS:
        return str(t["implementer"])
    raise Refused(
        f"card {t['id']} has no smith or designer to review; "
        f"use: kanban assign {t['id']} ROLE, then unblock it"
    )


def request_review_owner(
    conn: sqlite3.Connection,
    task_id: str,
    summary: str,
    *,
    pr_number: int,
    pr_url: str,
    reviewer: str | None = None,
    actor: str = OWNER,
) -> dict[str, Any]:
    """The owner's form of request-review: a blocked builder card whose PR is ready goes to review."""
    summary = _need(summary, "summary", "say what the PR changes and how it was checked")
    reviewer = reviewer or cfg(conn, "dispatch.reviewer")
    if reviewer not in active_roles(conn):
        raise Refused(f"reviewer {reviewer!r} is not a dispatchable role")
    with tx(conn):
        t = get_task(conn, task_id)
        builder = owner_builder(conn, t)
        _cas(
            conn, task_id, ["blocked"], status="review", implementer=builder, assignee=reviewer,
            pr_number=pr_number, pr_url=pr_url, consecutive_failures=0, protocol_violations=0,
            last_failure_error=None, retry_after=None,
        )  # fmt: skip
        _comment(conn, t, "owner", f"REVIEW REQUESTED: {summary}", actor)
        event(conn, t, "review_requested", actor, {"implementer": builder, "pr": pr_number})
    return {"id": task_id, "status": "review", "assignee": reviewer, "pr_number": pr_number}


def request_changes_owner(
    conn: sqlite3.Connection, task_id: str, reason: str, *, actor: str = OWNER
) -> dict[str, Any]:
    """The owner's form of request-changes: a card blocked in a review run goes back to its builder."""
    reason = _need(reason, "reason", "pass --reason")
    with tx(conn):
        t = get_task(conn, task_id)
        if t["status"] != "blocked" or not blocked_in_review(conn, task_id):
            raise Refused(
                f"card {task_id} is {t['status']}; request-changes needs a card blocked in a review run"
            )
        if not t["implementer"]:
            raise Refused(
                f"card {task_id} has no recorded implementer; use: kanban unblock {task_id}"
            )
        to = "ready" if parents_satisfied(conn, task_id) else "todo"
        _cas(
            conn, task_id, ["blocked"], status=to, assignee=t["implementer"],
            consecutive_failures=0, protocol_violations=0, last_failure_error=None,
            retry_after=None,
        )  # fmt: skip
        _comment(conn, t, "owner", f"CHANGES REQUESTED: {reason}", actor)
        event(conn, t, "changes_requested", actor, {"reason": reason})
    return {"id": task_id, "status": to, "assignee": t["implementer"]}


# --- grants, pause, stats -------------------------------------------------------------------

_RULE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*\((.+)\)$")
_MCP_RULE_RE = re.compile(r"^mcp__[A-Za-z0-9-]+__[A-Za-z0-9_]+$")


def _check_grant(rules: Sequence[str], approved: str | None) -> str:
    approved = _need(approved, "--approved", "quote the owner's words, source and date")
    if not rules:
        raise Refused("pass at least one --allow RULE")
    for r in rules:
        m = _RULE_RE.match(r)
        if _MCP_RULE_RE.match(r):
            continue
        if not m or m[1].strip() in ("*", ":*") or m[1].endswith(":*"):
            raise Refused(
                f"grant rule {r!r} must name the exact action, not a wildcard or a tool prefix"
            )
    return approved


def _insert_grant(
    conn: sqlite3.Connection,
    t: Row | dict[str, Any],
    rules: Sequence[str],
    approved: str,
    actor: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO grants(task_id, rules, approved, created_at) VALUES (?, ?, ?, ?)",
        (t["id"], json.dumps(list(rules)), approved, now()),
    )
    assert cur.lastrowid is not None
    event(conn, t, "grant_added", actor, {"grant_id": cur.lastrowid, "rules": list(rules)})
    return cur.lastrowid


def grant_add(
    conn: sqlite3.Connection,
    task_id: str,
    rules: Sequence[str],
    approved: str,
    *,
    actor: str = OWNER,
) -> dict[str, Any]:
    approved = _check_grant(rules, approved)
    with tx(conn):
        grant_id = _insert_grant(conn, get_task(conn, task_id), rules, approved, actor)
    return {"id": task_id, "grant_id": grant_id}


def grant_revoke(conn: sqlite3.Connection, task_id: str, *, actor: str = OWNER) -> dict[str, Any]:
    with tx(conn):
        t = get_task(conn, task_id)
        cur = conn.execute(
            "UPDATE grants SET revoked_at = ? WHERE task_id = ? AND revoked_at IS NULL",
            (now(), task_id),
        )
        if cur.rowcount:
            event(conn, t, "grant_revoked", actor, {"count": cur.rowcount})
    return {"id": task_id, "revoked": cur.rowcount}


def grants_list(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    get_task(conn, task_id)
    out = []
    for r in conn.execute("SELECT * FROM grants WHERE task_id = ? ORDER BY id", (task_id,)):
        d = dict(r)
        d["rules"] = json.loads(d["rules"])
        out.append(d)
    return out


def pause(conn: sqlite3.Connection, reason: str = "", *, actor: str = OWNER) -> None:
    with tx(conn):
        state_put(conn, "dispatch_paused", {"reason": "manual", "since": now(), "detail": reason})
        event(conn, None, "dispatch_paused", actor, {"reason": "manual", "detail": reason})


def resume(conn: sqlite3.Connection, *, actor: str = OWNER) -> bool:
    with tx(conn):
        cur = conn.execute("DELETE FROM state WHERE key = 'dispatch_paused'")
        if cur.rowcount:
            event(conn, None, "dispatch_resumed", actor)
    return bool(cur.rowcount)


def stats(conn: sqlite3.Connection, *, board: str | None = None, days: int = 7) -> dict[str, Any]:
    since = now() - days * 86400
    where = "r.started_at >= ?" + (" AND t.board = ?" if board else "")
    args: list[Any] = [since, *([board] if board else [])]
    base = f"FROM task_runs r JOIN tasks t ON t.id = r.task_id WHERE {where}"

    def table(sql: str) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, args)]

    return {
        "days": days,
        "cost_by_role": table(f"SELECT r.role, COUNT(*) runs, ROUND(COALESCE(SUM(r.cost_usd), 0), 4) cost {base} GROUP BY r.role ORDER BY r.role"),
        "cost_by_model": table(f"SELECT r.model, ROUND(COALESCE(SUM(r.cost_usd), 0), 4) cost {base} GROUP BY r.model ORDER BY r.model"),
        "cost_by_day": table(f"SELECT date(r.started_at, 'unixepoch', 'localtime') day, ROUND(COALESCE(SUM(r.cost_usd), 0), 4) cost {base} GROUP BY day ORDER BY day"),
        "tokens_by_model": table(
            "SELECT j.key model, COUNT(*) runs, "
            "COALESCE(SUM(json_extract(j.value, '$.inputTokens')), 0) input, "
            "COALESCE(SUM(json_extract(j.value, '$.outputTokens')), 0) output, "
            "COALESCE(SUM(json_extract(j.value, '$.cacheCreationInputTokens')), 0) cache_write, "
            "COALESCE(SUM(json_extract(j.value, '$.cacheReadInputTokens')), 0) cache_read, "
            "ROUND(COALESCE(SUM(json_extract(j.value, '$.costUSD')), 0), 4) cost "
            f"FROM task_runs r JOIN tasks t ON t.id = r.task_id, json_each(r.model_usage) j "
            f"WHERE {where} GROUP BY j.key ORDER BY j.key"
        ),
        "outcomes": table(f"SELECT r.outcome, COUNT(*) runs {base} GROUP BY r.outcome ORDER BY r.outcome"),
        "review_rounds": table(f"SELECT t.id, COUNT(*) rounds {base} AND r.kind = 'review' GROUP BY t.id ORDER BY rounds DESC"),
        **_measure_stats(conn, base, where, args),
    }  # fmt: skip


def _measure_stats(
    conn: sqlite3.Connection, base: str, where: str, args: list[Any]
) -> dict[str, Any]:
    """The 0.4.0 keys of `stats`: runtime, cost per role and model, the advisor arms, guards and
    what the harness did (tools, skills, MCP, agents, hooks); all but the first two read what the
    runner and the backfill recorded, so a run without `harness` only counts in `unmeasured_runs`."""

    def table(sql: str, extra: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, [*args, *extra])]

    def counts(section: str) -> list[dict[str, Any]]:
        return table(
            "SELECT j.key name, SUM(j.value) calls, COUNT(*) runs "
            f"FROM task_runs r JOIN tasks t ON t.id = r.task_id, json_each(r.harness, '$.{section}') j "
            f"WHERE {where} GROUP BY j.key ORDER BY calls DESC, name"
        )

    verdict = "r.kind = 'review' AND r.terminal_verb IN ('complete', 'request-changes')"
    reviews, sent = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(r.terminal_verb = 'request-changes'), 0) {base} AND {verdict}",
        args,
    ).fetchone()
    # a card's first verdict is its earliest review run that approved or sent back
    cards_judged, first_ok = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r.terminal_verb = 'complete'), 0) "
        f"{base} AND {verdict} AND r.n = (SELECT MIN(x.n) FROM task_runs x WHERE x.task_id = r.task_id "
        "AND x.kind = 'review' AND x.terminal_verb IN ('complete', 'request-changes'))",
        args,
    ).fetchone()
    approved = [
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT r.task_id {base} AND r.kind = 'review' AND r.terminal_verb = 'complete'",
            args,
        )
    ]
    spent = conn.execute(
        f"SELECT COALESCE(SUM(cost_usd), 0) FROM task_runs WHERE task_id IN ({','.join('?' * len(approved))})",
        approved,
    ).fetchone()[0]
    totals: dict[str, Any] = {
        "hooks_ok": 0, "hooks_failed": 0, "compactions": 0, "permission_denials": 0,
        "tool_errors": 0, "subagents_spawned": 0, "subagents_failed": 0, "by_terminal_reason": {},
    }  # fmt: skip
    for (raw,) in conn.execute(f"SELECT r.harness {base} AND r.harness IS NOT NULL", args):
        h = json.loads(raw)
        for v in h.get("hooks", {}).values():
            totals["hooks_ok"] += v.get("ok", 0)
            totals["hooks_failed"] += v.get("failed", 0)
        for k in ("compactions", "permission_denials", "tool_errors"):
            totals[k] += h.get(k, 0)
        totals["subagents_spawned"] += h.get("subagents", {}).get("spawned", 0)
        totals["subagents_failed"] += h.get("subagents", {}).get("failed", 0)
        reason = str(h.get("terminal_reason") or "none")
        totals["by_terminal_reason"][reason] = totals["by_terminal_reason"].get(reason, 0) + 1
    cards = len(approved)
    return {
        "runtime_by_role": table(
            "SELECT r.role, COUNT(*) runs, SUM(r.ended_at - r.started_at) total_s, "
            "ROUND(AVG(r.ended_at - r.started_at), 1) avg_s, MAX(r.ended_at - r.started_at) max_s, "
            f"ROUND(COALESCE(SUM(r.api_ms), 0) / 1000.0, 1) api_s {base} AND r.ended_at IS NOT NULL "
            "GROUP BY r.role ORDER BY r.role"
        ),
        # per model the run used (`modelUsage`, so the advisor's model is a row of its own); the
        # cache-write split is the run's, put on the model it ran as (a subagent's share stays there)
        "cost_by_role_model": table(
            "SELECT r.role, j.key model, COUNT(*) runs, "
            "ROUND(COALESCE(SUM(json_extract(j.value, '$.costUSD')), 0), 4) cost, "
            "ROUND(COALESCE(SUM(json_extract(j.value, '$.costUSD')), 0) / COUNT(*), 4) avg_cost, "
            "COALESCE(SUM(json_extract(j.value, '$.inputTokens')), 0) input, "
            "COALESCE(SUM(json_extract(j.value, '$.outputTokens')), 0) output, "
            "COALESCE(SUM(json_extract(j.value, '$.cacheCreationInputTokens')), 0) cache_write, "
            "COALESCE(SUM(CASE WHEN j.key = r.model THEN r.cache_write_1h_tokens END), 0) cache_write_1h, "
            "COALESCE(SUM(CASE WHEN j.key = r.model THEN r.cache_write_5m_tokens END), 0) cache_write_5m, "
            "COALESCE(SUM(json_extract(j.value, '$.cacheReadInputTokens')), 0) cache_read "
            f"FROM task_runs r JOIN tasks t ON t.id = r.task_id, json_each(r.model_usage) j "
            f"WHERE {where} GROUP BY r.role, j.key ORDER BY r.role, j.key"
        ),
        "advisor_arms": table(
            "SELECT r.role, r.advisor, COUNT(*) runs, ROUND(COALESCE(SUM(r.cost_usd), 0), 4) cost, "
            "ROUND(COALESCE(SUM(r.cost_usd), 0) / COUNT(*), 4) avg_cost, "
            f"COALESCE(SUM(r.advisor_calls), 0) advisor_calls {base} AND r.advisor IS NOT NULL "
            "GROUP BY r.role, r.advisor ORDER BY r.role, r.advisor"
        ),
        "per_approved_card": {
            "cards": cards,
            "cost": round(spent, 4),
            "avg_cost": round(spent / cards, 4) if cards else None,
        },  # fmt: skip
        "guards": {
            "reviews": reviews,
            "first_pass_approvals": first_ok,
            "send_backs": sent,
            "first_pass_rate": round(first_ok / cards_judged, 3) if cards_judged else None,
        },  # fmt: skip
        "skills": counts("skills"),
        "mcp": counts("mcp"),
        "tools": counts("tools"),
        "agents": counts("agents"),
        "harness_totals": totals,
        "unmeasured_runs": conn.execute(
            f"SELECT COUNT(*) {base} AND r.harness IS NULL", args
        ).fetchone()[0],
    }
