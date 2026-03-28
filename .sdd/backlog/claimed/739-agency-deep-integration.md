# 739 — Agency Deep Integration: Specialist Prompts + Capabilities

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Agency (github.com/msitarzewski/agency-agents) has 100+ specialist agents with detailed system prompts, but our integration only extracts name/role from the YAML frontmatter. We're ignoring the rich system prompts, capability declarations, and tool preferences that Agency agents define. Our agents use generic role prompts from `templates/roles/` instead of the specialized Agency prompts.

## Current state

`AgencyProvider` loads agents but only uses:
- `name` from frontmatter
- `division` → mapped to Bernstein role via `_DIVISION_ROLE_MAP`

It IGNORES:
- The full system prompt (the markdown body after frontmatter)
- `capabilities` field
- `tools` field
- `model_preferences` field

## Design

### Use Agency system prompts

When an Agency agent is matched to a task, use its FULL markdown body as the system prompt instead of the generic role template:

```python
# Current (generic):
prompt = render_role_prompt("backend", task_context)

# New (Agency-specific if available):
agency_agent = catalog.match(task)
if agency_agent and agency_agent.system_prompt:
    prompt = agency_agent.system_prompt + "\n\n" + task_context
else:
    prompt = render_role_prompt(task.role, task_context)
```

### Capability-based task matching

Agency agents declare capabilities like:
```yaml
capabilities: [api-design, database-schema, authentication]
```

Match tasks to agents by capability, not just role:
- Task "Add JWT auth" → match agent with `authentication` capability
- Task "Design database schema" → match agent with `database-schema` capability

### Tool preferences

Agency agents declare preferred tools:
```yaml
tools: [pytest, ruff, mypy]
```

Pass these to the agent's MCP config or system prompt.

### Auto-sync on startup

On `bernstein init` or first run, auto-clone/update Agency repo:
```
~/.bernstein/catalogs/agency/  # cached clone
```
Refresh every 24h or on `bernstein agents sync`.

### Enriched catalog display

`bernstein agents list` should show:
```
NAME                 ROLE      CAPABILITIES                    SOURCE
api-architect        backend   api-design, rest, graphql       agency
auth-specialist      security  authentication, oauth, jwt      agency
test-engineer        qa        pytest, integration, e2e        agency
react-developer      frontend  react, typescript, css          agency
...                  ...       ...                             built-in
```

## Files to modify

- `src/bernstein/agents/agency_provider.py` (use full prompts, capabilities)
- `src/bernstein/agents/catalog.py` (capability matching)
- `src/bernstein/core/spawner.py` (use Agency prompt when available)
- `tests/unit/test_agency_provider.py` (extend)

## Completion signal

- Agency agents' full system prompts used when matched
- Capability-based matching outperforms role-only matching
- `bernstein agents list` shows capabilities
- Tasks matched to specialist agents by capability
