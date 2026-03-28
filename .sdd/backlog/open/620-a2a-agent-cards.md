# 620 — A2A Agent Cards

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** #606

## Problem

Bernstein agents cannot be discovered or invoked by external frameworks. The A2A (Agent-to-Agent) protocol, supported by 150+ organizations, defines a standard for cross-framework agent discovery via Agent Cards. Without A2A support, Bernstein is isolated from the broader agent ecosystem.

## Design

Implement A2A Agent Card discovery so Bernstein agents can be found and orchestrated by external frameworks, and Bernstein can discover and use external agents. Each Bernstein agent role publishes an Agent Card at a well-known endpoint (`/.well-known/agent.json`) describing its capabilities, input/output schemas, and authentication requirements. The orchestrator can also consume external Agent Cards to discover and route tasks to agents outside Bernstein. Implement the A2A task lifecycle: create, update, complete, cancel. Store Agent Card definitions alongside role templates. Support both HTTP and local discovery modes.

## Files to modify

- `src/bernstein/core/a2a.py` (new)
- `src/bernstein/core/agent_card.py` (new)
- `src/bernstein/core/task_server.py`
- `templates/roles/` (add Agent Card metadata to each role)
- `tests/unit/test_a2a.py` (new)

## Completion signal

- Bernstein publishes Agent Cards at `/.well-known/agent.json`
- External A2A clients can discover and invoke Bernstein agents
- Bernstein can discover and route tasks to external A2A agents
