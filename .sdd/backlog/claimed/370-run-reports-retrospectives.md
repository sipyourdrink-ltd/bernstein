# 370 — Run Reports with Retrospectives

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

After a Bernstein run completes, there's no structured summary of what happened. Users have to piece together results from task server logs, git history, and cost data. DESIGN.md Phase 2 specifies "run reports with retrospectives" but it's not implemented. A post-run report is essential for debugging failed runs, understanding cost allocation, and building confidence in the system.

## Design

Generate a structured report after each run at `.sdd/reports/{run_id}.md`:

### Report contents
- **Run summary**: goal, duration, total cost, tasks completed/failed
- **Per-task breakdown**: agent, model, tokens, cost, duration, outcome
- **Cost analysis**: cost per task, model mix, budget utilization
- **Quality metrics**: tests passed/failed, lint score delta, files changed
- **Timeline**: Gantt-style text view of when each agent was active
- **Retrospective**: auto-generated observations (e.g., "Task 3 failed twice before succeeding — consider increasing timeout" or "80% of cost went to manager planning — consider pre-defined task lists")

### CLI
- `bernstein retro` — show latest run report
- `bernstein retro --run {id}` — show specific run
- `bernstein retro --format json` — machine-readable output

### Auto-generation
Report is generated automatically on run completion (or graceful stop). Uses the existing `generate_retrospective()` function in `src/bernstein/core/retrospective.py` if available, extends it.

## Files to modify

- `src/bernstein/core/retrospective.py` (enhance)
- `src/bernstein/cli/main.py` (retro command improvements)
- `tests/unit/test_recap.py` (new/enhance)

## Completion signal

- Report generated in `.sdd/reports/` after every run
- `bernstein retro` displays it cleanly
- Includes per-task cost breakdown
