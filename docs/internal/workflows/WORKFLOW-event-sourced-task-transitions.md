# WORKFLOW: Event-Sourced Task Transitions (CQRS)

**Version**: 0.1
**Date**: 2026-04-04
**Author**: Workflow Architect
**Status**: Draft
**Implements**: Task 14a4add4 — Event-sourced task transitions (CQRS)

---

## Overview

Replace the current `TaskStatus` enum-based mutable state machine with an append-only event log per task. Task state is derived by replaying (folding over) an ordered event sequence rather than mutating `task.status` directly. This enables full audit trails, arbitrary point-in-time state reconstruction, and decouples transition logic from storage format.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Orchestrator | Emits `TaskCreated`, `TaskStarted`, scheduling events |
| TaskStore | Writes events to the event log, computes projections |
| Agent (via HTTP API) | Triggers `TaskClaimed`, `TaskCompleted`, `TaskFailed` via API calls |
| Janitor | Triggers `TaskVerified`, `TaskClosed` after post-completion checks |
| Plan Loader | Emits `TaskCreated` with status=PLANNED for plan-mode tasks |
| Lifecycle module | Validates transition legality before event is appended |
| Audit Log | Receives events for HMAC-chained tamper-evident persistence (existing) |
| Prometheus | Receives transition reason counters (existing) |
| Listeners | Existing `LifecycleEvent` subscribers receive events unchanged |

---

## Current State Analysis

### What exists today

The current system (`lifecycle.py`, `task_store.py`, `models.py`) uses:

1. **`TaskStatus` enum** (12 states): `PLANNED`, `OPEN`, `CLAIMED`, `IN_PROGRESS`, `DONE`, `CLOSED`, `FAILED`, `BLOCKED`, `WAITING_FOR_SUBTASKS`, `CANCELLED`, `ORPHANED`, `PENDING_APPROVAL`
2. **`TASK_TRANSITIONS` dict** — a `(from, to) -> guard` table (30 transitions) in `lifecycle.py`
3. **`transition_task()` function** — validates against the table, mutates `task.status`, emits a `LifecycleEvent`, records to audit log
4. **`TaskStore` class** — in-memory `dict[str, Task]` with secondary indices by status, JSONL persistence, async lock
5. **`LifecycleEvent` dataclass** — emitted on every transition, consumed by listeners for metrics/telemetry
6. **HMAC-chained `AuditLog`** — daily JSONL files with tamper-evident chaining, already records every task transition

### What this means for CQRS

The system already has several CQRS-adjacent pieces:
- `LifecycleEvent` is effectively a domain event (timestamp, entity, from/to, actor, reason)
- The audit log is already an append-only event store
- Listeners already act as projections/read-model updaters

The gap is that `task.status` is the source of truth (mutable), not the event sequence. Events are side effects of mutations, not the primary record.

---

## Event Types

### Core events (map to existing transitions)

| Event type | Replaces transition | Payload fields |
|---|---|---|
| `TaskCreated` | `Task()` construction | `task_id`, `title`, `description`, `role`, `priority`, `scope`, `complexity`, `initial_status` (`OPEN` or `PLANNED`), `depends_on`, `parent_task_id`, `tenant_id`, full task metadata |
| `TaskClaimed` | `OPEN -> CLAIMED` | `task_id`, `agent_id`, `session_id` |
| `TaskStarted` | `CLAIMED -> IN_PROGRESS` | `task_id`, `agent_id` |
| `TaskCompleted` | `IN_PROGRESS/CLAIMED -> DONE` | `task_id`, `result_summary`, `files_modified`, `test_results`, `completion_data` |
| `TaskVerified` | (post-completion check by janitor) | `task_id`, `verification_count`, `verifier` |
| `TaskMerged` | (git merge after verification) | `task_id`, `branch`, `merge_commit`, `merge_result` |
| `TaskClosed` | `DONE -> CLOSED` | `task_id`, `closed_by` |
| `TaskFailed` | `* -> FAILED` | `task_id`, `reason`, `error_code`, `retryable` |

### Auxiliary events (existing transitions that need coverage)

| Event type | Replaces transition | Payload fields |
|---|---|---|
| `TaskBlocked` | `* -> BLOCKED` | `task_id`, `reason`, `blocker_task_id` |
| `TaskUnblocked` | `BLOCKED -> OPEN` | `task_id`, `unblocked_by` |
| `TaskCancelled` | `* -> CANCELLED` | `task_id`, `reason`, `cancelled_by` |
| `TaskOrphaned` | `IN_PROGRESS -> ORPHANED` | `task_id`, `dead_agent_id`, `reason` |
| `TaskRecovered` | `ORPHANED -> OPEN` | `task_id`, `recovered_by` |
| `TaskRequeued` | `CLAIMED/IN_PROGRESS -> OPEN` | `task_id`, `reason` (force-claim, timeout, unclaim) |
| `TaskRetried` | `FAILED -> OPEN` | `task_id`, `retry_count`, `escalation` (model/effort change) |
| `TaskSplitIntoSubtasks` | `* -> WAITING_FOR_SUBTASKS` | `task_id`, `subtask_ids` |
| `TaskSubtasksCompleted` | `WAITING_FOR_SUBTASKS -> DONE` | `task_id`, `subtask_ids` |
| `TaskApproved` | `PLANNED -> OPEN` | `task_id`, `approved_by` |
| `TaskProgressLogged` | (progress_log append) | `task_id`, `message`, `percent`, `snapshot` |

### Event record structure

```python
@dataclass(frozen=True)
class TaskEvent:
    """Immutable event in a task's lifecycle."""

    event_id: str          # UUID — globally unique
    task_id: str           # Which task this event belongs to
    event_type: str        # One of the event types above
    timestamp: float       # Unix epoch, monotonic within a task's stream
    actor: str             # Who/what triggered this (agent_id, "orchestrator", "janitor", "task_store")
    sequence_number: int   # Monotonically increasing per task — enables ordering and gap detection
    payload: dict[str, Any]  # Event-type-specific data
    transition_reason: str | None = None  # Maps to existing TransitionReason enum
    abort_reason: str | None = None       # Maps to existing AbortReason enum
```

**Invariant**: For any task, `sequence_number` is contiguous starting from 0. A gap means data loss.

---

## Workflow Tree: Write Path (Event Emission)

### STEP 1: Caller requests a state change

**Actor**: Any system component (TaskStore method, API route handler, orchestrator tick)
**Action**: Calls the transition function (e.g., `emit_task_event(task_id, "TaskCompleted", payload, actor=...)`)
**Timeout**: N/A (in-process call)
**Input**: `{ task_id: str, event_type: str, payload: dict, actor: str }`

**Output on SUCCESS**: Event accepted -> GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(invalid_transition)`: Event type implies a state transition not in the transition table -> raise `IllegalTransitionError`, no event written
  - `FAILURE(task_not_found)`: task_id has no event stream -> raise `KeyError`
  - `FAILURE(concurrent_modification)`: sequence_number conflict (two events with same seq) -> retry with incremented seq or fail

**Observable states during this step**:
  - Customer sees: nothing (internal)
  - Operator sees: API request in flight
  - Database: no change yet
  - Logs: `[lifecycle] transition requested task_id=X from=Y to=Z`

---

### STEP 2: Validate transition legality

**Actor**: Lifecycle module (refactored `transition_task`)
**Action**: Compute current state from event log projection. Check `(current_status, implied_new_status)` against `TASK_TRANSITIONS` table and guard predicate.
**Timeout**: <1ms (in-memory projection lookup)
**Input**: `{ task_id: str, event_type: str, current_projection: TaskProjection }`

**Output on SUCCESS**: Transition is legal -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(illegal_transition)`: `IllegalTransitionError` raised -> caller handles (return 409 to API, log warning in orchestrator)
  - `FAILURE(guard_failed)`: Guard predicate returned False -> same handling as illegal transition

**Observable states during this step**:
  - No state change. Validation only.

**Critical design decision**: The transition table (`TASK_TRANSITIONS`) remains unchanged. The CQRS layer maps event types to implied status transitions and reuses the existing validation logic.

---

### STEP 3: Append event to the event log

**Actor**: Event store (new component, or extension of TaskStore)
**Action**: Assign `event_id` (UUID) and `sequence_number` (last_seq + 1). Append the `TaskEvent` to the task's event stream. Persist to storage.
**Timeout**: 50ms (JSONL append) / 200ms (if WAL write)
**Input**: Validated `TaskEvent`

**Output on SUCCESS**: Event persisted -> GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(io_error_transient)`: NFS stale handle, EAGAIN -> retry 3x with exponential backoff (existing `_retry_io` pattern) -> if exhausted, ABORT
  - `FAILURE(io_error_permanent)`: ENOSPC, EROFS -> raise `TaskStoreUnavailable` immediately, no cleanup needed (event was never written)
  - `FAILURE(sequence_conflict)`: Another writer appended seq N before us -> re-read last seq, re-validate, re-append with seq N+1

**Observable states during this step**:
  - Database: Event appended to `{task_id}.events.jsonl` (or in-memory list with buffered flush)
  - Logs: `[event_store] appended event_id=E seq=N type=TaskCompleted task_id=X`

---

### STEP 4: Update in-memory projection

**Actor**: Projection engine (replaces direct `task.status = new_status`)
**Action**: Apply the new event to the in-memory `TaskProjection`. Update secondary indices (`_by_status`, `_by_role_status`, `_priority_queues`).
**Timeout**: <1ms (in-memory dict operations)
**Input**: The appended `TaskEvent`, current projection

**Output on SUCCESS**: Projection updated -> GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(projection_divergence)`: Projection state doesn't match expected post-event state -> log error, trigger full replay from event log to rebuild projection

**Observable states during this step**:
  - In-memory: Task projection now reflects the new state
  - Secondary indices updated for O(1) lookups

**Critical invariant**: The projection is a cache. It can always be rebuilt by replaying the event log. If it diverges, replay is the recovery path, not a bug fix.

---

### STEP 5: Emit side effects

**Actor**: Lifecycle module + listeners
**Action**: Emit `LifecycleEvent` to registered listeners (backward compatible with existing subscriber pattern). Record to HMAC-chained audit log. Update Prometheus counters. Fire OpenTelemetry span.
**Timeout**: 100ms total for all listeners (fire-and-forget, errors logged but not propagated)
**Input**: The `TaskEvent` mapped to a `LifecycleEvent`

**Output on SUCCESS**: All side effects dispatched -> DONE
**Output on FAILURE**:
  - `FAILURE(listener_error)`: Individual listener throws -> caught, logged, does not affect other listeners or the event append (event is already persisted)
  - `FAILURE(audit_log_error)`: Audit log write fails -> logged as warning, event is still valid (audit log is secondary)

**Observable states after this step**:
  - Operator sees: Task in new state on dashboard
  - Audit log: New HMAC-chained entry
  - Prometheus: Transition counter incremented
  - Logs: `[lifecycle] task X transitioned from Y to Z`

---

## Workflow Tree: Read Path (State Projection)

### PROJECTION 1: Current state query (hot path)

**Trigger**: `GET /tasks/{id}`, `GET /tasks?status=open`, dashboard, claim_next
**Action**: Read from in-memory projection (NOT from event log)
**Latency**: <1ms
**Consistency**: Eventually consistent with event log (but in practice, updated synchronously in STEP 4)

### PROJECTION 2: Full history query

**Trigger**: `GET /tasks/{id}/history` (new endpoint)
**Action**: Read all events from the task's event stream, return ordered list
**Latency**: O(N) where N = number of events for this task (typically <50)

### PROJECTION 3: Point-in-time reconstruction

**Trigger**: Debug/audit query — "what was the state of task X at time T?"
**Action**: Replay events up to timestamp T, return the projected state at that point
**Latency**: O(N) where N = events up to time T

### PROJECTION 4: Arbitrary aggregation

**Trigger**: "Show me all tasks that were verified but had merge conflicts"
**Action**: Scan event logs, filter by event types and payload fields
**Latency**: O(total_events) — use indices or materialized views for frequent queries

---

## State Transitions (unchanged)

The CQRS layer does NOT change the allowed transitions. It changes HOW transitions are recorded (append-only events) and HOW state is derived (projection/fold), not WHAT transitions are legal.

```
[PLANNED] -> (TaskApproved) -> [OPEN]
[PLANNED] -> (TaskCancelled) -> [CANCELLED]

[OPEN] -> (TaskClaimed) -> [CLAIMED]
[OPEN] -> (TaskSplitIntoSubtasks) -> [WAITING_FOR_SUBTASKS]
[OPEN] -> (TaskCancelled) -> [CANCELLED]

[CLAIMED] -> (TaskStarted) -> [IN_PROGRESS]
[CLAIMED] -> (TaskRequeued) -> [OPEN]
[CLAIMED] -> (TaskCompleted) -> [DONE]        # fast completion
[CLAIMED] -> (TaskFailed) -> [FAILED]
[CLAIMED] -> (TaskCancelled) -> [CANCELLED]
[CLAIMED] -> (TaskSplitIntoSubtasks) -> [WAITING_FOR_SUBTASKS]
[CLAIMED] -> (TaskBlocked) -> [BLOCKED]

[IN_PROGRESS] -> (TaskCompleted) -> [DONE]
[IN_PROGRESS] -> (TaskFailed) -> [FAILED]
[IN_PROGRESS] -> (TaskBlocked) -> [BLOCKED]
[IN_PROGRESS] -> (TaskSplitIntoSubtasks) -> [WAITING_FOR_SUBTASKS]
[IN_PROGRESS] -> (TaskRequeued) -> [OPEN]
[IN_PROGRESS] -> (TaskCancelled) -> [CANCELLED]
[IN_PROGRESS] -> (TaskOrphaned) -> [ORPHANED]

[ORPHANED] -> (TaskCompleted) -> [DONE]
[ORPHANED] -> (TaskFailed) -> [FAILED]
[ORPHANED] -> (TaskRecovered) -> [OPEN]

[BLOCKED] -> (TaskUnblocked) -> [OPEN]
[BLOCKED] -> (TaskCancelled) -> [CANCELLED]

[WAITING_FOR_SUBTASKS] -> (TaskSubtasksCompleted) -> [DONE]
[WAITING_FOR_SUBTASKS] -> (TaskCancelled) -> [CANCELLED]

[FAILED] -> (TaskRetried) -> [OPEN]

[DONE] -> (TaskClosed) -> [CLOSED]
[DONE] -> (TaskFailed) -> [FAILED]     # post-completion verification failure

[CLOSED] -> (terminal)
[CANCELLED] -> (terminal)
```

---

## Storage Design

### Option A: Per-task event files (recommended for current scale)

```
.sdd/runtime/events/
  {task_id}.events.jsonl    # One file per task, append-only
```

Each line is a JSON-serialized `TaskEvent`. File is append-only; never rewritten.

**Advantages**: Simple, one file handle per task, easy to reason about ordering.
**Disadvantages**: Many small files at scale (hundreds of tasks).

### Option B: Partitioned event log (for large scale)

```
.sdd/runtime/events/
  2026-04-04.events.jsonl   # All events for all tasks, partitioned by day
```

With a secondary index mapping `task_id -> [(file, offset, length)]` for efficient per-task replay.

**Advantages**: Fewer files, better for bulk queries.
**Disadvantages**: Requires index maintenance, more complex seek logic.

### Option C: In-memory with WAL (hybrid)

Events live in-memory in `list[TaskEvent]` per task. A write-ahead log (WAL) persists events for crash recovery (Bernstein already has a WAL module).

**Advantages**: Fastest read path, leverages existing WAL.
**Disadvantages**: Memory grows with event count (mitigated by archiving/snapshotting).

### Recommendation

Start with **Option A** (per-task JSONL files). It matches the existing JSONL persistence pattern in `TaskStore`, requires minimal infrastructure, and can be migrated to Option B or C later if scale demands it.

---

## Migration Path

### Phase 1: Dual-write (backward compatible)

1. `transition_task()` continues to mutate `task.status` as today
2. Additionally, append a `TaskEvent` to the new event log
3. Both writes happen atomically (same lock)
4. Existing code continues to read from `task.status`
5. New `GET /tasks/{id}/history` endpoint reads from event log

**Risk**: Double storage cost. Acceptable for validation period.
**Rollback**: Delete event log files. Zero impact on existing behavior.

### Phase 2: Read from projection

1. `TaskProjection` class added, builds state from event replay
2. On startup, replay all events to build projections (replacing JSONL task load)
3. Hot path reads switch from `task.status` to `projection.status`
4. `task.status` still written for backward compat during transition
5. Validation: assert `projection.status == task.status` after every transition

**Risk**: Projection bugs produce wrong state. Mitigated by assertion against legacy field.
**Rollback**: Remove projection reads, revert to `task.status`.

### Phase 3: Event log as source of truth

1. Remove `task.status` mutation from `transition_task()`
2. `Task.status` becomes a computed property from projection
3. Remove legacy JSONL task persistence (replaced by event log + projection)
4. Secondary indices rebuilt from projection on startup

**Risk**: Full commitment. Requires Phase 2 validation to pass for N days with zero assertion failures.
**Rollback**: Re-add `task.status` mutation, rebuild JSONL from event log replay.

---

## Handoff Contracts

### Caller -> Lifecycle Module (event emission)

**Function**: `emit_task_event(task_id, event_type, payload, actor)`
**Input**:
```python
{
    "task_id": "str — task ID",
    "event_type": "str — one of the defined event types",
    "payload": "dict — event-type-specific data",
    "actor": "str — who triggered this"
}
```
**Success response**: `TaskEvent` — the appended event with assigned `event_id` and `sequence_number`
**Failure response**: `IllegalTransitionError` or `TaskStoreUnavailable`
**Timeout**: 250ms (includes I/O)

### Event Store -> Projection Engine

**Internal**: Synchronous in-process call after event append
**Input**: `TaskEvent`
**Output**: Updated `TaskProjection`
**Failure**: Logged, triggers full replay

### API -> Event Store (history query)

**Endpoint**: `GET /tasks/{id}/history`
**Success response**:
```json
{
    "task_id": "str",
    "events": [
        {
            "event_id": "str",
            "event_type": "str",
            "timestamp": 1712188800.0,
            "actor": "str",
            "sequence_number": 0,
            "payload": {}
        }
    ],
    "current_state": "str — projected status"
}
```
**Failure response**:
```json
{
    "ok": false,
    "error": "Task not found",
    "code": "TASK_NOT_FOUND"
}
```

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Per-task event JSONL file | Step 3 (first event) | Archive job | Compress + move to `.sdd/archive/events/` |
| In-memory projection | Step 4 | Task archival | Remove from `_projections` dict |
| Secondary index entries | Step 4 | Task archival | Remove from `_by_status`, `_by_role_status`, `_priority_queues` |

---

## Concurrency and Ordering

### Write ordering

Events for a single task are serialized by the existing `asyncio.Lock` in `TaskStore`. The lock guarantees:
- `sequence_number` is monotonically increasing
- No two events for the same task are written concurrently
- The projection update (STEP 4) happens before the lock is released

### Cross-task ordering

Events across different tasks have no ordering guarantee (they don't need one). Each task's event stream is independent.

### Replay ordering

On startup, events are replayed in `sequence_number` order per task. If a gap in sequence numbers is detected, log a `CRITICAL` warning — this indicates data loss.

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — create through close | TaskCreated -> TaskClaimed -> TaskStarted -> TaskCompleted -> TaskVerified -> TaskMerged -> TaskClosed | Projection shows CLOSED; event count = 7; sequence numbers 0-6 contiguous |
| TC-02: Illegal transition rejected | Attempt TaskClosed on OPEN task | `IllegalTransitionError`; no event appended; projection unchanged |
| TC-03: Point-in-time reconstruction | Replay events 0-3 of a 7-event stream | Projection reflects state after event 3 (IN_PROGRESS) |
| TC-04: Concurrent claim conflict | Two agents attempt TaskClaimed on same task | First succeeds (CLAIMED); second gets `IllegalTransitionError` (task is no longer OPEN) |
| TC-05: Crash recovery via replay | Kill process mid-stream, restart | Events on disk replayed; projection matches pre-crash state |
| TC-06: Retry from failure | TaskFailed -> TaskRetried | Projection shows OPEN; event log contains both failure and retry events |
| TC-07: Orphan detection and recovery | TaskOrphaned -> TaskRecovered | Projection shows OPEN; orphan event records dead agent ID |
| TC-08: Subtask lifecycle | TaskSplitIntoSubtasks -> all subtasks complete -> TaskSubtasksCompleted | Parent projection shows DONE; event log tracks subtask IDs |
| TC-09: Dual-write consistency (Phase 1) | Any transition during Phase 1 | `assert projection.status == task.status` passes |
| TC-10: Sequence gap detection | Manually remove event from JSONL | Replay logs CRITICAL warning about sequence gap |
| TC-11: Full history endpoint | GET /tasks/{id}/history | Returns all events in sequence order with correct projected state |
| TC-12: Progress events | Multiple TaskProgressLogged events | Do not change status; appear in history; projection.progress reflects latest |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `asyncio.Lock` in TaskStore serializes all writes to a single task's event stream | Verified: `task_store.py` uses `self._lock` for all mutations | If lock is bypassed, sequence numbers can collide |
| A2 | Event JSONL files are append-only and never truncated | Design invariant | If violated, replay produces wrong state |
| A3 | The existing `TASK_TRANSITIONS` table remains the canonical set of legal transitions | Verified: `lifecycle.py` lines 106-144 | If transitions are added outside the table, events will be rejected |
| A4 | Events for a single task fit in memory (typically <50 events per task) | Estimated from typical task lifecycle | If tasks have thousands of events (e.g., frequent progress logging), projection rebuild is slow |
| A5 | The existing `LifecycleEvent` listeners do not depend on `task.status` being mutated before they are called | Not verified — listeners may read `task.status` | If wrong, Phase 3 requires listener updates |
| A6 | WAL module (`bernstein.core.wal`) can be extended for event persistence | Referenced in codebase | If WAL is incompatible, Option A (direct JSONL) is the fallback |

---

## Open Questions

1. **Should `TaskProgressLogged` events be in the same stream or a separate stream?** Progress events are high-frequency and don't affect status. Mixing them inflates the event count. Separate stream (e.g., `{task_id}.progress.jsonl`) may be cleaner but adds complexity.

2. **Snapshot frequency for projection rebuild performance?** If tasks accumulate many events, periodic snapshots (e.g., every 20 events, write a `{task_id}.snapshot.json`) would cap replay cost. Is this needed at current scale (typically <50 events per task)?

3. **Should the event store replace or supplement the audit log?** The event store and audit log record overlapping data. The audit log has HMAC chaining (tamper evidence) that the event store would not. Options: (a) keep both, (b) make the event store HMAC-chained, (c) derive audit entries from events.

4. **Event schema versioning?** When event payload shapes change, how are old events handled during replay? Options: (a) version field in event, upcasting on read, (b) never change payloads, only add new event types.

5. **Multi-tenant event isolation?** Current TaskStore scopes by tenant_id. Should event files be partitioned by tenant (`{tenant_id}/{task_id}.events.jsonl`) or rely on the task_id being globally unique?

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-04 | Initial spec created from analysis of `lifecycle.py`, `task_store.py`, `models.py`, `audit.py`, `tick_pipeline.py`, `task_lifecycle.py`, `task_completion.py` | — |
| 2026-04-04 | Current system already emits `LifecycleEvent` on every transition (quasi-events) but events are side effects, not source of truth | Documented as migration starting point |
| 2026-04-04 | `PENDING_APPROVAL` status exists in enum but has no transitions in `TASK_TRANSITIONS` table | Noted — either dead code or implicit workflow. Not included in event types until clarified |
| 2026-04-04 | Existing audit log already provides append-only HMAC-chained event persistence — significant overlap with proposed event store | Raised as Open Question #3 |
| 2026-04-04 | `task.version` field (optimistic locking) would be superseded by `sequence_number` in events | Migration note: remove `version` in Phase 3 |
