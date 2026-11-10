# Bare task creation integration

Base: origin/main 62aaed0c5. Existing HTTP POST /tasks (also /api/v1/tasks)
and MCP bernstein_create_task already create without a planner. The regression
is that bernstein_claim reads only task-backlog.json while creation and
completion use TaskStore. No task reaches the claimed state through that loop.

## Design

Keep TaskStore as the single source of truth for HTTP-created tasks. Preserve
legacy JSON backlog claims and receipt signatures. If no eligible legacy row
exists, select a tenant- and worker-scoped TaskStore candidate using existing
claim filters and claim_by_id's atomic dependency/status/version checks. A
TaskStore claim uses its real CLAIMED transition and existing release/audit
lifecycle. It never writes a second mutable backlog row. Signed receipts hash
a deterministic projection of the selected source. Priority remains ascending
(1 critical), scope is the existing small/medium/large enum, dependencies remain
TaskStore IDs and are checked against authoritative completion state. Caller
completed_ids cannot override that check. Preserve approval/model/effort fields
and existing completion approval gates. Non-default tenants cannot claim the
unscoped legacy backlog. Reuse existing creation and HMAC audit mechanisms.

## Numbered test matrix

1. Real HTTP /api/v1/tasks create -> signed claim -> complete; metadata preserved.
2. Real MCP create -> claim -> complete through ASGI HTTP, without planner/spawn.
3. Wrong role refuses; declared dependencies gate until actual completion.
4. Priority ordering, scope, model/effort survive acquisition; no dual-store row.
5. Tenant and task/role-scoped tokens cannot acquire other tasks.
6. Creation HMAC audit event binds task ID and fields; tampering fails verification.
7. Invalid scope/dependency/session input has no task, backlog, or audit effects.
8. Existing legacy backlog receipt and offline-verifier tests stay green.

Run focused isolated tests, Ruff, type checks and architecture lint. Update
README/operator docs and release fragment. Public request schema is unchanged;
check OpenAPI snapshot drift rather than introduce gratuitous schema changes.
