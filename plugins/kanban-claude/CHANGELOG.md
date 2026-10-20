# Changelog

## 0.2.0.dev8

### Removed

- The web board: the `board` verb, the daemon's web thread, `web.py` and its assets, `setup --port`, and the `web.port` and `web.jira_base_url` config keys. A leftover `web.port` row is ignored. The views are now the CLI (`list`, `show`, `digest`, `watch --actionable`) and the `/kanban-board` pane. A `web.secret` file left in `$KANBAN_HOME` is no longer read and can be deleted.

### Added

- `kanban watch --wait` polls like `--follow` but exits 0 once it has printed at least one line (lines `--actionable` hides do not count). The orchestrator skill now runs `kanban watch --actionable --wait --board <slug>` as a background command instead of under Monitor, which stopped it every 30 minutes and woke the session for nothing.
- `kanban watch --actionable` keeps only the events that need the owner (`blocked`, `block_loop_detected`, `gave_up`, `merge_failed`, `awaiting_owner_merge`, `account_paused`, `quota_wall`, `dispatch_paused`) and drops the owner's own. Watch lines end with the actor, for example `(worker:smith#3)`.
- `notify.command` runs once per new actionable event with the event line on stdin. It takes an argv list or a command line, runs with no shell and a 10 s timeout, and keeps a cursor, so a restart replays nothing.
- Worker verb `kanban wait-ci [--pr N]` waits up to 540 s on the run's PR checks: exit 0 passed, 1 failed, 2 pending or no checks yet, 4 no single open PR. Worker Bash calls get a 600 s default timeout so the wait is not cut off at 120 s.
- `block --kind transient --retry-after DUR` (`30m`, `2h`, `90s` or seconds) puts the card back in line after DUR. It counts as a failed run, so the failure limit still ends the retries.
- `kanban digest --since` opens with what needs you, then merged, in review, running and cost. `kanban followups --since` lists the `FOLLOW-UP:` comments. `list` and `show` print why the dispatcher passed over a waiting card, `stats` lists walled and logged-out accounts, and `kanban --help` gives every verb a one-line help.
- `--touches GLOB` on `create`, `specify` and `edit`: a card whose touches overlap a running card's in the same repo waits, with the skip reason `overlaps:<id>`.
- `--merge-method squash|merge|rebase` per card.
- Budget caps checked at claim: `budget.max_usd_per_card` (default 15, 0 turns it off) and `budget.max_usd_per_board_day` (off by default). A card over a cap is blocked with kind `capability` and a comment naming the key to raise.
- `effort.<role>` config keys. Effort resolves from the card, then the role, then the run kind's default, then `effort.default`; review runs ignore the card's effort. `config set` refuses an effort or model `claude` would reject.
- A partial-review approval into a feature base is left to the owner to merge (`awaiting_owner_merge`), and its children wait until the sweep sees it merged. A failed merge whose approved content already landed on the base settles as merged.
- Schema migration 5: `tasks.ignore_merge`, `owner_merge`, `touches` and `merge_method`.

### Changed

- The `/kanban-board` pane is reworked: a needs-you strip with the command that answers each need, a health header (daemon, pause, 24 h spend), richer rows, card detail, recently merged, toasts, and dock and inline layouts. It keeps the last good read through a failed refresh, reopens per folder and finds `~/.local/bin/kanban` when PATH lacks it. `kanban --json digest` adds event ids, block reasons, the pause and the daemon's state; `list` adds `block_kind`. After upgrading, run `/kanban-board` once in each folder where you want the pane back.
- Workers are isolated from the owner's setup. Every plugin the account enables is off in a run except kanban-claude, its declared dependency and `worker.plugin_allowlist`. Workers start with `--strict-mcp-config` and a private 0600 MCP file per run, holding the role's servers plus `worker.mcp_allowlist`. Per-role permission denies are passed with `--settings`.
- Each card's workers get their own `TMPDIR` under `$KANBAN_WORK/tmp/<card>`. A worker in a repo whose origin is not github.com gets `GH_HOST`.
- A paused login is re-probed after 5, 15, then 60 minutes, and `account_paused` carries the card, the error and the login command.
- `request-review` finds the PR from the card's branch and base, so the summary no longer has to name it; zero or several open PRs are refused, with the fix.
- `promote --ignore-merge` stays on the card until `reopen`, `hold` or `link`. A scratch card with `--branch` is refused. A worktree card with no `--base` and one same-repo worktree parent takes that parent's base.
- The run prompts live in `prompts/*.md`, with one rules file and a terminal list per run kind. `kanban context` is leaner. `preflight.py` ignores base merges and reports a merge-only re-review.
- The daemon writes its pid and version into `daemon.lock`; `doctor` and `setup` read them from there. An upgrade now waits up to 30 s for the restarted daemon to report the new version.
- The agent files no longer set `effort:`; `effort.<role>` replaces it.
- The runner no longer writes the unread `last_output_at` column.
- Jira sync is frozen: bug fixes only.

### Fixed

- A card the budget cap blocked at a review claim unblocks back to review, not to its builder.
- A PR the owner closed without merging ends the owner-merge wait as `merge_failed`. Before, the card waited forever and held its children.
- The sweep removes the `TMPDIR` of every archived or deleted card.

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
