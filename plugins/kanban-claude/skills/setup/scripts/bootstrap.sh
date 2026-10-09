#!/bin/sh
# Installs the runtime this plugin ships, then hands over to its `kanban setup` (INS-04).
set -eu

root=$(cd "$(dirname "$0")/../../.." && pwd)

command -v uv >/dev/null 2>&1 || {
    echo "kanban-claude: uv is not on PATH; install it with: brew install uv" >&2
    echo "  (without Homebrew: curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
    exit 1
}

# the version is read, never evaluated; the pattern tolerates a pretty-printed manifest and a pre-release suffix
want=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9A-Za-z.+-]*\)".*/\1/p' \
    "$root/.claude-plugin/plugin.json" | head -n 1)
[ -n "$want" ] || {
    echo "kanban-claude: no version in $root/.claude-plugin/plugin.json" >&2
    exit 1
}

uv tool install --force --quiet "$root/runtime"

# an absolute path: a bare `kanban` would run whatever comes first on PATH
kanban="$(uv tool dir --bin)/kanban"
have=$("$kanban" --version)
[ "${have#kanban }" = "$want" ] || {
    echo "kanban-claude: installed '$have' but this plugin is $want; setup stopped" >&2
    echo "  rerun /kanban-claude:setup, or: uv tool install --force '$root/runtime'" >&2
    exit 1
}

exec "$kanban" setup "$@"
