# Administrative runs

Create an organizational work period without launching orchestration:

```python
bernstein_run(template="administrative", goal="Weekly sprint")
bernstein_create_task(title="Review brief", description="Review the attached brief", run_id="admin-...")
bernstein_run_status(run_id="admin-...")
```

Creation returns an `admin-...` run identifier. The MCP create-task schema
accepts only `admin-` followed by 32 lowercase hexadecimal characters for `run_id`,
matching the REST membership contract; malformed identifiers fail before HTTP. The optional goal defaults to
`Administrative run` for this template only. The normal orchestration template
continues to require a goal. Administrative runs cannot have a parent task.

The REST equivalents are `POST /administrative-runs` with `{"goal":"Weekly sprint"}`,
`POST /tasks` with `run_id`, and `GET /administrative-runs/{run_id}`. All use existing
server authentication and tenant scope. Task-scoped credentials cannot create
containers or add members; reading an aggregate requires access to every member.
Cross-tenant reads and membership are rejected.

Containers create no task rows and start no planner, agent, subprocess or worker.
Their members remain ordinary manually claimable tasks, but the orchestrator
excludes them from automatic spawning. Claim, completion and verification policy
still apply. Empty containers stay working. Failed, blocked and in-review tasks
keep a run working. Once every member is done, verified/closed, or cancelled, the
run is completed and rejects new members. Create a new container for subsequent
work. Reopening a completed run's task invalidates its certified aggregate.

The existing installation HMAC audit chain is the durable container registry.
`administrative.run_created`, `administrative.task_added`,
`administrative.task_updated` and `administrative.run_closed` events record the
lifecycle. Task snapshots include id, title, tenant and state. Every durable task
write records its administrative snapshot before the mutation returns; a failed
audit write propagates an error. A task write and an audit append use separate
files: a crash or I/O failure between them can leave a mismatch. Status then
refuses certification instead of silently repairing or reporting completion.
Reconcile the original mutation and retained chain before retrying.

Status verifies the HMAC chain, compares all snapshots with the live task store,
and returns sorted task rows, `chainVerified`, `chainHead` and `receiptHash`.
The receipt is SHA-256 of canonical JSON (sorted keys, separators `,` and `:`)
of the returned body excluding `receiptHash`. It binds the aggregate to the
verified chain head; the hash alone is not an authentication signature. Offline
verification also needs the installation audit key and retained chain/task log.
Membership and closure survive a server restart through the normal task replay
and authenticated audit log. Chain corruption or a missing task returns an error.
