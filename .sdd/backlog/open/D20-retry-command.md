# D20 — Retry Command for Failed Tasks

**Priority:** P1
**Scope:** small (10-20 min)
**Wave:** 1 — Developer Love

## Problem
When a run partially fails, users must re-run the entire goal from scratch, wasting time and money re-executing tasks that already succeeded.

## Solution
- Implement `bernstein retry` that re-runs only failed tasks from the last run.
- Read task state from `.sdd/runs/latest/tasks/`, filtering for tasks with `status: failed`.
- Re-enqueue failed tasks while preserving their original configuration (model, context, dependencies).
- Skip tasks with `status: completed` — do not re-execute them.
- Display before execution: "Retrying 2 of 5 tasks (skipping 3 completed)."
- Write results back to the same run directory, updating task statuses.
- Support `--run <run-id>` to retry a specific historical run instead of the latest.

## Acceptance
- [ ] `bernstein retry` re-runs only failed tasks from the last run
- [ ] Completed tasks are skipped and not re-executed
- [ ] The pre-execution message correctly reports how many tasks will be retried vs skipped
- [ ] Task results are updated in the run directory after retry
- [ ] `--run <run-id>` retries tasks from the specified historical run
