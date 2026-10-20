End the run with exactly one terminal call, as your last tool call:
   - finished, no code changed:     kanban complete $KANBAN_TASK --summary "<what, how verified, where>"
   - code changed:                  push the branch, open the PR, run the gate, CI green on the head, then
                                    kanban request-review $KANBAN_TASK --reviewer warden --summary "<what, how verified, branch, PR #n>"
   - you need the owner:            kanban block $KANBAN_TASK --kind needs_input --reason "<exact question, proposed default>"
   - a tool or permission is missing: kanban block $KANBAN_TASK --kind capability --reason "<what exactly; a denied command verbatim, with its ids>"
   - a transient outside failure:   kanban block $KANBAN_TASK --kind transient --reason "<what>"
