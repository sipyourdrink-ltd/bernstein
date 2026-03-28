# 406 — CLI commands: bernstein agents sync/list/validate

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** low
**Depends on:** [400, 401, 403]

## Problem
Users need CLI commands to manage agent catalogs: sync from remote, list available agents, and validate catalog health.

## Implementation
1. Add `agents` command group to `src/bernstein/cli/main.py`:
   - `bernstein agents sync` — force-refresh all enabled catalogs, update cache
   - `bernstein agents list` — show all cached agents in table format (id, name, role, source)
   - `bernstein agents list --source agency` — filter by source
   - `bernstein agents validate` — check all providers reachable, validate agent schemas, report issues
2. Table output via rich.table for `list`
3. `sync` shows progress per provider (fetching, parsing, caching)
4. `validate` returns exit code 1 if any provider is unreachable or has invalid agents

## Files
- src/bernstein/cli/main.py — add agents command group
- tests/unit/test_cli_agents.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_cli_agents.py -x -q
- file_contains: src/bernstein/cli/main.py :: agents sync
