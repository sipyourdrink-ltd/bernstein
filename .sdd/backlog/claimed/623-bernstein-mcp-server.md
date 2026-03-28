# 623 — Bernstein MCP Server

**Role:** backend
**Priority:** 3 (medium)
**Scope:** medium
**Depends on:** #606

## Problem

Bernstein's orchestration capabilities cannot be invoked from MCP-compatible agents. If another agent (e.g., Claude Code, Cursor) wants to orchestrate a multi-agent task via Bernstein, there is no standard interface. This limits Bernstein's reach to direct CLI usage only.

## Design

Expose Bernstein's orchestration API as an MCP server. Tools to expose: `orchestrate` (run a full orchestration with task description and budget), `task_status` (check status of a running orchestration), `task_list` (list all tasks in current backlog), and `stop` (cancel a running orchestration). Use the MCP Python SDK to implement the server. Support both stdio transport (for local agent integration) and SSE transport (for remote access). The MCP server wraps the existing task server API, so no new business logic is needed — just the MCP transport layer. Register the server in the MCP server registry for discoverability.

## Files to modify

- `src/bernstein/mcp/server.py` (new)
- `src/bernstein/mcp/__init__.py` (new)
- `src/bernstein/cli/mcp.py` (new — `bernstein mcp serve`)
- `pyproject.toml` (add mcp SDK dependency)
- `tests/unit/test_mcp_server.py` (new)

## Completion signal

- `bernstein mcp serve` starts an MCP server
- Claude Code can invoke Bernstein orchestration via MCP tools
- Both stdio and SSE transports work
