# D25 — Offline Mode with Task Queuing

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When the network is unreachable (airplane, VPN issues, ISP outage), Bernstein fails immediately with a connection error. Users can't even queue work for later execution.

## Solution
- Detect network unavailability at the start of `bernstein run` by pinging the configured provider endpoint.
- When offline, queue tasks to `.sdd/queue/` as individual JSON files (one per task) with full task configuration.
- Display: "Queued 3 tasks (offline mode). Run `bernstein flush` when connected."
- Implement `bernstein flush` that processes all queued tasks in `.sdd/queue/`, executing them in order.
- After successful execution, remove the task file from the queue.
- `bernstein flush` checks network connectivity first; if still offline, report: "Still offline. Tasks remain queued (3 pending)."
- `bernstein queue list` shows pending queued tasks.
- `bernstein queue clear` removes all queued tasks with confirmation.

## Acceptance
- [ ] Running `bernstein run` while offline queues tasks instead of crashing
- [ ] The offline mode message correctly reports the number of queued tasks
- [ ] `bernstein flush` executes queued tasks when network is available
- [ ] Successfully flushed tasks are removed from the queue directory
- [ ] `bernstein queue list` shows all pending tasks with their goals
- [ ] `bernstein queue clear` removes queued tasks after confirmation
