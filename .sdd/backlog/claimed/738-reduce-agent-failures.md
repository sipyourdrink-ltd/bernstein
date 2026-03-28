# 738 — Reduce Agent Failure Rate (18% → <5%)

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** large
**Depends on:** none

## Problem

18.4% of tasks fail. 59 real failures (excluding test data) share one root cause: "Agent died; no completion signals and no files modified." The agent spawns, sees the task, and dies without doing anything. This wastes tokens and time.

### Root cause analysis

Pattern 1: **Task too large for one agent session** (70% of failures)
- #519 "Distributed cluster mode" — massive architecture task, agent can't even start
- #533 "WASM fast-path" — requires deep research + implementation, agent times out
- #414 "Modern git integration" — fails 3x in a row (max retries)

Pattern 2: **Insufficient context** (20% of failures)
- Agent gets a task description but no guidance on WHERE to start
- No list of relevant files to read first
- No examples of similar completed work

Pattern 3: **Wrong model/effort for task complexity** (10% of failures)
- Complex architect tasks routed to sonnet/high instead of opus/max
- Tasks that need research get normal effort with short timeouts

## Design

### Fix 1: Auto-decompose large tasks before spawning

Before spawning an agent for a `scope: large` or `complexity: high` task, automatically decompose it:
1. Check if task has subtasks already
2. If not, spawn a quick planner (haiku, 30s) that reads the task and outputs 3-5 subtasks
3. Create subtasks as children in the task server
4. Spawn agents for subtasks instead of the parent

```python
# In orchestrator, before spawning:
if task.scope == "large" and not task.children:
    subtasks = self._auto_decompose(task)
    for st in subtasks:
        self._create_subtask(task.id, st)
    return  # Don't spawn for parent, wait for children
```

### Fix 2: Inject file context into task description

When spawning an agent, automatically append relevant file paths:
1. Use the task title/description to grep for related files
2. Add a "Relevant files" section to the agent's prompt:
```
## Relevant files (read these first)
- src/bernstein/core/orchestrator.py (main loop)
- src/bernstein/core/spawner.py (agent spawning)
- tests/unit/test_orchestrator.py (existing tests)
```

This is already partially done in `TaskContextBuilder` but not used for all tasks.

### Fix 3: Complexity-aware model routing

Enforce minimum model requirements:
- `scope: large` → opus/max minimum (never sonnet)
- `scope: medium, complexity: high` → opus/high minimum
- `role: architect` → always opus/max
- After 1 failure → escalate model on retry

### Fix 4: Progressive timeout

Instead of one fixed timeout:
- First attempt: `estimated_minutes * 1.5` or 10min default
- Retry 1: `timeout * 2`
- Retry 2: `timeout * 3` with opus escalation

### Fix 5: Pre-flight validation

Before spawning, check:
- Does the task reference files that exist?
- Is the scope achievable in one agent session?
- Has this exact task failed before? If so, auto-decompose.

## Files to modify

- `src/bernstein/core/orchestrator.py` (auto-decompose, pre-flight)
- `src/bernstein/core/spawner.py` (file context injection, progressive timeout)
- `src/bernstein/core/router.py` (complexity-aware routing)
- `src/bernstein/core/context.py` (better file discovery)
- `tests/unit/test_failure_reduction.py` (new)

## Completion signal

- Large tasks auto-decompose into 3-5 subtasks
- Agent prompts include relevant file paths
- Opus used for all large/architect tasks
- Failure rate measured: target <5% on real tasks
