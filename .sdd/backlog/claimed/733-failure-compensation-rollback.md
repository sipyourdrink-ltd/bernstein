# 733 — Failure Compensation: Auto-Rollback Agent Changes

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

When an agent fails mid-task, its partial changes remain in the worktree. The current behavior is to mark the task as failed and move on, but the dirty worktree can cause conflicts for retry agents or leak incomplete code. Durable workflow patterns show that compensation logic (undo partial effects on failure) is essential for reliable orchestration.

## Design

### Compensation on task failure
When the janitor detects a task failure:
1. If the agent's worktree has uncommitted changes: `git stash` them with a descriptive message
2. If the agent made commits: `git reset --soft` to the pre-task commit, stash the diff
3. Store the stashed changes reference in `.sdd/runtime/stashes/{task_id}.json`
4. Log the compensation action to the audit trail

### Recovery options
On retry, the new agent can choose to:
- Start fresh (default): clean worktree, no prior context
- Resume from stash: `git stash pop` the failed agent's partial work

### Configuration
```yaml
# bernstein.yaml
on_failure:
  compensation: stash  # stash | reset | none
  preserve_partial: true  # keep stash for retry agent
```

### Never destructive
Compensation always preserves data (stash, not delete). The user can inspect stashes with `bernstein stashes` and apply them manually if needed.

## Files to modify

- `src/bernstein/core/janitor.py` (compensation after failure)
- `src/bernstein/core/worktree.py` (stash/reset helpers)
- `tests/unit/test_compensation.py` (new)

## Completion signal

- Failed task's changes are stashed automatically
- Stash reference stored in `.sdd/runtime/stashes/`
- Retry agent can optionally resume from stash
- No data loss on failure
