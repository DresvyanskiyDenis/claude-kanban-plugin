---
name: setup
description: Install or upgrade the kanban-claude daemon on this Mac. Use when the user runs /kanban-claude:setup, or a SessionStart line asks to run it. Installs the runtime with uv, probes claude, loads the LaunchAgent and checks that the daemon holds its lock.
disable-model-invocation: true
---

# kanban-claude setup

Run exactly this and nothing else, passing along any flags the user gave (`--no-daemon`, `--yes`, `--claude-bin PATH`, `--claude-config-dir DIR`):

```sh
sh "${CLAUDE_PLUGIN_ROOT}/skills/setup/scripts/bootstrap.sh"
```

The script installs the runtime this plugin ships, then runs `kanban setup`. Setup is safe to rerun and doubles as the upgrade: when the plugin is newer than the installed daemon it migrates the database, restarts the daemon and rewrites `installed.json`.

Setup records the Claude config directory of the session that runs it. If the user wants workers to use another one, they pass `--claude-config-dir`.

## When to upgrade

The plugin updates its role prompts and skills for new runs; a resumed session keeps the ones it started with. The daemon, the `kanban` CLI the workers call and the run protocol (`protocol.md`) are the installed runtime and stay old until setup runs, so a new skill may name a flag the old CLI lacks.

Setup restarts the daemon (`launchctl kickstart -k`); the daemon and dispatch are down for a moment. Runners are detached processes: the new daemon finds each live one by pid and start time and leaves it alone, so a running card is not stopped. It finishes on the runner code it already loaded, and its `kanban` calls use the new CLI. Setup does not check for running cards, so upgrade between cards, when `kanban --board <slug> list --status running` is empty.

## Reading the result

- Exit 0: relay the last lines, then add the owner permissions below.
- Any other exit: show the message as printed. Each one names its own fix command; do not improvise a different one and do not retry in a loop.
- "not logged in": the user logs in themselves. Tell them to type `! claude /login`, with the `CLAUDE_CONFIG_DIR=<dir>` prefix the message prints when it is not the default directory, then rerun.
- "scout agent does not resolve": the plugin is not enabled at user scope in that config directory. The message prints the `/plugin install` command to run in the Claude Code that uses it.
- Never read, print or pass on any token, key or password; setup captures none.

## Owner permissions

After exit 0, merge these read-only owner verbs into `permissions.allow` of the project's `.claude/settings.json` (create the file if it is missing, keep every existing entry, skip a rule that is already there) and tell the owner what you added. Each verb has a bare rule and a `*` rule, because the bare command does not match `kanban show *`. Never add a verb that writes.

```json
{"permissions": {"allow": ["Bash(kanban show)", "Bash(kanban show *)", "Bash(kanban list)", "Bash(kanban list *)", "Bash(kanban watch)", "Bash(kanban watch *)", "Bash(kanban digest)", "Bash(kanban digest *)", "Bash(kanban stats)", "Bash(kanban stats *)"]}}
```
