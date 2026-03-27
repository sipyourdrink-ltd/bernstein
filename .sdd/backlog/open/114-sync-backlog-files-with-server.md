# Sync .sdd/backlog/ files with task server state

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
Two separate task tracking systems exist and are NOT synced:
1. `.sdd/backlog/open/*.md` — static markdown ticket files
2. HTTP task server (tasks.jsonl) — dynamic runtime state

When agents complete tasks via POST /tasks/{id}/complete, the corresponding
.md file stays in backlog/open/ instead of moving to backlog/closed/.
This makes it impossible to track progress by looking at the filesystem.

## Implementation
Option A (preferred): In orchestrator tick, after verifying a task is done:
1. Match task title/id to a .md file in backlog/open/
2. Move the file to backlog/closed/ (rename/mv)
3. Append completion timestamp and result summary to the file

Option B: Add a `bernstein sync` CLI command that reads server state
and moves files accordingly.

Option C: Both — auto-sync in orchestrator + manual sync command.

## Matching logic
- Task titles from server should fuzzy-match filenames in backlog/open/
- Or: when manager creates tasks from backlog tickets, store the source
  filename in the task metadata so we can look it up later

## Files
- src/bernstein/core/orchestrator.py (add sync step to tick)
- src/bernstein/cli/main.py (optional sync command)
- tests/unit/test_orchestrator.py

## Completion signals
- test_passes: uv run pytest tests/unit/test_orchestrator.py -x -q
