# 626 — Merge Conflict Resolution

**Role:** backend
**Priority:** 3 (medium)
**Scope:** large
**Depends on:** none

## Problem

When multiple agents work in parallel on the same codebase, merge conflicts are inevitable. There is no automated conflict resolution strategy. Currently, conflicts require manual intervention, which defeats the purpose of automated multi-agent orchestration.

## Design

Build git worktree management with a 4-tier merge conflict resolution system. Tier 1 (manual): flag conflicts for human review. Tier 2 (simple): use git's built-in auto-merge for non-overlapping changes. Tier 3 (smart): AST-aware merge that understands code structure — if two agents add different functions to the same file, merge both without conflict. Tier 4 (AI-powered): spawn a dedicated merge-resolution agent that reads both versions and the base, understands the intent of each change, and produces a correct merge. The orchestrator selects the tier based on conflict complexity. Implement worktree lifecycle management: create worktree per agent, attempt merge after task completion, escalate through tiers on conflict. Track merge success rates per tier for continuous improvement.

## Files to modify

- `src/bernstein/core/worktree.py` (new)
- `src/bernstein/core/merge_resolver.py` (new)
- `src/bernstein/core/ast_merge.py` (new)
- `src/bernstein/core/orchestrator.py`
- `templates/roles/merge-resolver.md` (new)
- `tests/unit/test_merge_resolver.py` (new)

## Completion signal

- Each agent gets its own git worktree
- Non-conflicting parallel changes merge automatically
- AST-aware merge resolves structural conflicts (e.g., two new functions in same file)
- AI merge agent handles remaining conflicts
