# Current state

Spec version 0.3; build phases per spec section 24.

| Phase | State |
|---|---|
| P1 store + CLI | done: schema, migrations, domain operations, CLI verbs, worker mode (`KANBAN_RUN_ID`, verbs run in-process on the run's card), `watch`, legacy-board importer (dry run and real import). Deferred: interactive grant activation on import (needs the role-frontmatter check), runner signalling on `stop` |
| P2 dispatcher + runner | done: `daemon` (tick, housekeeping and web threads, singleton flock, nudge file), `run` (runner, stream classification, quota and auth walls, cost deltas), `dispatch --dry-run`, `pause`/`resume`, `snapshot`, worktree provisioning and cleanup, merge jobs, retention; the tick loop, each run and each card fail in isolation, and a provision that keeps failing for infrastructure reasons ends blocked after a few tries. Deferred: `doctor` and `setup` (P3), the smith to warden PR smoke on a real repository |
| P3 plugin surface (agents, skills, hooks), setup | surface done: manifests, seven agents with explicit tool allowlists, the `kanban`, `kanban-code-review` and `simplify-code-card` skills, the lint and session-start hooks, the pinned designer MCP config, and the TST-A frontmatter tests. Deferred: `setup`, `doctor`, `uninstall` and the session-start script (setup builder) |
| P4 web board | done: `web.py` with the static page in `web/`, login tokens, Host and CSRF checks, read and write API, board CLI verb (`--revoke`, `--no-open`), optimistic lock on `tasks.version`; the daemon serves it and feeds it the tick's skip reasons through `live`. Deferred: the unprotected-repo note (nothing detects branch protection before P3 setup/doctor), the Jira bar and badge (no `jira_links` table before the sync), stop signalling and the stream summary, the last run outcome on the card face (WEB-07), the five-hour and seven-day utilisation in the header (WEB-11). Known gap: a PATCH that changes the role and other fields assigns first, so an edit refused afterwards keeps the new role; one transaction plus a version check in the store CAS fixes both |

Next: P3 (plugin surface and setup).
