"""TST-A: the shipped surface (manifests, agents, skills, hooks, MCP config) keeps its contracts.

Frontmatter is parsed by hand: every value sits on one line, so no YAML library is needed.
A3 covers the files the surface owns; the runtime names Hermes on purpose.
"""

import argparse
import importlib.util
import json
import re
from pathlib import Path

import pytest

import kanban_claude
from kanban_claude import cli

PLUGIN = Path(__file__).resolve().parents[2]
REPO = PLUGIN.parents[1]
AGENTS = {p.stem: p for p in sorted((PLUGIN / "agents").glob("*.md"))}
SKILLS = ("kanban", "kanban-code-review", "simplify-code-card")
NS = "kanban-claude"

BUILTIN = {
    "Agent", "Bash", "Read", "Edit", "Write", "Skill", "ToolSearch", "LSP", "WebFetch", "WebSearch",
    "Grep", "Glob", "ListMcpResourcesTool", "ReadMcpResourceTool",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate",
}  # fmt: skip
MCP_SERVERS = {"context7", "lightpanda", "playwright"}
MCP_NAME = re.compile(r"mcp__([a-z0-9-]+)(?:__[a-z0-9_]+)?")
IGNORED_FOR_PLUGIN_AGENTS = {"permissionMode", "hooks", "mcpServers"}
MODEL_EFFORT = {
    "smith": ("sonnet", "xhigh"),
    "designer": ("sonnet", "xhigh"),
    "scout": ("sonnet", "high"),
    "warden": ("opus", "high"),
    "card-helper": ("sonnet", "high"),
    "review-helper": ("sonnet", "high"),
    "card-checker": ("sonnet", "medium"),
}
SPAWNS = {
    "smith": {"card-helper"},
    "designer": {"card-helper"},
    "scout": {"card-helper"},
    "warden": {"review-helper"},
}
WRITERS = {"Edit", "Write"}


def frontmatter(path: Path) -> dict[str, str]:
    m = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert m, f"{path}: no frontmatter"
    out: dict[str, str] = {}
    for line in m[1].splitlines():
        key, sep, value = line.partition(":")
        assert sep and re.fullmatch(r"[A-Za-z]+", key), f"{path}: not a one-line key: {line!r}"
        out[key] = value.strip()
    return out


def tools(role: str) -> list[str]:
    return [t.strip() for t in frontmatter(AGENTS[role])["tools"].split(",")]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_the_seven_agents_exist():
    assert set(AGENTS) == set(MODEL_EFFORT)


@pytest.mark.parametrize("role", MODEL_EFFORT)
def test_agent_frontmatter(role: str):
    fm = frontmatter(AGENTS[role])
    assert fm["name"] == role
    assert re.fullmatch(r'"(?:[^"\\]|\\.)+"', fm["description"]), "description must be quoted"
    assert (fm["model"], fm["effort"]) == MODEL_EFFORT[role]
    assert not IGNORED_FOR_PLUGIN_AGENTS & fm.keys()
    assert "tools" in fm and fm["tools"], "tools must be an explicit allowlist"


@pytest.mark.parametrize("role", MODEL_EFFORT)
def test_tools_are_known_builtins_namespaced_agents_or_pinned_mcp_names(role: str):
    for tool in tools(role):
        if m := re.fullmatch(r"Agent\((.+)\)", tool):
            assert m[1].startswith(f"{NS}:") and m[1].removeprefix(f"{NS}:") in AGENTS, tool
        elif tool.startswith("mcp__"):
            m = MCP_NAME.fullmatch(tool)
            assert m and m[1] in MCP_SERVERS, tool
        else:
            assert tool in BUILTIN, tool


@pytest.mark.parametrize("role", MODEL_EFFORT)
def test_no_agent_is_denied_through_disallowed_tools(role: str):
    assert "Agent(" not in frontmatter(AGENTS[role]).get("disallowedTools", "")


@pytest.mark.parametrize("role", MODEL_EFFORT)
def test_each_role_spawns_only_its_own_helper(role: str):
    spawned = {
        t[len("Agent(") : -1].removeprefix(f"{NS}:") for t in tools(role) if t.startswith("Agent(")
    }
    assert spawned == SPAWNS.get(role, set())
    assert "Agent" not in tools(role)


def test_card_checker_has_exactly_the_three_read_tools():
    assert tools("card-checker") == ["Read", "Grep", "Glob"]


def test_read_only_roles_cannot_write_files():
    for role in ("warden", "review-helper", "card-checker"):
        assert not WRITERS & set(tools(role)), role


def test_scout_writes_reports_but_cannot_edit():
    assert "Write" in tools("scout") and "Edit" not in tools("scout")


def test_only_the_designer_gets_the_browser():
    with_browser = {r for r in AGENTS if "mcp__playwright" in tools(r)}
    assert with_browser == {"designer"}


def test_no_worker_role_has_atlassian():
    for role in AGENTS:
        assert not [t for t in tools(role) if "atlassian" in t], role


def test_lint_hook_builders_are_real_agents():
    spec = importlib.util.spec_from_file_location(
        "lint_on_edit", PLUGIN / "hooks/scripts/lint_on_edit.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in module.BUILDERS:
        assert name.startswith(f"{NS}:") and name.removeprefix(f"{NS}:") in AGENTS, name


def test_plugin_manifest_matches_the_package():
    manifest = load_json(PLUGIN / ".claude-plugin/plugin.json")
    assert manifest["name"] == NS
    assert manifest["version"] == kanban_claude.__version__
    assert manifest["dependencies"] == ["frontend-design@claude-plugins-official"]


def test_marketplace_lists_the_one_plugin_and_allows_the_official_dependency():
    market = load_json(REPO / ".claude-plugin/marketplace.json")
    assert market["name"] == "claude-kanban-plugin"
    assert market["allowCrossMarketplaceDependenciesOn"] == ["claude-plugins-official"]
    (entry,) = market["plugins"]
    assert entry["name"] == NS and entry["source"] == "./plugins/kanban-claude"
    assert (REPO / entry["source"] / ".claude-plugin/plugin.json").is_file()
    assert "version" not in entry, "the version lives in plugin.json only"


def test_hooks_json_wires_the_lint_hook_and_the_session_start_script():
    hooks = load_json(PLUGIN / "hooks/hooks.json")["hooks"]
    (post,) = hooks["PostToolUse"]
    assert post["matcher"] == "Edit|Write"
    lint = post["hooks"][0]
    assert lint["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/lint_on_edit.py"]
    (start,) = hooks["SessionStart"]
    assert start["hooks"][0]["args"] == ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/session_start.sh"]


def test_every_mcp_config_pins_exact_versions():
    configs = sorted((PLUGIN / "runtime/src/kanban_claude/mcp").glob("*.json"))
    assert [c.stem for c in configs] == ["designer"]
    for config in configs:
        for name, server in load_json(config)["mcpServers"].items():
            pins = [a for a in server["args"] if re.fullmatch(r"@?[\w./-]+@\d+\.\d+\.\d+", a)]
            assert pins, f"{name} has no exact version pin"
            assert "latest" not in json.dumps(server)


@pytest.mark.parametrize("skill", SKILLS)
def test_skill_frontmatter_and_linked_files(skill: str):
    path = PLUGIN / "skills" / skill / "SKILL.md"
    fm = frontmatter(path)
    assert fm["name"] == skill
    assert re.fullmatch(r'"(?:[^"\\]|\\.)+"', fm["description"])
    for rel in re.findall(r"\b((?:references|scripts)/[\w./-]*\w)", path.read_text()):
        assert (path.parent / rel).is_file(), f"{skill} links {rel}, which does not exist"


def kanban_verbs() -> set[str]:
    sub = next(a for a in cli.build()._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def test_cli_reference_documents_exactly_the_real_verbs():
    text = (PLUGIN / "skills/kanban/references/cli.md").read_text()
    mentioned = set(re.findall(r"`kanban (?:--board \S+ )?([a-z][a-z-]*)", text))
    assert mentioned <= kanban_verbs(), mentioned - kanban_verbs()
    assert kanban_verbs() - {"run"} <= mentioned, kanban_verbs() - {"run"} - mentioned


def test_no_hermes_in_the_shipped_surface():
    roots = [
        PLUGIN / "agents",
        PLUGIN / "hooks",
        PLUGIN / ".claude-plugin",
        REPO / ".claude-plugin",
    ]
    roots += [PLUGIN / "skills" / s for s in SKILLS]
    files = [f for r in roots for f in r.rglob("*") if f.is_file() and f.suffix != ".pyc"]
    assert files
    offenders = [str(f.relative_to(REPO)) for f in files if "hermes" in f.read_text().lower()]
    assert offenders == []
