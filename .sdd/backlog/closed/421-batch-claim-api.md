# 421 — Batch task claim API endpoint

**Role:** backend
**Priority:** 2
**Scope:** small
**Complexity:** low
**Depends on:** [420]

## Problem
Orchestrator claims tasks one-by-one via `POST /tasks/{id}/claim`. With 10+ tasks per tick, this means 10+ sequential HTTP roundtrips. A batch endpoint eliminates this overhead.

## Implementation
1. Add `POST /tasks/claim-batch` to task server:
   - Request body: `{"task_ids": ["id1", "id2", ...], "agent_id": "orch-1"}`
   - Response: `{"claimed": ["id1", "id3"], "failed": ["id2"]}` (id2 may already be claimed)
   - Atomic per-task (each claim succeeds or fails independently)
2. Update orchestrator to use batch claim when available:
   - Group tasks by role, claim all tasks for a role in one batch call
   - Fall back to individual claims if batch endpoint returns 404 (backward compat)
3. Add rate limiting to batch endpoint: max 20 tasks per call

## Files
- src/bernstein/core/server.py — add /tasks/claim-batch endpoint
- src/bernstein/core/orchestrator.py — use batch claim
- tests/unit/test_server.py — test batch claim
- tests/unit/test_orchestrator.py — test batch claim integration

## Completion signals
- test_passes: uv run pytest tests/unit/test_server.py -x -q -k batch
- file_contains: src/bernstein/core/server.py :: claim-batch


---
**completed**: 2026-03-28 04:30:50
**task_id**: 95287e860380
**result**: Completed: Add batch task claim endpoint to server
