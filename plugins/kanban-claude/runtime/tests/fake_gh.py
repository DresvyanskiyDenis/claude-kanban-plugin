"""A stateful stand-in for `gh api` (REST and GraphQL), for the GitHub sync tests.

The state is one JSON file (`GH_FAKE_STATE`), so the test process edits what the `gh` process
serves. Run as a script it answers one `gh` call; `Fake` is the test's handle on the same file.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

START = 1_800_000_000  # the board clock the `clock` fixture freezes
OPTIONS = ["Todo", "In Progress", "Done"]


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Fake:
    """The server as a test sees it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            self.put(
                {
                    "now": START, "seq": 0, "issues": {}, "comments": {}, "items": {}, "calls": [],
                    "scopes": "'repo', 'project'", "auth": "ok", "hidden": False, "fail": [],
                    "lose": [], "options": OPTIONS, "project": True,
                }
            )  # fmt: skip

    def get(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.path.read_text())
        return data

    def put(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state))

    def update(self, **kw: Any) -> None:
        s = self.get()
        s.update(kw)
        self.put(s)

    def advance(self, seconds: int = 60) -> None:
        s = self.get()
        s["now"] += seconds
        self.put(s)

    def add(self, title: str, body: str = "", *, labels: tuple[str, ...] = ("kanban-claude",),
            state: str = "open", pr: bool = False, tags: tuple[str, ...] = ()) -> int:  # fmt: skip
        s = self.get()
        s["seq"] += 1
        n = s["seq"]
        issue = new_issue(s, n, title, body, list(labels), state)
        issue["tags"] = list(tags)
        if pr:
            issue["pull_request"] = {"url": "x"}
        s["issues"][str(n)] = issue
        self.put(s)
        return n

    def edit(self, n: int, **fields: Any) -> None:
        """A change made in GitHub: it bumps `updated_at`."""
        s = self.get()
        s["issues"][str(n)].update(fields)
        s["issues"][str(n)]["updated_at"] = iso(s["now"])
        self.put(s)

    def after(self, line: str, n: int, **fields: Any) -> None:
        """Once, right after the call `METHOD endpoint` is answered, issue `n` is edited in GitHub: the
        answer a pass already holds goes stale."""
        s = self.get()
        s.setdefault("after", {})[line] = {str(n): fields}
        self.put(s)

    def comment(self, n: int, body: str, login: str = "alice") -> str:
        s = self.get()
        s["cseq"] = s.get("cseq", 0) + 1
        cid = f"IC_{s['cseq']}"
        s["comments"].setdefault(str(n), []).append(
            {"node_id": cid, "body": body, "user": {"login": login}}
        )
        issue = s["issues"][str(n)]
        issue["comments"] = len(s["comments"][str(n)])
        issue["updated_at"] = iso(s["now"])
        self.put(s)
        return cid

    def delete(self, n: int) -> None:
        s = self.get()
        del s["issues"][str(n)]
        self.put(s)

    def move(self, n: int, status: str) -> None:
        """A Status move in the project board: it does not bump `updated_at`."""
        s = self.get()
        s["items"][f"PVTI_{n}"] = {"content": s["issues"][str(n)]["node_id"], "status": status}
        self.put(s)

    def issue(self, n: int) -> dict[str, Any]:
        out: dict[str, Any] = self.get()["issues"][str(n)]
        return out

    def comments(self, n: int) -> list[str]:
        return [c["body"] for c in self.get()["comments"].get(str(n), [])]

    def status_of(self, n: int) -> str | None:
        node = self.issue(n)["node_id"]
        return next(
            (i["status"] for i in self.get()["items"].values() if i["content"] == node), None
        )

    def calls(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = self.get()["calls"]
        return out

    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls() if c["write"]]


def new_issue(
    s: dict[str, Any], n: int, title: str, body: str, labels: list[str], state: str
) -> dict[str, Any]:
    return {
        "number": n, "node_id": f"I_{n}", "title": title, "body": body, "state": state,
        "state_reason": None, "updated_at": iso(s["now"]), "comments": 0,
        "labels": [{"name": x} for x in labels], "html_url": f"https://github.com/o/r/issues/{n}",
        "user": {"login": "alice"},
    }  # fmt: skip


# --- the gh process -------------------------------------------------------------------------


def die(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.exit(1)


def page(items: list[Any], params: dict[str, str]) -> list[Any]:
    per, n = int(params.get("per_page", 30)), int(params.get("page", 1))
    return items[(n - 1) * per : n * per]


def search(s: dict[str, Any], q: str) -> list[dict[str, Any]]:
    import shlex

    out = list(s["issues"].values())
    for tok in shlex.split(q):
        key, _, val = tok.partition(":")
        if key == "label":
            out = [i for i in out if val in [x["name"] for x in i["labels"]]]
        elif key == "updated":
            out = [i for i in out if i["updated_at"] >= val.removeprefix(">=")]
        elif key == "tag":
            out = [i for i in out if val in i.get("tags", [])]
    return out


def issues_get(s: dict[str, Any], params: dict[str, str]) -> list[dict[str, Any]]:
    out = list(s["issues"].values())
    if params.get("state", "open") != "all":
        out = [i for i in out if i["state"] == params.get("state", "open")]
    if "labels" in params:
        out = [i for i in out if params["labels"] in [x["name"] for x in i["labels"]]]
    if "since" in params:
        out = [i for i in out if i["updated_at"] >= params["since"]]
    return out


def graphql(s: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    q, v = body["query"], body["variables"]
    if s["auth"] == "scope":
        die(
            "gh: Your token has not been granted the required scopes to execute this query. INSUFFICIENT_SCOPES"
        )
    if "repositoryOwner" in q:
        if not s["project"]:
            return {"data": {"repositoryOwner": {"projectV2": None}}}
        field = {
            "id": "PVTSSF_status",
            "options": [{"id": f"opt_{o}", "name": o} for o in s["options"]],
        }
        return {"data": {"repositoryOwner": {"projectV2": {"id": "PVT_1", "field": field}}}}
    if "addProjectV2ItemById" in q:
        item = next((k for k, i in s["items"].items() if i["content"] == v["c"]), None)
        if not item:
            item = f"PVTI_{v['c'].removeprefix('I_')}"
            s["items"][item] = {"content": v["c"], "status": None}
        return {"data": {"addProjectV2ItemById": {"item": {"id": item}}}}
    if "updateProjectV2ItemFieldValue" in q:
        s["items"][v["i"]]["status"] = v["o"].removeprefix("opt_")
        return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["i"]}}}}
    die("unknown graphql")
    return {}


def api(s: dict[str, Any], args: list[str]) -> Any:
    method, params, endpoint, stdin = "GET", {}, "", False
    it = iter(args)
    for a in it:
        if a == "--hostname":
            next(it)
        elif a == "-X":
            method = next(it)
        elif a == "-f":
            k, _, v = next(it).partition("=")
            params[k] = v
        elif a == "--input":
            next(it)
            stdin = True
        else:
            endpoint = a
    body = json.load(sys.stdin) if stdin else None
    write = method != "GET" and not (endpoint == "graphql" and "mutation" not in body["query"])
    s["calls"].append(
        {"method": method, "endpoint": endpoint, "params": params, "body": body, "write": write}
    )
    line = f"{method} {endpoint}"
    if s["auth"] == "bad":
        die("gh: Bad credentials (HTTP 401)")
    if any(n in line for n in s["fail"]):
        die("gh: Validation Failed (HTTP 422)")
    if endpoint == "graphql":
        return graphql(s, body)
    if endpoint == "search/issues":
        return {"items": page(search(s, params["q"]), params)}
    parts = endpoint.split("/")  # repos O R [issues N [comments]]
    if s["hidden"] and parts[0] == "repos":
        die("gh: Not Found (HTTP 404)")
    if len(parts) == 3:
        visibility = s.get("visibility", "private")
        return {
            "full_name": "/".join(parts[1:]), "visibility": visibility,
            "private": visibility != "public",
        }  # fmt: skip
    if len(parts) == 4 and method == "GET":
        return page(issues_get(s, params), params)
    if len(parts) == 4:
        s["seq"] += 1
        n = s["seq"]
        s["issues"][str(n)] = new_issue(
            s, n, body["title"], body.get("body") or "", body.get("labels", []), "open"
        )
        if line in s["lose"]:
            s["lose"].remove(line)
            die("error connecting to api.github.com")
        return s["issues"][str(n)]
    n = parts[4]
    if n not in s["issues"]:
        die("gh: Not Found (HTTP 404)")
    issue = s["issues"][n]
    if len(parts) == 5:
        if method == "PATCH":
            issue.update(
                {k: v for k, v in body.items() if k in ("title", "body", "state", "state_reason")}
            )
            issue["updated_at"] = iso(s["now"])
        return issue
    if method == "POST":
        s["cseq"] = s.get("cseq", 0) + 1
        c = {"node_id": f"IC_{s['cseq']}", "body": body["body"], "user": {"login": "kanban-bot"}}
        s["comments"].setdefault(n, []).append(c)
        issue["comments"] = len(s["comments"][n])
        issue["updated_at"] = iso(s["now"])
        if line in s["lose"]:
            s["lose"].remove(line)
            die("error connecting to api.github.com")
        return c
    return page(s["comments"].get(n, []), params)


def save(s: dict[str, Any]) -> None:
    Path(os.environ["GH_FAKE_STATE"]).write_text(json.dumps(s))


def main() -> None:
    args = sys.argv[1:]
    s = Fake(Path(os.environ["GH_FAKE_STATE"])).get()
    if args[:2] == ["auth", "status"]:
        if s["auth"] == "out":
            die("You are not logged into any GitHub hosts.")
        host = args[args.index("--hostname") + 1]
        print(f"{host}\n  Logged in to {host} account t (keyring)\n  - Token scopes: {s['scopes']}")
        return
    if args[0] != "api":
        sys.exit(2)
    try:
        out = api(s, args[1:])
        text = json.dumps(out)
        last = s["calls"][-1]
        for n, fields in s.get("after", {}).pop(f"{last['method']} {last['endpoint']}", {}).items():
            s["issues"][n].update(fields, updated_at=iso(s["now"]))
    finally:
        save(s)
    if out is not None:
        print(text)


if __name__ == "__main__":
    main()
