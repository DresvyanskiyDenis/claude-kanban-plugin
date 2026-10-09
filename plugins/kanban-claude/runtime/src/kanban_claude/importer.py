"""One-way import of the legacy per-board databases (MIG-01..07, Appendix A.3).

Every source database is copied with its side files into a private temp directory and only the
copy is opened (read-only), so the source tree is never touched. The whole import is planned in
memory first: the dry run prints that plan, the real run applies it in one transaction.
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import store, validate
from .errors import NotFound, Refused, Usage

VERB = "import-hermes"
AUTHOR = "hermes-import"
GATEWAY = "ai.hermes.gateway"
DECOMMISSION = (
    f"launchctl bootout gui/$UID/{GATEWAY}",
    "launchctl bootout gui/$UID/kanban-hermes-claude.dashboard",
)
UNINSTALL = "then remove the old kanban plugin: /plugin uninstall <old plugin>@<its marketplace>"

STATUS_MAP = {
    "triage": "triage", "todo": "todo", "ready": "ready", "running": "ready", "review": "review",
    "blocked": "blocked", "scheduled": "blocked", "done": "done", "archived": "archived",
}  # fmt: skip
OUTCOMES = {"completed", "review_requested", "changes_requested", "blocked", "timed_out", "crashed"}
OUTCOME_MAP = {
    "rate_limited": "quota", "spawn_failed": "provision_failed", "gave_up": "crashed",
    "scheduled": "terminated",
}  # fmt: skip
WORKSPACES = ("scratch", "worktree", "dir")
REQUIRED = {
    "tasks": ("id", "title", "status", "created_at"),
    "task_links": ("parent_id", "child_id"),
    "task_comments": ("id", "task_id", "author", "body", "created_at"),
    "task_events": ("id", "task_id", "kind", "created_at"),
    "task_runs": ("id", "task_id", "status", "started_at"),
    "task_attachments": ("id", "task_id", "filename", "stored_path", "created_at"),
}
ORDER = (
    "boards", "tasks", "task_runs", "task_events", "task_comments", "task_links",
    "task_attachments", "grants",
)  # fmt: skip
RUN_LOG = (  # target column, key in the log line, accepted type
    ("model", "model", str), ("effort", "effort", str), ("session_id", "session_id", str),
    ("resumed", "resumed", bool), ("exit_code", "exit_code", int),
    ("claude_exit_code", "claude_exit_code", int), ("subtype", "subtype", str),
    ("is_error", "is_error", bool), ("api_error_status", "api_error_status", int),
    ("num_turns", "num_turns", int), ("session_cost_usd", "session_cost_usd", float),
    ("cost_usd", "total_cost_usd", float), ("stream_path", "stream", str),
)  # fmt: skip
PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)")
LAST = 1 << 62  # sorts import-made rows after the source rows of the same second

Key = tuple[int, str, int]  # (time, source board, source id): the global chronological order


class Source(NamedTuple):
    slug: str
    db: Path
    meta: dict[str, Any]


@dataclass
class Plan:
    rows: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {t: [] for t in ORDER})
    staged: dict[str, list[tuple[Key, dict[str, Any]]]] = field(default_factory=dict)
    baseline: dict[str, int] = field(default_factory=dict)  # table -> id counter when numbered
    config: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # `--force` overrides these
    errors: list[str] = field(default_factory=list)  # nothing overrides these

    def stage(self, table: str, key: Key, row: dict[str, Any]) -> None:
        self.staged.setdefault(table, []).append((key, row))


@dataclass
class Ctx:
    conn: sqlite3.Connection
    root: Path
    ts: int
    config_dir: str | None
    default_workdir: str | None
    force: bool


def gateway_running() -> bool:
    try:
        done = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{GATEWAY}"], capture_output=True, check=False
        )
    except FileNotFoundError:
        return False
    return done.returncode == 0


# --- reading --------------------------------------------------------------------------------


def inside(path: Path, root: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == root or root in real.parents


def side_file(p: Plan, root: Path, path: Path) -> str | None:
    if not path.is_file():
        return None
    if not inside(path, root):
        p.warnings.append(f"{path.name}: resolves outside {root}; skipped")
        return None
    return path.read_text(errors="replace")


def discover(p: Plan, root: Path, board: str | None, every: bool) -> list[Source]:
    if not (board or every):
        raise Usage("pass --board SLUG or --all")
    boards = root / "kanban" / "boards"
    found: list[tuple[str, Path]] = []
    if board:
        validate.slug(board)
        archived = sorted(boards.glob(f"_archived/{board}-*/kanban.db"), reverse=True)
        first = root / "kanban.db" if board == "default" else boards / board / "kanban.db"
        db = next((c for c in (first, *archived) if c.is_file()), None)
        if db is None:
            raise NotFound(f"no board {board!r} under {root}")
        if not inside(db, root):
            raise Refused(f"{db} resolves outside {root}")
        found.append((board, db))
    else:
        if (root / "kanban.db").is_file():
            found.append(("default", root / "kanban.db"))
        found += [(d.parent.name, d) for d in sorted(boards.glob("*/kanban.db"))]
    sources = []
    for slug, db in found:
        try:
            validate.slug(slug)
            if not inside(db, root):
                raise Refused(f"{db} resolves outside {root}")
        except Refused as e:
            p.dropped.append(f"board {slug}: {e}")
            continue
        meta: Any = {}
        text = None if slug == "default" else side_file(p, root, db.parent / "board.json")
        if text is not None:
            meta = payload_of(text)
        sources.append(Source(slug, db, meta if isinstance(meta, dict) else {}))
    return sources


def read_source(db: Path) -> dict[str, list[dict[str, Any]]]:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "kanban.db"
        for suffix in ("", "-wal", "-shm"):
            if Path(f"{db}{suffix}").is_file():
                shutil.copyfile(f"{db}{suffix}", f"{copy}{suffix}")
        try:
            src = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            try:
                have = {r[0] for r in src.execute("SELECT name FROM sqlite_master")}
                if "tasks" not in have:
                    raise Refused(f"{db} has no tasks table; it is not a kanban database")
                data: dict[str, list[dict[str, Any]]] = {}
                for table, needed in REQUIRED.items():
                    cur = src.execute(f"SELECT * FROM {table}") if table in have else None
                    names = [d[0] for d in cur.description] if cur else list(needed)
                    if missing := [c for c in needed if c not in names]:
                        raise Refused(f"{db}: table {table} lacks column(s) {', '.join(missing)}")
                    data[table] = [dict(r) for r in cur] if cur else []
                return data
            finally:
                src.close()
        except sqlite3.DatabaseError as e:
            raise Refused(f"{db} is not a readable kanban database: {e}") from e


def payload_of(text: object) -> Any:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text}


def valid(p: Plan, what: str, name: str, value: Any, check: Callable[[Any], Any]) -> Any:
    """Keep `value` if `check` accepts it; otherwise drop it with a warning."""
    if value is None:
        return None
    try:
        return check(value)
    except Refused as e:
        p.warnings.append(f"{what}: {name} dropped ({e})")
        return None


def group(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["task_id"], []).append(r)
    return out


# --- planning -------------------------------------------------------------------------------


def plan_board(p: Plan, ctx: Ctx, s: Source, taken: set[str]) -> None:
    workdir = ctx.default_workdir if s.slug == "default" else s.meta.get("default_workdir")
    resolved: str | None = None
    why = "it has no default_workdir" + (" (pass --default-workdir)" if s.slug == "default" else "")
    if isinstance(workdir, str) and workdir:
        why = f"default_workdir {workdir!r} is not an existing directory"
        path = Path(workdir).expanduser()
        if path.is_dir():
            try:
                resolved = validate.workspace("dir", str(path), strict=False)
            except Refused as e:
                why = str(e)
    if resolved is None:
        p.warnings.append(f"board {s.slug}: {why}; imported archived, its cards cannot dispatch")
    if s.slug in taken:
        p.errors.append(f"board {s.slug!r} already exists in the target; nothing is overwritten")
    created = s.meta.get("created_at")
    p.rows["boards"].append(
        {
            "slug": s.slug,
            "name": s.meta.get("name") if isinstance(s.meta.get("name"), str) else s.slug,
            "default_workdir": resolved,
            "created_at": created if isinstance(created, int) else ctx.ts,
            "archived_at": ctx.ts if resolved is None or s.meta.get("archived") else None,
        }
    )
    event = {"board": s.slug, "task_id": None, "run_id": None, "kind": "board_created"}
    p.stage(
        "task_events",
        (ctx.ts, s.slug, 0),
        {**event, "actor": AUTHOR, "payload": {"slug": s.slug}, "created_at": ctx.ts},
    )


def newest_pr(texts: list[str | None]) -> tuple[int, str] | None:
    for text in texts:
        if found := list(PR_RE.finditer(text or "")):
            return int(found[-1][1]), found[-1][0]
    return None


def plan_tasks(
    p: Plan, ctx: Ctx, s: Source, data: dict[str, list[dict[str, Any]]], owner_of: dict[str, str]
) -> set[str]:
    runs, comments, events = (group(data[t]) for t in ("task_runs", "task_comments", "task_events"))
    kept: set[str] = set()
    idem: set[str] = set()
    for t in data["tasks"]:
        tid, what = t["id"], f"{s.slug}/{t['id']}"
        status = STATUS_MAP.get(t["status"])
        kind = t.get("workspace_kind") or "scratch"
        bad = None
        if not isinstance(tid, str) or not validate.TASK_ID_RE.fullmatch(tid):
            bad = "its id is not t_<8 hex>"
        elif status is None:
            bad = f"unknown status {t['status']!r}"
        elif kind not in WORKSPACES:
            bad = f"unknown workspace kind {kind!r}"
        elif not t["title"] or not isinstance(t["created_at"], int):
            bad = "no title or created_at"
        if bad:
            p.dropped.append(f"tasks {what}: {bad}")
            continue
        if tid in owner_of:
            p.errors.append(
                f"task {tid} is on board {owner_of[tid]!r} and on {s.slug!r}; "
                "nothing is renamed silently"
            )
            continue
        owner_of[tid] = s.slug
        kept.add(tid)

        mine = comments.get(tid, [])
        review = max(
            (r for r in runs.get(tid, []) if r.get("outcome") == "review_requested"),
            key=lambda r: (r["started_at"], r["id"]),
            default=None,
        )
        implementer, pr = None, None
        if review:
            implementer = review.get("profile")
            newest_first = sorted(mine, key=lambda c: (c["created_at"], c["id"]), reverse=True)
            pr = newest_pr([review.get("summary"), *(c["body"] for c in newest_first)])
        sched = max(
            (e for e in events.get(tid, []) if e["kind"] == "scheduled"),
            key=lambda e: (e["created_at"], e["id"]),
            default=None,
        )
        sched = sched if t["status"] == "scheduled" else None
        block = t.get("block_kind")
        block = "needs_input" if block == "dependency" or sched else block
        if status != "blocked" or block not in store.WORKER_BLOCK_KINDS:
            block = None
        key = t.get("idempotency_key")
        if key and status != "archived":
            if key in idem:
                p.warnings.append(f"tasks {what}: idempotency_key {key!r} repeats on the board")
                key = None
            else:
                idem.add(key)
        path = t.get("workspace_path")
        wt = None
        if kind == "worktree":
            wt = "active" if path and Path(path).exists() else "removed"
        spent = [e["created_at"] for e in events.get(tid, [])]
        runtime = int(t["max_runtime_seconds"]) if t.get("max_runtime_seconds") else None
        if runtime is None and status != "triage":
            runtime = store.role_runtime(ctx.conn, t.get("assignee"))
        p.rows["tasks"].append(
            {
                "id": tid, "board": s.slug, "title": t["title"], "body": t.get("body") or "",
                "status": status, "assignee": t.get("assignee"), "implementer": implementer,
                "priority": int(t.get("priority") or 0), "workspace_kind": kind,
                "workspace_path": None if kind == "scratch" else path,
                "branch_name": valid(p, what, "branch_name", t.get("branch_name"), validate.branch),
                "max_runtime_seconds": runtime,
                "max_retries": t.get("max_retries"),
                "consecutive_failures": int(t.get("consecutive_failures") or 0),
                "last_failure_error": t.get("last_failure_error"), "block_kind": block,
                "block_recurrences": int(t.get("block_recurrences") or 0),
                "model_override": valid(p, what, "model", t.get("model_override"), validate.model),
                "reasoning_effort": valid(
                    p, what, "effort", t.get("reasoning_effort"), validate.effort
                ),
                "skills": store.dumps(
                    valid(p, what, "skills", payload_of(t.get("skills")) or [], validate.skills)
                    or []
                ),
                "idempotency_key": key, "pr_number": pr[0] if pr else None,
                "pr_url": pr[1] if pr else None, "worktree_state": wt,
                "created_by": t.get("created_by"), "created_at": t["created_at"],
                "updated_at": max([t["created_at"], *spent]), "started_at": t.get("started_at"),
                "completed_at": t.get("completed_at"),
            }
        )  # fmt: skip

        def note(body: str, at: int, tid: str = tid) -> None:
            row = {"task_id": tid, "author": AUTHOR, "body": body, "created_at": at}
            p.stage("task_comments", (at, s.slug, LAST), row)

        if t.get("result"):
            note(f"[result] {t['result']}", t.get("completed_at") or t["created_at"])
        if sched:
            reason = payload_of(sched.get("payload"))
            reason = reason.get("reason") if isinstance(reason, dict) else None
            note(f"SCHEDULED: {reason or 'no reason recorded'}", sched["created_at"])
        if t["status"] == "running":
            p.blockers.append(f"card {what} is running")
            note(
                "Imported while running: set to ready, its open run was closed as interrupted.",
                ctx.ts,
            )
    return kept


def plan_history(
    p: Plan, ctx: Ctx, s: Source, data: dict[str, list[dict[str, Any]]], kept: set[str]
) -> None:
    def orphan(table: str, row: dict[str, Any]) -> bool:
        if row["task_id"] in kept:
            return False
        p.dropped.append(
            f"{table} {s.slug}#{row.get('id', '')}: card {row['task_id']} not imported"
        )
        return True

    review_runs: set[int] = set()
    for e in data["task_events"]:
        if orphan("task_events", e):
            continue
        pl = payload_of(e.get("payload"))
        if pl is None and e["kind"] == "edited":
            pl = {"fields": None, "previous": None, "source": "hermes_dashboard"}
        body = pl if isinstance(pl, dict) else {}
        rid = e.get("run_id") if e.get("run_id") is not None else body.get("run_id")
        if e["kind"] == "claimed" and body.get("source_status") == "review" and rid is not None:
            review_runs.add(rid)
        actor = next((body[k] for k in ("actor", "author") if isinstance(body.get(k), str)), None)
        row = {
            "board": s.slug,
            "task_id": e["task_id"],
            "run_id": e.get("run_id"),
            "kind": e["kind"],
        }
        p.stage(
            "task_events",
            (e["created_at"], s.slug, e["id"]),
            {**row, "actor": actor, "payload": pl, "created_at": e["created_at"]},
        )
    for r in data["task_runs"]:
        if orphan("task_runs", r):
            continue
        running = r["status"] == "running"
        outcome = r.get("outcome")
        outcome = outcome if outcome in OUTCOMES else OUTCOME_MAP.get(str(outcome), "interrupted")
        row = {
            "task_id": r["task_id"], "role": r.get("profile"),
            "kind": "review" if r["id"] in review_runs else "work", "status": "ended",
            "outcome": "interrupted" if running else outcome, "summary": r.get("summary"),
            "metadata": r.get("metadata") if payload_of(r.get("metadata")) is not None else None,
            "error": r.get("error"), "config_dir": ctx.config_dir,
            "max_runtime_seconds": r.get("max_runtime_seconds"), "started_at": r["started_at"],
            "ended_at": ctx.ts if running else r.get("ended_at"),
            **{dst: None for dst, _, _ in RUN_LOG},
        }  # fmt: skip
        p.stage("task_runs", (r["started_at"], s.slug, r["id"]), row)
        if running:
            ev = {
                "board": s.slug,
                "task_id": r["task_id"],
                "run_id": r["id"],
                "kind": "interrupted",
            }
            payload = {"reason": "import"}
            p.stage(
                "task_events",
                (ctx.ts, s.slug, LAST + r["id"]),
                {**ev, "actor": AUTHOR, "payload": payload, "created_at": ctx.ts},
            )
    for c in data["task_comments"]:
        if not orphan("task_comments", c):
            row = {k: c[k] for k in ("task_id", "author", "body", "created_at")}
            p.stage("task_comments", (c["created_at"], s.slug, c["id"]), row)
    for a in data["task_attachments"]:
        if orphan("task_attachments", a):
            continue
        raw = Path(a["stored_path"])
        blob = raw if raw.is_absolute() else s.db.parent / raw
        label = f"task_attachments {s.slug}#{a['id']}"
        if not inside(blob, ctx.root):
            p.dropped.append(f"{label}: {a['stored_path']!r} resolves outside {ctx.root}")
        elif not Path(os.path.realpath(blob)).is_file():
            p.dropped.append(f"{label}: blob {a['stored_path']!r} is missing")
        else:
            row = {k: a.get(k) for k in ("task_id", "filename", "content_type", "uploaded_by")}
            row["stored_path"] = os.path.realpath(blob)
            row["size"] = a.get("size") or Path(row["stored_path"]).stat().st_size
            p.stage(
                "task_attachments",
                (a["created_at"], s.slug, a["id"]),
                {**row, "created_at": a["created_at"]},
            )
    pairs = set()
    for link in data["task_links"]:
        ends = (link["parent_id"], link["child_id"])
        if ends[0] == ends[1] or not set(ends) <= kept:
            p.dropped.append(f"task_links {s.slug} {ends[0]} -> {ends[1]}: a card is not imported")
        elif ends not in pairs:
            pairs.add(ends)
            p.rows["task_links"].append({"parent_id": ends[0], "child_id": ends[1]})


def id_counter(conn: sqlite3.Connection, table: str) -> int:
    """The highest id `table` has handed out: its rows, or the AUTOINCREMENT counter after deletes."""
    seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
    last = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0]
    return int(max(last, seq[0] if seq else 0))


def number(p: Plan, conn: sqlite3.Connection, table: str) -> list[tuple[Key, dict[str, Any]]]:
    """Sort a staged table chronologically and give its rows fresh ids (MIG-03)."""
    first = p.baseline[table] = id_counter(conn, table)
    staged = sorted(p.staged.get(table, []), key=lambda kr: kr[0])
    for i, (_, row) in enumerate(staged, 1):
        row["id"] = first + i
    return staged


def renumber(p: Plan, ctx: Ctx) -> dict[tuple[str, int], int]:
    remap: dict[tuple[str, int], int] = {}
    runs = number(p, ctx.conn, "task_runs")
    count: Counter[str] = Counter()
    for (_, board, old), row in runs:
        count[row["task_id"]] += 1
        row["n"] = count[row["task_id"]]
        remap[(board, old)] = row["id"]
    p.rows["task_runs"] = [row for _, row in runs]
    dangling = 0
    events = number(p, ctx.conn, "task_events")
    for (_, board, _), row in events:
        pl = row["payload"]
        if row["run_id"] is not None:
            row["run_id"] = remap.get((board, row["run_id"]))
            dangling += row["run_id"] is None
        if isinstance(pl, dict) and pl.get("run_id") is not None:
            pl["run_id"] = remap.get((board, pl["run_id"]))
            dangling += pl["run_id"] is None
        row["payload"] = None if pl is None else store.dumps(pl)
    if dangling:
        p.warnings.append(f"{dangling} event run reference(s) pointed at no run and are now NULL")
    p.rows["task_events"] = [row for _, row in events]
    for table in ("task_comments", "task_attachments"):
        p.rows[table] = [row for _, row in number(p, ctx.conn, table)]
    return remap


def plan_run_log(p: Plan, ctx: Ctx, remap: dict[tuple[str, int], int]) -> None:
    text = side_file(p, ctx.root, ctx.root / "kanban" / "claude-runs.jsonl")
    if text is None:
        return
    lines: dict[tuple[Any, Any, Any], tuple[int, dict[str, Any]]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        rec = payload_of(line)
        if not isinstance(rec, dict) or "raw" in rec:
            p.dropped.append(f"claude-runs.jsonl line {n}: not a JSON object")
        else:
            # Hermes logs run_id as text ("1"); compare as text so int ids match too.
            ident = (rec.get("board") or "default", rec.get("task"), str(rec.get("run_id")))
            lines[ident] = (n, rec)
    by_id = {r["id"]: r for r in p.rows["task_runs"]}
    for (board, old), new in remap.items():
        row = by_id[new]
        hit = lines.pop((board, row["task_id"], str(old)), None)
        if hit is None:
            continue
        for dst, key, kind in RUN_LOG:
            v = hit[1].get(key)
            v = float(v) if kind is float and type(v) is int else v
            row[dst] = (int(v) if kind is bool else v) if type(v) is kind else None
        what = f"{board}/{row['task_id']}"
        row["model"] = valid(p, what, "model", row["model"], validate.model)
        row["effort"] = valid(p, what, "effort", row["effort"], validate.effort)
    for n, _ in sorted(lines.values(), key=lambda x: x[0]):
        p.dropped.append(f"claude-runs.jsonl line {n}: no matching run")


def plan_grants(p: Plan, ctx: Ctx, tasks: set[str]) -> None:
    for f in sorted((ctx.root / "kanban" / "grants").glob("*.json")):
        text = side_file(p, ctx.root, f)
        body = payload_of(text)
        rules = body.get("allow") if isinstance(body, dict) else None
        approved = body.get("approved") if isinstance(body, dict) else None
        if f.stem not in tasks:
            p.dropped.append(f"grants {f.name}: card {f.stem} is not imported")
        elif not (isinstance(rules, list) and rules and all(isinstance(r, str) for r in rules)):
            p.dropped.append(f"grants {f.name}: 'allow' is not a non-empty list of rules")
        elif not isinstance(approved, str):
            p.dropped.append(f"grants {f.name}: 'approved' is not a string")
        else:
            p.rows["grants"].append(
                {"task_id": f.stem, "rules": store.dumps(rules), "approved": approved,
                 "created_at": ctx.ts, "revoked_at": ctx.ts}
            )  # fmt: skip


def plan_limit(p: Plan, ctx: Ctx) -> None:
    body = payload_of(side_file(p, ctx.root, ctx.root / "kanban" / "claude-limit.json"))
    resets = body.get("resetsAt") if isinstance(body, dict) else None
    if isinstance(resets, int | float) and resets > ctx.ts:
        key = f"account:{ctx.config_dir or Path.home() / '.claude'}"
        old = store.state_get(ctx.conn, key) or {}
        floor = old.get("hard_until") or 0
        blank = {"soft_until": None, "last_rate_limit": None, "auth_paused": None}
        p.state[key] = {**blank, **old, "hard_until": max(int(resets), floor)}


def scalar(text: str) -> Any:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [scalar(x) for x in text[1:-1].split(",") if x.strip()]
    text = text.strip("\"'")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return {"null": None, "~": None, "true": True, "false": False}.get(text.lower(), text)


def kanban_section(text: str) -> dict[str, Any]:
    """The direct children of a top-level `kanban:` mapping; scalars, `[a, b]` and `- a` lists."""
    out: dict[str, Any] = {}
    in_section, key, indent = False, "", 0
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            in_section, key, indent = stripped == "kanban:", "", 0
        elif in_section and stripped.startswith("- ") and isinstance(out.get(key), list):
            out[key].append(scalar(stripped[2:]))
        elif in_section:
            depth = len(line) - len(line.lstrip())
            indent = indent or depth
            if depth == indent:
                key, _, value = stripped.partition(":")
                out[key] = scalar(value) if value.strip() else []
    return out


def plan_config(p: Plan, ctx: Ctx) -> None:
    text = side_file(p, ctx.root, ctx.root / "config.yaml")
    if text is None:
        return
    conf = kanban_section(text)
    roles = store.cfg(ctx.conn, "dispatch.roles")
    if isinstance(conf.get("dispatch_profiles"), list):
        wanted = [r for r in conf["dispatch_profiles"] if isinstance(r, str)]
        atlassian = store.cfg(ctx.conn, "roles.atlassian.enabled")
        usable = [r for r in wanted if r != "atlassian" or atlassian]
        if len(usable) != len(wanted):
            p.warnings.append("dispatch_profiles: atlassian left out, its role is not enabled")
        if usable:
            p.config["dispatch.roles"] = roles = usable
    limit = conf.get("max_in_progress")
    if "max_in_progress" in conf:
        if type(limit) is int and limit >= 1:
            p.config["dispatch.max_in_progress"] = limit
        else:
            p.warnings.append(f"max_in_progress {limit!r} is not a positive integer; ignored")
    if (who := conf.get("default_assignee")) is not None:
        if who in roles:
            p.config["dispatch.default_assignee"] = who
        else:
            p.warnings.append(f"default_assignee {who!r} is not dispatchable; left unset (null)")


def build(
    conn: sqlite3.Connection,
    root: Path,
    sources: list[Source],
    p: Plan,
    *,
    default_workdir: str | None,
    force: bool,
    gateway: bool,
) -> None:
    ctx = Ctx(conn, root, store.now(), store.cfg(conn, "claude.config_dir"), default_workdir, force)
    owner_of = {r["id"]: r["board"] for r in conn.execute("SELECT id, board FROM tasks")}
    taken = {r["slug"] for r in conn.execute("SELECT slug FROM boards")}
    tasks: set[str] = set()
    for s in sorted(sources):
        data = read_source(s.db)
        plan_board(p, ctx, s, taken)
        kept = plan_tasks(p, ctx, s, data, owner_of)
        plan_history(p, ctx, s, data, kept)
        tasks |= kept
    remap = renumber(p, ctx)
    plan_run_log(p, ctx, remap)
    plan_grants(p, ctx, tasks)
    plan_limit(p, ctx)
    plan_config(p, ctx)
    if gateway:
        p.blockers.append(
            f"the legacy gateway is running (stop it: launchctl bootout gui/$UID/{GATEWAY})"
        )


# --- applying and reporting -----------------------------------------------------------------


def insert(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        cols = list(rows[0])
        sql = f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def apply(conn: sqlite3.Connection, p: Plan) -> None:
    written: list[Path] = []
    try:
        with store.tx(conn):
            for table, first in p.baseline.items():
                if id_counter(conn, table) != first:
                    raise Refused("database changed during planning; re-run")
            for table in ORDER:
                if table == "task_attachments":
                    for row in p.rows[table]:
                        data = Path(row["stored_path"]).read_bytes()
                        path, _ = store.write_blob(row["task_id"], row["filename"], data)
                        written.append(path)
                        row["stored_path"] = str(path)
                insert(conn, table, p.rows[table])
            for key, value in p.config.items():
                store.config_put(conn, key, value)
            for key, value in p.state.items():
                store.state_put(conn, key, value)
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def report(p: Plan, *, dry: bool) -> tuple[dict[str, Any], str]:
    data: dict[str, Any] = {
        "dry_run": dry,
        "counts": {t: len(rows) for t, rows in p.rows.items()},
        "statuses": dict(sorted(Counter(r["status"] for r in p.rows["tasks"]).items())),
        "grants": [
            f"{g['task_id']}: {len(json.loads(g['rules']))} rule(s), imported revoked"
            for g in p.rows["grants"]
        ],
        "blockers": p.blockers, "errors": p.errors, "warnings": p.warnings, "dropped": p.dropped,
    }  # fmt: skip
    lines = ["dry run, nothing written" if dry else "imported"]
    lines.append("counts: " + " ".join(f"{k}={v}" for k, v in data["counts"].items()))
    lines.append("statuses: " + " ".join(f"{k}={v}" for k, v in data["statuses"].items()))
    for title in ("blockers", "errors", "warnings", "grants", "dropped"):
        if data[title]:
            lines += [f"{title} ({len(data[title])}):", *(f"  {x}" for x in data[title])]
    if not dry:
        data["next"] = [*DECOMMISSION, UNINSTALL]
        lines += ["", "Decommission the old setup yourself; these were not run:"]
        lines += [f"  {c}" for c in data["next"]]
    return data, "\n".join(lines)


def cli(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    root = Path(os.path.realpath(Path(getattr(a, "from") or "~/.hermes").expanduser()))
    if not root.is_dir():
        raise NotFound(f"{root} is not a directory; pass --from <dir>")
    p = Plan()
    sources = discover(p, root, a.board, a.all)
    build(
        conn, root, sources, p,
        default_workdir=a.default_workdir, force=a.force, gateway=gateway_running(),
    )  # fmt: skip
    if not a.dry_run:
        refusal = p.errors or ([] if a.force else p.blockers)
        if refusal:
            raise Refused("import refused:\n  " + "\n  ".join(refusal) + "\nnothing was written")
        apply(conn, p)
    return report(p, dry=a.dry_run)
