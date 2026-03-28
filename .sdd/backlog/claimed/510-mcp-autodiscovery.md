# 510 — MCP server auto-discovery and auto-installation

**Role:** architect
**Priority:** 2
**Scope:** medium
**Complexity:** high

## Problem
Agents work with bare tools (file read/write, bash). If a task requires database access, web search, or API interaction, the agent either can't do it or improvises with curl. MCP servers provide these capabilities, but discovering and configuring them is manual. Bernstein should automatically detect needed MCP servers from task context and install/configure them.

## Implementation

### 1. MCP registry (`src/bernstein/core/mcp_registry.py`)
Maintain a catalog of known MCP servers with capability tags:
```yaml
# .sdd/config/mcp_servers.yaml
servers:
  - name: tavily
    package: "@anthropic/mcp-tavily"
    capabilities: [web_search, web_crawl]
    env_required: [TAVILY_API_KEY]
  - name: postgres
    package: "@anthropic/mcp-postgres"
    capabilities: [database, sql]
    env_required: [DATABASE_URL]
  - name: github
    package: "@anthropic/mcp-github"
    capabilities: [github_api, pr_management]
    env_required: [GITHUB_TOKEN]
```

### 2. Auto-detection
When creating a task prompt, scan for capability hints:
- Task mentions "search the web" / "look up" → suggest tavily
- Task touches `.sql` files or mentions "database" → suggest postgres
- Task mentions "create PR" / "GitHub" → suggest github
- Manager can explicitly request MCP capabilities in task metadata

### 3. Auto-installation
If a needed MCP server isn't installed:
- Check if env vars are available (don't install if keys are missing)
- Install via npm/npx (MCP servers are typically Node packages)
- Cache installation in `.sdd/mcp/`
- Pass MCP config to agent via `--mcp-config`

### 4. Per-agent MCP config
Build dynamic MCP config per agent based on task needs:
```json
{"mcpServers": {"tavily": {"command": "npx", "args": ["-y", "@anthropic/mcp-tavily"]}}}
```

## Files
- src/bernstein/core/mcp_registry.py (new)
- .sdd/config/mcp_servers.yaml (new)
- src/bernstein/core/spawner.py — inject MCP config per task
- tests/unit/test_mcp_registry.py (new)

## Completion signals
- file_contains: src/bernstein/core/mcp_registry.py :: MCPRegistry
- path_exists: .sdd/config/mcp_servers.yaml
