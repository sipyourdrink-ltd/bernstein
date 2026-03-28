# 734 — Synthesis Agent: Fan-Out / Fan-In Pattern

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

When Bernstein decomposes a feature into N parallel subtasks (e.g., "implement endpoint", "write tests", "update docs"), each agent works independently. After all complete, the changes are merged individually. But nobody reviews the combined result for consistency — the endpoint agent might use different naming than the test agent, or the docs might reference a different API shape. A synthesis step that reviews all parallel work together catches these integration issues.

## Design

### Automatic synthesis after parallel fan-out
When a group of tasks with the same parent complete:
1. Orchestrator detects all children of a parent task are done
2. Spawns a lightweight "synthesis" agent with all diffs as context
3. Synthesis agent reviews for consistency: naming, API contracts, import paths
4. Can make small fixes (rename variables, fix imports) or flag inconsistencies
5. Reports a synthesis summary to the parent task

### Synthesis agent role
```markdown
# Synthesis Agent
You review the combined output of multiple parallel agents.
Check for: naming consistency, API contract alignment, import correctness.
Make minimal fixes. Flag anything that needs human review.
```

### When to trigger
- Only when 2+ tasks completed in parallel for the same feature
- Only when task has `synthesis: true` in config or auto-detected from task graph
- Uses cheap model (haiku/sonnet normal effort) since it's review, not creation

### Configuration
```yaml
# bernstein.yaml
synthesis:
  enabled: true
  model: sonnet
  effort: normal
  auto_detect: true  # trigger when 2+ parallel tasks complete
```

## Files to modify

- `src/bernstein/core/orchestrator.py` (detect fan-in completion, spawn synthesis)
- `templates/roles/synthesis.md` (new role prompt)
- `tests/unit/test_synthesis.py` (new)

## Completion signal

- After 3 parallel tasks complete, synthesis agent runs automatically
- Synthesis agent catches naming inconsistencies in test cases
- Cheap model used for synthesis (not expensive)
