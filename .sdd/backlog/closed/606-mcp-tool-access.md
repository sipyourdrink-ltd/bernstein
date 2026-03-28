# 606 — MCP Tool Access

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

Bernstein agents cannot use MCP (Model Context Protocol) servers as tools. MCP has 97M monthly SDK downloads and is table stakes for any agent framework in 2026. Without MCP support, Bernstein agents are limited to CLI tools and cannot access the growing ecosystem of MCP-based capabilities.

## Design

Implement native MCP tool access so Bernstein agents can discover and invoke tools from any MCP server. The orchestrator should manage MCP server lifecycle: start servers as needed, maintain connections, and shut down when the run completes. Support both stdio and SSE transport modes. Agent task descriptions can specify required MCP servers (e.g., `mcp_servers: ["github", "filesystem"]`). The spawner passes MCP server connection info to the agent adapter, which configures the underlying CLI agent to use them. Store MCP server configs in `.sdd/config.toml` under `[mcp.servers]`. Provide sensible defaults for common servers (GitHub, filesystem, web search).

## Files to modify

- `src/bernstein/core/mcp_manager.py` (new)
- `src/bernstein/core/spawner.py`
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/adapters/claude.py`
- `.sdd/config.toml`
- `tests/unit/test_mcp_manager.py` (new)

## Completion signal

- Agents can invoke tools from a configured MCP server during a run
- Both stdio and SSE transports work
- MCP server lifecycle managed (start/stop) by the orchestrator
