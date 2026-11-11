# Bare-task claim ownership

`core/tasks/store_claim.py` projects TaskStore rows for signed MCP receipts.
TaskStore remains the owning store for HTTP/MCP-created tasks, including the
atomic claimed transition and authoritative dependency check. The projection
is read-only; creation, claiming and completion never mirror these rows into
`task-backlog.json`. Legacy backlog rows remain owned by their existing atomic
claim primitive.

The receipt route chooses one source, hashes its post-claim snapshot, and signs
the existing claim receipt. TaskStore snapshots are tenant- and task-scoped;
`task_claim_snapshot` is the deterministic offline reprojection function.
TaskStore claims use the existing `by_id` audit path, which participates in the
store's release-receipt lifecycle; legacy backlog claims retain `mcp_claim`.
The HTTP create route records declared fields in the application-owned HMAC
chain, independently of an orchestrator's optional lifecycle listener.

## Legacy tokenless completion and stale claims (#6644)

The legacy `POST /tasks/{task_id}/complete` route accepts completion reports
without a claim identity. This leaves a stale-report gap when a task is released
and reclaimed by another worker.

The regression sequence is:

1. Worker A claims a task.
2. Worker A releases the task.
3. Worker B claims the task.
4. A delayed, tokenless completion report from A arrives.
5. The current route accepts the report with HTTP 200 and transitions the task
   to `done`, while the stored `claimed_by_session` remains `worker-b`.

The test
`test_tokenless_completion_after_reclaim_documents_legacy_gap` records this
existing behavior. It is a characterization of the compatibility gap, not a
claim that stale tokenless reports are safely rejected.

A version or state snapshot captured when the completion request arrives cannot
identify the original claim that produced the report. By that time, the task
may already belong to a newer claimant. Without claim identity carried by the
report, the server cannot distinguish a delayed report from an earlier attempt
from a report belonging to the current attempt.

This first regression slice intentionally leaves tokenless admission and public
request fields unchanged. It adds tests and documentation only; it does not
implement a production fence or define a legacy-caller migration policy.

Claim identity and legacy migration require a separate design coordinated with
the server-side claim fence in PR #6628. Durable terminal-report outbox work is
outside this slice.
