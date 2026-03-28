# 380 — A2A (Agent-to-Agent) protocol support

**Role:** architect
**Priority:** 3
**Scope:** large
**Complexity:** high

## Problem
Bernstein agents communicate only through the task server (create task → assign → complete). There's no direct agent-to-agent communication. Google's A2A protocol provides a standard for agent interop that could enable: agents requesting help from each other, agents from different orchestrators collaborating, and external agents connecting to Bernstein.

## Implementation

### 1. A2A Agent Card
Each Bernstein agent publishes an Agent Card (A2A spec) at a well-known URL:
```json
{
  "name": "bernstein-backend-1",
  "description": "Backend development agent",
  "capabilities": ["code_write", "test_run", "file_edit"],
  "protocol_version": "0.1",
  "endpoint": "http://localhost:8052/a2a/backend-1"
}
```

### 2. A2A endpoints on task server
Add A2A-compatible endpoints to the existing HTTP server:
- `GET /.well-known/agent.json` — Bernstein orchestrator agent card
- `POST /a2a/tasks/send` — receive task from external A2A agent
- `POST /a2a/tasks/{id}/artifacts` — receive artifacts from agents
- Map A2A task lifecycle to Bernstein task states

### 3. Agent-to-agent delegation
Allow agents to delegate sub-tasks to other running agents:
- Agent writes to bulletin board: "Need help: review this PR diff"
- Orchestrator routes to appropriate agent (reviewer role)
- Response posted back to bulletin board
- Requesting agent picks up the result

### 4. External agent federation
Allow external A2A-compatible agents to connect:
```yaml
# bernstein.yaml
federation:
  - name: external-security-scanner
    endpoint: https://security-agent.example.com
    capabilities: [vulnerability_scan]
```

## Files
- src/bernstein/core/a2a.py (new) — A2A protocol handler
- src/bernstein/core/server.py — add A2A endpoints
- src/bernstein/core/bulletin.py — agent-to-agent messaging
- tests/unit/test_a2a.py (new)

## Completion signals
- file_contains: src/bernstein/core/a2a.py :: A2AHandler
- file_contains: src/bernstein/core/server.py :: .well-known/agent.json
