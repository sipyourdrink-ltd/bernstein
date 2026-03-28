# 333f — Manager Agent Can Correct Task Assignments

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #333d

## Problem

The deterministic orchestrator assigns tasks based on static role/priority fields. But sometimes the initial decomposition is wrong — a "backend" task turns out to need frontend work, or a task is too vague for any agent. Currently nobody re-evaluates assignments. The manager agent (which did the initial planning) should be able to intervene.

## Design

### Manager review trigger
After every N completed tasks (or on any failure), the manager gets a brief review prompt:
```
3 tasks completed, 2 failed. Current queue:
- [backend] "Add auth" — claimed by backend-abc123 (2min, in progress)
- [qa] "Write tests" — open
- [backend] "Fix CSS layout" — THIS IS FRONTEND WORK

Actions available:
1. Re-assign "Fix CSS layout" to frontend role
2. Kill stalled agent backend-xyz (no progress 5min)
3. Split "Add auth" into subtasks
4. Add new task to queue
5. No changes needed
```

### Implementation
The manager doesn't control the orchestrator directly. Instead:
1. Manager POSTs corrections to the task server: `PATCH /tasks/{id}` (change role, priority)
2. Manager POSTs new tasks: `POST /tasks` (decompose or add missing work)
3. Manager POSTs kill signals: `POST /tasks/{id}/cancel`
4. Orchestrator picks up changes on next tick (deterministic — no LLM in the loop)

### When to invoke manager review
- After 3+ task completions (periodic check-in)
- After any task failure (maybe wrong assignment?)
- After 5 minutes of zero progress across all agents
- On explicit user trigger: `bernstein review`

### Cost control
Manager review uses haiku (cheap) with a short prompt. Max 500 tokens output. Skip if budget < 10% remaining.

## Files to modify

- `src/bernstein/core/orchestrator.py` (trigger manager review)
- `src/bernstein/core/manager.py` (review prompt + correction logic)
- `src/bernstein/core/server.py` (PATCH /tasks/{id} for role change)

## Completion signal

- Manager corrects a mis-assigned task role
- Manager kills a stalled agent
- Manager decomposes a too-large task mid-run
- All corrections go through task server (deterministic orchestrator preserved)
