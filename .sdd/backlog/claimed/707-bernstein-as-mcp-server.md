# 707 — Bernstein as MCP Server

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Depends on:** none

## Problem

MCP has 97M monthly SDK downloads. Every IDE and coding tool is integrating MCP. If Bernstein IS an MCP server, then Cursor, Claude Code, Cline, Windsurf — ANY MCP client — can orchestrate multi-agent work through Bernstein. This makes Bernstein the universal orchestration backend for the entire ecosystem, not just a CLI tool.

## Design

Expose Bernstein's orchestration as MCP tools:

### MCP Tools
- `bernstein.run` — start an orchestration run with a goal
- `bernstein.status` — get run status
- `bernstein.tasks` — list tasks and their states
- `bernstein.cost` — get cost summary
- `bernstein.stop` — graceful shutdown
- `bernstein.approve` — approve a pending task (for approval gates)

### Transport
- stdio (for local IDE integration)
- SSE (for remote/web integration)

### Usage
From any MCP client (Cursor, Claude Code, etc.):
```
Use the bernstein tool to run "Add auth, tests, and docs" with a $5 budget
```

This turns Bernstein from "a CLI tool you install" into "the orchestration layer every AI tool can call."

## Files to modify

- `src/bernstein/mcp/server.py` (new)
- `src/bernstein/mcp/__init__.py` (new)
- `pyproject.toml` (entry point)
- `tests/unit/test_mcp_server.py` (new)

## Completion signal

- `bernstein mcp` starts an MCP server
- Claude Code can call bernstein.run via MCP
- Tools list, describe, and execute correctly
