# 410h — Git hygiene checkpoint

**Role:** devops
**Priority:** 2 (high)
**Scope:** small
**Complexity:** low

## Task

Periodic git hygiene before continuing with next batch of tickets:

1. Check for stale worktrees — remove any that are merged or abandoned
2. Check for uncommitted changes across all worktrees
3. Verify `main` is pushed and up to date with `origin/main`
4. Verify no `master` branch exists (local or remote)
5. Clean up merged local branches
6. Run `uv run ruff check src/ && uv run ruff format --check src/`
7. Run `uv run pyright` — 0 errors
8. Run `uv run python scripts/run_tests.py -x` — all pass
9. Drop any stale git stashes

## Completion signals
- `git worktree list` shows only the main worktree
- `git branch -a` shows only `main` and `remotes/origin/main`
- All CI checks pass
