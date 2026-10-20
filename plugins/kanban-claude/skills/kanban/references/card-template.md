# Card template

Every card body has these six sections, in this order. A role starts with nothing but this text, the repo and the board's context, so each section must stand on its own.

```markdown
## Goal
One sentence: the single outcome this card delivers.

## Context
Absolute paths, links and keys the work needs. Name files and notes; do not state conclusions the
role cannot check. Why the work matters, and anything the owner said that the repo does not show.
For smith and designer: `Files to touch:` the absolute paths the change edits, tests included (`new` marks a file to create).

## Acceptance gate
An exact command that exits 0 (`uv run pytest -q && uv run ruff check`), or evidence a reviewer
can check (a screenshot at 375 px, a report with sources). Never "works well". For smith and
designer: every gate command and the acceptance check, so the builder does not hunt for them.

## Constraints / never do
What bounds the work: files that must not change, interfaces that must stay, budgets, deadlines.

## Out of scope
What a reader might assume is included and is not. Paths in backticks are checked against the PR.

## Deliverable
Where the result lands: `PR against <repo>:<base branch>`, or a file at an absolute path, or a
summary with sources.
```

## Rules of thumb

- A smith or designer card names the files to touch and the exact gate and acceptance commands; each turn the builder spends finding them is paid for again. The card-checker adds what is missing.
- One outcome per card. If the Goal needs "and", it is two cards.
- A single run must be able to finish it. If it cannot, split it and link the parts with `--parent`.
- The gate is the repo's own (CI workflow, Makefile, `pyproject.toml`). Never invent one in the card.
- Paths are absolute and use the real repo root, never a snapshot or worktree path.
- A parent card's PR and summary reach the child in its run context; do not ask the child to look them up.
- Do not add a constraint about push or PR approval. Roles push their own branch and open a PR; they never merge.
- Put each `Out of scope` path in backticks, one per bullet, so the reviewer's pre-flight can match it against the changed files.

## Commands that need a grant

When the work runs a live write the deny rules refuse (a `gh` call that changes something, an upload, a deploy step), list the exact commands in `## Context` under "Commands that need a grant", one per bullet, spelled as the worker will run them. The list is not an approval; only the grant line in the run is.

Before `create`, turn each bullet into a `--grant '<exact rule>'` flag and quote the owner's approval once: `--approved "<owner's words> · <source> · <date>"`. No owner words, no grant: ask first.

- Exact rules only. A prefix, `:*` or any wildcard is refused.
- One grant per exact command. Cards that differ in one argument need one grant each.
- An id that exists only after a submit cannot be listed. Prefer one command that waits for the run in a single call, so no id is needed. Otherwise the first run is denied and blocks `capability`; the first-denial flow is in `cli.md`.

## Role defaults

| Role | Deliverable usually | Default runtime |
|---|---|---|
| smith, designer | `PR against <repo>:<base>` | 2h |
| scout | `reports/<slug>.md` and a summary with sources | 30m |
| warden | the verdict on a named PR | 45m |
