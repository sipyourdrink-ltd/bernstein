# 401 — AgencyProvider for msitarzewski/agency-agents

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium
**Depends on:** [400]

## Problem
Agency (`msitarzewski/agency-agents`) should be the default agent catalog. Need a provider that can load agents from the Agency repo (local clone or remote fetch), parse the Agency format, and map it to `CatalogAgent`.

## Implementation
1. Create `AgencyProvider` implementing `CatalogProvider` in `src/bernstein/agents/agency_provider.py`:
   - Support two modes: local path (git clone) and remote URL (GitHub raw/API)
   - Parse Agency agent definitions (YAML/JSON/MD — inspect actual repo format)
   - Map Agency fields to `CatalogAgent` model
   - Handle missing/optional fields gracefully
2. Auto-clone Agency repo to `.sdd/agents/agency/` if not present
3. Pull updates on `refresh()` (git pull if local, re-fetch if remote)
4. Register as default provider with highest priority

## Files
- src/bernstein/agents/agency_provider.py (new)
- tests/unit/test_agency_provider.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_agency_provider.py -x -q
- file_contains: src/bernstein/agents/agency_provider.py :: AgencyProvider
