# 741 — Cross-Run Task Quarantine

**Role:** backend
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Tasks that fail repeatedly across runs get re-attempted every time Bernstein starts. #519 "Distributed cluster mode" has failed 3+ times — it's too complex for a single agent session. Without cross-run memory of failures, Bernstein burns tokens re-attempting known-bad tasks.

## Design

Maintain `.sdd/runtime/quarantine.json`:
```json
[
  {"task_title": "519 — Distributed cluster mode", "fail_count": 3, "last_failure": "2026-03-28", "reason": "Agent died; no files modified", "action": "skip"},
  {"task_title": "533 — WASM fast-path", "fail_count": 3, "last_failure": "2026-03-28", "reason": "Scope too large", "action": "decompose"}
]
```

On task assignment, check quarantine:
- If `action: skip` — log warning, don't assign
- If `action: decompose` — auto-decompose before assigning
- Tasks exit quarantine after 7 days or manual `bernstein quarantine clear`

## Files to modify

- `src/bernstein/core/orchestrator.py` (check quarantine before spawn)
- `src/bernstein/core/quarantine.py` (new — quarantine CRUD)
- `tests/unit/test_quarantine.py` (new)

## Completion signal

- Repeatedly-failing tasks auto-quarantined after 3 failures
- Quarantined tasks skipped on next run
- `bernstein quarantine list` shows quarantined tasks
