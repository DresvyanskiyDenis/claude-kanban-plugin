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

Verbs that need the dispatcher, the web board or setup (`dispatch`, `daemon`, `run`, `snapshot`, `doctor`, `setup`, `uninstall`, `board`) exit 1 with "not available in this build" until their phase lands.

With `KANBAN_RUN_ID` set the CLI is in worker mode: only the worker verbs work, each on the card of that run, and every owner verb exits 5.

Tests isolate `KANBAN_HOME`, `KANBAN_WORK` and `HOME` under pytest's `tmp_path` and never touch a real board.

## License

MIT, see [LICENSE](LICENSE).
