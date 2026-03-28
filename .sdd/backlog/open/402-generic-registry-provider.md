# 402 — GenericRegistryProvider for custom agent catalogs

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** medium
**Depends on:** [400]

## Problem
Users should be able to plug in any external agent catalog without modifying Bernstein core. Need a generic provider that handles arbitrary repos/URLs with configurable field mapping.

## Implementation
1. Create `GenericRegistryProvider` implementing `CatalogProvider` in `src/bernstein/agents/generic_provider.py`:
   - Accept config: `url`, `path`, `format` (yaml/json), `field_map` (dict mapping source fields to CatalogAgent fields)
   - Support git repos (clone + pull) and HTTP endpoints (GET + parse)
   - Apply field_map to transform source format into CatalogAgent
   - Handle pagination for HTTP sources
2. Validate field_map at init time — fail fast on missing required mappings
3. Support glob patterns for discovering agent files in repos (e.g. `agents/**/*.yaml`)

## Files
- src/bernstein/agents/generic_provider.py (new)
- tests/unit/test_generic_provider.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_generic_provider.py -x -q
- file_contains: src/bernstein/agents/generic_provider.py :: GenericRegistryProvider
