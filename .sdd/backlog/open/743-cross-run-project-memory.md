# 743 — Cross-Run Project Memory

**Role:** backend
**Priority:** 2 (high)
**Scope:** small
**Depends on:** none

## Problem

Each Bernstein run starts with zero memory of past runs. The manager re-analyzes the codebase, re-discovers the same patterns, and sometimes re-proposes tasks that already failed. A lightweight project memory injected into the planning context would make agents smarter across runs.

## Design

### Memory file
`.sdd/memory/project_memory.json` — last 20 run outcomes:
```json
[
  {"run_id": "20260328-154500", "goal": "Add auth", "tasks": 5, "done": 4, "failed": 1, "cost": 0.42, "lesson": "JWT tests need separate test database"},
  {"run_id": "20260328-160000", "goal": "Improve coverage", "tasks": 3, "done": 3, "failed": 0, "cost": 0.18, "lesson": ""}
]
```

### Auto-populate
On run completion, the retrospective appends a summary to project_memory.json.

### Inject into planning
When the manager agent starts planning, include the last 5 memory entries in its context:
```
## Recent run history
- "Add auth": 4/5 tasks done, 1 failed (JWT tests need separate test database)
- "Improve coverage": 3/3 done
```

This helps the manager avoid known pitfalls and not re-propose failed approaches.

## Files to modify

- `src/bernstein/core/retrospective.py` (append to project_memory.json)
- `src/bernstein/core/context.py` (inject memory into planning context)
- `tests/unit/test_project_memory.py` (new)

## Completion signal

- Project memory populated after each run
- Manager agent receives last 5 run summaries in context
- Agents avoid repeating known failures
