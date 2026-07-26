---
description: Start a Bernstein orchestration run with a goal
---

Start an orchestration run. Bernstein decomposes the goal into tasks, spawns CLI coding agents in parallel git worktrees, verifies their output, and merges results. No model sits in the coordination loop, so the run replays byte-identically.

Usage: /bernstein:run $ARGUMENTS

The argument is the goal string describing what you want built or fixed. Bernstein handles task decomposition, agent assignment, and verification automatically.
