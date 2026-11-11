# Administrative runs

Root cause: `bernstein_run` unconditionally creates orchestration work, and run status only projects orchestration journals. There is no durable container or task membership record. Bare task creation is already on origin/main (0004).

Implement an administrative registry using the existing HMAC chain as its durable source, integrate task updates through the existing store listener, expose tenant-scoped REST endpoints, and route the MCP template explicitly. Keep ordinary orchestration unchanged. Verify chain integrity and live task snapshots before certifying an aggregate.

## Numbered failing test matrix (before implementation)

1. Creating an empty administrative run records creation, stays working, creates no task and invokes no orchestration.
2. Adding ordinary bare tasks records membership; status lists every task/state with chain head and deterministic receipt hash.
3. Only done/cancelled tasks close a nonempty run; failed/blocked/review states keep it open; closure is recorded once.
4. Registry reconstruction from the chain preserves membership, states and closed status.
5. Tampered chains and live/audit snapshot mismatches refuse certification.
6. Unknown/closed runs reject additions; tenant mismatches cannot read or attach tasks.
7. Concurrent additions/completions serialize membership and closure; adding after closure fails.
8. MCP administrative template and create-task run_id reach the REST surface without invoking the normal run implementation; default template retains old behavior.

Run targeted lifecycle/MCP tests, existing task/status regressions, schema drift checks, lint and formatting. Regenerate public schema and update README/operator documentation.
