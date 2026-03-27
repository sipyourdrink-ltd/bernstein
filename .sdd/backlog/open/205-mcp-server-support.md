# Pass MCP servers to spawned Claude agents

**Role:** backend
**Priority:** 1 (critical)
**Scope:** medium
**Complexity:** medium

## Problem
Claude Code supports --mcp-config to connect to MCP servers. Bernstein's spawned agents don't get any MCP servers, so they can't use tools like deep-research, Tavily, browser, etc.

## Current state
The Claude adapter spawns agents with:
```
claude --model X --dangerously-skip-permissions --max-turns 50 -p PROMPT
```

It should also pass MCP config so agents can use external tools.

## Implementation

1. Read MCP config from `~/.claude/mcp.json` (user's global config)
2. Optionally read project-level `.claude/mcp.json`
3. Merge and pass to Claude via `--mcp-config` flag as JSON string
4. Add a `mcp_servers` field to `bernstein.yaml` for per-project MCP config:
   ```yaml
   mcp_servers:
     tavily:
       command: npx
       args: ["-y", "@anthropic/tavily-mcp"]
       env:
         TAVILY_API_KEY: ${TAVILY_API_KEY}
   ```
5. The spawner merges: project config + user global config → final --mcp-config

## Files
- src/bernstein/adapters/claude.py — add --mcp-config to spawn command
- src/bernstein/core/seed.py — parse mcp_servers from seed
- src/bernstein/core/spawner.py — pass MCP config through

## Acceptance criteria
- Spawned agents receive MCP server config
- User's ~/.claude/mcp.json is read and passed through
- Project-level MCP config in bernstein.yaml works
- Agents can use MCP tools (e.g. tavily_search, deep_research)
- Tests verify MCP config is passed in spawn command
