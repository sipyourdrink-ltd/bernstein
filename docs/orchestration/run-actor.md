# Single-writer run-state actor

`core/orchestration/run_actor.py` funnels every mutation of an
orchestration run's live state through one actor task. Callers
enqueue typed events; one writer applies them via a pure reducer and
appends to a bounded replay buffer. Subscribers read from the buffer
with a `last_seen_seq` and receive explicit `Gap` markers when they
have fallen past the window.

This actor owns run-level state. Individual worker sessions have their
own lifecycle. The literal event identifiers `session_started` and
`session_ended` below remain the run reducer's event names.

## TL;DR

| Concept       | Role |
|---------------|---|
| `RunState`    | Frozen dataclass; canonical per-run state. |
| `Event`       | Typed mutation request; stamped with a monotonic `seq`. |
| `apply_event` | Pure reducer `(state, event) -> state`. |
| `RunActor`    | Owns one `RunState`; single-writer async task. |
| `ReplayBuffer`| Bounded ring of stamped events (default 1024). |
| `Gap`         | Sentinel `{up_to_seq}` returned when buffer evicted past a subscriber's resume point. |

## Why

Without a single writer, many call sites can race to mutate live run
state and many subscribers can re-derive it from disk or DB. Under a
reconnect with a slow consumer, the subscriber can silently observe an
inconsistent snapshot. A `Gap{up_to_seq}` marker lets a client
distinguish "I am caught up" from "I missed events between
`last_seen_seq` and `up_to_seq`; reconcile from a fresh snapshot".

## Event vocabulary

Today's reducer recognises the kinds below. New kinds extend the
reducer without breaking existing subscribers; unknown kinds advance
the sequence number and otherwise no-op.

| Kind                  | Mutates                                       |
|-----------------------|-----------------------------------------------|
| `session_started`     | `status -> "running"`                         |
| `task_started`        | `tasks[task_id].status = "running"` + payload |
| `task_progress`       | `tasks[task_id].status = "running"` + payload |
| `task_completed`      | `tasks[task_id].status = "done"`              |
| `task_failed`         | `tasks[task_id].status = "failed"`            |
| `approval_requested`  | `approvals[approval_id] = "pending"`          |
| `approval_granted`    | `approvals[approval_id] = "granted"`          |
| `approval_denied`     | `approvals[approval_id] = "denied"`           |
| `watchdog_tick`       | only advances `last_seq`                      |
| `session_ended`       | `status -> "done"` / `"failed"`               |

The reducer is pure and total: it always returns a new frozen
`RunState` and never mutates its inputs.

## Sequence-number contract

* `seq = 0` means "no events applied".
* The actor stamps each accepted event with `state.last_seq + 1`
  before applying.
* `apply_event` rejects any event whose `seq != state.last_seq + 1`
  (returns `state` unchanged, logs a warning). The actor never produces
  such events itself; the check is defensive against direct reducer
  use.
* `RunState` is the only object that holds `last_seq`. Subscribers
  persist it themselves.

## Gap-marker contract

`ReplayBuffer.since(last_seen_seq)` returns one of:

1. **Empty list** - caller is at or past the latest stored seq.
2. **List of events in `(last_seen_seq, latest_held]`** - caller is
   inside the buffer window. No `Gap` is produced.
3. **`[Gap(up_to_seq=oldest_held - 1), ...remaining]`** - caller is
   below the buffer's oldest stored seq. Exactly one `Gap` precedes
   the events still in the buffer. `up_to_seq` is the highest seq
   that has been evicted and can never be replayed.

A subscriber that observes a `Gap` must re-snapshot via
`RunActor.snapshot()` rather than try to rebuild missed state from
the truncated stream.

## Usage

```python
import asyncio

from bernstein.core.orchestration.run_actor import Event, RunActor


async def main() -> None:
    actor = RunActor("sess-42", replay_capacity=1024)
    await actor.start()
    try:
        # Writers: enqueue typed events.
        await actor.submit_and_wait(
            Event(kind="session_started", source="bootstrap"),
        )
        await actor.submit_and_wait(
            Event(
                kind="task_started",
                payload={"task_id": "T1", "role": "backend"},
                source="worker",
            ),
        )

        # Readers: snapshot returns the frozen state right now.
        state = actor.snapshot()
        assert state.tasks["T1"]["status"] == "running"

        # Subscribers: replay strictly after their last-seen seq.
        items = await actor.since(last_seen_seq=0)
        for item in items:
            ...  # Event or Gap
    finally:
        await actor.stop()
```

### Reconnecting subscribers (SSE / websocket)

Clients carry their highest applied seq (typically as the
`Last-Event-Id` HTTP header). On reconnect:

1. Server calls `await actor.since(last_seen_seq)`.
2. If item 0 is a `Gap`, the server emits a `gap` event with the
   payload `{"up_to_seq": gap.up_to_seq}`. The client interprets this
   as "your local projection is stale; ignore it and treat the next
   snapshot as ground truth".
3. The server emits the remaining events.
4. The server resumes normal incremental delivery from the latest seq.

This means a reconnect-after-eviction is **observable**, not silently
corrupt.

## Scope

* In-memory only. Persistence is out of scope; pair with the existing
  WAL if durability is required.
* One actor per orchestration run, one process. Cross-process fan-out is not
  handled here.
* Migrating every legacy writer is its own follow-up. The actor lands
  alongside one critical caller adaptation; the remaining call sites
  will be moved over incrementally.

## Testing

Unit tests live under `tests/unit/run_actor/`:

* `test_single_writer.py` - concurrent writers see a strictly
  monotonic seq log; events are applied exactly once.
* `test_pure_apply.py` - `apply_event` is deterministic and does not
  mutate its inputs; `fold` reconstructs state from an event log.
* `test_snapshot_read.py` - `snapshot()` returns a stable frozen view.
* `test_replay_after_gap.py` - `since()` emits a `Gap` marker when
  the buffer has evicted past the caller's `last_seen_seq`.
* `test_evicted_reconnect.py` - a reconnect past the window observes
  exactly one `Gap` followed by the current snapshot.
