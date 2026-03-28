# 725 — Fast Session Resume (Skip Re-Planning)

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** medium
**Depends on:** none

## Problem

When Bernstein restarts (even after a brief stop), the manager agent re-scans the entire codebase and re-plans from scratch, burning tokens and wasting 2-5 minutes. If the previous run was stopped 1 minute ago, the state is identical — there's nothing new to discover. This makes the stop/start cycle painfully slow and expensive.

## Design

### Session state persistence
On graceful stop (`bernstein stop` or Ctrl+C):
- Save full orchestrator state to `.sdd/runtime/session.json`:
  - Active tasks (claimed, in_progress)
  - Completed tasks this run
  - Cost tracker state
  - Planning output (task decomposition)
  - Timestamp of last save
- Already-verified worktrees are preserved

### Fast resume on start
When `bernstein` starts:
1. Check `.sdd/runtime/session.json`
2. If exists and < 30 min old (configurable):
   - Skip manager planning phase entirely
   - Restore task list from saved state
   - Resume spawning for uncompleted tasks
   - Print "Resuming from previous session (3 tasks remaining)"
3. If stale (> 30 min) or missing:
   - Full fresh start (current behavior)
4. Force fresh: `bernstein --fresh` to ignore saved state

### Graceful shutdown improvements
- On SIGINT/SIGTERM: save state before killing agents
- On `bernstein stop`: save state, then stop agents gracefully
- On agent crash: don't lose overall session state

### Configuration
```yaml
# bernstein.yaml
session:
  resume: true  # default
  stale_after_minutes: 30
```

## Files to modify

- `src/bernstein/core/orchestrator.py` (save/load session state)
- `src/bernstein/core/bootstrap.py` (detect and offer resume)
- `src/bernstein/cli/main.py` (add --fresh flag)
- `tests/unit/test_session_resume.py` (new)

## Completion signal

- `bernstein stop && bernstein` resumes in < 5 seconds (no re-planning)
- State file written on graceful stop
- `--fresh` forces full restart
- Stale sessions (> 30 min) auto-discard


---
**completed**: 2026-03-28 19:17:58
**task_id**: e28b010002f0
**result**: Completed: [RETRY 1] 725 — Fast Session Resume (Skip Re-Planning). session.py already had save/load/discard/check_resume_session. orchestrator._save_session_state saves on cleanup. bootstrap.py checks for prior session and skips manager re-planning. main.py has --fresh flag. Added SessionConfig to seed.py for session.resume and session.stale_after_minutes YAML config. 19 tests pass.
