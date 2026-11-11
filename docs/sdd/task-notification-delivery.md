# Task creation notification boundary

`TaskStore` invokes best-effort creation listeners after durable append in
`create`, `create_batch`, `create_gate_task`, `create_gate_with_edges` and
`create_refusal_follow_ups`. Replay and updates do not invoke them. Listener
failure is logged and cannot fail a committed mutation. Batch callbacks run
per committed row so a later disk error cannot hide an earlier durable row.

`create_app` registers a creation listener on its own store and removes it at
lifespan shutdown. It publishes `SSEEvent.task_created` through the app's
`SSEBus`, carrying role, tenant and task ID as internal routing metadata.
Only explicitly scoped task subscriptions receive these title-bearing events;
legacy dashboard and WebSocket subscribers never receive them. Filtering occurs
before queue insertion, preserving bounded queue and per-IP accounting.

`GET /events/tasks` resolves tenant authority and authenticates role access
before registration; identity task allowlists further narrow delivery. A finalizer
unsubscribes on stream cancellation/disconnect. Heartbeats preserve idle streams.
`bernstein_subscribe` consumes this stream within one total deadline, using the
configured backend credential. Its context managers close connections on event,
timeout, cancellation or error. It does not retain subscriptions across calls.

See [operator semantics](../operations/notifications.md#role-task-subscriptions-sse)
for delivery limits and recovery after a lost event.
