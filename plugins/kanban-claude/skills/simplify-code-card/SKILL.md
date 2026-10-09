---
name: simplify-code-card
description: "Measured cleanup of one area of a codebase on a kanban card: find bloat, measure it, remove it without changing behaviour, prove the numbers went down, and ship one refactor PR. Use when a card's body says to run the simplify-code-card skill."
---

You make one area of the code smaller and simpler, and you prove it with numbers. You change no behaviour and no tests. The result is a single `refactor: simplify <area>` pull request, or no PR at all when there is nothing worth removing.

Keep your scratch files in `$TMPDIR/simplify-$KANBAN_TASK`. The measuring script is `scripts/slop_gate.py`; it needs `uvx` and network access on first use, to download the pinned `scb-check` release.

## 1. Scope

Read the card (`kanban context`). It names the area: a directory or a set of modules. If it names none, or names more than about fifteen source files, block with `needs_input` and propose a narrower area. One area per card.

## 2. Green first

Run the repo's whole gate (from CI, the Makefile or `pyproject.toml`) on the untouched branch. If it is red, stop and block with `needs_input`: you cannot show that a refactor preserved behaviour on top of a red gate.

## 3. Find

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/slop_gate.py" find <files in the area>
```

prints the files the measuring tool can read. Exit 3 means none of them is supported: complete the card with a summary saying there was nothing to measure.

## 4. Baseline

```bash
mkdir -p "$TMPDIR/simplify-$KANBAN_TASK"
python3 "${CLAUDE_SKILL_DIR}/scripts/slop_gate.py" snapshot "$TMPDIR/simplify-$KANBAN_TASK/before.json" <files>
```

## 5. Apply

Remove, in this order of value: unused code and imports, dead branches, commented-out code, wrappers and abstractions with a single user, copy-pasted blocks folded into one function, hand-written code the standard library already provides. Do one kind of change at a time and run the gate after each. A change that breaks the gate is reverted, not repaired.

Do not touch test files, public interfaces or behaviour. Do not rename for taste. Do not add code to remove code; a refactor that grows the total is not a simplification.

## 6. Prove

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/slop_gate.py" compare "$TMPDIR/simplify-$KANBAN_TASK/before.json" <the same files>
```

Exit 0 means no gated metric grew. Exit 1 prints each metric that did, as `metric: before -> after`; find the change that caused it and revert that change, then compare again. The numbers are measured per file, so duplication between files is not seen.

## 7. Ship

If the diff removes nothing, complete the card with `kanban complete $KANBAN_TASK --summary "nothing worth simplifying in <area>"`. Otherwise:

1. Commit as `refactor: simplify <area>`.
2. Run the whole gate on the head, push the card branch, and open the PR with `gh pr create`. The body carries the before and after numbers from the compare step, the diffstat, and the line `` Gate: `<exact command>` -> exit 0 at <head sha> ``.
3. Wait for CI with `gh pr checks <n> --watch`.
4. End with `kanban request-review $KANBAN_TASK --reviewer warden --summary "<area, numbers, PR #n>"`.

The reviewer checks that the PR removes more lines than it adds and leaves the tests alone.
