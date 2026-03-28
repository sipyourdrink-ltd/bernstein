# 513 — Agency catalog showcase + auto-discovery of agent directories

**Role:** backend
**Priority:** 2
**Scope:** medium
**Complexity:** medium

## Problem
Agency integration exists (#400-406) but is invisible as a competitive advantage. Users don't know: (a) that 100+ agents are available, (b) how catalogs improve task outcomes, (c) which agents are being used for their tasks. Also, agent directory auto-discovery is limited to one hardcoded source (Agency).

## Implementation

### 1. Showcase in CLI
`bernstein agents showcase`:
- Rich display of available agents grouped by division (engineering, QA, security, design, etc.)
- For each agent: name, description, match count (how many tasks used this agent), success rate
- Highlight "featured" agents with highest success rates
- Show which agent will be selected for a given role: `bernstein agents match --role security`

### 2. Auto-discovery of agent directories
Scan known sources for agent catalogs:
- GitHub: search for repos tagged `bernstein-agents` or containing `.bernstein-catalog.yaml`
- npm: packages with `bernstein-agent` keyword
- Local: `~/.bernstein/agents/` user-level agent definitions
- Project: `.sdd/agents/local/` project-level custom agents

### 3. Agent directory registry
`.sdd/agents/registry.json`:
```json
{
  "directories": [
    {"name": "agency", "url": "https://github.com/msitarzewski/agency-agents", "agents": 127, "last_sync": "2026-03-28T12:00:00Z"},
    {"name": "local", "path": "~/.bernstein/agents/", "agents": 3, "last_sync": "2026-03-28T12:00:00Z"}
  ],
  "total_agents": 130,
  "last_full_sync": "2026-03-28T12:00:00Z"
}
```

### 4. Dashboard integration
Show active catalog agent in TUI agent widget:
```
◉ BACKEND (agency:code-reviewer) SONNET 2:14
  → Add rate limiting middleware
```
Instead of just `◉ BACKEND SONNET 2:14`.

### 5. Metrics tracking
Track per-agent-source success rates:
- Built-in roles vs Agency agents vs custom agents
- Feed into evolution: if Agency agents perform better for certain tasks, increase their priority

## Files
- src/bernstein/cli/main.py — add `agents showcase`, `agents match`
- src/bernstein/agents/discovery.py (new) — auto-discovery logic
- src/bernstein/agents/catalog.py — registry tracking
- src/bernstein/cli/dashboard.py — show catalog agent name
- tests/unit/test_agent_discovery.py (new)

## Completion signals
- file_contains: src/bernstein/agents/discovery.py :: AgentDiscovery
- file_contains: src/bernstein/cli/main.py :: agents showcase
