## Spawned agents no longer inherit the operator's user-global MCP servers

The `claude` and `pi` adapters built their argv without an MCP isolation flag,
so every spawned agent launched a private instance of every MCP server the
operator happened to have installed -- dozens of background processes across
concurrent runs, and a tool surface in the agent's context that the task never
asked for. `claude` now spawns with `--strict-mcp-config` beside its
`--mcp-config`, and `pi` with `-ne`. Servers a task declares itself are
unaffected: they are merged into the payload the flag scopes to (#5965).
