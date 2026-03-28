# 722 — Crash Recovery / Orphan Agent Resume

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

When agents crash mid-work (OOM, network timeout, process killed), Bernstein spawns fresh agents. The partial work in the crashed agent's worktree is lost. Stoneforge's orphan recovery auto-resumes crashed sessions — users cite this as their #1 reason for choosing it over alternatives. For long-running tasks (30+ min), losing progress is unacceptable.

## Design

### Crash detection
- Spawner polls agent process status every tick
- If process exited with non-zero code and task not completed: mark as `orphaned`
- Record partial state: worktree path, last known file changes, progress

### Recovery strategies
1. **Resume**: Spawn new agent in same worktree with context: "Previous agent crashed. Continue from where it left off. Changed files so far: ..."
2. **Restart**: Wipe worktree, spawn fresh agent (current behavior)
3. **Escalate**: If 2+ crashes on same task, mark task as `blocked` and notify

### Configuration
```yaml
# bernstein.yaml
recovery: resume  # resume | restart | escalate
max_crash_retries: 2
```

### Orphan cleanup
- On orchestrator start, scan `.sdd/runtime/` for orphaned worktrees
- Offer to resume or clean up

## Files to modify

- `src/bernstein/core/orchestrator.py` (crash detection in tick loop)
- `src/bernstein/core/spawner.py` (resume logic)
- `src/bernstein/core/models.py` (orphaned task status)
- `tests/unit/test_crash_recovery.py` (new)

## Completion signal

- Killed agent is detected and task resumes in same worktree
- Resume context includes partial work summary
- max_crash_retries enforced
