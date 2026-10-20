# claude-kanban-plugin

A local kanban board for Claude Code. Cards are dispatched to headless Claude workers (`claude -p`) under a macOS LaunchAgent; the board is a SQLite file you read with the `kanban` CLI and the `/kanban-board` pane. No external service.

Status: pre-release (`0.2.0.dev8`). The plugin surface (agents, skills, hooks), setup, doctor and uninstall are in place; see [CHANGELOG](plugins/kanban-claude/CHANGELOG.md).

## Install

macOS only. You need `uv` (`brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`), `git`, `gh` and Claude Code 2.1.259 or newer (checked on 2.1.295); `npx` is optional (only the designer uses it). If your repositories live on a GitHub Enterprise host, log `gh` in there first: `gh auth login --hostname <your GHE host>`. Behind a proxy that re-signs TLS, export your CA bundle in `~/.zshrc` and run setup from a new terminal:

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

1. checks the host (macOS, `uv`, `git`, `gh`) and `claude --version`, and refuses a `claude` older than 2.1.259 with `claude update` as the fix;
2. picks the Claude config directory: `--claude-config-dir`, else the one a previous setup recorded, else the session's `CLAUDE_CONFIG_DIR`, else `~/.claude`. The default directory is never exported to a worker; any other one is;
3. captures the environment for launchd: `PATH` (the directories of `claude`, `gh`, `git`, `uv`, `npx`, `node` plus the system ones), the CA bundle and proxy variables and `LANG`. A variable whose name contains `TOKEN`, `KEY`, `SECRET`, `PASSWORD` or `AUTH` is never captured unless you list it in `worker.env_passthrough`. A proxy URL keeps its password in the plist (the proxy needs it) and is masked as `http://***@host` in every log line;
4. probes `claude` with the flags workers use (auto permission mode, your review model) under that captured environment alone, and explains a failure: not logged in (prints the login command), no Opus on this seat (`kanban config set models.review sonnet`, or rerun with `--yes`), or an organisation that disables auto mode (setup stops);
5. creates the store, migrating it after a backup, and records `claude.bin` and `claude.config_dir` only where they differ from what is stored, so a rerun keeps everything you configured;
6. writes the LaunchAgent `kanban-claude.daemon` (0600, in `~/Library/LaunchAgents`), unloads any old one and loads it in your `gui/<uid>` domain;
7. writes `$KANBAN_HOME/installed.json` (the version, the Claude binary and config directory and the captured environment; data only, no hook executes it);
8. waits up to 10 s for the daemon to hold `$KANBAN_HOME/daemon.lock` and then starts the scout agent once, the way a worker will, to prove that the plugin resolves. It finishes with `kanban-claude X is set up.`

`--no-daemon` skips step 6 and step 8's daemon wait, and `--yes` accepts the Sonnet fallback and a deliberate downgrade. Progress is also written to `$KANBAN_HOME/logs/upgrade.log`.

Upgrading is the same skill on a newer plugin: when the recorded version is older, setup takes an exclusive `flock` on `$KANBAN_HOME/upgrade.lock` (a second run exits 0; the lock ends with its process, so a crashed setup leaves nothing to clear), migrates the database after a backup, restarts the daemon with `launchctl kickstart -k` and rewrites `installed.json`. Runs in flight are not touched. A plugin older than the recorded version is refused unless you pass `--yes`. Nothing upgrades itself: a new plugin only announces itself at the next session start, and the daemon changes when you run the skill. Third-party marketplaces do not auto-update by default; run `/plugin update` or enable auto-update for this marketplace.

## What the plugin ships

- Agents `kanban-claude:smith`, `designer`, `scout` and `warden` run cards; `card-helper` and `review-helper` are their subagents; `card-checker` (tools `Read, Grep, Glob`) checks card bodies for the orchestrator. The optional `atlassian` role makes one exact Jira or Confluence write per card (see Jira sync). Each agent file lists its tools explicitly.
- Skills: `kanban` (the orchestrator rulebook and CLI reference), `kanban-code-review` (warden's procedure and `preflight.py`), `simplify-code-card` (`slop_gate.py`).
- Hooks: a lint check on the lines a builder just edited, a session-start status check, and the board pane mod.
- The designer runs Playwright MCP, pinned to an exact version in `runtime/src/kanban_claude/mcp/designer.json`.

Each worker session is named `kanban <card-id> <role>`. Ask your session to steer a running card and it sends your words there and records them on the card; a worker asks back only by blocking its card with `needs_input`.

Agents cannot enforce a deny list for Bash commands; the runner's `--settings` carries the rules for `gh pr merge`, force pushes and `kanban grant`, and branch protection on the remote is the backstop.

Workers do not inherit your own setup. In a run, every plugin your account enables is off except kanban-claude, its declared dependency and the names in `worker.plugin_allowlist`. MCP servers come only from a private per-run file (`--strict-mcp-config`, mode 0600) that holds the role's servers plus `worker.mcp_allowlist`, copied from the config directory's `.claude.json`. Each role also gets its own permission denies: no subagent but its helper, and no edits for warden, atlassian and (except its report) scout. Each card's workers share one `TMPDIR` under `$KANBAN_WORK/tmp/<card>`.

Spend is capped at claim: `budget.max_usd_per_card` (default 15 USD, 0 turns it off) and `budget.max_usd_per_board_day` (off by default). A card over a cap is blocked with a comment naming the key to raise.

To follow the board from a session, run `kanban watch --actionable --wait --board <slug>` as a background command. It blocks until something needs you, prints it and exits; run it again with `--since <last event id>`. `notify.command` (an argv list, for example `kanban config set notify.command '["/abs/path/script"]'`) gets each such event line on stdin from the daemon, with no session open.

## Session start

`hooks/scripts/session_start.sh` (POSIX `sh`, well under 50 ms, read-only) runs at the start of every session except a worker's. It prints nothing when the machine is not set up or all is well. When the plugin's version differs from `installed.json` it prints `kanban-claude: daemon a.b.c, plugin x.y.z — run /kanban-claude:setup to upgrade`. While dispatch is paused it prints one line of enumerated codes (`manual`, `systemic`, `auth:<config dir>`, `quota:<config dir> until <time>`) and `kanban doctor` as the way to the fix. It reads the two versions with a fixed `sed` pattern, queries the database with `sqlite3 -readonly`, never evaluates a file, never prints card or worker text, and never installs anything.

## Board pane

`/kanban-board` docks a short board beside the transcript. Under the board's name and its open count, a bar across the pane shows the board's cards by status, done on the left and triage on the right. Below it, each lane with cards (running, in review, blocked, up next) is a round card in its status color, listing up to four cards, then `+N more`, each with its priority, role and the age of its last change. The footer is the bar's legend for done and triage. Colors are Claude Code theme keys, so the pane follows your theme. While the pane has the keyboard (click it, or Ctrl+X then Tab), `r` reloads it. It reads `kanban --json list --sort priority` for the board of the session's folder every 15 s while the pane is open, and only then. `/kanban-board SLUG` pins another board; `/kanban-board close` closes it. A pane left open reopens in the next session; one closed by hand stays closed. Card workers never load it. `kanban list`, `show` and `watch` are the full view.

It is a Claude Code mod and needs Claude Code 2.1.287 or later; an organization's managed settings can disable mods. Narrower than 144 columns, Claude Code places a pane it was not asked for only from 110 columns, so a reopened pane may wait until the terminal is wide enough.

## Doctor

`kanban doctor` runs twenty independent checks and exits 1 when any one fails (a warning leaves it at 0); `--json` gives the same list as data. Each line names what is wrong and the command that fixes it. `--probe` adds the two checks that start `claude` (the scout agent resolves and the login passes, in every config directory a board uses; this costs a few cents) and, for check 20, starts `jira.mcp_command` and confirms it lists the seven Jira tools. `--fix` repairs only what is safe: file modes (`$KANBAN_HOME` and `$KANBAN_WORK` 0700, the database, `installed.json`, logs and the plist 0600), a LaunchAgent that is not loaded (it rebuilds a missing plist from `installed.json`), and a `running` card or run that disagrees with the other through the dispatcher's reconcile step. Everything else is yours to repair.

The checks, in order: 1 LaunchAgent loaded; 2 the daemon holds `daemon.lock` (its pid and version are read from it); 3 tool, `installed.json`, daemon and (inside a Claude session) plugin versions match; 4 `PRAGMA quick_check`; 5 schema version; 6 a `running` card has a running run and the other way round; 7 `claude` path and version; 8 every config directory exists; 9 the plugin is enabled at user scope in each (read from `settings.json`; `--probe` proves it); 10 `gh auth status` per host the boards use, with a warning when the login is a `GH_TOKEN` variable instead of the keychain; 11 `uv`; 12 CA variables of your shell are in the plist; 13 the plist `PATH` still finds `claude`, `gh`, `git`, `uv` and the `kanban` entry point (`npx` and `node` only warn); 14 at least 2 GB free; 15 dispatch pause, logged-out accounts and quota walls with their times; 16 kept worktrees; 17 file modes; 18 a live runner for every running run; 19 branch protection; 20 Jira pairing (a paired board needs `jira.assignee` and `jira.mcp_command`; a paused sync is a warning that names the reason, and so is `jira.claim` with a `jira.assignee` that matches none of the assignees Jira has reported).

### Branch protection

Workers push with your login, so the only server-side stop for a push to the wrong branch is branch protection (GIT-08). Check 19 asks GitHub, for the default branch and one existing `feature/*` branch of every repo a board uses, through both `repos/<owner>/<repo>/rules/branches/<branch>` (rulesets) and `repos/<owner>/<repo>/branches/<branch>` (classic protection); either one counts. A repo with neither is a warning in `doctor`, and never delays dispatch. One that cannot be read (you lack the rights) is listed as "could not be read" in `doctor` only. The answer is cached in the database and refreshed every six hours.

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

For `feature/*`, leave the pull request rule off: a part card of a feature pushes its own branch `feature/<slug>-<part>`, which matches the same pattern, so requiring a pull request there would stop the worker it is meant to serve. Forbid deleting and rewriting instead:

```sh
gh api --method POST repos/<owner>/<repo>/rulesets --input - <<'JSON'
{
  "name": "kanban-claude: feature branches",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/heads/feature/*"], "exclude": []}},
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
- **The `atlassian` role.** `kanban config set roles.atlassian.enabled true` makes cards assignable to it once `jira.mcp_command` is set too; `dispatch.roles` is not rewritten. Its agent lists nine read tools and the eight write tools by exact name, pinned to `mcp-atlassian` 0.23.1 (a denylist is complete only for a pinned version), and writes only with a grant naming the exact tool (`kanban grant ID --allow mcp__atlassian__jira_add_comment --approved "..."`), and the card body must give the exact key or page and the exact content.

The tool and argument names, the result shapes and the description round trip come from the `mcp-atlassian` version you run. Run `kanban doctor --probe`, then pair one board with a test project before a real one.

## Uninstall

`kanban uninstall [--purge] [--force] [--yes]` refuses while any run is active; `--force` first stops them as an owner stop (the cards go to `blocked` as stopped, nothing is lost; a stop only updates the database, and each runner sees its row change within about five seconds and ends `claude` itself, which the command waits for briefly). It then unloads and deletes the LaunchAgent and runs `uv tool uninstall kanban-claude`. Card worktrees are never removed: any that are dirty or have commits no remote has are listed with their paths. The database, logs and scratch directories stay unless you pass `--purge --yes`, which prints their sizes and paths and then deletes `$KANBAN_HOME` and `$KANBAN_WORK` (it refuses a directory that is, or contains, your home, a `$KANBAN_HOME` without `kanban.db`, and a `$KANBAN_WORK` that holds anything but `scratch/`). The last line printed is the command that removes the plugin itself, which only a Claude Code session can run: `/plugin uninstall kanban-claude@claude-kanban-plugin`.

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
