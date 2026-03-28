# 500 -- Idle agent detection: kill finished agents when open tasks exist

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** medium

## Problem
Agents that have completed their task continue to linger as live OS processes instead of exiting promptly. When new open tasks appear (e.g., from backlog files not yet pushed to server, or from evolution cycles), the orchestrator spawns fresh agents while old finished ones still consume memory and process slots. Result: idle agents accumulate, concurrency ceiling is hit, and new tasks wait unnecessarily.

Two sub-problems:
1. **Finished agents do not exit**: An agent completes its task (calls /complete) but the spawner process does not reap it, leaving zombie/lingering processes.
2. **Backlog not auto-ingested**: Tickets in `.sdd/backlog/open/` are not automatically pushed to the task server on startup or periodically, so the server shows 0 open tasks even though work is waiting.

## Root cause
- Check `spawner.py`: does it call `proc.wait()` after `POST /complete`? Does it have a post-completion hook to terminate the subprocess?
- Check `orchestrator.py` tick loop: does it scan `.sdd/backlog/open/` and POST any files not yet on the server?
- Check `janitor.py` (if exists): is there a reaper that monitors agent PIDs and cleans up?

## Implementation

### 1. Spawner post-completion cleanup
In `src/bernstein/core/spawner.py`:
- After agent signals completion (exit code 0 or `/complete` called), call `proc.terminate()` then `proc.wait(timeout=5)`
- Remove agent from active set immediately
- Log: `"Agent {id} finished task {task_id}, process reaped"`

### 2. Backlog auto-ingestion
In `src/bernstein/core/orchestrator.py` tick loop (or a dedicated `IngestBacklog` step):
- On each tick, scan `.sdd/backlog/open/*.md`
- For each file not already in the task server (check by title or source comment), POST it to `/tasks`
- Move ingested files to `.sdd/backlog/claimed/` to avoid re-ingestion
- This ensures the server always reflects ground truth from the backlog

### 3. Idle agent watchdog
In `src/bernstein/core/janitor.py` (or orchestrator):
- Every N seconds, check: are there open tasks AND idle/stuck agents (claimed for > timeout_minutes)?
- If yes: log warning, attempt to reassign or respawn
- Metric: `idle_agent_waste_seconds` — tracks how long tasks waited due to agent idling

### 4. Agent exit discipline
In adapter prompts (`templates/roles/*/system_prompt.md`):
- Reinforce: "When you have completed all assigned tasks and called the completion endpoint, immediately exit with `exit 0`."
- Spawner should enforce max_turns as hard limit

## Files
- src/bernstein/core/spawner.py — post-completion reap
- src/bernstein/core/orchestrator.py — backlog auto-ingestion
- src/bernstein/core/janitor.py — idle watchdog
- templates/roles/ — exit discipline reminder

## Completion signals
- file_contains: src/bernstein/core/orchestrator.py :: ingest_backlog
- file_contains: src/bernstein/core/spawner.py :: proc.terminate
- test_passes: uv run pytest tests/unit/test_idle_agent_detection.py -x -q


---
**completed**: 2026-03-28 05:09:51
**task_id**: 97688522adc7
**result**: Completed: 500 -- Idle agent detection: kill finished agents when open tasks exist
