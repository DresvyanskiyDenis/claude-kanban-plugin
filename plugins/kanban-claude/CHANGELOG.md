# Changelog

## 0.2.0.dev5 (unreleased)

### Added

- An optional `theme.css` in `$KANBAN_HOME` restyles the web board. It loads after the board's own stylesheet, is read on every page load, and only sets the custom properties the README lists.
- `/` focuses the filter. An empty lane says what lands there, and an empty lane under a filter says that nothing matches.

### Changed

- The web board is redesigned on a token layer: neutral greys with one accent, a status dot per lane and per move button, a card count pill, priority and role pills, a slimmer runtime bar with a pulsing dot while a run is live, a searchable header with a board picker, an Archived switch and ok, warn or bad dots on the dispatch and account stats. Light and dark follow the system; animations stop under reduced motion.

### Fixed

- Toasts show above an open drawer or dialog and can be dismissed there. Before, a modal dialog covered them, so an action that failed from the drawer gave no visible message.

## 0.2.0.dev4

### Fixed

- Setup's and doctor's agent probe decides from claude's start-up event, which lists the agents it loaded, instead of the scout's first turn. A scout that spent its one turn on a tool (`error_max_turns`) no longer reads as "does not resolve", and an unknown agent now reports claude's own reason instead of an empty one or a JSON parse error.
- Setup prints which claude it uses, so a `--claude-bin` recorded by an earlier run is visible.

## 0.2.0.dev3

### Added

- The `kanban` skill steers a running card: the owner's words go to the worker's session, `kanban <id> <role>`, by SendMessage and onto the card as a comment. Workers still ask back only by blocking the card.

## 0.2.0.dev2

### Added

- A board pane mod (`hooks/board.tsx`, Claude Code 2.1.287 or later): `/kanban-board` docks a short board beside the transcript (running, review, blocked, up next, triage and done counts), read from `kanban --json list` every 15 s while it is open. It reopens in the next session until closed, and never loads in a card's worker.

## 0.2.0.dev1

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
