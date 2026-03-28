# 336d — A2A / ACP Protocol Compliance

**Role:** backend
**Priority:** 0 (urgent)
**Scope:** large
**Depends on:** none

## Problem

A2A (Agent-to-Agent Protocol) is now a Linux Foundation standard backed by Google, OpenAI, Anthropic, Microsoft, AWS. ACP (Agent Client Protocol) is backed by JetBrains (Air) and Zed. No CLI orchestrator implements either. First to comply = enterprise adoption + JetBrains Air integration for free.

## Design

### A2A Agent Card
Serve `/.well-known/agent.json` from the task server:
```json
{
  "name": "Bernstein",
  "description": "Multi-agent orchestration for CLI coding agents",
  "url": "http://localhost:8052",
  "capabilities": ["orchestration", "parallel-execution", "cost-tracking"],
  "skills": [
    {"name": "code-generation", "description": "Spawn agents to write code"},
    {"name": "code-review", "description": "Cross-model code review"},
    {"name": "test-generation", "description": "Generate tests for existing code"}
  ],
  "protocol": "a2a/1.0"
}
```

### A2A Runs API
- `POST /a2a/runs` — start an orchestration run (maps to bernstein's goal system)
- `GET /a2a/runs/{id}` — get run status
- `GET /a2a/runs/{id}/events` — SSE stream of run events
- `POST /a2a/runs/{id}/cancel` — cancel a run

### ACP Transport
- Implement ACP message format over HTTP
- JetBrains Air auto-discovers ACP-compatible agents
- No custom JetBrains plugin needed — just protocol compliance

### Discovery
External systems find Bernstein via:
1. DNS-SD / mDNS on local network
2. Agent Card at well-known URL
3. MCP server registration (already implemented)

## Files to modify

- `src/bernstein/core/routes/a2a.py` (enhance existing A2A handler)
- `src/bernstein/core/server.py` (register A2A routes)
- `tests/unit/test_a2a.py` (extend)

## Completion signal

- `curl localhost:8052/.well-known/agent.json` returns valid Agent Card
- External A2A client can start and monitor a Bernstein run
- JetBrains Air can discover Bernstein via ACP
