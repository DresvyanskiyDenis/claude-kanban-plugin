# Feature branches

Use a feature branch when several cards must land together, or when the default branch must not see half a feature. Each part is reviewed and merged into the feature branch; one final pull request takes the whole branch to the default branch, and only the owner merges that one.

`<root>` is the repo's absolute root, `<slug>` a short name for the feature, `<default>` the repo's default branch, `<board>` the board slug.

## Steps

1. Create the feature branch on the remote from the current default:

   ```bash
   git -C <root> fetch origin && git -C <root> push origin origin/<default>:refs/heads/feature/<slug>
   ```

   This is the one push you make yourself. It only creates the ref; the primary checkout is not touched.

2. Create one card per part. Each part works on its own branch and opens its PR against the feature branch:

   ```bash
   kanban --board <board> create "<part title>" --assignee smith \
     --workspace worktree:<root> --branch feature/<slug>-<part> --base feature/<slug> \
     --max-runtime 2h --body-file -
   ```

   The Deliverable section says `PR against <repo>:feature/<slug>`. Link parts that depend on each other with `--parent`. A child starts only after its parent's PR is merged into the feature branch.

3. Create one finalize card, a child of every part:

   ```bash
   kanban --board <board> create "Finalize feature/<slug>" --assignee smith \
     --workspace worktree:<root> --branch feature/<slug> --max-runtime 2h \
     --parent <part id> --parent <part id> --body-file -
   ```

   Its body tells the worker to: merge `origin/feature/<slug>`, then `origin/<default>`; write an HTML explainer of the change to `docs/explanations/<date>-<slug>.html`; run the gate; open a PR from `feature/<slug>` to the default branch; then request review.

4. When warden approves the finalize PR, the board records `awaiting_owner_merge`. Tell the owner, and give them the command to read the explainer:

   ```bash
   git show origin/feature/<slug>:docs/explanations/<file>.html > "$TMPDIR/<file>.html" && open "$TMPDIR/<file>.html"
   ```

5. Merge the finalize PR only on the owner's explicit go, after checking `reviewDecision` and `mergeStateStatus` with `gh pr view`:

   ```bash
   gh pr merge <n> --merge --delete-branch
   ```

## Rules

- The daemon merges an approved part PR into the feature branch (squash) and records `merged`. If it records `merge_failed`, tell the owner; `kanban merge-retry <id>` tries again after the cause is fixed.
- Only the finalize card may use the branch `feature/<slug>` itself. Part branches are `feature/<slug>-<part>`.
- A PR into the default branch is never merged by the system or by a role.
