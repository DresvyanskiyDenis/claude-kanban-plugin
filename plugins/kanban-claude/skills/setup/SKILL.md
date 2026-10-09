---
name: setup
description: Install or upgrade the kanban-claude daemon on this Mac. Use when the user runs /kanban-claude:setup, or a SessionStart line asks to run it. Installs the runtime with uv, probes claude, loads the LaunchAgent and checks that the board answers.
disable-model-invocation: true
---

# kanban-claude setup

Run exactly this and nothing else, passing along any flags the user gave (`--port N`, `--no-daemon`,
`--yes`, `--claude-bin PATH`, `--claude-config-dir DIR`):

```sh
sh "${CLAUDE_PLUGIN_ROOT}/skills/setup/scripts/bootstrap.sh"
```

The script installs the runtime this plugin ships, then runs `kanban setup`. Setup is safe to rerun and
doubles as the upgrade: when the plugin is newer than the installed daemon it migrates the database,
restarts the daemon and rewrites `installed.json`.

Setup records the Claude config directory of the session that runs it. If the user wants workers to use
another one, they pass `--claude-config-dir`.

## Reading the result

- Exit 0: relay the last lines, which name the board command (`kanban board`) and its URL.
- Any other exit: show the message as printed. Each one names its own fix command; do not improvise a
  different one and do not retry in a loop.
- "not logged in": the user logs in themselves. Tell them to type `! claude /login`, with the
  `CLAUDE_CONFIG_DIR=<dir>` prefix the message prints when it is not the default directory, then rerun.
- "scout agent does not resolve": the plugin is not enabled at user scope in that config directory. The
  message prints the `/plugin install` command to run in the Claude Code that uses it.
- Never read, print or pass on any token, key or password; setup captures none.
