# 742 — Agent Progress Checkpoints (Stuck Detection)

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** #736

## Problem

Agents can burn tokens while making zero progress — spinning in loops, retrying the same command, or stuck waiting for something. The orchestrator only knows "process is alive" but not "agent is making progress." Detecting stuck agents early saves tokens and frees slots for productive work.

## Design

### Progress snapshots
Agents write a progress snapshot to the task server every 60 seconds:
```json
{"files_changed": 3, "tests_passing": 12, "errors": 2, "last_file": "src/auth.py"}
```

### Stall detection
The orchestrator compares consecutive snapshots:
- If 3 identical snapshots (3 minutes of no progress) → write WAKEUP signal
- If 5 identical snapshots (5 minutes) → write SHUTDOWN signal
- If 7 identical snapshots → kill process

### Implementation
Add to agent system prompt:
```
Every 60 seconds, report progress:
curl -s -X POST http://127.0.0.1:8052/tasks/{TASK_ID}/progress \
  -H "Content-Type: application/json" \
  -d '{"files_changed": N, "tests_passing": N}'
```

Server stores last 10 snapshots per task. Orchestrator checks in tick loop.

## Files to modify

- `src/bernstein/core/server.py` (POST /tasks/{id}/progress endpoint)
- `src/bernstein/core/orchestrator.py` (stall detection in tick)
- `templates/prompts/progress-report.md` (new — inject into agent prompts)
- `tests/unit/test_progress_checkpoints.py` (new)

## Completion signal

- Agents report progress every 60s
- Stalled agents detected and killed after 5min of no change
- Token waste reduced measurably
