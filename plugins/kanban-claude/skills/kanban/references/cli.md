# kanban CLI reference

Usage: `kanban [--board SLUG] [--json] <verb> ...`. `--board` and `--json` are also accepted after the verb. Run `kanban <verb> --help` for the exact flags of the build you have.

## Conventions

- **Board.** `--board`, else `KANBAN_BOARD` (set for workers), else the board whose `default_workdir` is the git root of the current directory, else exit 2. There is no silent default board. In zsh keep no command in a variable (`K="kanban --board demo"` does not word-split); use a function defined in the same Bash call: `k() { kanban --board demo "$@"; }`.
- **Exit codes.** 0 ok, 1 error, 2 usage, 3 not found, 4 guard refused (state conflict or validation), 5 not allowed in this context. Messages go to stderr and name the fix.
- **`--json`.** Output is an object with `"schema": 1`, never a bare array. A list sits under its own key: `list` is `{"schema": 1, "board": ..., "cards": [...]}`, `runs` has `runs`, `grants` has `grants`, `boards list` has `boards`. `watch --json` prints one object per line. A `list` card carries `id`, `title`, `status`, `assignee`, `priority`, `branch_name`, `pr_number` and the timestamps; for anything else, `show`.
- **Multiline text.** `--body-file -` and `--summary-file -` read stdin. `comment <id> -` reads the comment from stdin.
- **Two modes.** With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs below work, and each acts on the run's own card (`KANBAN_TASK` is the default id). An owner verb in worker mode exits 5. Without it, every owner verb works; `block` and `complete` are worker-only and exit 5 for the owner. `request-review` and `request-changes` exist in both modes (see Moves).
- **Daemon warning.** `create`, `specify`, `promote` and `unblock` still succeed when the daemon is not running, and print one warning: cards will not dispatch until it runs.
- **Structured comments.** `block`, `unblock`, `request-changes`, `reopen` and `reopen-review` also write a comment prefixed `BLOCKED: `, `UNBLOCK: `, `CHANGES REQUESTED: ` or `REOPEN: `, and the owner's `request-review` one prefixed `REVIEW REQUESTED: `, so `kanban context` shows it to the next run. `hold` keeps its `--reason` on the event only.

## Worker verbs

| Command | Notes |
|---|---|
| `kanban show [ID]` | The card. `--json`: `task` (`id`, `status`, `body`, `branch_name`, `base_branch`, `pr_url`, `pr_number`, `block_kind`, `merge_state`, `implementer`), `parents`, `children`, `comments` (`body`, `author`), `runs`, `events`, `grants`, `attachments`, `latest_summary`; for the wait fields see "Links and waiting" |
| `kanban context [ID]` | Markdown for a run. Its header names the card's branch, PR base, PR number and worktree path, then body, attachments, prior attempts, parent results, comment thread, grants. Quoted worker text is framed as data |
| `kanban comment ID TEXT` | Notifies nobody; `--author` is set for workers. `comment ID -` reads the text from stdin (there is no `--body-file`). A comment starting `FOLLOW-UP:` marks work for a separate card; `followups` collects them |
| `kanban attach ID PATH` | Copies a file (25 MiB at most) next to the card |
| `kanban block ID --kind needs_input\|capability\|transient --reason TEXT [--retry-after DUR]` | Terminal. `--kind transient --retry-after 30m` retries by itself after the wait, capped by the card's failure limit; without it a transient block waits for `unblock` |
| `kanban wait-ci [ID]` | Waits for the PR's checks and returns within 540 s: exit 0 passed, 1 failed, 2 still pending (also when no checks exist yet). Run it again while it exits 2; never in the background, never under Monitor, never in a `sleep` loop |
| `kanban complete ID [--summary TEXT \| --summary-file -]` | Terminal. A repeat within the same run is a no-op |
| `kanban request-review ID --reviewer warden --summary TEXT` | Terminal. The summary must name `PR #<n>` or a PR URL; the PR must be open with the card's head and base |
| `kanban request-changes ID --reason TEXT` | Terminal, review runs only. The owner has a form of its own, see Moves |

## Owner verbs

Cards:

| Command | Notes |
|---|---|
| `kanban create TITLE` | `--assignee --workspace worktree:<root>\|dir:<abs> --branch --base --priority --max-runtime 30m\|2h --max-retries --skill --model --effort --budget --touches GLOB (repeatable) --merge-method squash\|merge\|rebase --body \| --body-file - --idempotency-key --parent ID (repeatable) --triage`, and `--grant RULE` (repeatable) with `--approved "WORDS · SOURCE · DATE"` to attach exact grants at creation. A `--parent` in `triage` prints a warning: the child would never start. `--touches` globs make the dispatcher hold back the card while a running card's touches overlap (skip reason `overlaps:<id>`). `--merge-method` (default squash) is for the daemon's merge into a feature branch; a merge into the default branch stays the owner's |
| `kanban specify ID` | Same fields plus `--title`; takes a `triage` card to `ready`, or to `todo` while a parent is open. The card starts at once, so stage its inputs first |
| `kanban edit ID` | The same fields, including `--touches` and `--merge-method`; `--allow-empty-body`. Changes text and settings, never the status |
| `kanban list` | `--status --assignee --sort created\|priority\|completed-desc --limit`; needs a resolvable board. Prints why a card waits and marks a `done` PR card `awaiting merge` (see "Links and waiting") |
| `kanban assign ID ROLE`, `kanban reassign ID ROLE [--reclaim]` | Roles: smith, designer, scout, warden, and atlassian when `roles.atlassian.enabled` is set |
| `kanban link PARENT CHILD`, `kanban unlink PARENT CHILD` | Dependencies. `link` warns when PARENT is in `triage`: the child would never start |

Moves, each with `--reason` (`request-review` takes `--summary`):

| Command | From -> to |
|---|---|
| `kanban promote ID --reason TEXT [--ignore-merge]` | `todo` -> `ready`; `--ignore-merge` starts a child although its parent's merge is pending or failed |
| `kanban hold ID --reason TEXT` | `todo`, `ready` or `blocked` -> `triage`: parked, never dispatched. `specify` takes it out and starts it (to `todo` while a parent is open) |
| `kanban unblock ID --reason TEXT` | `blocked` -> `review` when the last run was a review (warden looks again), else `ready` (`todo` while a parent is open). Clears the block kind |
| `kanban request-review ID --summary "PR #<n> ..."` | `blocked` -> `review`. Owner form, run outside a run on a `blocked` builder card whose PR is open. Warden reviews it; the normal approval and merge path then applies |
| `kanban request-changes ID --reason TEXT` | `blocked` -> `ready`. Owner form, on a card whose last run was a review: the builder gets the reason. Plain `unblock` would send it back to warden instead |
| `kanban reopen-review ID --reason TEXT` | `review` -> `ready` (`todo` while a parent is open), for the builder; the owner's own feedback. Also a `done` card whose merge failed: its merge state is cleared |
| `kanban reopen ID --reason TEXT` | `done` -> `ready`, for the builder, whether the PR is open or merged. Clears the completion and merge state. A child in `ready` falls back to `todo`; a child that is running, in `review` or `done` gets a comment |
| `kanban mark-done ID --reason TEXT` | `review` or `blocked` -> `done` |
| `kanban stop ID` | ends a running card; it becomes `blocked` |
| `kanban archive ID [--rm --yes]` | hides the card; `--rm --yes` deletes it permanently |
| `kanban merge-retry ID` | tries a `failed` merge again (the daemon retries a `pending` one itself) |

Links and waiting:

- A child stays `todo` until every parent is `done` or `archived` and has no merge state or the state `merged`.
- A scout card is `done` when its run completes; its children start at once.
- A builder card is `done` when warden approves its PR. That is approval, not merge:
  - PR into a feature branch: the daemon sets the merge state `pending`, then `merged`, with the card's `--merge-method` (squash by default). The child waits for `merged`; a `failed` merge holds it too (`merge-retry`, or `promote CHILD --ignore-merge`).
  - An approval marked `PARTIAL REVIEW` into a feature branch: the merge state stays `pending`, the board records `awaiting_owner_merge`, and `show` prints `merge: pending (owner merges)`. The daemon never merges it; the owner merges the PR on GitHub, the sweep records `merged`, and the child starts then.
  - PR into the default branch: no merge state, so the child starts at approval, before the owner merges. If the child needs the parent's code in its base, use a feature branch.
- `list` and `show` print why a card waits, e.g. `waits for t_0001 (blocked: transient)`. JSON: `waiting_for`.
- A `done` card whose PR still waits for the owner's merge shows `awaiting merge` until the daemon's sweep (every ten minutes) sees the PR merged. JSON: `awaiting_merge`.

Grants:

| Command | Notes |
|---|---|
| `kanban grant ID --allow RULE --approved "WORDS · SOURCE · DATE"` | `--allow` is repeatable. Exact rules only: `:*` and any wildcard are refused. One grant per exact command; cards that differ in one argument need one each |
| `kanban grant ID --revoke`, `kanban grants ID` | Revoke; list the active grants |

An id that exists only after a submit cannot be granted in advance. The first denial runs like this:

1. The run ends `blocked` `capability`; the reason names the exact command with the id.
2. You relay it to the owner.
3. On the owner's words: `kanban grant ID --allow 'Bash(<exact command>)' --approved "<words> · <source> · <date>"`.
4. `kanban unblock ID --reason "<what was granted>"`.

A command that waits for the run in a single call, instead of submit then poll, needs no id and skips the round trip.

Observing:

| Command | Notes |
|---|---|
| `kanban watch` | `--kinds a,b --since EVENT_ID \| --since-start --follow --actionable`; spans all boards unless `--board` is given. Starts at the end of the log by default. `--actionable` keeps only the events that need the owner (blocks, review verdicts, `merge_failed`, `awaiting_owner_merge`, `account_paused`, `quota_wall` and similar). One line per event: `[time] #<event id> <card> <kind> (@role) {payload}`, with the actor |
| `kanban digest [--since DUR]` | Read-only summary of the last `DUR` (default `2h`). Opens with "Needs you: N" and those items, then what merged, what is in review, what is running, and the cost |
| `kanban followups [--since DUR]` | Lists the `FOLLOW-UP:` comments of that window, for the owner to turn into `create --triage` cards. Creates nothing |
| `kanban runs ID` | Every run of the card with its outcome |
| `kanban log ID [--run N] [--tail N]`, `kanban tail ID` | A run's log; `tail` follows the live run |
| `kanban stats [--days N]` | Cost by role, model and day; outcomes; review rounds |
| `kanban dispatch --dry-run` | What the next tick would claim, and why each other card is skipped |
| `kanban snapshot REPO [--ref REF]` | Prints the path of a fresh detached checkout of `origin/HEAD`, or of the branch, tag or sha `--ref` names, for the card check. Each ref keeps its own checkout, so two refs of one repo do not move each other's tree |

Boards and system:

| Command | Notes |
|---|---|
| `kanban boards list` | `kanban boards create\|edit\|archive SLUG [--default-workdir PATH] [--name NAME] [--claude-config-dir DIR]`; `create` and `edit` also take `--jira-project KEY [--jira-jql JQL]` to pair the board with a Jira project, and `edit` takes `--jira-off` to unpair it |
| `kanban jira import ISSUE [--board B]`, `kanban jira link ID ISSUE`, `kanban jira unlink ID` | Bring an issue in as a triage card (also one that is Done or was unlinked); tie a card to an issue (Jira's text wins field by field, the card's goes into a conflict comment); cut the tie. A paired board syncs every `jira.poll_s` seconds; the owner's own Atlassian MCP server does the talking |
| `kanban jira sync [--board B]`, `kanban jira status` | Run one sync pass now (prints `<board>: synced` per board; exit 1 while a board is paused); list paired boards, their pause and the cards with a conflict or error |
| `kanban pause [--reason TEXT]`, `kanban resume` | Global dispatch switch; `resume` also re-probes sign-in |
| `kanban config list`, `kanban config get KEY`, `kanban config set KEY VALUE` | Settings; values are JSON. `jira.claim true` makes the Jira assignee the lock for paired boards: a linked card dispatches only when its issue is assigned to `jira.assignee` |
| `kanban daemon` | The dispatcher and housekeeping; started by the service the setup skill installs |
| `kanban migrate` | Brings the database schema up to date |
| `kanban doctor`, `kanban setup`, `kanban uninstall` | Health check, install, removal. A build that prints `not available in this build` does not have them yet; use `/kanban-claude:setup` |
