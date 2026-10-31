# Changelog

## Unreleased

### Changed

- A worker's foreground `Bash` call may now run for 30 minutes, not 10. `BASH_DEFAULT_TIMEOUT_MS` alone stays capped at claude's own 600 s default, so the runner sets `BASH_MAX_TIMEOUT_MS` with it; measured on the jobbot board, 202 of 434 gate runs hit the old cap and the agent then spent turns polling the backgrounded run with `pgrep`/`tail`. `worker.stall_s` follows the cap (floor 1900 s, default 2400 s): a foreground call is silent by design for as long as the cap allows, and a lower watchdog would stop a healthy run.
- `kanban stats` reports `owner_reopens` and `cards_owner_reopened` beside `send_backs`. The `guards` block only ever counted the reviewer's verdicts, so a card the owner sent back himself still read as a first-pass approval; on the jobbot board that hid 10 of 46 returns in a 7-day window. `first_pass_rate` stays the reviewer's metric.

### Fixed

- `kanban request-review` adds the `Closes OWNER/REPO#N` line to the PR body itself when a card tied to a GitHub issue lacks it, instead of refusing with exit 4 and telling the builder to run `gh pr edit`. `--permission-mode auto --permission-prompts none` classifies that edit as an external system write and refuses it, so 26 of 29 `blocked/capability` runs on the jobbot board were finished work with a green gate and an open PR, thrown away over one missing line. The CLI runs outside the classifier; it keeps whatever the builder wrote and appends the line, and a failed edit is still loud.
- A worker may now run `gh pr create`, `gh pr edit` and `gh pr comment`, the writes its own protocol asks for. `--permission-mode auto` classifies a `gh` write as an external system write and `--permission-prompts none` turns that question into a refusal, so a builder asked to put the gate evidence or a `Closes` line in its own PR body ended `blocked/capability` with the work finished. `Bash(gh pr merge:*)` stays in the deny list, which outranks an allow: a worker still cannot merge its own PR.
- `card-checker` now puts the *directory* on the `touches:` line for a file the card adds, not the new filename. Two new filenames never overlap, so the dispatcher ran both cards and the second collided: 4 of 39 reviewer send-backs on the jobbot board were a second Alembic head.

- The review pre-flight no longer clears a stale `Gate:` line on a repo without CI. A gate sha that is an ancestor of the head with only merge commits since is an `ok` only when every check on the head passed, because those checks are what tested the merged tree; with no green CI the line is a `CHECK` and warden runs the gate on the head itself. `smith` and `designer` now say on the line *below* `Gate:` when no CI covers the merged head, instead of leaving the pre-merge sha to speak for it, and never name the head on the `Gate:` line itself: pre-flight reads every sha on that line, and one matching the head reads as a gate run that happened.
- The runtime tests run on CPython 3.14. The autouse fixture fakes macOS by setting `sys.platform`, and `urllib.request` runs its one-time `_scproxy` import under that fake the first time something imports it lazily (`Path.as_uri()` in `web.py` on 3.14), which fails on Linux: every test in `test_web.py` returned 500. `conftest.py` now imports `urllib.request` before any fake is in place.

## 0.4.0.dev3

Upgrade between cards: run `/kanban-claude:setup` (or the plugin's `skills/setup/scripts/bootstrap.sh`) so the daemon and the CLI move together. The schema stays at 7. Then run `kanban doctor`: check 19 now also reads protection on one `milestone/*` branch per repo, and the README's ruleset covers both `feature/*` and `milestone/*`. On Linux the daemon is a systemd user unit and needs `loginctl enable-linger $USER` once. Workers that began under 0.4.0.dev2 keep running without the new stall watchdog.

### Added

- Linux support. Setup, upgrade, `kanban doctor --fix` and `kanban uninstall` now run on Linux with a systemd user unit, `~/.config/systemd/user/kanban-claude.daemon.service` (0600, one `Environment=` line per variable the LaunchAgent carries, `Restart=on-failure`, `KillMode=process` so a restart leaves the detached runners alone, as `AbandonProcessGroup` does on macOS), driven by `systemctl --user`. Setup needs `systemctl` on Linux and says to pass `--no-daemon` when it is missing, and warns with `loginctl enable-linger <user>` when lingering is off, because the unit would stop at logout. `launchd.py` stays for macOS; `service.py` picks the backend, and doctor checks 1, 12, 13 and 17 name the LaunchAgent or the unit by platform.
- Automatic milestone branches. Two or more `smith` or `designer` cards in one repo become numbered milestones (`milestone/<n>-<slug>`, one finalize PR each) without asking the owner; exactly one code card still goes straight to the default branch. Milestone n+1 runs while milestone n awaits the owner's merge.
- `kanban wait-ci` exits 6 with `no_ci` when the repo has no workflows (checked after a 60 s grace, on the workflows API and on the PR head). It is never reported as passed.
- `kanban wait-ci` exits 7 (`skipped`) when every check on the head was skipped or neutral. A worker never reports that as passed.
- The run outcomes `network` and `stalled`, both uncounted. A `network` or `stalled` retry waits 5, 10, 20, then 30 minutes, and only the seventh in a row counts as a failure.
- The `worker.stall_s` config key (default 1200 s, 0 turns it off): a run with no assistant or tool event for that long is stopped as `stalled`. It accepts only 0 or at least 700, so the watchdog cannot kill a healthy `wait-ci` or foreground Bash call.
- A `systemic` dispatch pause now lifts itself after a passing model-call probe (5, 15 and 60 minutes, then hourly) and logs `dispatch_resumed`.

### Changed

- `references/feature-branch.md` is now `references/milestone-branches.md`. A finalize card builds `milestone/<n>-<slug>-final` from the default branch with `--base <default>` and replays only its milestone's commits, so a squash, merge or rebase of the previous milestone leaves no conflict and no force-push.
- Warden skips the full gate re-run when every check on the head sha is successful and the `Gate:` line names that sha. A missing, failing, running or absent check keeps the re-run.
- Designer and warden agree that a written description or screenshot file paths satisfy a screenshot request where the host cannot attach images; warden no longer requests changes over a missing image alone.
- `kanban request-review` refuses, for a card tied to a GitHub issue, a PR body without `Closes OWNER/REPO#N`. Smith and designer instructions list the line.
- Creating, specifying or editing a smith or designer scratch card whose Deliverable is a PR is refused, with the worktree flags to use. A negated mention ("Do not open a PR") is ignored. The scratch refusal in `request-review` now tells the worker to block, not to complete.
- `kanban wait-ci` stops waiting for checks that never register after 180 s on a repo with workflows (still exit 2). `rules.md` no longer tells workers to treat missing CI as passed.
- Workers run with `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0`, so `claude -p` waits for background tasks instead of stopping them after 10 minutes.
- A 401 or 403 pauses the account only after a fresh login probe fails too; when the probe passes, the run ends as `network`.
- Doctor checks 1, 12, 13 and 17 name the LaunchAgent or the systemd unit by platform.
- Two tests no longer depend on the environment the runner is started in.

### Fixed

- Preflight prints `ok CI green on the head` only when every check on the head passed (skipped, neutral and non-required failing checks no longer count as green), and it reads `gh pr checks` output when gh exits 1 or 8 instead of failing with "cannot read the PR".
- A worker on a scratch PR card no longer completes past the warden because the refusal said to complete.
- DNS and connection outages were counted as crashes and could trip the systemic pause.
- Runs that sat in endless `api_retry` loops for hours were never stopped.
- The stall clock restarts when a background wait or an API-retry spell begins or ends, so a quiet background wait is not stopped as stalled.
- Every worker crashed at claim (`exit -`, `0 turns`) when this process's `LC_TIME` was not English and the daemon's environment had no `LANG` (the common `LANG=en_US.UTF-8` with `LC_TIME=de_DE.UTF-8`): `start_time()` ran `ps` in the C locale but parsed its date with `strptime` in the daemon's own locale. On Linux it now reads `/proc/<pid>/stat` and the boot time from `/proc/stat`, with no subprocess and no locale; on macOS it keeps `ps` and reads the English date by hand.
- `boot_time()` returned 0 on Linux (`sysctl kern.boottime` is macOS only), so a run left `running` by a reboot was never reclaimed. It reads `btime` from `/proc/stat` there.

## 0.4.0.dev2

Upgrade between cards: run `/kanban-claude:setup` (or the plugin's `skills/setup/scripts/bootstrap.sh`) so the daemon and the CLI move together. The schema stays at 7. Then run `kanban doctor`: check 22 names each board repo that turns off the designer's browser, with the command to turn it back on.


### Changed

- Designer: checks pages in Chromium and in WebKit, and tests that every dialog opens, closes, cancels and stays open on a click inside. When the `playwright` MCP tools are missing it writes a short Playwright script under `uv run --no-project --with playwright` instead, after installing the browsers that match that version.
- `kanban doctor` check 22 warns when a board's repo turns off `playwright` in the account's `.claude.json` (`projects[<repo>].disabledMcpServers`). Claude applies that list to the card worktrees inside the repo, even to the server the designer brings with it, so the designer had no browser on such a repo.
- Warden: `kanban request-changes` on a card with no builder, a PR reviewed on its own, now blocks the card `needs_input` with a `CHANGES REQUESTED:` reason for the owner instead of being refused. Wardens had worked around the refusal with a hand-written block.

### Fixed

- `kanban web`: a followed transcript ended up to 42 px short of its last lines when the run finished on a narrow screen, because the longer "(completed)" label wrapped the logs toolbar onto a second row and nothing scrolled again. Follow now also re-pins when the log box resizes (toolbar wrap, window resize, rotated phone) and still leaves a paused view where it is.

## 0.4.0.dev1

Upgrade between cards: run `/kanban-claude:setup` (or the plugin's `skills/setup/scripts/bootstrap.sh`) so the daemon and the CLI move together. The first command of this version migrates the database to schema 7, after a backup, and a 0.3.0 daemon then refuses it (`database schema 7 is newer than this kanban`). Then run `kanban stats --backfill` once to measure the runs that finished before the upgrade.

### Added

- `kanban web [--port N] [--board SLUG] [--open]`: a read-only local dashboard on `http://127.0.0.1:8742/`. Unlike the web board 0.2.0.dev8 removed, it has no daemon thread, no secret and no write path: it opens the database read-only on every request and never migrates it. The board has one lane per status and a dock following every running card. A card opens in a drawer with Overview, Parameters (every card setting, then per run the command line, environment, what the session loaded and used), Runs, Logs (the transcript with filters, search and follow, or the raw log, plus downloads) and Comments. A Stats page shows spend per day, role and model, runtime by role and the harness numbers. It binds to `127.0.0.1` only, answers `GET` and `HEAD` only, rejects other `Host` headers and serves log and prompt files only from under the kanban home.
- Run measurement (schema 6): each run records advisor policy and calls, cache writes by TTL (1 h and 5 min), API time, a `harness` summary (turns, tools, skills, MCP servers, agents, hooks, compactions, permission denials, tool errors, subagents) and its `params` (command line, environment, what the session loaded). `kanban stats` reports them with runtime by role, cost per role and model, the warden first-pass rate and the cost per approved card. `kanban show --json` carries `harness` and `params`.
- `kanban stats --backfill` reads those numbers from the saved stream of each run that finished before schema 6; a run whose stream was not kept stays unmeasured.
- `advisor.<role>` config: `inherit` (the default) leaves the session's advisor as it is, `off` turns it off for that role, `ab` turns it off for a stable half of that role's cards (hashed from the role and the card id, so a card keeps its arm across runs) so the two halves can be compared in `kanban stats`.
- Worker verb `kanban gate [--lines N] -- CMD` runs a gate command, keeps its full output in `runs/<board>/<task>/run-N.gate-K.log`, prints only the last lines and the log path, and exits with the command's exit code (127 when it cannot start). Each run's prompt is kept beside its log as `run-N.prompt.md`.
- GitHub sync (schema 7): pair a board with one repository (`kanban boards edit SLUG --github-repo [HOST/]OWNER/REPO`, optional `--github-query` and `--github-project OWNER/NUMBER`, `--github-off` to unpair) and its cards and issues stay in step both ways, with no model in the loop. GitHub owns the text, the board owns execution: title and body merge per field as in the Jira sync, comments flow both ways, done comes from the issue being closed, and a Projects v2 board follows the card status. New verbs `kanban github import|link|unlink|sync|status`, config keys `github.poll_s`, `github.label` and `github.status_map`, doctor check 21, and `Closes OWNER/REPO#N` in a linked card's worker context. Cards are never exported to a public repository (or one whose visibility GitHub does not report); its issues are still imported and synced.

### Changed

- Worker prompts carry a short paragraph of turn rules. smith, designer, warden, `kanban-code-review` and `simplify-code-card` run long gate commands through `kanban gate`, so a passing suite costs a few lines of context instead of its whole output. designer takes one cropped screenshot per check.
- Cards name `Files to touch:` with absolute paths and the exact gate and acceptance commands; the card template, the kanban skill and card-checker ask for them.

### Fixed

- The input, output and cache token columns of a run held only the last turn's `usage`, so `kanban stats` undercounted tokens by orders of magnitude (cost was right). They now sum the run's `modelUsage`, and the schema 6 migration rewrites the old rows from the `model_usage` they kept.

## 0.3.0

The first stable release. Its code is 0.2.0.dev11's, plus the README line that describes the pane's triage rows; the 0.2.0.dev0 to 0.2.0.dev11 sections below are what it holds. Development continues as `0.4.0.devN` pre-releases, starting at `0.4.0.dev1`.

## 0.2.0.dev11

### Fixed

- The `/kanban-board` pane lists triage cards. Before, it only counted them in the footer, so a board holding only raw ideas read "Nothing running or planned." with no way to see or open them. The card view lists them as rows under the lane grid, in the rows the lanes leave, with `+N more` when the list is cut; the list view adds Triage as a fifth lane. Enter on a row opens the card. The footer legend now counts only done cards.
- The runtime's test fixtures fake macOS (`sys.platform`) along with the macOS tools, so the suite runs on a Linux CI runner. Before, setup's macOS guard failed every setup, doctor and uninstall test there.

## 0.2.0.dev10

### Added

- The `/kanban-board` pane opens on a card view: Running, Review, Blocked and Up next as columns of bordered cards (priority, the title wrapped onto two lines, then owner and age, or PR number or block kind), as many across as fit and as many down as the pane's height holds, then `+N more`. An empty lane keeps its place, drawn faint. `v` switches to the list and back, and the pane remembers the choice across sessions.

### Changed

- Each need on the pane is a card in the warning color: the reason on one line, the command on the next after `→`. A failed merge's reason reads in words (`approval not clean`).
- The pane's footer reads `ctrl+x tab: keys` until the pane has the keyboard, and shows the hotkeys after. Before, `v` and `r` looked armed while they typed into the prompt.
- The merge fixes for `approval_not_clean` and `base_is_default` end in `then run: kanban merge-retry <id>`. A card whose PR the owner merged on GitHub stayed in Needs you until a retry saw the PR merged, and the old text did not say so.

## 0.2.0.dev9

### Changed

- The runner, not the agent file, forms each dispatched role's tool list. New config keys `tools.smith`, `tools.designer`, `tools.scout`, `tools.warden` and `tools.atlassian` hold the built-in tools passed as `--tools`, so `kanban config set tools.<role> '[...]'` makes a list longer or shorter without editing an agent. The dispatched roles' agent files now say `tools: "*"`. Whatever the list says, warden and atlassian are still denied Edit, Write and NotebookEdit, and scout Edit.
- A PreToolUse hook in each run's settings lets a role start only its own helper (`card-helper` for smith, designer and scout, `review-helper` for warden) and a helper start nothing; `--tools` cannot name an `Agent(type)`, so the old per-type grant moved here. The hook blocks the spawn when it cannot read its input, because a PreToolUse hook that crashes lets the tool run.
- The atlassian role gets only its own MCP server, not `worker.mcp_allowlist`, and that server starts with `ENABLED_TOOLS` naming its nine read and eight write tools instead of the agent file listing them.

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
