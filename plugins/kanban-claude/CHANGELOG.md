# Changelog

## 0.2.0.dev1 (unreleased)

### Fixed

- The daemon merges approved PRs again: `gh repo view` takes the repository positionally, and `-R` made every merge job fail.
- The warden's pre-flight reads a PR on a repository without CI as having no checks instead of aborting.
- `daemon.log` is 0600 even when launchd created it first, so a fresh install passes doctor check 17.

## 0.2.0.dev0

### Added

- Plugin surface: `.claude-plugin/plugin.json` (depends on `frontend-design@claude-plugins-official`) and the repository marketplace `claude-kanban-plugin`.
- Agents `smith`, `designer`, `scout`, `warden`, `card-helper`, `review-helper` and `card-checker`, each with a quoted description and an explicit `tools` allowlist.
- Skills `kanban` (with CLI, card-template and feature-branch references), `kanban-code-review` (with `preflight.py`) and `simplify-code-card` (with `slop_gate.py`).
- Hooks: a `PostToolUse` lint check on the lines a builder changed, and a `SessionStart` entry.
- Pinned Playwright MCP configuration for the designer.
- Tests for the agents' frontmatter, the manifests, the hooks and every script.
- Jira sync (optional): `kanban jira import|link|unlink|sync|status`, `kanban boards create|edit --jira-project KEY [--jira-jql JQL] | --jira-off`, a sync pass every `jira.poll_s` in the daemon's housekeeping thread, `jira.*` settings, the Jira key, badges and a paused-sync note on the board, `setup` filling `jira.mcp_command` from the `atlassian` MCP server, doctor check 20 (`--probe` lists the server's tools), the optional `atlassian` agent (exact tool names, dispatchable once `roles.atlassian.enabled` and `jira.mcp_command` are set), a `kanban-<id>` label on each exported issue so a timed-out create is adopted, per-board server processes, and exact `mcp__<server>__<tool>` names in grants. Schema migration 2.
