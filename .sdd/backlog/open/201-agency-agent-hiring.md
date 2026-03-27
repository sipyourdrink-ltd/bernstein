# Hire specialist agents from Agency and skill registries

**Role:** backend
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
Bernstein only uses generic role templates (backend, qa, architect). It should be able to discover and hire specialist agents from external registries like Agency (https://github.com/msitarzewski/agency-agents) and skill directories.

## Sources to integrate
1. **Agency agents** — /Users/sasha/IdeaProjects/cloned/agency-agents/ (local clone, 100+ specialist agents with system prompts)
2. **Skills registries** — external repos with reusable agent skills/tools

## Implementation

### Phase 1: Agency loader (local)
The `agency_loader.py` already exists but needs to be wired into the spawner:
1. On startup, scan agency-agents directory (configurable path in bernstein.yaml)
2. Parse each agent .md file — extract name, description, tools, system prompt
3. Build a catalog: role → AgencyAgent mapping
4. When spawner needs a specialist role (e.g. "security-engineer", "database-optimizer"), check catalog first
5. If found, use the agency agent's system prompt instead of the generic template

### Phase 2: Smart role matching
When the manager creates tasks, it should:
1. See the available agency agents (pass catalog summary in manager prompt)
2. Assign tasks to the most specific role available (e.g. "database-optimizer" instead of generic "backend")
3. Fall back to generic roles if no specialist matches

### Phase 3: Remote skill discovery (future)
- Fetch agent definitions from GitHub repos (skills-hub, awesome-skills, etc.)
- Cache locally in .sdd/agents/external/
- Match tasks to skills by description similarity

## Config (bernstein.yaml)
```yaml
agency:
  path: /path/to/agency-agents  # local clone
  auto_discover: true
  # Future: remote registries
  # registries:
  #   - https://github.com/msitarzewski/agency-agents
```

## Files
- src/bernstein/core/agency_loader.py — already exists, enhance
- src/bernstein/core/spawner.py — use agency catalog in prompt rendering
- src/bernstein/core/seed.py — parse agency config from seed
- templates/roles/manager/system_prompt.md — include available specialists

## Acceptance criteria
- Agency agents from local directory are discovered on startup
- Tasks assigned to specialist roles use agency system prompts
- Manager agent sees available specialists when planning
- Falls back to generic roles when no specialist matches
- Tests cover loader, matching, and fallback
