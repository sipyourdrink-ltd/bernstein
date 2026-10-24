# Bare tasks

Use `bernstein_create_task` to queue a brief for a worker without starting a
planner or agent. The equivalent REST call is `POST /api/v1/tasks` (legacy
`POST /tasks` remains supported):

```json
{
  "title": "Prepare an internal brief",
  "description": "Summarize the reviewed findings in the internal report.",
  "role": "docs",
  "priority": 2,
  "scope": "small",
  "depends_on": []
}
```

Priority is ascending: 1 critical, 2 normal, 3 low. Scope is `small`, `medium`,
or `large`. Dependency IDs must already exist. The role defaults to `auto`
(local classification); explicit roles follow the configured role policy.
Creation preserves optional model/effort hints and existing risk/approval gates.
The server appends `task.created` to its install-local HMAC audit chain, including
the task ID, title, description, role, priority, scope and dependency IDs.

Call `bernstein_claim` with `claimer_id` and the matching `role`, then report
work with `bernstein_complete`. A dependency must actually be complete in the
task store and appear in the claim's `completed_ids`; naming an unfinished ID
does not bypass the dependency gate. Claiming updates the original TaskStore
row atomically to `claimed`. It creates no second backlog row. Completion uses
the ordinary task completion and approval policy.

Legacy `task-backlog.json` claims retain precedence for the default tenant.
Non-default tenants never access this unscoped legacy backlog. TaskStore
selection and receipt snapshots are limited to the authenticated tenant and
the caller's task scope; agent tokens cannot change their issued role.

TaskStore receipts add `backlogSource: "task_store"` and `backlogSnapshot`.
The signed `backlogHead` binds that snapshot. Offline callers can pass it to
`verify_claim_receipt(ClaimReceipt.from_wire(wire), wire["backlogSnapshot"], chain)`.
To independently reproject current disk state, load TaskStore and call
`bernstein.core.tasks.store_claim.task_claim_snapshot(store, tenant_id, task_ids)`
with the same authorized scope. As with legacy receipts, later mutations change
the current head; retain the returned snapshot for historical verification.
Legacy receipts keep their existing shape and verification inputs.
