# 745 — Task Progress Stage Indicators

**Role:** frontend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** #744

## Problem

In-progress tasks show "● working" but no indication of what stage the agent is in. Is it just starting? Halfway done? Running tests? A stage-based progress indicator gives users confidence the system is working and helps estimate completion time.

## Design

### Task stages
Each in-progress task shows its current stage:
```
spawning → reading files → editing code → running tests → reviewing → done
```

### Visual
In the task table and TUI:
```
T-002  ● editing   qa    Write auth tests    sonnet  ██████░░░░  2m
```

### Data source
The agent progress endpoint (#742) includes a `stage` field. The orchestrator maps agent activity to stages:
- `spawning`: agent process just started
- `reading`: agent accessing files (first 30s typically)
- `editing`: agent creating/modifying files
- `testing`: agent running test commands
- `reviewing`: janitor checking completion signals

### Fallback
If no progress data, estimate stage from elapsed time and task complexity.

## Files to modify

- `src/bernstein/tui/widgets.py` (stage display in task list)
- `src/bernstein/dashboard/templates/index.html` (stage indicator)
- `src/bernstein/core/server.py` (include stage in task status)

## Completion signal

- In-progress tasks show current stage in TUI and web dashboard
- Stage updates in real-time as agent progresses
