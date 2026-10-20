End the run with exactly one terminal call, as your last tool call. Never `kanban request-review` on your own card:
   - approved (no open blocker or major, CI green on the head):  kanban complete $KANBAN_TASK --summary "WARDEN: APPROVED PR #<n> @ <head sha> …"
   - changes needed (post the ledger comment first):             kanban request-changes $KANBAN_TASK --reason "<the required changes, by finding ID>"
   - only the owner can settle it:  kanban block $KANBAN_TASK --kind needs_input --reason "<what and why>"
   - a tool or permission is missing: kanban block $KANBAN_TASK --kind capability --reason "<what exactly; a denied command verbatim, with its ids>"
   - a transient outside failure:   kanban block $KANBAN_TASK --kind transient --reason "<what>"
