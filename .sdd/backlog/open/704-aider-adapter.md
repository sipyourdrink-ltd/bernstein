# 704 — Aider CLI Adapter

**Role:** backend
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Aider has 25K+ GitHub stars and is the most popular CLI coding assistant. Every Aider user is a potential Bernstein user — "what if I could run 5 Aiders in parallel?" Aider users are exactly our target audience. Not supporting Aider is leaving the biggest community on the table.

## Design

Implement `src/bernstein/adapters/aider.py`:
- Spawn aider with `--message` flag for non-interactive mode
- Map Bernstein model configs to aider model flags (`--model`)
- Support aider's `--yes` flag for auto-confirmation
- Handle aider's git commit behavior (it auto-commits)
- Register in adapter registry

## Files to modify

- `src/bernstein/adapters/aider.py` (new)
- `src/bernstein/agents/registry.py`
- `tests/unit/test_adapter_aider.py` (new)
- `README.md` (add aider to supported agents list)

## Completion signal

- `bernstein -g "task" --cli aider` works
- Tests pass
- Aider's auto-commit integrates with worktree isolation
