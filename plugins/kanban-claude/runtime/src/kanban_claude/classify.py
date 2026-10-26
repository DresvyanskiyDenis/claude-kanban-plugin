"""Stream-json facts and outcome classification (RUN-09, RUN-12). Pure: no DB, clock or process."""

import re
from typing import Any, NamedTuple

QUOTA_RE = re.compile(r"^(You've hit your .*limit|.*\b(session|usage|weekly) limit\b.*resets)")
AUTH_RE = re.compile(r"^(Invalid API key|Please run /login|.*[Nn]ot logged in)")
NETWORK_RE = re.compile(
    r"Can't reach the API server|ENOTFOUND|ECONNRESET|ECONNREFUSED|EAI_AGAIN|ETIMEDOUT"
)
PROGRESS_SYSTEM = (
    "task_progress",
    "task_notification",
)  # subagent work and a finished background task


def is_progress(ev: dict[str, Any]) -> bool:
    """Did the model or a tool just do something? api_retry, status, hook and thinking_tokens
    events say only that claude is alive: a run in an API outage emits them for hours. The
    synthetic "cut off mid-stream" user turn and the API-error assistant turn are claude's own."""
    kind = ev.get("type")
    if kind == "user":
        return not ev.get("isSynthetic")
    if kind == "assistant":
        return not ev.get("is_api_error_message")
    return kind == "tool_progress" or (kind == "system" and ev.get("subtype") in PROGRESS_SYSTEM)


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


class Stream:
    """What the runner learns from the stream: init, every result, and the rate-limit state."""

    def __init__(self) -> None:
        self.init: dict[str, Any] = {}
        self.results: list[dict[str, Any]] = []
        self.rejected = False
        self.reset: int | None = None
        self.limit: dict[str, Any] | None = None
        self.five_hour: tuple[float, int | None] | None = None

    @property
    def result(self) -> dict[str, Any] | None:
        return self.results[-1] if self.results else None

    def feed(self, ev: dict[str, Any]) -> str | None:
        """Absorb one event; returns "init", "result" or "limit" when it changed the state."""
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.init = ev
            return "init"
        if kind == "result":
            self.results.append(ev)
            return "result"
        if kind != "rate_limit_event":
            return None
        info = ev.get("rate_limit_info")
        info = info if isinstance(info, dict) else {}
        windows = info.get("unifiedWindows")
        five = windows.get("five_hour") if isinstance(windows, dict) else None
        five = five if isinstance(five, dict) else {}
        reset = number(five.get("resetsAt"))
        self.limit = info
        if info.get("status") == "rejected":
            self.rejected = True
            wall = number(info.get("resetsAt")) or reset
            self.reset = int(wall) if wall else self.reset
        used = number(five.get("utilization"))
        if used is not None:
            self.five_hour = (used, None if reset is None else int(reset))
        return "limit"


class Verdict(NamedTuple):
    outcome: str
    error: str
    retry_after: int | None = None


def first_line(result: dict[str, Any] | None) -> str:
    text = (result or {}).get("result")
    lines = text.strip().splitlines() if isinstance(text, str) else []
    return lines[0] if lines else ""


def classify(
    stream: Stream,
    *,
    exit_code: int | None,
    timed_out: bool,
    now: int,
    requeue_s: int,
    stderr: str = "",
    stalled_s: int = 0,
) -> Verdict:
    """RUN-12 rows 3..9 (rows 1 and 2 are decided by `end_run`'s compare-and-swap)."""
    result = stream.result or {}
    line = first_line(result)
    status = result.get("api_error_status")
    if stream.rejected or (result.get("is_error") and status in (429, 529)) or QUOTA_RE.match(line):
        return Verdict("quota", line or "rate limited", stream.reset or now + requeue_s)
    if status in (401, 403) or AUTH_RE.match(line):
        return Verdict("auth_lost", line or f"authentication failed ({status})")
    if result.get("subtype") == "error_max_budget_usd":
        return Verdict("budget_exceeded", "the per-run budget fuse ended the run")
    if (result.get("is_error") if result else exit_code) and NETWORK_RE.search(line or stderr):
        return Verdict("network", line or stderr.strip()[-300:])
    if timed_out:
        return Verdict("timed_out", "the runtime cap ended the run")
    if stalled_s:
        return Verdict("stalled", f"no assistant or tool event for {stalled_s // 60} min; stopped")
    if exit_code == 0:
        return Verdict("protocol_violation", "claude exited 0 without a terminal kanban call")
    detail = line or stderr.strip()[-1500:]
    return Verdict("crashed", f"claude exited {exit_code}" + (f": {detail}" if detail else ""))
