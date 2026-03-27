# Implement SandboxValidator with git worktree isolation

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** high

## Problem
Every evolution proposal must be tested in isolation before applying. Research shows
git worktrees are the 2026 standard for AI agent evolution experiments. The sandbox
must:
1. Create isolated git worktree
2. Apply proposed changes
3. Run full test suite + janitor signals
4. Report pass/fail with metrics
5. Clean up worktree

## Implementation
- create_sandbox(proposal) → SandboxResult
  1. git worktree add .sdd/sandboxes/{proposal_id} -b evolution/{proposal_id}
  2. Apply proposal diff to worktree
  3. Run: uv run pytest tests/ -x -q (in worktree)
  4. Run: janitor verification on worktree
  5. Compare metrics against baseline
  6. Return SandboxResult(passed, metrics_delta, log_path)
  7. git worktree remove .sdd/sandboxes/{proposal_id}

- For L0 changes: simple schema validation (no worktree needed)
- For L1 changes: worktree + synthetic task replay
- For L2 changes: worktree + full test suite + golden dataset

## Files
- src/bernstein/evolution/sandbox.py (new)
- tests/unit/test_sandbox.py (new)

## Completion signals
- path_exists: src/bernstein/evolution/sandbox.py
- test_passes: uv run pytest tests/unit/test_sandbox.py -x -q
- file_contains: src/bernstein/evolution/sandbox.py :: git worktree
