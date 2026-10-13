# Changelog

## 0.2.0.dev7

### Added

- Jira team use: `jira.claim` (off by default) makes the issue's assignee the lock. A linked card dispatches only when its issue is assigned to `jira.assignee`; the sync pass assigns an unassigned issue, reads it back and does not overwrite an assignee it has read (Jira has no compare-and-set, so two boards that assign in the same instant end with the last write). Other cards skip as `jira_unclaimed` or `claimed_elsewhere`, dispatch fails closed, and only the board that holds an issue moves its status. Doctor check 20 warns when `jira.assignee` matches none of the assignees Jira reported. The README documents the team query and the limits.
- Review verdicts reach Jira as `kanban review approved:`, `kanban review changes requested:` or `kanban review blocked:`. Verdicts given before the upgrade are marked sent by the migration, so it posts no backlog.
- Owner forms of `request-review` (send a blocked builder card with an open PR to review) and `request-changes` (send a card blocked in review back to its builder with the owner's decision).
- `create --grant RULE --approved TEXT` attaches exact grants to a new card in the same transaction.
- `snapshot REPO --ref REF` checks out a branch, tag or sha, so the card check reads the card's base instead of the default branch. Each ref has its own checkout under `snapshots/`, so two refs of one repo do not move each other's tree; nothing prunes them.
- Each run records its token counts (input, output, cache write, cache read) and a per-model breakdown; `kanban runs` prints them and `kanban stats` adds `tokens_by_model`.
- A `quota_wall` event, once per wall per account, with `until`, `kind` and the number of waiting cards.
- `list` and `show` name the parent a waiting card waits for, and mark a done card whose PR waits for the owner's merge as `awaiting merge`, until the daemon's sweep sees the PR merged. `create` and `link` warn when the parent is in triage.
- Role files list the `code-review-graph` MCP tools, so a plugin update no longer drops them.

### Changed

- `reopen` and `reopen-review` write the reason as a `REOPEN:` comment, so the builder's next run reads it.
- The web header shows only accounts with a problem, without their config directory, and the events feed no longer carries the payload of events that belong to no board. The running count says how many run on this board and how many on all boards.
- The skill, the CLI reference and the role prompts cover: re-arming the watch Monitor, the first-denial grant flow, owner feedback on review and done cards, merging into the default branch and sibling PR conflicts, waiting inside a run instead of ending the turn, and when to run setup after an update.

### Fixed

- Leaving `blocked` clears the block chip; loop detection reads its memory from the events instead.
- One runner test started the real `claude` from `PATH`; it uses the fake now, and the test environment puts a `claude` that fails loudly ahead of the real one.

## 0.2.0.dev6

### Added

- The board pane has keys: `r` reloads it and `o` opens the web board, as `kanban board` does.

### Changed

- The board pane is redesigned: a status bar across the pane, one round card per lane in its status color, priority badges in an aligned column, and a legend for done and triage. Colors follow the Claude Code theme.

## 0.2.0.dev5

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
