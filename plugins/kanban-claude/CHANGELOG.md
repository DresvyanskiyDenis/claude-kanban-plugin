# Changelog

## 0.2.0.dev0 (unreleased)

### Added

- Plugin surface: `.claude-plugin/plugin.json` (depends on `frontend-design@claude-plugins-official`) and the repository marketplace `claude-kanban-plugin`.
- Agents `smith`, `designer`, `scout`, `warden`, `card-helper`, `review-helper` and `card-checker`, each with a quoted description and an explicit `tools` allowlist.
- Skills `kanban` (with CLI, card-template and feature-branch references), `kanban-code-review` (with `preflight.py`) and `simplify-code-card` (with `slop_gate.py`).
- Hooks: a `PostToolUse` lint check on the lines a builder changed, and a `SessionStart` entry.
- Pinned Playwright MCP configuration for the designer.
- Tests for the agents' frontmatter, the manifests, the hooks and every script.
