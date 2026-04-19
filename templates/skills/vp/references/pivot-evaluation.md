# Pivot evaluation

When a pivot signal is routed to you (severity=high or affects 3+ tickets):

1. **Read the pivot signal** — understand what was discovered, by whom,
   during which task.
2. **Assess affected tickets** — read each to understand current
   assumptions.
3. **Decide**:
   - **APPROVE** — the pivot is valid; update affected ticket descriptions
     and priorities as needed.
   - **REJECT** — the pivot is noise or premature; add a note explaining
     why and proceed as-is.
   - **ESCALATE** — implications beyond your authority (budget, timeline,
     external stakeholders); pause affected work and notify human.
4. **Record your decision** — write to `.sdd/signals/vp_decisions.jsonl`.
5. **Log ticket changes** — priority or scope changes go to
   `.sdd/signals/ticket_changes.jsonl` with before/after values.

## Evaluation criteria
- Does the discovery invalidate core assumptions of the affected tickets?
- Is the proposed action cheaper than continuing with stale assumptions?
- How many in-progress agents would need to be interrupted?
- Is there a simpler mitigation that avoids a full pivot?

## Ticket mutation rules
- Only the VP can change ticket priority or scope.
- Any role can add context / notes to a ticket.
- Closed / done tickets are never modified.
- All changes logged with before / after values.
