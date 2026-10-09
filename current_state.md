# Current state

Spec version 0.3; build phases per spec section 24.

| Phase | State |
|---|---|
| P1 store + CLI | done: schema, migrations, domain operations, CLI verbs, worker mode (`KANBAN_RUN_ID`, verbs run in-process on the run's card), `watch`, legacy-board importer (dry run and real import). Deferred: interactive grant activation on import (needs the role-frontmatter check) |
| P2 dispatcher + runner | done: `daemon` (tick, housekeeping and web threads, singleton flock, nudge file), `run` (runner, stream classification, quota and auth walls, cost deltas), `dispatch --dry-run`, `pause`/`resume`, `snapshot`, worktree provisioning and cleanup, merge jobs, retention; the tick loop, each run and each card fail in isolation, and a provision that keeps failing for infrastructure reasons ends blocked after a few tries. Deferred: `doctor` and `setup` (P3), the smith to warden PR smoke on a real repository |
| P3 plugin surface (agents, skills, hooks), setup | done: manifests and the repository marketplace, seven agents with explicit tool allowlists, the `kanban`, `kanban-code-review`, `simplify-code-card` and `setup` skills (`bootstrap.sh`), the lint and session-start hooks (`hooks/scripts/session_start.sh`), the pinned designer MCP config and the TST-A frontmatter tests; `launchd` (plist, env scrub, `launchctl` in the GUI domain), `setup` with upgrade, `doctor` (twenty checks, `--probe`, `--fix`; branch protection is read every six hours and fed to the board) and `uninstall` (refuses while runs are active unless `--force`, lists unsafe worktrees, `--purge --yes` deletes data). Deferred: the smith to warden PR smoke on a real repository |
| P4 web board | done: `web.py` with the static page in `web/`, login tokens, Host and CSRF checks, read and write API, board CLI verb (`--revoke`, `--no-open`), optimistic lock on `tasks.version`; the daemon serves it and feeds it the tick's skip reasons through `live`. The unprotected-repo note is fed by P3's branch-protection check. Deferred: the Jira bar and badge (no `jira_links` table before the sync), the stream summary, the last run outcome on the card face (WEB-07), the five-hour and seven-day utilisation in the header (WEB-11). The card version is checked by the store's compare-and-swap, and a PATCH that changes the role and other fields is one transaction |

Next: P6 (Jira sync and the `atlassian` role).
