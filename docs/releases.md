# Releases

Every version, dev or stable, adds one explain-diff page under `docs/explanations/` and a row here, newest first. A dev version is a PEP 440 pre-release of the next minor version (`0.4.0.devN` after 0.3.0, numbered from dev1) published with `gh release create --prerelease`; its page covers the diff from the previous tag, or from the previous installed `main` when there is none. A stable version (`0.3.0`) is published without `--prerelease`, and its page covers the diff from the previous stable tag; 0.3.0, the first, covers the whole plugin from the empty tree.

| Version | Channel | Date | Highlights | What changed |
|---|---|---|---|---|
| 0.4.0.dev1 | dev | 2026-10-20 | `kanban web`, a read-only local dashboard with every card's parameters and logs; run measurement and richer `kanban stats` (runtime, cost per role and model, cache writes, skills, MCP servers, harness numbers) with `--backfill`; two-way GitHub issue and Projects sync; `kanban gate` and leaner worker prompts; token columns fixed; schema 7 | [explainer](explanations/2026-10-20-v0.4.0.dev1.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |
| 0.3.0 | stable | 2026-10-20 | First stable release: the 0.2.0.dev0 to dev11 series as one version, with the code of 0.2.0.dev11 | [explainer](explanations/2026-10-20-v0.3.0.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |
| 0.2.0.dev11 | dev | 2026-10-20 | The `/kanban-board` pane lists triage cards: rows under the card grid and a lane in the list view, and Enter opens one; the footer counts only done cards; the runtime tests run on Linux | [explainer](explanations/2026-10-20-v0.2.0.dev11.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |
| 0.2.0.dev10 | dev | 2026-10-20 | The `/kanban-board` pane shows the lanes as columns of cards by default, sized to the pane's height, with `v` to switch to the list; needs are cards with the reason and the answering command; the footer says `ctrl+x tab: keys` until the pane has the keyboard; the merge fix texts point to `kanban merge-retry` | [explainer](explanations/2026-10-20-v0.2.0.dev10.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |
| 0.2.0.dev9 | dev | 2026-10-20 | The runner forms each dispatched role's tool list from the new `tools.<role>` config keys (`--tools`); a PreToolUse hook limits spawning to a role's own helper; the atlassian role gets only its own MCP server with `ENABLED_TOOLS` | [explainer](explanations/2026-10-20-v0.2.0.dev9.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |
| 0.2.0.dev8 | dev | 2026-10-20 | Web board removed; worker isolation, budget caps, `watch --actionable --wait`, `notify.command`, `wait-ci`, partial-review owner merges, schema v5; reworked `/kanban-board` pane | [explainer](explanations/2026-10-20-v0.2.0.dev8.html), [CHANGELOG](../plugins/kanban-claude/CHANGELOG.md) |

## Cut a release

1. On the integration branch, set the version in `plugins/kanban-claude/.claude-plugin/plugin.json` and `plugins/kanban-claude/runtime/pyproject.toml` (the marketplace entry carries no version), then run `uv lock` in `plugins/kanban-claude/runtime`.
2. Write the explain-diff page for `<previous tag>...<head>` as `docs/explanations/<YYYY-MM-DD>-v<version>.html` and add its row above.
3. Merge into `main` once the runtime gates (`ruff check`, `ruff format --check`, `mypy`, `pytest`) are green, then `git tag v<version> && git push origin main v<version>` and `gh release create v<version> --verify-tag --notes-file <notes>` (add `--prerelease` for a dev version).
