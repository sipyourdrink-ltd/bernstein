# Brief 0005: role task notifications

Approved boundary: SSE, not status polling. Base: origin/main 62aaed0c5; dependency 0004 (bernstein_create_task) already ships here. Dirty cluster work is excluded.

## Design

Reuse SSEBus bounded fan-out, reconnect budgets and heartbeats. Add authenticated GET /events/tasks?role=... . Filter task.created events by exact role and resolved request tenant before enqueue, and authenticated agent task allowlist when present. Agent identities may subscribe only to their own role; operators may select any role within their authorized tenant. Existing dashboard behavior stays compatible: new title-bearing events never enter its unscoped queues. Unscoped events never enter scoped task subscriptions except heartbeats.

A central best-effort post-persistence store creation listener publishes task.created with task_id/title after successful persistence for single, batch survivors, direct planner/integration creates, self-create, clearance gates and refusal follow-ups. Routing metadata stays internal. No replay, no assignment/status notifications; delivery is best effort under existing bounded-buffer pressure. Initial heartbeat acknowledges readiness. Disconnect/cancellation unsubscribes.

Standard-tier MCP bernstein_subscribe(role, timeout_seconds=30), bounded to 1–60 seconds, consumes authenticated SSE until first creation or deadline, then closes. Timeout returns empty notifications. No background persistent listener or dormant-chat wake-up. Cancellation unwinds the HTTP stream. Register input/output schemas, timeout budget and remote facade.

## Implementation plan and numbered regression tests

1. Real localhost HTTP stream: acknowledge connection, create via API, receive task_id/title in <5 seconds; fanout to two subscribers; disconnect clears subscriber_count promptly.
2. Interleave wrong-role/tenant and failed creations; deliver only matching persisted creations. Batch dedup survivors emit once; self-create emits after persistence.
3. Auth: missing/invalid token denied, other role denied to agent, task allowlist does not leak titles, tenant override denied without operator scope.
4. MCP over real HTTP SSE: advertised schema/tier, auth forwarding, first event, timeout/cancellation cleanup, no status polling; remote facade registration/dispatch.
5. Impacted isolated tests plus lint/type/import checks; update README, operator docs and API schemas; commit with validation evidence.
