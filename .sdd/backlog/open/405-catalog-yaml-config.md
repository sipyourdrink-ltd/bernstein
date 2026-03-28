# 405 — Catalog configuration in bernstein.yaml

**Role:** backend
**Priority:** 2
**Scope:** small
**Complexity:** low
**Depends on:** [400]

## Problem
Users need to configure which agent catalogs are enabled, their sources, and priority order via `bernstein.yaml`.

## Implementation
1. Add `catalogs` section to `bernstein.yaml` schema:
   ```yaml
   catalogs:
     - name: agency
       type: agency
       enabled: true
       source: https://github.com/msitarzewski/agency-agents
       priority: 100  # higher = checked first
     - name: internal-agents
       type: generic
       enabled: true
       path: ./custom-agents/
       format: yaml
       glob: "**/*.yaml"
       field_map:
         id: agent_id
         name: display_name
         role: category
         system_prompt: prompt
       priority: 50
   ```
2. Parse catalogs config in `src/bernstein/core/seed.py` during init
3. Pass config to `CatalogRegistry` which instantiates the correct provider per entry
4. Default config (no `catalogs` section): Agency provider only, remote mode

## Files
- src/bernstein/core/seed.py — parse catalogs config
- src/bernstein/agents/catalog.py — accept config in CatalogRegistry
- templates/bernstein.yaml — add catalogs section with Agency default

## Completion signals
- file_contains: src/bernstein/core/seed.py :: catalogs
- file_contains: templates/bernstein.yaml :: catalogs
