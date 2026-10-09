# claude-kanban-plugin

A local kanban board for Claude Code. Cards are dispatched to headless Claude workers (`claude -p`) under a macOS LaunchAgent; the board is a SQLite file and a loopback web page. No external service.

Status: pre-release (`0.2.0.dev0`). The plugin surface (agents, skills, hooks), setup, doctor and uninstall are in place; see [CHANGELOG](plugins/kanban-claude/CHANGELOG.md).

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
5. creates the store, migrating it after a backup, and records `claude.bin`, `claude.config_dir` and the port only where they differ from what is stored, so a rerun keeps everything you configured;
6. writes the LaunchAgent `kanban-claude.daemon` (0600, in `~/Library/LaunchAgents`), unloads any old one and loads it in your `gui/<uid>` domain;
7. writes `$KANBAN_HOME/installed.json` (the version, the Claude binary and config directory, the port and the captured environment; data only, no hook executes it);
8. waits up to 10 s for the board and then starts the scout agent once, the way a worker will, to prove that the plugin resolves. It finishes by printing `kanban board` and the URL.

`--no-daemon` skips steps 6 and 8's board wait, `--port N` moves the board, `--import-hermes` runs the one-time import of the previous kanban tool afterwards, and `--yes` accepts the Sonnet fallback and a deliberate downgrade. Progress is also written to `$KANBAN_HOME/logs/upgrade.log`.

Upgrading is the same skill on a newer plugin: when the recorded version is older, setup takes an exclusive `flock` on `$KANBAN_HOME/upgrade.lock` (a second run exits 0; the lock ends with its process, so a crashed setup leaves nothing to clear), migrates the database after a backup, restarts the daemon with `launchctl kickstart -k` and rewrites `installed.json`. Runs in flight are not touched. A plugin older than the recorded version is refused unless you pass `--yes`. Nothing upgrades itself: a new plugin only announces itself at the next session start, and the daemon changes when you run the skill. Third-party marketplaces do not auto-update by default; run `/plugin update` or enable auto-update for this marketplace.

## What the plugin ships

- Agents `kanban-claude:smith`, `designer`, `scout` and `warden` run cards; `card-helper` and `review-helper` are their subagents; `card-checker` (tools `Read, Grep, Glob`) checks card bodies for the orchestrator. Each agent file lists its tools explicitly.
- Skills: `kanban` (the orchestrator rulebook and CLI reference), `kanban-code-review` (warden's procedure and `preflight.py`), `simplify-code-card` (`slop_gate.py`).
- Hooks: a lint check on the lines a builder just edited, and a session-start status check.
- The designer runs Playwright MCP, pinned to an exact version in `runtime/src/kanban_claude/mcp/designer.json`.

Agents cannot enforce a deny list for Bash commands; the runner's `--settings` carries the rules for `gh pr merge`, force pushes and `kanban grant`, and branch protection on the remote is the backstop.

## Session start

`hooks/scripts/session_start.sh` (POSIX `sh`, well under 50 ms, read-only) runs at the start of every session except a worker's. It prints nothing when the machine is not set up or all is well. When the plugin's version differs from `installed.json` it prints `kanban-claude: daemon a.b.c, plugin x.y.z — run /kanban-claude:setup to upgrade`. While dispatch is paused it prints one line of enumerated codes (`manual`, `systemic`, `auth:<config dir>`, `quota:<config dir> until <time>`) and `kanban doctor` as the way to the fix. It reads the two versions with a fixed `sed` pattern, queries the database with `sqlite3 -readonly`, never evaluates a file, never prints card or worker text, and never installs anything.

## Doctor

`kanban doctor` runs twenty independent checks and exits 1 when any one fails (a warning leaves it at 0); `--json` gives the same list as data. Each line names what is wrong and the command that fixes it. `--probe` adds the two checks that start `claude` (the scout agent resolves and the login passes, in every config directory a board uses; this costs a few cents). `--fix` repairs only what is safe: file modes (`$KANBAN_HOME` and `$KANBAN_WORK` 0700, the database, `web.secret`, `installed.json`, logs and the plist 0600), a LaunchAgent that is not loaded (it rebuilds a missing plist from `installed.json`), and a `running` card or run that disagrees with the other through the dispatcher's reconcile step. Everything else is yours to repair.

The checks, in order: 1 LaunchAgent loaded; 2 `/healthz` answers as this board; 3 tool, `installed.json`, daemon and (inside a Claude session) plugin versions match; 4 `PRAGMA quick_check`; 5 schema version; 6 a `running` card has a running run and the other way round; 7 `claude` path and version; 8 every config directory exists; 9 the plugin is enabled at user scope in each (read from `settings.json`; `--probe` proves it); 10 `gh auth status` per host the boards use, with a warning when the login is a `GH_TOKEN` variable instead of the keychain; 11 `uv`; 12 CA variables of your shell are in the plist; 13 the plist `PATH` still finds `claude`, `gh`, `git`, `uv` and the `kanban` entry point (`npx` and `node` only warn); 14 at least 2 GB free; 15 dispatch pause, logged-out accounts and quota walls with their times; 16 kept worktrees; 17 file modes; 18 a live runner for every running run; 19 branch protection; 20 Jira pairing (no board is paired until the Jira sync exists, and `--probe` does not start `jira.mcp_command` yet).

### Branch protection

Workers push with your login, so the only server-side stop for a push to the wrong branch is branch protection (GIT-08). Check 19 asks GitHub, for the default branch and one existing `feature/*` branch of every repo a board uses, through both `repos/<owner>/<repo>/rules/branches/<branch>` (rulesets) and `repos/<owner>/<repo>/branches/<branch>` (classic protection); either one counts. A repo with neither is a warning in `doctor` and a note on the board, and never delays dispatch. One that cannot be read (you lack the rights) is listed as "could not be read" in `doctor` only. The answer is cached in the database and refreshed every six hours.

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

`pytest` also collects the script tests under `skills/` and `hooks/` and the frontmatter tests in `tests/test_surface.py`, so that one command is the whole suite.

Try the CLI against a scratch board without touching a real one:

```sh
export KANBAN_HOME=$TMPDIR/kb-home KANBAN_WORK=$TMPDIR/kb-work
uv run kanban boards create demo --default-workdir "$PWD"
uv run kanban --board demo create "First card" --assignee smith --body "do the thing"
uv run kanban --board demo list
uv run kanban watch --since-start
```

Moving boards over from the previous kanban tool is a verb of its own (`uv run kanban --help` lists it). Run it with `--dry-run` first: it reads a read-only copy of the old databases, prints what would be created, dropped or renamed, and writes nothing. The real run applies everything in one transaction and refuses on conflicts unless `--force` overrides the blockers.

The dispatcher runs as `uv run kanban daemon` (one per machine; a second one logs a line and exits 0, and a daemon whose thread died exits 1 so its supervisor restarts it). Each tick claims ready cards and starts one runner per card (`kanban run RUN_ID`, internal), which launches `claude -p` headless and ends the run from the stream. `kanban dispatch --dry-run --json` shows what the next tick would claim and why the rest are skipped; `kanban pause [--reason TEXT]` and `kanban resume` stop and restart dispatch; `kanban snapshot REPO` prints a detached checkout of the repo's `origin/HEAD`.

Per-run files live under `$KANBAN_HOME`: `runs/<board>/<task>/run-<n>.log` and `streams/<board>/<task>/run-<n>.jsonl`. The runner needs `claude` (and `gh` for review cards) on `PATH`, or `claude.bin` in the config. A worker inherits the daemon's environment except `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` and, unless listed in `worker.env_passthrough`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL`, so a stray API key cannot switch it to metered billing.

The daemon also serves the web board: a loopback page on `web.port` (stdlib `http.server`, plain JS and CSS, no build step). Ask the CLI for a login link:

```sh
uv run kanban board              # opens a single-use link, valid for 60 s
uv run kanban board --no-open    # prints it instead
uv run kanban board --revoke     # invalidates every login link and session at once
```

`uv run python -m kanban_claude.web` serves the page without the daemon, for development; it then shows no dispatch skip reasons.

The link carries the token in the URL fragment, which the page exchanges for a 24 h session and then strips from the address bar. The server answers only to `127.0.0.1` and `localhost` Host headers and rejects cross-origin writes. Everything the board writes goes through the same domain functions as the CLI, as `owner:web`. Dragging a card runs the matching verb (specify, promote, hold, unblock, reopen, mark done, archive) and asks for a reason where the verb needs one; a card the dispatcher passes over shows why. The edit form can move a scratch or `dir:` card onto its board's git worktree, but a card cannot go back to scratch and `dir:` paths are set with the CLI only. Jira links, stopping a run and the worker-stream summary arrive with the Jira sync and later phases.

With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs work, each on the card of that run, and every owner verb exits 5.

Tests isolate `KANBAN_HOME`, `KANBAN_WORK` and `HOME` under pytest's `tmp_path` and never touch a real board.

## License

MIT, see [LICENSE](LICENSE).
