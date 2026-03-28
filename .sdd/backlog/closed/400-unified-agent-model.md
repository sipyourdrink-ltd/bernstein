# 400 — Unified agent model and catalog provider interface

**Role:** architect
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
Bernstein uses hardcoded built-in roles from `templates/roles/`. To support external agent catalogs (Agency, custom registries), we need a unified agent model and a pluggable provider interface that all catalog sources implement.

## Implementation
1. Define `CatalogAgent` dataclass in `src/bernstein/agents/catalog.py`:
   - `id: str` — unique identifier (e.g. `agency:code-reviewer`)
   - `name: str` — human-readable name
   - `role: str` — role category (backend, qa, security, etc.)
   - `description: str` — what the agent does
   - `system_prompt: str` — full system prompt text
   - `tools: list[str]` — tool names/capabilities
   - `source: str` — catalog origin (e.g. `agency`, `custom-registry`)
2. Define `CatalogProvider` protocol in `src/bernstein/agents/providers.py`:
   - `async def fetch_agents() -> list[CatalogAgent]`
   - `async def refresh() -> list[CatalogAgent]`
   - `def provider_id() -> str`
   - `def is_available() -> bool`
3. Define `CatalogRegistry` that manages multiple providers, handles priority, dedup, and fallback.

## Files
- src/bernstein/agents/catalog.py (new)
- src/bernstein/agents/providers.py (new)
- tests/unit/test_catalog_model.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_catalog_model.py -x -q
- file_contains: src/bernstein/agents/catalog.py :: CatalogAgent
- file_contains: src/bernstein/agents/providers.py :: CatalogProvider


---
**completed**: 2026-03-28 04:30:50
**task_id**: 559b23f1591d
**result**: Completed: Add WorktreeManager for agent isolation
