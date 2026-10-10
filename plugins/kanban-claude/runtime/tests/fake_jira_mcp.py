"""Stands in for the owner's Atlassian MCP server in the tests (spec 23): the seven JIR-02 tools over
issues kept in a JSON file, so state survives the one-process-per-pass the sync uses. Results have the
shapes of mcp-atlassian 0.23.1. A workflow with named transitions (one can need a field), comments, a
`updated >= -Nm` window on the fake clock, and a deliberately lossy Markdown round trip.

Run as `python fake_jira_mcp.py` with FAKE_JIRA_STATE naming the file; tests import `Fake` to read
and change that file between passes. Every tool call is logged in the file under `calls`.

`mode` flags: `auth`, `start`, `init` (the server fails), `hang` (never answers), `hang_after` (a tool
name: does the work, then never answers), `no_total` (searches report total -1), `tools` (the list),
`fail` (`{"tool": "message"}` or `{"tool:needle": ..}`: that call is an error).
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

WORKFLOW = {
    "To Do": [("11", "Start progress", "In Progress"), ("12", "Done", "Done")],
    "In Progress": [("21", "Stop progress", "To Do"), ("22", "Done", "Done")],
    "Done": [("31", "Reopen", "To Do")],
}
CATEGORY = {"To Do": "To Do", "In Progress": "In Progress", "Review": "In Progress", "Done": "Done"}
WRITES = ("jira_update_issue", "jira_transition_issue", "jira_add_comment", "jira_create_issue")


def lossy(text: str) -> str:
    """What the server stores for a description: CRLF line ends and `*` bullets, like a wiki round trip."""
    return re.sub(r"(^|\n)- ", r"\1* ", text.replace("\r\n", "\n")).replace("\n", "\r\n")


class Fake:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.refresh()

    def refresh(self) -> "Fake":
        """Re-read the file: the server process changed it since."""
        self.s: dict[str, Any] = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"issues": {}, "calls": [], "clock": 0, "next": 100, "mode": {}, "needs_field": []}
        )
        return self

    def save(self) -> None:
        self.path.write_text(json.dumps(self.s))

    def tick(self) -> dict[str, Any]:
        """The `updated` fields of a change made now: the stamp and its second on the fake clock."""
        self.s["clock"] += 1
        c = self.s["clock"]
        return {"updated": f"2026-10-10T{c // 3600:02d}:{c // 60 % 60:02d}:{c % 60:02d}.000+0000",
                "updated_s": c}  # fmt: skip

    def advance(self, seconds: int = 60) -> None:
        """Time passes for the server, as `clock` makes it pass for the board."""
        self.refresh().s["clock"] += seconds
        self.save()

    def add(
        self, key: str, summary: str, description: str = "", status: str = "To Do", **kw: Any
    ) -> dict[str, Any]:
        self.refresh()
        issue = {
            "id": str(len(self.s["issues"]) + 1000), "key": key, "summary": summary,
            "description": lossy(description), "status": {"name": status, "category": CATEGORY[status]},
            **self.tick(), "comments": [], "in_jql": True, "labels": [], **kw,
        }  # fmt: skip
        self.s["issues"][key] = issue
        self.save()
        return issue

    def edit(self, key: str, **fields: Any) -> None:
        issue = self.refresh().s["issues"][key]
        if "description" in fields:
            fields["description"] = lossy(fields["description"])
        if "status" in fields:
            fields["status"] = {"name": fields["status"], "category": CATEGORY[fields["status"]]}
        issue.update(fields, **self.tick())
        self.save()

    def comment(self, key: str, body: str, who: str = "Pat Example") -> str:
        self.refresh()
        return str(self.add_comment(key, body, who)["id"])

    def add_comment(self, key: str, body: str, who: str) -> dict[str, Any]:
        issue = self.s["issues"][key]
        cid = str(self.s["next"])
        self.s["next"] += 1
        stamp = self.tick()
        comment = {"id": cid, "body": body, "author": {"display_name": who, "name": who.lower()},
                   "created": stamp["updated"], "updated": stamp["updated"]}  # fmt: skip
        issue["comments"].append(comment)
        issue.update(stamp)
        self.save()
        return comment

    def delete(self, key: str) -> None:
        del self.refresh().s["issues"][key]
        self.save()

    def issue(self, key: str) -> dict[str, Any]:
        issue: dict[str, Any] = self.refresh().s["issues"][key]
        return issue

    def put(self, **top: Any) -> None:
        self.refresh().s.update(top)
        self.save()

    def mode(self, **flags: Any) -> None:
        self.refresh().s["mode"].update(flags)
        self.save()

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.refresh().s["calls"] if tool in (None, c["tool"])]

    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["tool"] in WRITES]


class Failure(Exception):
    pass


def serve(fake: Fake, tool: str, a: dict[str, Any]) -> Any:
    issues, mode = fake.s["issues"], fake.s["mode"]
    if mode.get("auth"):
        raise Failure("401 Unauthorized: the API token is invalid")
    for spec, message in mode.get("fail", {}).items():
        name, _, needle = spec.partition(":")
        if name == tool and needle in json.dumps(a):
            raise Failure(message)

    def get(key: str) -> dict[str, Any]:
        if key not in issues:
            raise Failure(f"Issue {key} not found")
        return issues[key]  # type: ignore[no-any-return]

    if tool == "jira_search":
        jql = a["jql"]
        wanted = re.findall(r"[A-Z][A-Z0-9_]*-\d+", jql) if "key in (" in jql else None
        label = re.fullmatch(r'labels = "(.*)"', jql)
        window = re.search(r"updated >= -(\d+)m$", jql)
        since = fake.s["clock"] - 60 * int(window[1]) if window else None
        hits = [
            i
            for k, i in sorted(issues.items())
            if (
                label[1] in i["labels"]
                if label
                else k in wanted
                if wanted is not None
                else i["in_jql"]
            )
            and (since is None or i["updated_s"] >= since)
        ]
        start, size = a.get("start_at", 0), a.get("limit", 50)
        return {"total": -1 if mode.get("no_total") else len(hits), "start_at": start,
                "max_results": size,
                "issues": [{"key": i["key"], "summary": i["summary"]} for i in hits[start : start + size]]}  # fmt: skip
    if tool == "jira_get_issue":
        return get(a["issue_key"])
    if tool == "jira_create_issue":
        n = fake.s["next"]
        fake.s["next"] += 1
        extra = json.loads(a.get("additional_fields") or "{}")
        key = f"{a['project_key']}-{n}"
        issues[key] = {
            "id": str(n), "key": key, "summary": a["summary"], "description": lossy(a.get("description", "")),
            "status": {"name": "To Do", "category": "To Do"}, **fake.tick(), "comments": [],
            "in_jql": True, "labels": extra.get("labels", []), "assignee": a.get("assignee"),
        }  # fmt: skip
        return {"message": "Issue created successfully",
                "issue": {"key": key, "id": str(n), "summary": a["summary"]}}  # fmt: skip
    if tool == "jira_update_issue":
        issue = get(a["issue_key"])
        fields = json.loads(a["fields"])
        if "description" in fields:
            fields["description"] = lossy(fields["description"])
        issue.update(fields, **fake.tick())
        return {"message": "Issue updated successfully", "issue": {"key": issue["key"]}}
    if tool == "jira_get_transitions":
        status = get(a["issue_key"])["status"]["name"]
        return [{"id": i, "name": name, "to_status": to}
                for i, name, to in fake.s.get("workflow", WORKFLOW)[status]]  # fmt: skip
    if tool == "jira_transition_issue":
        issue = get(a["issue_key"])
        for i, _, to in fake.s.get("workflow", WORKFLOW)[issue["status"]["name"]]:
            if i != a["transition_id"]:
                continue
            if to in fake.s["needs_field"]:
                raise Failure(f"Transition {i} needs a value for the field resolution")
            issue.update(status={"name": to, "category": CATEGORY[to]}, **fake.tick())
            return {"message": f"Issue {issue['key']} transitioned successfully"}
        raise Failure(f"Transition {a['transition_id']} is not available for {a['issue_key']}")
    if tool == "jira_add_comment":
        return fake.add_comment(get(a["issue_key"])["key"], a["body"], "Kanban Owner")
    raise Failure(f"unknown tool {tool}")


def main() -> None:
    path = os.environ["FAKE_JIRA_STATE"]
    if Fake(path).s["mode"].get("start"):
        sys.exit(3)
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        fake = Fake(path)
        if msg["method"] == "initialize":
            if fake.s["mode"].get("init"):
                sys.exit(3)
            res: dict[str, Any] = {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-jira", "version": "1"},
            }  # fmt: skip
        elif msg["method"] == "tools/list":
            names = [t for t in fake.s["mode"].get("tools", FAKE_TOOLS)]
            res = {"tools": [{"name": n} for n in names]}
        else:
            tool, args = msg["params"]["name"], msg["params"]["arguments"]
            fake.s["calls"].append({"tool": tool, "args": args})
            if fake.s["mode"].get("hang"):
                time.sleep(3600)
            try:
                text, err = json.dumps(serve(fake, tool, args)), False
            except Failure as e:
                text, err = str(e), True
            fake.save()
            if fake.s["mode"].get("hang_after") == tool:
                time.sleep(3600)
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
        sys.stdout.flush()


FAKE_TOOLS = (
    "jira_search", "jira_get_issue", "jira_create_issue", "jira_update_issue",
    "jira_get_transitions", "jira_transition_issue", "jira_add_comment", "jira_get_all_projects",
)  # fmt: skip

if __name__ == "__main__":
    main()
