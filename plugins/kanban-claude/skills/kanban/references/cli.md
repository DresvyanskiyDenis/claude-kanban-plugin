# kanban CLI reference

Usage: `kanban [--board SLUG] [--json] <verb> ...`. `--board` and `--json` are also accepted after the verb. Run `kanban <verb> --help` for the exact flags of the build you have.

## Conventions

- **Board.** `--board`, else `KANBAN_BOARD` (set for workers), else the board whose `default_workdir` is the git root of the current directory, else exit 2. There is no silent default board.
- **Exit codes.** 0 ok, 1 error, 2 usage, 3 not found, 4 guard refused (state conflict or validation), 5 not allowed in this context. Messages go to stderr and name the fix.
- **`--json`.** Output is an object with `"schema": 1`; lists are `{"schema": 1, "cards": [...]}`, never a bare array.
- **Multiline text.** `--body-file -` and `--summary-file -` read stdin. `comment <id> -` reads the comment from stdin.
- **Two modes.** With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs below work, and each acts on the run's own card (`KANBAN_TASK` is the default id). An owner verb in worker mode exits 5. Without it, every verb works.
- **Daemon warning.** `create`, `specify`, `promote` and `unblock` still succeed when the daemon is not running, and print one warning: cards will not dispatch until it runs.
- **Structured comments.** `block`, `unblock` and `request-changes` also write a comment prefixed `BLOCKED: `, `UNBLOCK: ` or `CHANGES REQUESTED: `.

## Worker verbs

| Command | Notes |
|---|---|
| `kanban show [ID]` | The card. `--json`: `task` (`id`, `status`, `body`, `branch_name`, `base_branch`, `pr_url`, `pr_number`), `comments` (`body`, `author`), `runs` |
| `kanban context [ID]` | Markdown for a run: body, attachments, prior attempts, parent results, comment thread, grants. Quoted worker text is framed as data |
| `kanban comment ID TEXT` | Notifies nobody; `--author` is set for workers |
| `kanban attach ID PATH` | Copies a file (25 MiB at most) next to the card |
| `kanban block ID --kind needs_input\|capability\|transient --reason TEXT` | Terminal |
| `kanban complete ID [--summary TEXT \| --summary-file -]` | Terminal. A repeat within the same run is a no-op |
| `kanban request-review ID --reviewer warden --summary TEXT` | Terminal. The summary must name `PR #<n>` or a PR URL; the PR must be open with the card's head and base |
| `kanban request-changes ID --reason TEXT` | Terminal, review runs only |

## Owner verbs

Cards:

| Command | Notes |
|---|---|
| `kanban create TITLE` | `--assignee --workspace worktree:<root>\|dir:<abs> --branch --base --priority --max-runtime 30m\|2h --max-retries --skill --model --effort --budget --body \| --body-file - --idempotency-key --parent ID (repeatable) --triage` |
| `kanban specify ID` | Same fields plus `--title`; takes a `triage` card to `todo` or `ready` |
| `kanban edit ID` | The same fields; `--allow-empty-body` |
| `kanban list` | `--status --assignee --sort created\|priority\|completed-desc --limit`; needs a resolvable board |
| `kanban assign ID ROLE`, `kanban reassign ID ROLE [--reclaim]` | Roles: smith, designer, scout, warden, and atlassian when `roles.atlassian.enabled` is set |
| `kanban link PARENT CHILD`, `kanban unlink PARENT CHILD` | Dependencies |

Moves, each with `--reason`:

| Command | From -> to |
|---|---|
| `kanban promote ID --reason TEXT [--ignore-merge]` | `todo` -> `ready`; `--ignore-merge` starts a child although its parent's merge is pending or failed |
| `kanban hold ID --reason TEXT` | `todo`, `ready` or `blocked` -> `triage` (parked) |
| `kanban unblock ID --reason TEXT` | `blocked` -> `ready`, `todo`, or `review` |
| `kanban reopen-review ID --reason TEXT` | `review` -> `ready`; the owner's own feedback |
| `kanban reopen ID --reason TEXT` | `done` -> `ready` |
| `kanban mark-done ID --reason TEXT` | `review` or `blocked` -> `done` |
| `kanban stop ID` | ends a running card; it becomes `blocked` |
| `kanban archive ID [--rm --yes]` | hides the card; `--rm --yes` deletes it permanently |
| `kanban merge-retry ID` | tries a failed or pending merge again |

Grants:

| Command | Notes |
|---|---|
| `kanban grant ID --allow RULE --approved "WORDS · SOURCE · DATE"` | `--allow` is repeatable; exact rules only |
| `kanban grant ID --revoke`, `kanban grants ID` | Revoke; list the active grants |

Observing:

| Command | Notes |
|---|---|
| `kanban watch` | `--kinds a,b --since EVENT_ID \| --since-start --follow`; spans all boards unless `--board` is given. Starts at the end of the log by default. One line per event: `[time] #<event id> <card> <kind> (@role) {payload}` |
| `kanban runs ID` | Every run of the card with its outcome |
| `kanban log ID [--run N] [--tail N]`, `kanban tail ID` | A run's log; `tail` follows the live run |
| `kanban stats [--days N]` | Cost by role, model and day; outcomes; review rounds |
| `kanban dispatch --dry-run` | What the next tick would claim, and why each other card is skipped |
| `kanban snapshot REPO` | Prints the path of a fresh detached checkout of `origin/HEAD`, for the card check |
| `kanban board [--revoke] [--no-open]` | Opens the web board with a one-time login; `--revoke` invalidates every board token |

Boards and system:

| Command | Notes |
|---|---|
| `kanban boards list` | `kanban boards create\|edit\|archive SLUG [--default-workdir PATH] [--name NAME] [--claude-config-dir DIR]`; `create` and `edit` also take `--jira-project KEY [--jira-jql JQL]` to pair the board with a Jira project, and `edit` takes `--jira-off` to unpair it |
| `kanban jira import ISSUE [--board B]`, `kanban jira link ID ISSUE`, `kanban jira unlink ID` | Bring an issue in as a triage card (also one that is Done or was unlinked); tie a card to an issue (Jira's text wins field by field, the card's goes into a conflict comment); cut the tie. A paired board syncs every `jira.poll_s` seconds; the owner's own Atlassian MCP server does the talking |
| `kanban jira sync [--board B]`, `kanban jira status` | Run one sync pass now (prints `<board>: synced` per board; exit 1 while a board is paused); list paired boards, their pause and the cards with a conflict or error |
| `kanban pause [--reason TEXT]`, `kanban resume` | Global dispatch switch; `resume` also re-probes sign-in |
| `kanban config list`, `kanban config get KEY`, `kanban config set KEY VALUE` | Settings; values are JSON |
| `kanban daemon` | The dispatcher and housekeeping; started by the service the setup skill installs |
| `kanban migrate` | Brings the database schema up to date |
| `kanban doctor`, `kanban setup`, `kanban uninstall` | Health check, install, removal. A build that prints `not available in this build` does not have them yet; use `/kanban-claude:setup` |
