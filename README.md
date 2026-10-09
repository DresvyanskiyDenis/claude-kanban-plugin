# claude-kanban-plugin

A local kanban board for Claude Code. Cards are dispatched to headless Claude workers (`claude -p`) under a macOS LaunchAgent; the board is a SQLite file and a loopback web page. No external service.

Status: under construction. Nothing is installable yet.

## Development

The runtime is a stdlib-only Python package in `plugins/kanban-claude/runtime/`, managed with `uv`. From that directory, all four must pass before a commit:

```sh
uv run ruff format
uv run ruff check
uv run mypy src
uv run pytest -q
```

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

Verbs that belong to later phases (`doctor`, `setup`, `uninstall`) exit 1 with "not available in this build" until their phase lands.

With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs work, each on the card of that run, and every owner verb exits 5.

Tests isolate `KANBAN_HOME`, `KANBAN_WORK` and `HOME` under pytest's `tmp_path` and never touch a real board.

## License

MIT, see [LICENSE](LICENSE).
