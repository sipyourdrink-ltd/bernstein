# 418 — Automated agent conflict resolution and merge strategy

**Role:** architect
**Priority:** 2
**Scope:** medium
**Complexity:** high
**Depends on:** [414]

## Problem
File ownership enforcement prevents concurrent edits but is a pessimistic lock that reduces parallelism. When agents finish and their worktree changes conflict, there's no automated resolution. Any team using Bernstein on a real project hits merge conflicts within the first hour. Without automation, users fall back to manual `git merge` — defeating the purpose.

## Implementation
1. After agent completion, automated `git merge --no-commit` from agent worktree into main branch with conflict detection
2. If no conflicts: fast-forward merge, done
3. If conflicts detected:
   a. Route to a `resolver` agent role that reads both sides and resolves
   b. If resolver fails: queue the conflicting task for re-execution after the earlier one lands
4. Track merge success/failure rates as evolution metrics
5. System learns which task decompositions cause conflicts and adjusts batch grouping

## Files
- src/bernstein/core/git_ops.py — merge_with_conflict_detection()
- src/bernstein/core/spawner.py — integrate conflict resolution
- templates/roles/resolver/system_prompt.md (new)
- tests/unit/test_conflict_resolution.py (new)

## Completion signals
- file_contains: src/bernstein/core/git_ops.py :: merge_with_conflict_detection
- path_exists: templates/roles/resolver/system_prompt.md
