# kanban CLI reference

Usage: `kanban [--board SLUG] [--json] <verb> ...`. `--board` and `--json` are also accepted after the verb. `kanban --help` lists every verb with one line each; run `kanban <verb> --help` for the exact flags of the build you have.

## Conventions

- **Board.** `--board`, else `KANBAN_BOARD` (set for workers), else the board whose `default_workdir` is the git root of the current directory, else exit 2. There is no silent default board. In zsh keep no command in a variable (`K="kanban --board demo"` does not word-split); use a function defined in the same Bash call: `k() { kanban --board demo "$@"; }`.
- **Exit codes.** 0 ok, 1 error, 2 usage, 3 not found, 4 guard refused (state conflict or validation), 5 not allowed in this context. Messages go to stderr and name the fix.
- **`--json`.** Output is an object with `"schema": 1`, never a bare array. A list sits under its own key: `list` is `{"schema": 1, "board": ..., "cards": [...]}`, `runs` has `runs`, `grants` has `grants`, `boards list` has `boards`. `watch --json` prints one object per line. A `list` card carries `id`, `title`, `status`, `assignee`, `priority`, `branch_name`, `pr_number` and the timestamps; for anything else, `show`.
- **Multiline text.** `--body-file -` and `--summary-file -` read stdin. `comment <id> -` reads the comment from stdin.
- **Two modes.** With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs below work, and each acts on the run's own card (`KANBAN_TASK` is the default id). An owner verb in worker mode exits 5. Without it, every owner verb works; `block`, `complete`, `wait-ci` and `gate` are worker-only and exit 5 for the owner. `request-review` and `request-changes` exist in both modes (see Moves).
- **Daemon warning.** `create`, `specify`, `promote` and `unblock` still succeed when the daemon is not running, and print one warning: cards will not dispatch until it runs.
- **Structured comments.** `block`, `unblock`, `request-changes`, `reopen` and `reopen-review` also write a comment prefixed `BLOCKED: `, `UNBLOCK: `, `CHANGES REQUESTED: ` or `REOPEN: `, and the owner's `request-review` one prefixed `REVIEW REQUESTED: `, so `kanban context` shows it to the next run. `hold` keeps its `--reason` on the event only.

## Worker verbs

| Command | Notes |
|---|---|
| `kanban show [ID]` | The card. `--json`: `task` (`id`, `status`, `body`, `branch_name`, `base_branch`, `pr_url`, `pr_number`, `block_kind`, `merge_state`, `implementer`), `parents`, `children`, `comments` (`body`, `author`), `runs`, `events`, `grants`, `attachments`, `latest_summary`, and `skip` (`reason`, `at`: why the dispatcher last passed over a `ready` or `review` card, null once a run claims it); for the wait fields see "Links and waiting". The text form prints it as `not started: <reason> (since …)`, and `touches: a, b` for a card with touches |
| `kanban context [ID]` | Markdown for a run. Its header names the card's branch, PR base, PR number and worktree path, then body, attachments, prior attempts, parent results, comment thread, grants. Quoted worker text is framed as data |
| `kanban comment ID TEXT` | Notifies nobody; `--author` is set for workers. `comment ID -` reads the text from stdin (there is no `--body-file`). A comment starting `FOLLOW-UP:` marks work for a separate card; `followups` collects them |
| `kanban attach ID PATH` | Copies a file (25 MiB at most) next to the card |
| `kanban block ID --kind needs_input\|capability\|transient --reason TEXT [--retry-after DUR]` | Terminal. `--retry-after` goes with `--kind transient` only (exit 4 otherwise); DUR is `30m`, `2h`, `90s` or plain seconds. The card goes back to `ready` (to `review` for a review run) and the dispatcher retries it after DUR. It counts as a failed run, so after the card's failure limit (`--max-retries`, else `dispatch.failure_limit`, default 2) the card ends `blocked` as `gave_up`. Without `--retry-after` a transient block waits for `unblock` |
| `kanban wait-ci [--pr N]` | Worker only; the owner gets exit 5. Waits up to 540 s on the CI of the run's PR (the one open PR from the card's branch into its base, or `--pr N`) and prints one line. Exit 0 passed, 1 failed (the line names the failing checks), 2 still pending, also on gh or lookup trouble and when no checks are reported yet (it stops after 180 s of that), 6 `no_ci`: the repo has no workflows at all, confirmed after a 60 s grace by the workflows API and the PR head, so no check will ever report; it is never a pass, 7 `skipped`: every check on the head is skipped or neutral, so none ran; never a pass (a head with at least one passing check and some skipped ones is `passed`), 4 no open PR or several open (the message says how to fix it). A `kanban:` line on stderr is an error, never a CI verdict. Run it again while it exits 2, at most 3 runs in all: on exit 6 do not run it again, write "no CI on this repo" in the summary and go on; on exit 7 do not run it again, write "all CI checks skipped" in the summary and on the `Gate:` line and go on; after the third exit 2 with "no checks reported" write "no CI checks registered on the head", never "passed"; with checks still running after the third, `block --kind transient --retry-after 30m`. Never in the background, never under Monitor, never in a `sleep` loop |
| `kanban gate [--lines N] -- CMD [ARG...]` | Worker only; the owner gets exit 5. Runs the gate command (tests, lint, build) with its whole output in a log file under the card's run directory in `KANBAN_HOME` (`run-N.gate-K.log`, never in the worktree, where it would land in the PR diff) and prints one header line, `gate: exit CODE after SECSs, N lines; full log: PATH`, then at most `--lines` lines (default 40): the failure-looking lines first (`FAIL`, `Error`, `error:`, `failed`, `Traceback`, `AssertionError`, `✗`; each once, in order, at most half the budget), then the tail, where test runners print their summary. One argument after `--` is a shell string run as `bash -o pipefail -c STRING`; several are an argv. The exit code is the command's own, 127 when it cannot start. Read the full log with `Read` only when the digest is not enough: `kanban gate -- 'uv run pytest -q'` |
| `kanban complete ID [--summary TEXT \| --summary-file -]` | Terminal. A repeat within the same run is a no-op |
| `kanban request-review ID --reviewer warden --summary TEXT` | Terminal. The CLI finds the PR itself, from the card's branch and base; it must be open, and the summary need not name it. Exit 4 when no PR or several are open, or (worker only) when the card is tied to a GitHub issue and the PR body has no `Closes OWNER/REPO#N` line, with a message that says how to fix it. A Jira-only or unlinked card needs no such line |
| `kanban request-changes ID --reason TEXT` | Terminal. On a review run it sends the card back to its builder. On warden's work run, a standalone review card with no builder, it blocks the card `needs_input` with a `CHANGES REQUESTED:` reason for the owner. The owner has a form of its own, see Moves |

## Owner verbs

Cards:

| Command | Notes |
|---|---|
| `kanban create TITLE` | `--assignee --workspace worktree:<root>\|dir:<abs> --branch --base --priority --max-runtime 30m\|2h --max-retries --skill --model --effort --budget --touches GLOB (repeatable) --merge-method squash\|merge\|rebase --body \| --body-file - --idempotency-key --parent ID (repeatable) --triage`, and `--grant RULE` (repeatable) with `--approved "WORDS · SOURCE · DATE"` to attach exact grants at creation. A `--parent` in `triage` prints a warning: the child would never start. `--touches` is stored as a list; a card whose touches overlap a running work card's in the same repo waits with the skip reason `overlaps:<card id>` (shown by `dispatch --dry-run`, `list`, `show` and `skipped` events), and a card with no touches is never scheduled around. A scratch card (the default without `--workspace`) with `--branch` is refused (exit 4, the message names `--workspace worktree:<repo>`), for a `--triage` card at `specify`. A worktree card with no `--base` and exactly one same-repo worktree parent takes that parent's base. `--merge-method` (default squash) is for the daemon's merge into a milestone or feature branch; a merge into the default branch stays the owner's. A smith or designer card with no `--workspace` is scratch, and a scratch card whose `## Deliverable` names a PR is refused (exit 4) because its worker cannot run `request-review`; pass `--workspace worktree:<root> --branch feature/<slug>` |
| `kanban specify ID` | Same fields plus `--title`; takes a `triage` card to `ready`, or to `todo` while a parent is open. The card starts at once, so stage its inputs first |
| `kanban edit ID` | The same fields, including `--touches` (`--touches ''` clears it) and `--merge-method`; `--allow-empty-body`. Changes text and settings, never the status |
| `kanban list` | `--status --assignee --sort created\|priority\|completed-desc --limit`; needs a resolvable board. Prints why a card waits, the latest skip reason as `not started: <reason> (since …)` (`skip` in `--json`), and marks a `done` PR card `awaiting merge` (see "Links and waiting") |
| `kanban assign ID ROLE`, `kanban reassign ID ROLE [--reclaim]` | Roles: smith, designer, scout, warden, and atlassian when `roles.atlassian.enabled` is set |
| `kanban link PARENT CHILD`, `kanban unlink PARENT CHILD` | Dependencies. `link` warns when PARENT is in `triage`: the child would never start |

Moves, each with `--reason` (`request-review` takes `--summary`):

| Command | From -> to |
|---|---|
| `kanban promote ID --reason TEXT [--ignore-merge]` | `todo` -> `ready`; `--ignore-merge` starts a child although its parent's merge is pending or failed; the waiver stays on the card until `reopen`, `hold` or `link` |
| `kanban hold ID --reason TEXT` | `todo`, `ready` or `blocked` -> `triage`: parked, never dispatched. `specify` takes it out and starts it (to `todo` while a parent is open) |
| `kanban unblock ID --reason TEXT` | `blocked` -> `review` when the last run was a review (warden looks again), else `ready` (`todo` while a parent is open). Clears the block kind |
| `kanban request-review ID --summary TEXT` | `blocked` -> `review`. Owner form, run outside a run on a `blocked` builder card whose PR is open (found from the card's branch and base; none or several open is exit 4). Warden reviews it; the normal approval and merge path then applies |
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
  - PR into a milestone or feature branch: the daemon sets the merge state `pending`, then `merged`, with the card's `--merge-method` (squash by default). The child waits for `merged`; a `failed` merge holds it too (`merge-retry`, or `promote CHILD --ignore-merge`).
  - An approval marked `PARTIAL REVIEW` into a milestone or feature branch: the merge state stays `pending`, the board records `awaiting_owner_merge`, and `show` prints `merge: pending (owner merges)`. The daemon never merges it; the owner merges the PR on GitHub, the sweep records `merged`, and the child starts then.
  - PR into the default branch: no merge state, so the child starts at approval, before the owner merges. If the child needs the parent's code in its base, use a milestone branch. Nothing announces the owner's merge, so a card that must wait for it is created by the orchestrator after the merge (`milestone-branches.md`, step 5).
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
| `kanban watch` | `--kinds a,b --since EVENT_ID \| --since-start --follow --wait --actionable`; `--wait` polls like `--follow` but stops after the first lines it prints (lines `--actionable` hides do not count) with exit 0, for a background command that should wake the session once; a non-zero exit is an error, not an event; spans all boards unless `--board` is given. Starts at the end of the log by default. `--actionable` keeps only the events that need the owner and were not raised by the owner: `blocked`, `block_loop_detected`, `gave_up`, `merge_failed`, `awaiting_owner_merge`, `account_paused`, `quota_wall`, `dispatch_paused`; it cannot be combined with `--kinds` (exit 2). One line per event: `[time] #<event id> <card> <kind> (<actor>) {payload}`, the actor being for example `worker:smith#3` or `owner:cli` |
| `kanban digest [--since DUR]` | Read-only summary of the last `DUR` (default `2h`). Opens with "Needs you: N" and those items, then what merged, what is in review, what is running, and the cost |
| `kanban followups [--since DUR]` | Lists the `FOLLOW-UP:` comments of that window (default `24h`), for the owner to turn into `create --triage` cards. Creates nothing |
| `kanban runs ID` | Every run of the card with its outcome, cost and tokens. Since 0.4.0 `in`, `out`, `cache-write` and `cache-read` are the run's whole usage, summed over every model it billed (subagents and the advisor included), not the last turn's; a run with no per-model usage keeps the last result's figures |
| `kanban log ID [--run N] [--tail N]`, `kanban tail ID` | A run's log; `tail` follows the live run |
| `kanban stats [--days N] [--backfill]` | Cost by role, model and day; outcomes; review rounds; `accounts`: each Claude config dir that is behind a quota wall (`wall_until`) or paused on sign-in (`auth_paused`) right now. Since 0.4.0 each run is also measured from its own stream, which adds: `runtime_by_role` (`total_s`, `avg_s`, `max_s` of ended minus started, and `api_s`, the API time), `cost_by_role_model` (per role and model the run used, the advisor's model being a row of its own; `cache_write_1h` and `cache_write_5m` are the run's split, put on the model it ran as), `advisor_arms` (per role and arm: runs, cost, `avg_cost`, `advisor_calls`), `per_approved_card` (all runs of the cards warden approved in the window, over those cards), `guards` (`reviews`, `first_pass_approvals`, `send_backs`, `first_pass_rate`: the share of reviewed cards whose first verdict was an approval; owner-given verdicts are not counted), `skills`, `mcp`, `tools`, `agents` (each `name`, `calls`, `runs`), `harness_totals` (hooks ok and failed, compactions, permission denials, tool errors, subagents, runs by `terminal_reason`) and `unmeasured_runs` (runs with no measurement). `--backfill` measures the runs from before 0.4.0 from their stream files (`advisor` set to `inherit`, which was the policy of all of them), oldest first; it is safe to repeat, commits in batches and prints how many runs it filled and how many it skipped for want of a stream file. Run it once after upgrading |
| `kanban web [--port N] [--board SLUG] [--open]` | Read-only dashboard on `127.0.0.1:8742`: board, cards, every run's parameters and logs, statistics; foreground, Ctrl-C stops it; never writes the database |
| `kanban dispatch --dry-run` | What the next tick would claim, and why each other card is skipped |
| `kanban snapshot REPO [--ref REF]` | Prints the path of a fresh detached checkout of `origin/HEAD`, or of the branch, tag or sha `--ref` names, for the card check. Each ref keeps its own checkout, so two refs of one repo do not move each other's tree |

Boards and system:

| Command | Notes |
|---|---|
| `kanban boards list` | `kanban boards create\|edit\|archive SLUG [--default-workdir PATH] [--name NAME] [--claude-config-dir DIR]`; `create` and `edit` also take `--jira-project KEY [--jira-jql JQL]` to pair the board with a Jira project, and `edit` takes `--jira-off` to unpair it; the same two verbs take `--github-repo [HOST/]OWNER/REPO [--github-query Q] [--github-project OWNER/NUMBER]` to pair the board with a GitHub repository (and a Projects v2 board), and `edit` takes `--github-off`; a board pairs with Jira or with GitHub, never both |
| `kanban jira import ISSUE [--board B]`, `kanban jira link ID ISSUE`, `kanban jira unlink ID` | Bring an issue in as a triage card (also one that is Done or was unlinked); tie a card to an issue (Jira's text wins field by field, the card's goes into a conflict comment); cut the tie. A paired board syncs every `jira.poll_s` seconds; the owner's own Atlassian MCP server does the talking |
| `kanban jira sync [--board B]`, `kanban jira status` | Run one sync pass now (prints `<board>: synced` per board; exit 1 while a board is paused); list paired boards, their pause and the cards with a conflict or error |
| `kanban github import ISSUE [--board B]`, `kanban github link ID ISSUE`, `kanban github unlink ID` | `ISSUE` is `12`, `OWNER/REPO#12` or the issue's URL. Bring an issue in as a triage card (also one outside the board's label or query, or closed, or unlinked); tie a card to an issue (GitHub's text wins field by field, the card's goes into a conflict comment); cut the tie. A paired board syncs every `github.poll_s` seconds through the owner's own `gh` login; a linked card's worker context carries the line to put in its PR body (`Closes OWNER/REPO#12`) |
| `kanban github sync [--board B]`, `kanban github status` | Run one sync pass now (prints `<board>: synced` per board; exit 1 while a board is paused); list paired boards, their pause, whether export is off (cards are never exported to a public repository) and the cards with a conflict or error. Done comes only from the issue being closed; the project Status is written, never read back |
| `kanban pause [--reason TEXT]`, `kanban resume` | Global dispatch switch; `resume` also re-probes sign-in |
| `kanban config list`, `kanban config get KEY`, `kanban config set KEY VALUE` | Settings; values are JSON. `github.label` (default `kanban-claude`), `github.poll_s` (default 120) and `github.status_map` (card status to project Status option) tune the GitHub sync. `jira.claim true` makes the Jira assignee the lock for paired boards: a linked card dispatches only when its issue is assigned to `jira.assignee`. `budget.max_usd_per_card` (default 15.0, 0 is off) and `budget.max_usd_per_board_day` (default null; 0 or null is off) are checked at claim: over a cap the card goes `blocked` `capability` with a comment naming the key to raise; raise it, then `kanban unblock ID --reason "cap raised"` (the board-day block does not lift by itself at midnight). `advisor.<role>` (`smith`, `designer`, `scout`, `warden`, `atlassian`; `inherit` by default) sets the advisor for that role's runs: `inherit` leaves it as your own `~/.claude/settings.json` has it (a run inherits `advisorModel`), `off` sets `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` for the run, and `ab` runs an online A/B on the live board: each card gets `off` or `on` from a stable hash of role and card id (`sha256("role:card")`, first byte, lowest bit: 1 is `off`), so a card keeps its arm on every rerun and the roles' arms are independent. `on` leaves the advisor as your settings have it, so the A/B only differs if `advisorModel` is set. The arm is recorded on each run; read the result in `kanban stats`: `advisor_arms` gives, per role, the runs, cost, `avg_cost` and `advisor_calls` of each arm, and `per_approved_card` and `guards` (`first_pass_rate`, `send_backs`) say whether the cheaper arm paid for itself in review rounds; judge the arms on `avg_cost` together with `first_pass_rate` over enough cards, not on a few. `notify.command` is the owner's own command, run by the daemon on each actionable event (the `watch --actionable` set) with the event line on stdin: an argv list is preferred, `kanban config set notify.command '["/abs/path/script","arg"]'`; a string is split like a shell would but run with no shell. Use an absolute path, since launchd's PATH is minimal. It times out after 10 s, failures go to `daemon.log`, and it sees only events raised after the daemon's first pass |
| `kanban daemon` | The dispatcher and housekeeping; started by the service the setup skill installs |
| `kanban migrate` | Brings the database schema up to date |
| `kanban doctor`, `kanban setup`, `kanban uninstall` | Health check, install, removal. A build that prints `not available in this build` does not have them yet; use `/kanban-claude:setup` |
