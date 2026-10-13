#!/bin/sh
# INS-08: announces a waiting upgrade or a paused dispatch; never installs or changes anything.
# Everything printed is a version number or an enumerated code, never card or worker text.

[ -z "${KANBAN_RUN_ID:-}" ] || exit 0

home=${KANBAN_HOME:-$HOME/.local/share/kanban-claude}
root=${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}

# the version is read with a fixed pattern, never evaluated; a pre-release suffix is allowed
version() {
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([0-9][0-9A-Za-z.+-]*\)".*/\1/p' "$1" 2>/dev/null |
        head -n 1
}

daemon=$(version "$home/installed.json")
[ -n "$daemon" ] || exit 0

plugin=$(version "$root/.claude-plugin/plugin.json")
if [ -n "$plugin" ] && [ "$plugin" != "$daemon" ]; then
    echo "kanban-claude: daemon $daemon, plugin $plugin — new roles and skills run on the old daemon, CLI and protocol until /kanban-claude:setup; run it between cards (a running card finishes on old code)"
fi

[ -f "$home/kanban.db" ] || exit 0
command -v sqlite3 >/dev/null 2>&1 || exit 0

# one code per row; the reason is mapped, not echoed, and an account is named by its directory only
codes=$(sqlite3 -readonly "$home/kanban.db" "
    SELECT CASE json_extract(value, '\$.reason') WHEN 'systemic' THEN 'systemic' ELSE 'manual' END
      FROM state WHERE key = 'dispatch_paused'
    UNION ALL
    SELECT 'auth:' || substr(key, 9) FROM state
     WHERE key LIKE 'account:%' AND json_extract(value, '\$.auth_paused') IS NOT NULL
    UNION ALL
    SELECT 'quota:' || substr(key, 9) || ' until '
           || strftime('%Y-%m-%d %H:%M', max(ifnull(json_extract(value, '\$.hard_until'), 0),
                                              ifnull(json_extract(value, '\$.soft_until'), 0)),
                       'unixepoch', 'localtime')
      FROM state WHERE key LIKE 'account:%'
       AND max(ifnull(json_extract(value, '\$.hard_until'), 0),
               ifnull(json_extract(value, '\$.soft_until'), 0)) > CAST(strftime('%s', 'now') AS INTEGER)
" 2>/dev/null | LC_ALL=C tr -cd ' -~\n')

list=
while IFS= read -r code; do
    [ -n "$code" ] && list="${list:+$list, }$code"
done <<EOF2
$codes
EOF2
[ -z "$list" ] || echo "kanban-claude: dispatch paused ($list) — run kanban doctor"
exit 0
