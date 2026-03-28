# 732 — Live Task Introspection

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

`bernstein status` shows task status (open/claimed/done/failed) but no insight into what a running agent is currently doing. When an agent has been running for 5 minutes, the user has no way to check: is it stuck? How many files has it changed? How many tokens has it consumed? This makes long-running tasks anxiety-inducing.

## Design

### Live introspection per task
`bernstein status --live <task-id>` shows real-time data for a running agent:

```
Task: T-001 "Add auth middleware" (in_progress, 3m 42s)
Agent: backend-abc123 (claude-sonnet, high effort)
─────────────────────────────────
Files modified:  3 (+src/auth.py, +tests/test_auth.py, ~src/app.py)
Git diff size:   +142 / -8 lines
Tokens so far:   8,420 input / 3,200 output
Cost so far:     $0.08
Last activity:   12s ago (editing tests/test_auth.py)
─────────────────────────────────
[tail log: bernstein logs --task T-001]
```

### Data sources
- Files modified: `git diff --stat` in the agent's worktree
- Tokens/cost: from cost tracker (if agent reports incrementally) or estimated from elapsed time
- Last activity: mtime of most recently changed file in worktree
- Log tail: from `.sdd/runtime/logs/{session_id}.log`

### API endpoint
`GET /tasks/{id}/live` returns the same data as JSON.

## Files to modify

- `src/bernstein/cli/main.py` (status --live flag)
- `src/bernstein/core/server.py` (live endpoint)
- `src/bernstein/core/worktree.py` (diff stat helper)
- `tests/unit/test_live_introspection.py` (new)

## Completion signal

- `bernstein status --live T-001` shows real-time agent data
- API endpoint returns live JSON
- Updates every 2 seconds
