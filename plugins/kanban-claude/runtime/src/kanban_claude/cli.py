"""The `kanban` command (CLI-01..11): argument parsing, mode gating and output."""

import argparse
import json
import os
import signal
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import (
    __version__,
    daemon,
    dispatcher,
    doctor,
    gitops,
    install,
    runner,
    store,
    views,
    web,
    worker,
)
from .errors import Error, NotAllowed, NotFound, Refused, Usage
from .paths import dirs, nudge
from .procs import Procs

Handler = Callable[[argparse.Namespace, sqlite3.Connection], tuple[dict[str, Any], str | None]]

RUN_ONLY = {"block", "complete", "request-review", "request-changes"}
NEEDS_DAEMON = {"create", "specify", "promote", "unblock"}
NUDGE = NEEDS_DAEMON | {
    "assign", "reassign", "edit", "reopen", "reopen-review", "merge-retry", "mark-done", "resume",
    "stop",
}  # fmt: skip
LONG_RUNNING: dict[str, Callable[[argparse.Namespace], int]] = {
    "daemon": lambda a: daemon.serve(),
    "run": lambda a: runner.run(a.run_id),
}
OWNER: dict[str, Handler] = {}


def owner_verb(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        OWNER[name] = fn
        return fn

    return register


def plain(fn: Callable[[argparse.Namespace, sqlite3.Connection], dict[str, Any]]) -> Handler:
    """A verb whose store result is its whole output."""
    return lambda a, conn: (fn(a, conn), None)


def _reasoned(fn: Callable[..., dict[str, Any]]) -> Handler:
    return plain(lambda a, conn: fn(conn, a.id, a.reason))


# --- parser ---------------------------------------------------------------------------------


def build() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--board", default=argparse.SUPPRESS, metavar="SLUG")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    root = argparse.ArgumentParser(prog="kanban", allow_abbrev=False)
    root.add_argument("--board", metavar="SLUG")
    root.add_argument("--json", action="store_true")
    root.add_argument("--version", action="version", version=f"kanban {__version__}")
    sub = root.add_subparsers(dest="verb", required=True, metavar="VERB")

    def add(name: str, *spec: str | tuple[str, Any]) -> None:
        """A bare name is a positional, `--x` takes a value; a tuple adds its type or kind."""
        p = sub.add_parser(name, parents=[common], allow_abbrev=False)
        for item in spec:
            arg, kind = (item, None) if isinstance(item, str) else item
            if not arg.startswith("--"):
                extra: dict[str, Any] = {"type": int} if kind is int else {}
                p.add_argument(arg, nargs="?" if kind == "?" else None, **extra)
            elif kind == "flag":
                p.add_argument(arg, action="store_true")
            elif kind == "append":
                p.add_argument(arg, action="append")
            elif kind == "required":
                p.add_argument(arg, required=True)
            else:
                p.add_argument(arg, **({"type": kind} if kind else {}))

    text = ("--body", "--body-file")
    card = (
        "--assignee", "--workspace", "--branch", "--base", ("--priority", int), "--max-runtime",
        ("--max-retries", int), ("--skill", "append"), "--model", "--effort", ("--budget", float),
    )  # fmt: skip
    add("show", ("id", "?"))
    add("context", ("id", "?"))
    add("comment", "id", "text", "--author")
    add("attach", "id", "path")
    add("block", "id", ("--kind", "required"), ("--reason", "required"))
    add("complete", "id", "--summary", "--summary-file")
    add("request-review", "id", "--reviewer", "--summary", "--summary-file")
    add("request-changes", "id", ("--reason", "required"))
    add("boards", "action", ("slug", "?"), "--default-workdir", "--name", "--claude-config-dir")
    add(
        "create", "title", *card, *text, "--idempotency-key", ("--parent", "append"),
        ("--triage", "flag"),
    )  # fmt: skip
    add("specify", "id", "--title", *card, *text)
    add("list", "--status", "--assignee", "--sort", ("--limit", int))
    add("edit", "id", "--title", *card, *text, ("--allow-empty-body", "flag"))
    add("assign", "id", "role")
    add("reassign", "id", "role", ("--reclaim", "flag"))
    add("promote", "id", ("--reason", "required"), ("--ignore-merge", "flag"))
    for name in ("hold", "unblock", "reopen-review", "reopen", "mark-done"):
        add(name, "id", ("--reason", "required"))
    add("link", "parent", "child")
    add("unlink", "parent", "child")
    add("stop", "id")
    add("dispatch", ("--dry-run", "flag"))
    add("daemon")
    add("run", ("run_id", int))
    add("snapshot", "repo")
    add("archive", "id", ("--rm", "flag"), ("--yes", "flag"))
    add("merge-retry", "id")
    add("grant", "id", ("--allow", "append"), "--approved", ("--revoke", "flag"))
    add("grants", "id")
    add(
        "watch", "--kinds", ("--since", int), ("--since-start", "flag"), ("--follow", "flag"),
    )  # fmt: skip
    add("runs", "id")
    add("log", "id", ("--run", int), ("--tail", int))
    add("tail", "id")
    add("stats", ("--days", int))
    add("pause", "--reason")
    add("resume")
    add("config", "action", ("key", "?"), ("value", "?"))
    add("migrate")
    add("board", ("--revoke", "flag"), ("--no-open", "flag"))
    add(
        "setup", "--claude-bin", "--claude-config-dir", ("--port", int), ("--no-daemon", "flag"),
        ("--yes", "flag"),
    )  # fmt: skip
    add("doctor", ("--probe", "flag"), ("--fix", "flag"))
    add("uninstall", ("--purge", "flag"), ("--force", "flag"), ("--yes", "flag"))
    return root


# --- helpers --------------------------------------------------------------------------------


def render(data: dict[str, Any]) -> str:
    flat = (f"{k}={v}" for k, v in data.items() if v is not None and not isinstance(v, dict | list))
    return "  ".join(flat) or "ok"


def present(a: argparse.Namespace, data: dict[str, Any], text: str | None = None) -> None:
    if a.json:
        print(json.dumps(views.clean_obj({"schema": 1, **data})))
    else:
        print(views.clean(render(data) if text is None else text))


def text_arg(inline: str | None, file: str | None) -> str | None:
    if inline is not None and file is not None:
        raise Usage("pass the text inline or with the -file flag, not both")
    if file is None:
        return inline
    try:
        return sys.stdin.read() if file == "-" else Path(file).read_text()
    except OSError as e:
        raise Error(f"cannot read {file}: {e.strerror}") from e


def fields(a: argparse.Namespace) -> dict[str, Any]:
    """The card fields the user gave, under the store's names."""
    out = {k: getattr(a, k, None) for k in (*store.FIELD_COLUMNS, "workspace") if k != "skills"}
    out["skills"] = getattr(a, "skill", None)
    out["body"] = text_arg(getattr(a, "body", None), getattr(a, "body_file", None))
    return {k: v for k, v in out.items() if v is not None}


def need_id(a: argparse.Namespace) -> str:
    tid: str | None = a.id or os.environ.get("KANBAN_TASK")
    if not tid:
        raise Usage(f"{a.verb} needs a card ID (or KANBAN_TASK)")
    return tid


def find_board(conn: sqlite3.Connection, a: argparse.Namespace) -> str | None:
    """CLI-01: --board, then KANBAN_BOARD, then the board whose default_workdir is the cwd repo."""
    slug = a.board or os.environ.get("KANBAN_BOARD")
    if slug:
        return str(store.get_board(conn, slug)["slug"])
    top = gitops.toplevel(Path.cwd())
    if top:
        for b in store.boards_list(conn):
            if b["archived_at"] is None and Path(b["default_workdir"]).resolve() == top:
                return str(b["slug"])
    return None


def need_board(conn: sqlite3.Connection, a: argparse.Namespace) -> str:
    board = find_board(conn, a)
    if board is None:
        raise Usage(
            "no board: pass --board SLUG, set KANBAN_BOARD, or run inside a board's default workdir"
        )
    return board


def stop_on_sigterm() -> None:
    def leave(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, leave)


# --- owner verbs ----------------------------------------------------------------------------


@owner_verb("show")
def v_show(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    d = views.show_data(conn, need_id(a))
    return d, views.show_text(d)


@owner_verb("context")
def v_context(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    text = views.context_md(conn, need_id(a))
    return {"text": text}, text.rstrip("\n")


@owner_verb("comment")
def v_comment(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    text = sys.stdin.read() if a.text == "-" else a.text
    return store.comment(conn, a.id, text, author=a.author or "owner"), None


@owner_verb("attach")
def v_attach(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    return store.attach(conn, a.id, os.path.basename(a.path), read_upload(a.path)), None


@owner_verb("boards")
def v_boards(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    if a.action == "list":
        boards = store.boards_list(conn)
        lines = [
            f"{b['slug']}  {b['default_workdir']}{'  (archived)' if b['archived_at'] else ''}"
            for b in boards
        ]
        return {"boards": boards}, "\n".join(lines) or "(no boards)"
    if a.action not in ("create", "edit", "archive") or not a.slug:
        raise Usage("usage: kanban boards list | create|edit|archive SLUG [flags]")
    if a.action == "archive":
        return store.board_archive(conn, a.slug), None
    opts = {
        "default_workdir": a.default_workdir,
        "name": a.name,
        "claude_config_dir": a.claude_config_dir,
    }
    if a.action == "edit":
        return store.board_edit(conn, a.slug, **opts), None
    if not a.default_workdir:
        raise Usage("boards create needs --default-workdir PATH")
    return store.board_create(conn, a.slug, **opts), None


@owner_verb("create")
def v_create(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    f = fields(a)
    f.pop("title")
    got = store.create(
        conn, need_board(conn, a), a.title, parents=a.parent or [], triage=a.triage,
        idempotency_key=a.idempotency_key, **f,
    )  # fmt: skip
    return got, None


@owner_verb("list")
def v_list(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    board = need_board(conn, a)
    if a.sort and a.sort not in views.SORTS:
        raise Usage(f"--sort must be one of {', '.join(views.SORTS)}")
    cards = views.cards_list(
        conn, board, status=a.status, assignee=a.assignee, sort=a.sort or "created", limit=a.limit
    )
    lines = [
        f"{c['id']}  {c['status']:<8}  @{c['assignee'] or '-':<8}  p{c['priority']}  {c['title']}"
        for c in cards
    ]
    return {"board": board, "cards": cards}, "\n".join(lines) or "(no cards)"


OWNER["specify"] = plain(lambda a, conn: store.specify(conn, a.id, fields(a)))
OWNER["edit"] = plain(
    lambda a, conn: store.edit(conn, a.id, fields(a), allow_empty_body=a.allow_empty_body)
)
OWNER["assign"] = plain(lambda a, conn: store.assign(conn, a.id, a.role))
OWNER["reassign"] = plain(lambda a, conn: store.assign(conn, a.id, a.role, reclaim=a.reclaim))
OWNER["promote"] = plain(
    lambda a, conn: store.promote(conn, a.id, a.reason, ignore_merge=a.ignore_merge)
)
OWNER["hold"] = _reasoned(store.hold)
OWNER["unblock"] = _reasoned(store.unblock)
OWNER["reopen-review"] = _reasoned(store.reopen_review)
OWNER["reopen"] = _reasoned(store.reopen)
OWNER["mark-done"] = _reasoned(store.mark_done)
OWNER["link"] = plain(lambda a, conn: store.link(conn, a.parent, a.child))
OWNER["unlink"] = plain(lambda a, conn: store.unlink(conn, a.parent, a.child))
OWNER["stop"] = plain(lambda a, conn: store.stop(conn, a.id))
OWNER["merge-retry"] = plain(lambda a, conn: store.merge_retry(conn, a.id))


@owner_verb("archive")
def v_archive(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    if a.rm and not a.yes:
        raise Usage("archive --rm deletes the card and its history; add --yes to confirm")
    return store.archive(conn, a.id, rm=a.rm), None


@owner_verb("grant")
def v_grant(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    if a.revoke:
        return store.grant_revoke(conn, a.id), None
    if not a.allow or not a.approved:
        raise Usage('grant needs --allow RULE (repeatable) and --approved "WORDS · SOURCE · DATE"')
    return store.grant_add(conn, a.id, a.allow, a.approved), None


@owner_verb("grants")
def v_grants(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    grants = store.grants_list(conn, a.id)
    lines = [
        f"{'revoked' if g['revoked_at'] else 'active'}  {', '.join(g['rules'])}  ({g['approved']})"
        for g in grants
    ]
    return {"id": a.id, "grants": grants}, "\n".join(lines) or "(no grants)"


@owner_verb("watch")
def v_watch(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    board = find_board(conn, a)
    kinds = [k for k in (a.kinds or "").split(",") if k]
    if a.since is not None:
        cursor = a.since
    elif a.since_start:
        cursor = 0
    else:
        cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    stop_on_sigterm()
    try:
        while True:
            batch = store.events_since(conn, cursor, kinds=kinds, board=board, limit=500)
            for ev in batch:
                cursor = ev["id"]
                if a.json:
                    ev = {**ev, "payload": json.loads(ev["payload"]) if ev["payload"] else None}
                    print(json.dumps(views.clean_obj({"schema": 1, **ev})), flush=True)
                else:
                    print(views.clean(views.watch_line(ev)), flush=True)
            if len(batch) < 500:
                if not a.follow:
                    return {"cursor": cursor}, ""
                time.sleep(1)
    except KeyboardInterrupt:
        return {"cursor": cursor}, ""


@owner_verb("runs")
def v_runs(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    runs = views.runs_list(conn, a.id)
    lines = [
        f"#{r['n']}  {r['kind']:<6}  {r['role'] or '-':<8}  {r['status']:<7}  "
        f"{r['outcome'] or '-':<18}  {views.iso(r['started_at'])}  ${r['cost_usd'] or 0:.2f}"
        for r in runs
    ]
    return {"id": a.id, "runs": runs}, "\n".join(lines) or "(no runs)"


def run_log(conn: sqlite3.Connection, task_id: str, n: int | None) -> tuple[Path, str]:
    q = "SELECT log_path, status FROM task_runs WHERE task_id = ? "
    row = conn.execute(
        q + ("AND n = ?" if n else "ORDER BY n DESC LIMIT 1"), (task_id, *([n] if n else []))
    ).fetchone()
    if row is None:
        raise NotFound(f"card {task_id} has no run{f' {n}' if n else ''}")
    if not row["log_path"]:
        raise NotFound("that run has no log yet")
    return views.log_path(row["log_path"], dirs().home, ("runs", "streams")), row["status"]


@owner_verb("log")
def v_log(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    path, _ = run_log(conn, a.id, a.run)
    try:
        lines = views.log_lines(path)
    except OSError as e:
        raise Error(f"cannot read {path}: {e.strerror}") from e
    return {"id": a.id, "path": str(path)}, "\n".join(lines[-a.tail :] if a.tail else lines)


@owner_verb("tail")
def v_tail(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    path, status = run_log(conn, a.id, None)
    stop_on_sigterm()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read().decode(errors="replace")
                if chunk:
                    print(views.clean(chunk), end="", flush=True)
                elif status != "running":
                    break
                else:
                    time.sleep(0.5)
                    status = run_log(conn, a.id, None)[1]
    except (KeyboardInterrupt, OSError):
        pass
    return {"id": a.id}, ""


@owner_verb("stats")
def v_stats(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    data = store.stats(conn, board=find_board(conn, a), days=a.days or 7)
    return data, json.dumps(data, indent=2, sort_keys=True)


@owner_verb("pause")
def v_pause(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    store.pause(conn, a.reason or "")
    return {"paused": True}, "dispatch paused"


@owner_verb("resume")
def v_resume(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    """OBS-03: an auth pause clears only for an account whose login passes the probe."""
    cleared: list[str] = []
    failing: dict[str, str] = {}
    for row in conn.execute("SELECT key FROM state WHERE key LIKE 'account:%'").fetchall():
        config_dir = row["key"].removeprefix("account:")
        if store.account_get(conn, config_dir).get("auth_paused"):
            reason = runner.auth_probe(conn, config_dir)
            if reason is None:
                cleared.append(config_dir)
            else:
                failing[config_dir] = reason
    with store.tx(conn):
        for config_dir in cleared:
            store.account_set(conn, config_dir, auth_paused=None)
            store.event(conn, None, "account_resumed", store.OWNER, {"config_dir": config_dir})
    was = store.resume(conn)
    text = "dispatch resumed" if was else "dispatch was not paused"
    if failing:
        text += "; still paused: " + "; ".join(f"{d}: {why}" for d, why in failing.items())
    return {"resumed": was, "accounts_cleared": cleared, "accounts_failing": failing}, text


@owner_verb("dispatch")
def v_dispatch(
    a: argparse.Namespace, conn: sqlite3.Connection
) -> tuple[dict[str, Any], str | None]:
    """One tick in this process; `--dry-run` only shows what the tick would do."""
    report = dispatcher.dispatch_once(conn, store.now(), Procs(), dry_run=a.dry_run)
    lines = [f"slots={report.get('slots')} paused={report['paused']}"]
    lines += [f"{'would claim' if a.dry_run else 'claimed'} {c['id']}" for c in report["claimed"]]
    lines += [f"skipped {k}: {v}" for k, v in report["skipped"].items()]
    return report, "\n".join(lines)


@owner_verb("snapshot")
def v_snapshot(
    a: argparse.Namespace, conn: sqlite3.Connection
) -> tuple[dict[str, Any], str | None]:
    """CHK-03: print the path of a detached origin checkout; a warning goes to stderr."""
    path, warning = gitops.snapshot(a.repo)
    if warning:
        print(f"kanban: {warning}", file=sys.stderr)
    return {"path": str(path), "warning": warning}, str(path)


@owner_verb("config")
def v_config(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    if a.action == "list":
        cfg = store.config_all(conn)
        return {"config": cfg}, "\n".join(f"{k} = {json.dumps(v)}" for k, v in cfg.items())
    if a.action == "get" and a.key:
        value = store.config_all(conn).get(a.key, ...)
        if value is ...:
            raise NotFound(f"no config key {a.key!r}; list them with: kanban config list")
        return {"key": a.key, "value": value}, json.dumps(value)
    if a.action == "set" and a.key and a.value is not None:
        try:
            value = json.loads(a.value)
        except ValueError:
            value = a.value
        store.config_set(conn, a.key, value)
        return {"key": a.key, "value": value}, f"{a.key} = {json.dumps(value)}"
    raise Usage("usage: kanban config list | get KEY | set KEY VALUE")


OWNER["board"] = web.cli


def v_setup(a: argparse.Namespace) -> int:
    """INS-05; setup owns its store connection."""
    data, text = install.setup(a)
    present(a, data, text)
    return 0


LONG_RUNNING["setup"] = v_setup


def v_doctor(a: argparse.Namespace) -> int:
    """OBS-05: exit 1 when any check fails; warnings leave it 0."""
    data, text = doctor.run(probe=a.probe, fix=a.fix)
    present(a, data, text)
    return 0 if data["ok"] else 1


LONG_RUNNING["doctor"] = v_doctor


def v_uninstall(a: argparse.Namespace) -> int:
    data, text = install.uninstall(a)
    present(a, data, text)
    return 0


LONG_RUNNING["uninstall"] = v_uninstall


@owner_verb("migrate")
def v_migrate(a: argparse.Namespace, conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {"schema_version": version}, f"schema version {version}"


# --- worker mode ----------------------------------------------------------------------------


def read_upload(path: str) -> bytes:
    try:
        if os.path.getsize(path) > store.MAX_ATTACH_BYTES:
            raise Refused(f"{path} is larger than {store.MAX_ATTACH_BYTES} bytes (25 MiB)")
        return Path(path).read_bytes()
    except OSError as e:
        raise Error(f"cannot read {path}: {e.strerror}") from e


def worker_args(a: argparse.Namespace) -> dict[str, Any]:
    args: dict[str, Any] = {"id": getattr(a, "id", None)}
    match a.verb:
        case "comment":
            args["text"] = sys.stdin.read() if a.text == "-" else a.text
        case "attach":
            args["filename"] = os.path.basename(a.path)
            args["data"] = read_upload(a.path)
        case "block":
            args.update(kind=a.kind, reason=a.reason)
        case "complete" | "request-review":
            args["summary"] = text_arg(a.summary, a.summary_file)
            args["reviewer"] = getattr(a, "reviewer", None)
        case "request-changes":
            args["reason"] = a.reason
    return args


def run_as_worker(a: argparse.Namespace) -> int:
    """CLI-10: owner verbs exit 5 without touching the DB; worker verbs act on the run's card."""
    if a.verb not in worker.VERBS:
        raise NotAllowed(
            f"{a.verb} is an owner verb; a worker may only use: {', '.join(worker.VERBS)}"
        )
    try:
        run_id = int(os.environ["KANBAN_RUN_ID"])
    except ValueError:
        raise Usage("KANBAN_RUN_ID must be a run number") from None
    args = worker_args(a)
    conn = store.connect()
    try:
        data = worker.act(conn, run_id, a.verb, args) or {}
    finally:
        conn.close()
    if a.verb == "show":
        present(a, data, views.show_text(data))
    elif a.verb == "context":
        present(a, data, data["text"].rstrip("\n"))
    else:
        present(a, data, "already done" if data.get("noop") else None)
    return 0


def owner(a: argparse.Namespace) -> int:
    if a.verb in RUN_ONLY:
        raise NotAllowed(
            f"{a.verb} ends a worker's run, so only a worker can use it; "
            "the owner unblocks, reopens, marks done or stops a card instead"
        )
    if a.verb in LONG_RUNNING:
        return LONG_RUNNING[a.verb](a)
    conn = store.connect()
    try:
        data, text = OWNER[a.verb](a, conn)
    finally:
        conn.close()
    if a.verb in NUDGE:
        nudge()
    if text != "":
        present(a, data, text)
    if a.verb in NEEDS_DAEMON and not daemon.running():
        print(
            "kanban: daemon not running — cards will not dispatch; run: kanban doctor",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    a = build().parse_args(argv)
    try:
        return run_as_worker(a) if "KANBAN_RUN_ID" in os.environ else owner(a)
    except Error as e:
        print(f"kanban: {views.clean(str(e))}", file=sys.stderr)
        return e.exit
    except sqlite3.OperationalError as e:
        print(f"kanban: database busy or unavailable: {e}; retry", file=sys.stderr)
        return 1
    except sqlite3.DatabaseError as e:
        print(f"kanban: database error: {views.clean(str(e))}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
