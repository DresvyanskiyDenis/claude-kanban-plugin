# Milestone branches

Work that needs two or more code cards in one repo lands as milestones. A milestone is a branch that collects its part PRs; one finalize PR takes it to the default branch, and only the owner merges that PR. The owner merges finished milestones one after another; the board keeps building the next milestone meanwhile.

`<root>` is the repo's absolute root, `<slug>` a short name for the work, `<n>` the milestone number (1, 2, … in the order they build on each other), `<default>` the repo's default branch, `<board>` the board slug, `<p>` the finalize PR number, `<prev>` the branch milestone `<n>` builds on: `<default>` for `n` = 1, else `milestone/<n-1>-<slug>`.

## Plan

One milestone, unless the request has stages that can each be merged and used on their own, or the owner names them. A later milestone builds on the one before; unrelated work is its own milestone 1.

| Branch | Used by |
|---|---|
| `milestone/<n>-<slug>` | collects the part PRs; nothing else touches it |
| `milestone/<n>-<slug>-<part>` | one part card (never name a part `final`) |
| `milestone/<n>-<slug>-final` | the finalize card; head of the one PR into `<default>` |

With more than one milestone, post the rest of the plan once, as `kanban comment <finalize 1 id> "PLAN: 2 <slug>: <goal>; 3 <slug>: <goal>"`, so a later session finds it. Create milestone 1 now; each later one is created at step 4 below.

## Steps for milestone `<n>`

1. Create the milestone branch on the remote from `<prev>`:

   ```bash
   git -C <root> fetch origin && git -C <root> push origin origin/<prev>:refs/heads/milestone/<n>-<slug>
   ```

   This is the one push you make yourself; it only creates the ref. It must exist before any part starts: a card's worktree is cut from its base, and a missing base fails the card.

2. Create one card per part. Each opens its PR against the milestone branch:

   ```bash
   kanban --board <board> create "<part title>" --assignee smith \
     --workspace worktree:<root> --branch milestone/<n>-<slug>-<part> --base milestone/<n>-<slug> \
     --max-runtime 2h --body-file -
   ```

   The Deliverable section says `PR against <repo>:milestone/<n>-<slug>`. Link parts that depend on each other with `--parent`; a child starts only after its parent's PR is merged into the milestone branch. Check each card against `snapshot --ref milestone/<n>-<slug>`.

3. Create one finalize card, a child of every part. It has no `--base`, so its branch is cut from the current `origin/<default>`:

   ```bash
   kanban --board <board> create "Finalize milestone <n>: <slug>" --assignee smith \
     --workspace worktree:<root> --branch milestone/<n>-<slug>-final --max-runtime 2h \
     --parent <part id> --parent <part id> --body-file -
   ```

   Its body names the exact gate command and tells the worker to: replay the milestone (below); merge `origin/<default>` if it moved since; write an HTML explainer of the change to `docs/explanations/<date>-<slug>-m<n>.html`; run the gate; open a PR from the `-final` branch to the default branch; then request review.

   **Replay.** Run once, on a branch that has no commit of its own yet:

   ```bash
   git fetch origin
   git cherry-pick $(git rev-list --topo-order --reverse --no-merges origin/<prev>..origin/milestone/<n>-<slug>)
   ```

   Why not merge the default into the milestone: what lands on the default branch is the `-final` branch (squashed, merged or rebased), whose commits are copies of milestone `n-1`'s. Milestone `n`, cut from `n-1`, still holds the originals, so merging the default into it conflicts wherever `n` touched a line `n-1` touched, whichever merge command the owner used; and a worker cannot force-push a rewritten branch. The replay applies only `n`'s own commits to the fresh default, so it is clean for squash, merge and rebase alike (`tests/test_milestone.py`). A conflict left over is a real clash with other work on the default branch: the worker resolves it or blocks with the question.

4. When warden approves the finalize PR, the card is `done` and the board records `awaiting_owner_merge`: approved, not merged. Do both at once:

   - Tell the owner in one line, with the merge command the repo's rule allows (last section) and the command to read the explainer. Milestones merge in order; milestone `n+1`'s finalize card does not exist until `n` is merged, so they cannot be merged out of order.

     ```bash
     git show origin/milestone/<n>-<slug>-final:docs/explanations/<file>.html > "$TMPDIR/<file>.html" && open "$TMPDIR/<file>.html"
     ```

   - Start milestone `n+1` if the plan has one: do steps 1 and 2 for it. Its parts run while `n` waits for the owner. Do not delete `milestone/<n>-<slug>`: the next replay reads it.

5. Merge the finalize PR only on the owner's explicit go (last section), or when the owner says they merged it. Confirm with `gh pr view <p> --json state` (`MERGED`), then do step 3 for milestone `n+1`. The finalize card cannot be a child of milestone `n`'s finalize card: a child of an approved PR into the default branch starts at approval (`cli.md`, Links and waiting), and nothing announces the owner's merge, so the board cannot wait for it. After the last milestone, nothing follows.

## Rules

- The daemon merges an approved part PR into the milestone branch (squash unless the card sets `--merge-method`) and records `merged`; a `PARTIAL REVIEW` approval waits for the owner. If it records `merge_failed`, tell the owner; `kanban merge-retry <id>` tries again after the cause is fixed.
- Changes the owner or warden asks for on a finalize PR go to its card with `reopen-review` or `reopen`; the worker pushes them to the `-final` branch.
- A PR into the default branch is never merged by the system or by a role.

## Merging a PR into the default branch

Any approved PR into the default branch, the finalize PR included. Only the owner merges.

1. **Read the repo's merge rule** in AGENTS.md or CONTRIBUTING.md, else from the repo settings:

   ```bash
   gh api repos/{owner}/{repo} --jq '{squash: .allow_squash_merge, merge: .allow_merge_commit, rebase: .allow_rebase_merge}'
   ```

   Tell the owner which command applies: `gh pr merge <n> --squash`, `--merge` or `--rebase`, plus `--delete-branch`. Never assume one.

2. **A warden approval is not a GitHub review.** Before you call a PR mergeable, run `gh pr view <n> --json reviewDecision,mergeStateStatus`. `REVIEW_REQUIRED`, or `BLOCKED` (a required review or check is missing), means the owner may still need a human approval: tell them. `--admin` is never the answer.

3. **After a merge, look for overlap.** Compare the open PRs' files with the merged PR's:

   ```bash
   gh pr list --json number,files
   ```

   Find each overlapping PR's card by `pr_number` in `kanban --board <board> --json list`. Send it back with the reason "rebase on the base and adapt": `reopen-review` when the card is in `review`, `reopen` when it is `done`; post the same words with `kanban comment`. Merge overlapping PRs one at a time.
