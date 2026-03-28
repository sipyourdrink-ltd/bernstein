# 404 — Role matching with catalog fallback

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** high
**Depends on:** [400, 403]

## Problem
When the orchestrator assigns a task to a role, it currently only uses built-in roles from `templates/roles/`. It should first search the agent catalog for a specialized agent, then fall back to built-in roles.

## Implementation
1. Add `CatalogRegistry.match(role: str, task_description: str) -> CatalogAgent | None`:
   - Exact role match first (e.g. task role "security" matches agent with role "security")
   - Fuzzy match by description keywords if no exact match
   - Return highest-priority match (by provider priority)
2. Modify `src/bernstein/core/spawner.py`:
   - Before building prompt from `templates/roles/`, check `catalog.match(role, task)`
   - If match found: use `CatalogAgent.system_prompt` instead of template
   - If no match: existing behavior (built-in template)
3. Modify `src/bernstein/core/orchestrator.py`:
   - Pass catalog reference to spawner
   - Log which source was used (catalog vs built-in) in metrics
4. Add `agent_source` field to metrics records

## Files
- src/bernstein/agents/catalog.py — add match()
- src/bernstein/core/spawner.py — integrate catalog lookup
- src/bernstein/core/orchestrator.py — pass catalog to spawner
- src/bernstein/core/metrics.py — add agent_source field
- tests/unit/test_catalog_matching.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_catalog_matching.py -x -q
- file_contains: src/bernstein/core/spawner.py :: CatalogAgent
