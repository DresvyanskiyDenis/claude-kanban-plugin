"""What one run did, counted from its own stream events (the per-run measurement of 0.4.0).

`Measure` is a pure accumulator: feed it every stream event and read the totals. The last `result`
event is no whole-run summary (it can describe one turn), so everything here comes from the events
themselves, except what only a result carries: API time, permission denials and the subagent counts.
"""

import collections
import json
import sqlite3
from pathlib import Path
from typing import Any

from . import store

# the only env keys `params` keeps: those the runner itself sets, so a future secret cannot leak
PARAM_ENV = (
    "CLAUDE_CODE_WEB_FETCH_AGENT", "CLAUDE_SKIP_SOUL", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL",
    "KANBAN_TASK", "KANBAN_BOARD", "KANBAN_RUN_ID", "KANBAN_WORKSPACE", "BASH_DEFAULT_TIMEOUT_MS",
    "GH_HOST", "CLAUDE_CONFIG_DIR",
)  # fmt: skip
BATCH = 50


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def _int(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


class Measure:
    def __init__(self) -> None:
        self.init: dict[str, Any] | None = None
        self.api_total = 0  # duration_api_ms is cumulative over the session, so the largest wins
        self._ids: set[str] = set()
        self._blocks: set[str] = set()
        self._errors: set[str] = set()
        self._cache: dict[str, tuple[int, int]] = {}
        self._tools: collections.Counter[str] = collections.Counter()
        self._skills: collections.Counter[str] = collections.Counter()
        self._mcp: collections.Counter[str] = collections.Counter()
        self._agents: collections.Counter[str] = collections.Counter()
        self._hooks: dict[str, dict[str, int]] = {}
        self._compactions = 0
        self._denials = 0
        self._spawned = 0
        self._failed = 0
        self._reason: str | None = None
        self._seen_result = False

    def feed(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type")
        if kind == "assistant":
            self._assistant(_dict(ev.get("message")), ev)
        elif kind == "user":
            self._user(_dict(ev.get("message")))
        elif kind == "system":
            self._system(ev)
        elif kind == "result":
            self._result(ev)

    def _assistant(self, msg: dict[str, Any], ev: dict[str, Any]) -> None:
        mid = msg.get("id") or ev.get("uuid")
        if isinstance(mid, str):
            self._ids.add(mid)
            cc = _dict(_dict(msg.get("usage")).get("cache_creation"))
            if cc:
                self._cache[mid] = (
                    _int(cc.get("ephemeral_1h_input_tokens")),
                    _int(cc.get("ephemeral_5m_input_tokens")),
                )
        for b in _list(msg.get("content")):
            if not isinstance(b, dict) or b.get("type") not in ("tool_use", "server_tool_use"):
                continue
            bid, name = b.get("id"), b.get("name")
            if not isinstance(name, str) or bid in self._blocks:
                continue
            if bid is not None:
                self._blocks.add(bid)
            self._tools[name] += 1
            args = _dict(b.get("input"))
            if name.startswith("mcp__"):
                self._mcp[name[5:].split("__", 1)[0]] += 1
            elif name == "Skill" and isinstance(args.get("skill"), str):
                self._skills[args["skill"]] += 1
            elif name in ("Agent", "Task") and isinstance(args.get("subagent_type"), str):
                self._agents[args["subagent_type"]] += 1

    def _user(self, msg: dict[str, Any]) -> None:
        for b in _list(msg.get("content")):
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                self._errors.add(str(b.get("tool_use_id") or len(self._errors)))

    def _system(self, ev: dict[str, Any]) -> None:
        sub = ev.get("subtype")
        if sub == "init" and self.init is None:
            self.init = ev
        elif sub == "compact_boundary":
            self._compactions += 1
        elif sub == "hook_response":
            ok = ev.get("outcome") == "success" or (
                ev.get("outcome") is None and not ev.get("exit_code")
            )
            h = self._hooks.setdefault(str(ev.get("hook_name") or "?"), {"ok": 0, "failed": 0})
            h["ok" if ok else "failed"] += 1

    def _result(self, ev: dict[str, Any]) -> None:
        self._seen_result = True
        self.api_total = max(self.api_total, _int(ev.get("duration_api_ms")))
        self._denials += (
            len(ev["permission_denials"]) if isinstance(ev.get("permission_denials"), list) else 0
        )
        stats = _dict(
            ev.get("subagent_stats")
        )  # cumulative within one invocation, not across a resume
        self._spawned = max(self._spawned, _int(stats.get("spawned")))
        self._failed = max(self._failed, _int(stats.get("failed")))
        if isinstance(ev.get("terminal_reason"), str):
            self._reason = ev["terminal_reason"]

    @property
    def advisor_calls(self) -> int:
        return self._tools.get("advisor", 0)

    @property
    def cache_1h(self) -> int:
        return sum(a for a, _ in self._cache.values())

    @property
    def cache_5m(self) -> int:
        return sum(b for _, b in self._cache.values())

    @property
    def api_ms(self) -> int | None:
        """The session's cumulative API time, or None when no result reported any."""
        return self.api_total if self._seen_result else None

    def harness(self) -> dict[str, Any]:
        return {
            "turns": len(self._ids),
            "tools": dict(self._tools),
            "skills": dict(self._skills),
            "mcp": dict(self._mcp),
            "agents": dict(self._agents),
            "hooks": self._hooks,
            "compactions": self._compactions,
            "permission_denials": self._denials,
            "tool_errors": len(self._errors),
            "subagents": {"spawned": self._spawned, "failed": self._failed},
            "terminal_reason": self._reason,
        }

    def init_params(self) -> dict[str, Any]:
        i = self.init or {}
        servers = [
            {"name": s.get("name"), "status": s.get("status")}
            for s in i.get("mcp_servers", ()) if isinstance(s, dict)
        ]  # fmt: skip
        plugins = [p.get("name") if isinstance(p, dict) else p for p in i.get("plugins") or ()]
        return {
            "model": i.get("model"),
            "permissionMode": i.get("permissionMode") or i.get("permission_mode"),
            "claude_code_version": i.get("claude_code_version") or i.get("claudeCodeVersion"),
            "tools": list(i.get("tools") or ()), "mcp_servers": servers,
            "skills": list(i.get("skills") or ()), "plugins": plugins,
            "agents": list(i.get("agents") or ()),
        }  # fmt: skip


def read_stream(path: str | Path) -> Measure | None:
    """Replay a stream sidecar through the same accumulator the runner feeds; None if unreadable."""
    m = Measure()
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    m.feed(ev)
    except OSError:
        return None
    return m


def backfill(conn: sqlite3.Connection) -> dict[str, int]:
    """Fill the runs measured before 0.4.0 from their sidecars: `advisor` was `inherit` for all of
    them. Oldest first, so a resumed run's API time can subtract its session's earlier runs."""
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE harness IS NULL AND status = 'ended' "
        "AND stream_path IS NOT NULL ORDER BY id"
    ).fetchall()
    filled = skipped = 0
    for batch in (rows[i : i + BATCH] for i in range(0, len(rows), BATCH)):
        with store.tx(conn):
            for run in batch:
                m = read_stream(run["stream_path"])
                if m is None:
                    skipped += 1  # the sidecar is gone (retention) or unreadable
                    continue
                store.record_measure(conn, run, m, advisor="inherit")
                filled += 1
    return {"filled": filled, "skipped": skipped}
