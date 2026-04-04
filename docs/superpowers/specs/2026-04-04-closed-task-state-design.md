# Design: CLOSED Task State

## Problem

Tasks reach `DONE` and stay there forever. There is no terminal success state that means "verified, merged, and closed." This causes:

1. `_processed_done_tasks` set grows without bound (no way to distinguish fresh-done from verified)
2. `sync.py` moves backlog files on `done` status, before janitor verification
3. GitHub issue close logic exists in dead code (`task_completion.py:process_completed_tasks`) that nobody imports
4. Dashboard cannot distinguish "agent says done" from "actually shipped"

## Design

### State Machine

Add `CLOSED` as the single absorbing (terminal success) state. `DONE` becomes transient — it means "agent claims done, pending verification."

```
OPEN -> CLAIMED -> IN_PROGRESS -> DONE -> CLOSED  (happy path)
                                  DONE -> FAILED   (janitor/gates/rules/merge fail)
```

Verification pipeline (deterministic, no LLM):
```
DONE -> janitor_verify -> quality_gates -> rules -> cross_model_review -> merge -> CLOSED
        any step fails -> FAILED + auto-retry (creates new task)
```

New transitions in `TASK_TRANSITIONS`:
- `(DONE, CLOSED)` — janitor passed + merge succeeded
- `(DONE, FAILED)` — verification failed (was implicit, now explicit)

`TERMINAL_TASK_STATUSES` becomes `{CLOSED, CANCELLED}`.

### Files Changed

| File | Change |
|------|--------|
| `core/models.py` | Add `CLOSED = "closed"` to `TaskStatus` |
| `core/lifecycle.py` | Add `(DONE, CLOSED)` and `(DONE, FAILED)` transitions |
| `core/task_store.py` | Add `async def close(task_id)` — DONE->CLOSED, sets `closed_at` |
| `core/routes/tasks.py` | Add `POST /tasks/{id}/close` endpoint |
| `core/tick_pipeline.py` | Add `close_task()` HTTP helper |
| `core/task_lifecycle.py` | Wire `close_task()` at janitor_passed+merge point; add GitHub issue close |
| `core/sync.py` | Query `closed` status instead of `done` for backlog file moves |
| `core/task_completion.py` | Delete unused `process_completed_tasks` (dead code) |

### What Fires on CLOSED Transition

1. Task status -> CLOSED in task store (persisted to JSONL)
2. Backlog file moves open/ -> closed/
3. GitHub issue closed (if `metadata.issue_number` exists)
4. SSE event published for dashboard
5. Task exits `done` query -> `_processed_done_tasks` stays bounded

### Failure Path

When janitor/quality gates/rules/cross-model review fails:
- Existing `maybe_retry_task` / `retry_or_fail_task` logic handles this
- Task transitions DONE -> FAILED (now explicit in transition table)
- Retry creates a new OPEN task with fix context
- Original task stays FAILED (terminal failure state, can be reopened via FAILED->OPEN)

### API

```
POST /tasks/{task_id}/close
Body: {} (no payload needed — orchestrator is the only caller)
Response: TaskResponse with status "closed"
Allowed from: DONE only
```

### Invariants

- Only the orchestrator's `process_completed_tasks` can close a task
- A task can only be closed if janitor passed AND merge succeeded (or skip_merge with approval)
- CLOSED is terminal — no transitions out
- Every task eventually reaches CLOSED, FAILED, or CANCELLED
