# Current state

Spec version 0.3; build phases per spec section 24.

| Phase | State |
|---|---|
| P1 store + CLI | done: schema, migrations, domain operations, CLI verbs, worker mode (`KANBAN_RUN_ID`, verbs run in-process on the run's card), `watch`, legacy-board importer (dry run and real import). Deferred: interactive grant activation on import (needs the role-frontmatter check), runner signalling on `stop` |
| P2 dispatcher + runner | not started |
| P3 web board | not started |
| P4 plugin (agents, skills, hooks), setup | not started |

Next: P2 (dispatcher and runner).
