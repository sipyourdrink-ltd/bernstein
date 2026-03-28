# 403 — Catalog cache and auto-discovery on startup

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium
**Depends on:** [400, 401]

## Problem
Loading agents from remote catalogs on every task assignment is too slow. Need local cache with TTL-based refresh and auto-discovery at startup.

## Implementation
1. Cache layer in `CatalogRegistry`:
   - Write merged catalog to `.sdd/agents/catalog.json` after each sync
   - Store per-entry metadata: `source`, `fetched_at`, `ttl_seconds`
   - On startup: load from cache if fresh (within TTL), else refresh from providers
   - Default TTL: 1 hour for remote, 5 minutes for local
2. Auto-discovery at orchestrator startup:
   - `CatalogRegistry.discover()` called during bootstrap
   - Fetches from all enabled providers in priority order
   - Merges results (higher-priority provider wins on conflict by role)
   - Writes cache
3. Manual refresh via `CatalogRegistry.refresh(force=True)` bypasses TTL
4. Graceful degradation: if all providers fail, use cached data; if no cache, fall back to built-in roles

## Files
- src/bernstein/agents/catalog.py — add cache logic to CatalogRegistry
- src/bernstein/core/bootstrap.py — call discover() on startup
- tests/unit/test_catalog_cache.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_catalog_cache.py -x -q
- path_exists: .sdd/agents/catalog.json (after first run)
