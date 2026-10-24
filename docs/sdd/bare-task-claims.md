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
