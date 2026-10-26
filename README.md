# claude-kanban-plugin

A local kanban board for Claude Code. Cards are dispatched to headless Claude workers (`claude -p`) under a macOS LaunchAgent or a Linux systemd user unit; the board is a SQLite file you read with the `kanban` CLI and the `/kanban-board` pane. No external service.

Status: stable (`0.3.0`). The plugin surface (agents, skills, hooks), setup, doctor and uninstall are in place; see [CHANGELOG](plugins/kanban-claude/CHANGELOG.md).

## Install

macOS and Linux. On macOS the daemon is a LaunchAgent. On Linux it is a systemd user unit, so you need `systemctl --user`, and lingering (`loginctl enable-linger $USER`, once) for a daemon that survives logout; without it the unit stops when your last session ends and the board stops dispatching. Setup warns when lingering is off and never turns it on. On a host without systemd (a container, WSL without systemd) pass `--no-daemon` and start the daemon yourself with `kanban daemon`; the rest of setup is the same.

You need `uv` (`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`), `git`, `gh` and Claude Code 2.1.259 or newer (checked on 2.1.295); `npx` is optional (only the designer uses it). If your repositories live on a GitHub Enterprise host, log `gh` in there first: `gh auth login --hostname <your GHE host>`. Behind a proxy that re-signs TLS, export your CA bundle in `~/.zshrc` and run setup from a new terminal:

```sh
export SSL_CERT_FILE=<path to your CA bundle>
export NODE_EXTRA_CA_CERTS=<path to your CA bundle>
```

Inside the Claude Code whose config directory the workers will use:

```text
/plugin marketplace add DresvyanskiyDenis/claude-kanban-plugin
/plugin install kanban-claude@claude-kanban-plugin
/kanban-claude:setup
```

From a shell the first two are `claude plugin marketplace add DresvyanskiyDenis/claude-kanban-plugin` and `claude plugin install kanban-claude@claude-kanban-plugin`. The shell form is checked against `claude plugin marketplace add --help` (Claude Code 2.1.295: the source is a URL, a path or a GitHub repo); the slash form is the same command inside a session. Workers resolve `--agent kanban-claude:<role>` from the config directory they run under, so the plugin must be enabled at user scope there. The plugin depends on `frontend-design@claude-plugins-official`; the marketplace lists that marketplace as an allowed dependency source, so Claude Code installs it along with the plugin.

To install from a copy on GitHub Enterprise Server, the `owner/repo` shorthand always means github.com, so give the full clone URL:

```text
/plugin marketplace add https://<your GHE host>/<org>/claude-kanban-plugin.git
```

Claude Code clones with the git credentials already on the machine and never prompts; if the host is not signed in yet, also run `gh auth setup-git --hostname <your GHE host>`.

The setup skill runs `skills/setup/scripts/bootstrap.sh`, which installs the runtime this plugin ships (`uv tool install --force`), checks that the installed `kanban` has the plugin's version, and runs `kanban setup`. Setup is idempotent. In order, it:

1. checks the host (macOS or Linux, `systemctl` on Linux, `uv`, `git`, `gh`) and `claude --version`, and refuses a `claude` older than 2.1.259 with `claude update` as the fix;
2. picks the Claude config directory: `--claude-config-dir`, else the one a previous setup recorded, else the session's `CLAUDE_CONFIG_DIR`, else `~/.claude`. The default directory is never exported to a worker; any other one is;
3. captures the environment for the daemon's service: `PATH` (the directories of `claude`, `gh`, `git`, `uv`, `npx`, `node` plus the system ones), the CA bundle and proxy variables and `LANG`. A variable whose name contains `TOKEN`, `KEY`, `SECRET`, `PASSWORD` or `AUTH` is never captured unless you list it in `worker.env_passthrough`. A proxy URL keeps its password in the service file (the proxy needs it) and is masked as `http://***@host` in every log line;
4. probes `claude` with the flags workers use (auto permission mode, your review model) under that captured environment alone, and explains a failure: not logged in (prints the login command), no Opus on this seat (`kanban config set models.review sonnet`, or rerun with `--yes`), or an organisation that disables auto mode (setup stops);
5. creates the store, migrating it after a backup, and records `claude.bin` and `claude.config_dir` only where they differ from what is stored, so a rerun keeps everything you configured;
6. writes the service file (0600): on macOS the LaunchAgent `kanban-claude.daemon` in `~/Library/LaunchAgents`, unloaded and loaded again in your `gui/<uid>` domain; on Linux the user unit `kanban-claude.daemon.service` in `~/.config/systemd/user`, disabled and then enabled with `systemctl --user enable --now`. The unit sets `KillMode=process`, so a restart leaves the detached runners alone, as the LaunchAgent's `AbandonProcessGroup` does;
7. writes `$KANBAN_HOME/installed.json` (the version, the Claude binary and config directory and the captured environment; data only, no hook executes it);
8. waits up to 10 s for the daemon to hold `$KANBAN_HOME/daemon.lock` and then starts the scout agent once, the way a worker will, to prove that the plugin resolves. It finishes with `kanban-claude X is set up.`

`--no-daemon` skips step 6 and step 8's daemon wait, and `--yes` accepts the Sonnet fallback and a deliberate downgrade. Progress is also written to `$KANBAN_HOME/logs/upgrade.log`.

Upgrading is the same skill on a newer plugin: when the recorded version is older, setup takes an exclusive `flock` on `$KANBAN_HOME/upgrade.lock` (a second run exits 0; the lock ends with its process, so a crashed setup leaves nothing to clear), migrates the database after a backup, restarts the daemon (`launchctl kickstart -k`, or `systemctl --user restart` on Linux) and rewrites `installed.json`. Runs in flight are not touched. A plugin older than the recorded version is refused unless you pass `--yes`. Nothing upgrades itself: a new plugin only announces itself at the next session start, and the daemon changes when you run the skill. Third-party marketplaces do not auto-update by default; run `/plugin update` or enable auto-update for this marketplace.

## What the plugin ships

- Agents `kanban-claude:smith`, `designer`, `scout` and `warden` run cards; `card-helper` and `review-helper` are their subagents; `card-checker` (read-only by its instructions) checks card bodies for the orchestrator. The optional `atlassian` role makes one exact Jira or Confluence write per card (see Jira sync). Each agent file lists its tools explicitly.
- Skills: `kanban` (the orchestrator rulebook and CLI reference), `kanban-code-review` (warden's procedure and `preflight.py`), `simplify-code-card` (`slop_gate.py`).
- Hooks: a lint check on the lines a builder just edited, a session-start status check, and the board pane mod.
- Every run is measured from its own stream (turns, tools, skills, MCP servers, agents, hooks, cache writes by TTL, advisor calls, API time) and `kanban stats` reports it with the runtime by role, the cost per role and model, and the first-pass approval rate; `kanban stats --backfill` fills the runs from before 0.4.0. `advisor.<role>` (`inherit`, `off` or `ab`) switches the advisor tool off for a role or A/B-tests it per card, and `kanban gate -- CMD` runs a gate command with its full output in a log file and a short digest on the screen.
- The designer runs Playwright MCP, pinned to an exact version in `runtime/src/kanban_claude/mcp/designer.json`.

Each worker session is named `kanban <card-id> <role>`. Ask your session to steer a running card and it sends your words there and records them on the card; a worker asks back only by blocking its card with `needs_input`.

Agents cannot enforce a deny list for Bash commands; the runner's `--settings` carries the rules for `gh pr merge`, force pushes and `kanban grant`, and branch protection on the remote is the backstop.

Workers do not inherit your own setup. In a run, every plugin your account enables is off except kanban-claude, its declared dependency and the names in `worker.plugin_allowlist`. MCP servers come only from a private per-run file (`--strict-mcp-config`, mode 0600) that holds the role's servers plus `worker.mcp_allowlist`, copied from the config directory's `.claude.json`. Each role also gets its own permission denies: no edits for warden, atlassian and (except its report) scout. Each role's built-in tools come from `tools.<role>` (`kanban config set tools.smith '[...]'` makes a list longer or shorter), which the runner passes as `--tools`; whatever the list says, warden and atlassian never get Edit, Write or NotebookEdit and scout never gets Edit. A PreToolUse hook in the run's settings lets a role start only its own helper, and a helper start nothing. The atlassian role gets only its own MCP server, started with `ENABLED_TOOLS` naming its nine read and eight write tools. Each card's workers share one `TMPDIR` under `$KANBAN_WORK/tmp/<card>`.

Spend is capped at claim: `budget.max_usd_per_card` (default 15 USD, 0 turns it off) and `budget.max_usd_per_board_day` (off by default). A card over a cap is blocked with a comment naming the key to raise.

To follow the board from a session, run `kanban watch --actionable --wait --board <slug>` as a background command. It blocks until something needs you, prints it and exits; run it again with `--since <last event id>`. `notify.command` (an argv list, for example `kanban config set notify.command '["/abs/path/script"]'`) gets each such event line on stdin from the daemon, with no session open.

## Session start

`hooks/scripts/session_start.sh` (POSIX `sh`, well under 50 ms, read-only) runs at the start of every session except a worker's. It prints nothing when the machine is not set up or all is well. When the plugin's version differs from `installed.json` it prints `kanban-claude: daemon a.b.c, plugin x.y.z — run /kanban-claude:setup to upgrade`. While dispatch is paused it prints one line of enumerated codes (`manual`, `systemic`, `auth:<config dir>`, `quota:<config dir> until <time>`) and `kanban doctor` as the way to the fix. It reads the two versions with a fixed `sed` pattern, queries the database with `sqlite3 -readonly`, never evaluates a file, never prints card or worker text, and never installs anything.

## Board pane

`/kanban-board` opens the board of the session's folder beside the transcript; `/kanban-board SLUG` pins another board and `/kanban-board close` closes it. It reads `kanban --json list` and `kanban --json digest --since 24h` every 15 s while the pane is open, and only then; `r` refreshes now and `v` switches between cards and a list while the pane has the keyboard (click it, or Ctrl+X then Tab); until then the footer reads `ctrl+x tab: keys`, since those keys would type into the prompt.

- **Needs you** comes first: questions, grants, merges, logins and quota walls, each a card with its reason and, on its own line, the command that answers it. A new need raises one toast.
- A health line shows whether the daemon is live, whether dispatch is paused, and the last 24 hours' spend.
- Running, Review, Blocked and Up next are columns of cards: priority, the title on up to two lines, then owner and age, or PR number or block kind. A lane shows as many cards as the pane's height holds, then `+N more`; an empty lane keeps its place, drawn faint. Triage cards follow as rows, in the rows the lanes leave, and Enter opens one; in the list, Triage is a fifth lane. Recently merged and a bar of the board's spread follow.
- `v` switches to a compact list, one row a card, and back; the pane remembers the choice across sessions.
- Select a card for its detail (branch, PR, last run, comments); `back` (or `b`) returns.
- Card columns sit as many across as fit, at least 22 cells each: two in a dock, four inline at 120 columns. In the list, inline from 72 columns the lanes sit side by side; in a dock or narrower they stack. Colors are Claude Code theme keys, so the pane follows your theme.
- A failed refresh keeps the last good read and says how stale it is and why.
- The pane reopens next session only in the same folder, and only if that board still reads; one closed by hand stays closed. It needs `kanban` on PATH or at `~/.local/bin/kanban`. Card workers never load it.

`kanban list`, `show` and `watch` are the full view.

It is a Claude Code mod and needs Claude Code 2.1.287 or later; an organization's managed settings can disable mods. Narrower than 144 columns, Claude Code places a pane it was not asked for only from 110 columns, so a reopened pane may wait until the terminal is wide enough.

## Web dashboard

`kanban web [--port N] [--board SLUG] [--open]` serves a read-only dashboard at `http://127.0.0.1:8742/` (the default port is fixed, and the command says so and exits when it is taken). It runs in the foreground until Ctrl-C and works offline: the page is plain HTML, CSS and JavaScript shipped in the package, with no CDN and no build step.

- The board has one lane per status, live cards with the last thing the worker did, and a dock that follows every running card's transcript.
- A card opens in a drawer: Overview, Parameters (every card setting, then per run the command line, environment, what the session loaded and what it used), Runs, Logs and Comments.
- Logs show a run's transcript with tools, results, hooks and subagent work folded under their parent, or the raw text log, with filters, search, paging for large streams, follow mode and a download of the original file. Long entries are cut on the page and expanded on request.
- Stats shows spend per day, per role and per model, outcomes, review rounds and, on a schema 6 database, the harness numbers (skills, MCP, tools, subagents, hooks).

It opens the database read-only on every request and never migrates it, so it can run beside an older daemon: on a schema 5 database it shows the board, cards and logs, and the Stats page says it needs the schema 6 columns. It binds to `127.0.0.1` only, answers `GET` and `HEAD` only, rejects other `Host` headers, and sends a content security policy with no inline script or style. Log and prompt files are read only through the run rows of the database, and only under the kanban home.

## Doctor

`kanban doctor` runs twenty-two independent checks and exits 1 when any one fails (a warning leaves it at 0); `--json` gives the same list as data. Each line names what is wrong and the command that fixes it. `--probe` adds the two checks that start `claude` (the scout agent resolves and the login passes, in every config directory a board uses; this costs a few cents) and, for check 20, starts `jira.mcp_command` and confirms it lists the seven Jira tools. `--fix` repairs only what is safe: file modes (`$KANBAN_HOME` and `$KANBAN_WORK` 0700, the database, `installed.json`, logs and the service file 0600), a daemon service that is not loaded (it rebuilds a missing plist or unit file from `installed.json`), and a `running` card or run that disagrees with the other through the dispatcher's reconcile step. Everything else is yours to repair.

The checks, in order: 1 the daemon's service (LaunchAgent or systemd unit) loaded; 2 the daemon holds `daemon.lock` (its pid and version are read from it); 3 tool, `installed.json`, daemon and (inside a Claude session) plugin versions match; 4 `PRAGMA quick_check`; 5 schema version; 6 a `running` card has a running run and the other way round; 7 `claude` path and version; 8 every config directory exists; 9 the plugin is enabled at user scope in each (read from `settings.json`; `--probe` proves it); 10 `gh auth status` per host the boards use, with a warning when the login is a `GH_TOKEN` variable instead of the keychain; 11 `uv`; 12 CA variables of your shell are in the service file; 13 the service file's `PATH` still finds `claude`, `gh`, `git`, `uv` and the `kanban` entry point (`npx` and `node` only warn); 14 at least 2 GB free; 15 dispatch pause, logged-out accounts and quota walls with their times; 16 kept worktrees; 17 file modes; 18 a live runner for every running run; 19 branch protection; 20 Jira pairing (a paired board needs `jira.assignee` and `jira.mcp_command`; a paused sync is a warning that names the reason, and so is `jira.claim` with a `jira.assignee` that matches none of the assignees Jira has reported); 21 GitHub pairing (`gh auth status` per paired host; a board with `--github-project` also needs the `project` token scope, and `--probe` reads the repository and the project; a paused sync is a warning that names the reason); 22 designer's browser MCP (a warning when a board's repo lists `playwright` in its `disabledMcpServers` in the account's `.claude.json`: Claude applies that list to the card worktrees inside the repo, even to the server the designer brings, so the designer falls back to a Playwright script).

### Branch protection

Workers push with your login, so the only server-side stop for a push to the wrong branch is branch protection (GIT-08). Check 19 asks GitHub, for the default branch and one existing `feature/*` and one `milestone/*` branch of every repo a board uses, through both `repos/<owner>/<repo>/rules/branches/<branch>` (rulesets) and `repos/<owner>/<repo>/branches/<branch>` (classic protection); either one counts. A repo with neither is a warning in `doctor`, and never delays dispatch. One that cannot be read (you lack the rights) is listed as "could not be read" in `doctor` only. The answer is cached in the database and refreshed every six hours.

Setting it up needs admin rights on the repo. For the default branch (no direct pushes, no force pushes, a pull request required; use `--hostname <host>` for GitHub Enterprise):

```sh
gh api --method POST repos/<owner>/<repo>/rulesets --input - <<'JSON'
{
  "name": "kanban-claude: default branch",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false, "require_last_push_approval": false,
      "required_review_thread_resolution": false}},
    {"type": "non_fast_forward"},
    {"type": "deletion"}
  ]
}
JSON
```

For `feature/*`, leave the pull request rule off: a part card of a feature or milestone pushes its own branch (`feature/<slug>-<part>`, `milestone/<n>-<slug>-<part>`), which matches the same pattern, so requiring a pull request there would stop the worker it is meant to serve. Forbid deleting and rewriting instead:

```sh
gh api --method POST repos/<owner>/<repo>/rulesets --input - <<'JSON'
{
  "name": "kanban-claude: feature and milestone branches",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/feature/*", "refs/heads/milestone/*"], "exclude": []}},
  "rules": [{"type": "non_fast_forward"}, {"type": "deletion"}]
}
JSON
```

## Jira sync (optional)

Pair a board with one Jira project and its cards and issues stay in step both ways, with no model in the loop. **Jira owns the text, the board owns execution.** The sync talks to the `atlassian` MCP server (`mcp-atlassian`) that you add to Claude yourself; `kanban setup` copies only its `command` and `args` into `jira.mcp_command`, never its `env` or a token.

```sh
kanban config set jira.assignee you@example.com
kanban boards edit main --jira-project KAN       # optional: --jira-jql 'project = KAN AND sprint in openSprints()'
kanban jira sync                                 # one pass now; the daemon runs one every jira.poll_s (120 s)
```

- **Membership.** Issues that match the board's JQL (default `project = KAN AND labels = "kanban-claude" AND assignee = currentUser()`) become triage cards, Done ones excepted; a new card gets an issue with the `jira.label`, a `kanban-<card id>` label, `jira.issue_type` and `jira.assignee`; the id label lets a pass that timed out after creating the issue adopt it instead of making a second. Done and archived cards are not exported. `kanban jira import|link|unlink` do the same by hand, and an unlinked issue stays out until you import it.
- **Text.** Title and body are merged per field, each side against its own last synced value: Jira against what it read back, the card against its own text then. So a server that rewrites the markup never looks like an edit, and a card touched for any other reason sends nothing. One side changed: that side wins. Both changed: Jira wins, and the card's text goes into a `[jira] conflict` comment. Jira's text on an approved card (todo, ready, blocked) puts it back in triage with a `[jira]` comment; a running or review card waits until it has left that state.
- **Status.** Only a change of the card's own status moves its issue, by one direct transition to the status in `jira.status_map`; `blocked` and `archived` leave Jira alone, and an unrelated card edit (a title, a comment) never undoes a colleague's move in Jira. Without `jira.claim` a board pushes the status of any issue it holds, whoever else holds it too (see Team use). A missing transition, or one that needs fields, is a `jira error` badge and is tried again every pass until it passes. A Jira Done closes a card in triage, todo, ready or blocked; on a running or review card, or one whose `todo` child waits on it, it is a `jira conflict` badge instead. Leaving Done in Jira puts a done card back in triage.
- **Comments.** Jira comments become card comments by `jira:<name>`; your own comments (including the `UNBLOCK:` and `REOPEN:` ones a card gets when you unblock or reopen it) go to Jira, and so does the closing text of each work run that ends in complete, request-review or block, and each review verdict, under a label of its own: `kanban completed:`, `kanban ready for review:`, `kanban blocked:`, `kanban review approved:`, `kanban review changes requested:`, `kanban review blocked:`. Worker comments and `[run]` lines stay on the board. A comment that was being sent when the call's answer never came is found by its text and adopted, not sent twice (an equal text with no send under way is someone else's and stays theirs). Workers read them as quoted data.
- **Team use.** Several people can pair their own boards (each with its own `$KANBAN_HOME` and Atlassian login) to one Jira project, and Jira is then all they share. By default nothing stops two boards that hold the same issue from both running it, so switch the claim on: `kanban config set jira.claim true` (one switch for every paired board of that home; a per-board setting is the upgrade path), set `jira.assignee` to your account id or email as Jira reports it (never your display name, which two people can share), and give each board the team query `--jira-jql 'project = KAN AND labels = "kanban-claude" AND (assignee is EMPTY OR assignee = currentUser())'`. The issue's assignee is then the lock. A ready or review card dispatches only when Jira says the issue is assigned to you. If nobody holds it, the next pass reads it, assigns it to you if the pass read it as empty, reads it back and stores what Jira says; the card dispatches after that, up to `jira.poll_s` later (an issue the board created is assigned to `jira.assignee` from the start and does not wait). An issue someone else holds shows `skipped: claimed_elsewhere` on the board, and an unclaimed one, or one whose claim failed (the Assign Issues permission is missing, Jira is unreachable), `skipped: jira_unclaimed`: dispatch fails closed. Only the board that holds the issue moves its status. An assignee the board has read is not overwritten (Jira has no compare-and-set, see the limits); if Jira gives the issue to someone else while your card runs, the run finishes and the card shows a `jira conflict` badge. Title, body, status, comments, the closing text of work runs and review verdicts cross; runs, costs, PR fields, worktrees, grants and the reviewer's full output stay on the board that made them. `kanban doctor` warns (check 20) when `jira.claim` is on and `jira.assignee` matches none of the assignees Jira has reported on your linked issues. Limits: the claim is only as fresh as the last pass, so a hand-over in Jira reaches the old holder's dispatcher up to `jira.poll_s` later, or until the sync resumes if it is paused; two boards that assign within the same moment can both read themselves back (the last write stands, the loser's next pass reads the real assignee, and a card of theirs that already runs gets the `jira conflict` badge); and a text edit in Jira that lands between a pass's read and its write is overwritten by a card edit pushed in that pass, silently (Jira's own issue history keeps the overwritten text). None of this has been run against a live Jira server; the assignee shape is taken from `mcp-atlassian` 0.23.1.
- **Failures.** An unreachable server, a rejected login, a timeout or a server that exits pauses that board's sync only: `kanban jira status` and doctor check 20 show it. Dispatch never waits on it (with `jira.claim`, a card that has not yet been claimed stays unclaimed until a pass gets through), and the next pass retries. One failing card or link is an error on that card and the others go on. Each board gets its own server process for the pass, and the pass runs inside the daemon's housekeeping tick, so a slow server delays the other housekeeping jobs by up to `JIRA_CALL_S` per call.
- **The `atlassian` role.** `kanban config set roles.atlassian.enabled true` makes cards assignable to it once `jira.mcp_command` is set too; `dispatch.roles` is not rewritten. The runner starts its server with `ENABLED_TOOLS` naming nine read tools and the eight write tools, pinned to `mcp-atlassian` 0.23.1 (a denylist is complete only for a pinned version), and writes only with a grant naming the exact tool (`kanban grant ID --allow mcp__atlassian__jira_add_comment --approved "..."`), and the card body must give the exact key or page and the exact content.

The tool and argument names, the result shapes and the description round trip come from the `mcp-atlassian` version you run. Run `kanban doctor --probe`, then pair one board with a test project before a real one.

## GitHub sync (optional)

Pair a board with one GitHub repository and its cards and issues stay in step both ways, with no model in the loop; an optional Projects v2 board follows the cards too. **GitHub owns the text, the board owns execution.** It is the Jira sync's rules over `gh api`, so there is no server to configure: it uses the `gh` login you already have, per host (`github.com`, or a GitHub Enterprise host such as `lis-github.liebherr.com`). A board pairs with Jira or with GitHub, never both.

```sh
kanban boards edit main --github-repo OWNER/REPO                       # or HOST/OWNER/REPO; optional --github-query 'label:ux'
kanban boards edit main --github-project OWNER/3                       # optional: a Projects v2 board, by owner and number
gh auth refresh -h github.com -s project                               # only for --github-project: the token needs the project scope
kanban github sync                                                     # one pass now; the daemon runs one every github.poll_s (120 s)
```

- **Text.** Title and body merge per field exactly as in the Jira sync: one side changed, that side wins; both changed, GitHub wins and the card's text goes into a `[github] conflict` comment. GitHub's text on an approved card puts it back in triage; a running or review card waits.
- **State.** Done comes from one thing only: the issue being closed. A close closes a card in triage, todo, ready or blocked; on a running or review card, or one whose `todo` child waits on it, it is a conflict shown on the board. A reopened issue sends a done card to triage. A card that becomes done closes its issue as completed, and a reopened card reopens it. With a project, the card's status also picks the Status option in `github.status_map` (default Todo, In Progress, Done) and the issue is added to the board. The Status column is written, never read back: moving a card by hand in the project does nothing on the board, because that move does not change the issue's `updated_at` and a later pass could not see it.
- **Comments.** GitHub comments become card comments by `github:<login>`; your own comments, the closing text of each work run and each review verdict go to the issue under the same labels as in the Jira sync. A comment whose send never got an answer is found by its text and adopted, not sent twice.
- **Membership.** Open issues with the `github.label` (default `kanban-claude`) become triage cards, pull requests excepted; with `--github-query` the search API picks them instead (its index lags a little). A new card gets an issue with that label, except in a public repository: cards are never exported to a public repository; its issues are still imported and synced, and `kanban github status` says so. One whose create got no answer is adopted by its title on the next pass. A deleted issue is unlinked and its card kept. `kanban github import ISSUE` brings in any issue by number, `OWNER/REPO#N` or URL; `link` and `unlink` tie a card to one or cut the tie, and `archive --rm` leaves a tombstone so the issue is not imported again.
- **Pull requests.** A linked card's worker context carries `Closes OWNER/REPO#N` to put in the PR body, so the merge closes the issue.
- **Failures.** A rejected login, a missing scope, a rate limit, a server error or a network failure pauses that board's sync only: `kanban github status` and doctor check 21 show it, with the `gh auth refresh` command when the scope is the cause. Dispatch never waits on it.

Limits: no assignee lock (`jira.claim` has no GitHub counterpart), so two boards on one repository both run an issue they both hold; a pass runs inside the daemon's housekeeping tick, so a slow `gh` call delays the other housekeeping jobs by up to a minute; the Status option names are GitHub's documented defaults and have not been checked against a live project. None of this has been run against a live GitHub repository; the gh calls are tested against `tests/fake_gh.py`.

## Uninstall

`kanban uninstall [--purge] [--force] [--yes]` refuses while any run is active; `--force` first stops them as an owner stop (the cards go to `blocked` as stopped, nothing is lost; a stop only updates the database, and each runner sees its row change within about five seconds and ends `claude` itself, which the command waits for briefly). It then unloads and deletes the LaunchAgent or systemd unit and runs `uv tool uninstall kanban-claude`. Card worktrees are never removed: any that are dirty or have commits no remote has are listed with their paths. The database, logs and scratch directories stay unless you pass `--purge --yes`, which prints their sizes and paths and then deletes `$KANBAN_HOME` and `$KANBAN_WORK` (it refuses a directory that is, or contains, your home, a `$KANBAN_HOME` without `kanban.db`, and a `$KANBAN_WORK` that holds anything but `scratch/`). The last line printed is the command that removes the plugin itself, which only a Claude Code session can run: `/plugin uninstall kanban-claude@claude-kanban-plugin`.

## Development

The runtime is a stdlib-only Python package in `plugins/kanban-claude/runtime/`, managed with `uv`. From that directory, all four must pass before a commit:

```sh
uv run ruff format . ../skills ../hooks
uv run ruff check . ../skills ../hooks
uv run mypy src
uv run pytest -q
```

`pytest` also collects the script tests under `skills/` and `hooks/` and the frontmatter tests in `tests/test_surface.py`, so that one command is the whole Python suite. The board pane has its own: `claude plugin test` and `claude plugin validate` from `plugins/kanban-claude`.

Try the CLI against a scratch board without touching a real one:

```sh
export KANBAN_HOME=$TMPDIR/kb-home KANBAN_WORK=$TMPDIR/kb-work
uv run kanban boards create demo --default-workdir "$PWD"
uv run kanban --board demo create "First card" --assignee smith --body "do the thing"
uv run kanban --board demo list
uv run kanban watch --since-start
```

The dispatcher runs as `uv run kanban daemon` (one per machine; a second one logs a line and exits 0, and a daemon whose thread died exits 1 so its supervisor restarts it). Each tick claims ready cards and starts one runner per card (`kanban run RUN_ID`, internal), which launches `claude -p` headless and ends the run from the stream. `kanban dispatch --dry-run --json` shows what the next tick would claim and why the rest are skipped; `kanban pause [--reason TEXT]` and `kanban resume` stop and restart dispatch; `kanban snapshot REPO [--ref REF]` prints a detached checkout of the repo's `origin/HEAD`, or of the branch, tag or sha `--ref` names.

Per-run files live under `$KANBAN_HOME`: `runs/<board>/<task>/run-<n>.log` and `streams/<board>/<task>/run-<n>.jsonl`. The runner needs `claude` (and `gh` for review cards) on `PATH`, or `claude.bin` in the config. A worker inherits the daemon's environment except `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and, unless listed in `worker.env_passthrough`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`, so a stray API key cannot switch it to metered billing.

With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs work, each on the card of that run, and every owner verb exits 5.

Tests isolate `KANBAN_HOME`, `KANBAN_WORK` and `HOME` under pytest's `tmp_path` and never touch a real board.

## License

MIT, see [LICENSE](LICENSE).
