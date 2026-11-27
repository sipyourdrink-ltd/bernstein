---
name: bernstein-create-task
description: >
  Create a new task in the Bernstein task queue. Use when the user wants to
  add a task, delegate work to an agent, file a bug fix, or queue up work
  for orchestration to handle.
---

# Create Bernstein Task

Create a new task in the Bernstein task queue for orchestration and have it picked up by an agent.

## When to Use

- User says "create a task for..." or "add this to bernstein"
- User wants to delegate a coding task to an agent
- User describes a bug fix, feature, or refactor that should be orchestrated
- User says "have an agent do this" or "queue this up"

## Instructions

1. Ask the user (if not already clear) for:
   - **Title**: short description of the task
   - **Role**: one of `backend`, `frontend`, `qa`, `security`, `devops`, `docs` (default: `backend`)
   - **Priority**: 0 (urgent) to 3 (low) (default: 1)
   - **Scope**: `tiny`, `small`, `medium`, `large` (default: `small`)

2. Run `scripts/create-task.sh` with the task details:
   ```bash
   scripts/create-task.sh "Fix the auth middleware to handle expired tokens" backend 1 small
   ```

3. Show the created task ID and confirm it's queued:
   ```
   Task TASK-042 created (role=backend, priority=1)
   An agent will pick it up shortly.
   ```

4. If the user wants to track it, suggest using `/bernstein-status`.

## Tips

- For complex tasks, suggest breaking them into smaller subtasks
- For risky changes, add `--require-review` flag
- High-priority (0) tasks get picked up immediately
