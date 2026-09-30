## Spawned claude agents get only the MCP servers bernstein passes

`claude` agents are now started with `--strict-mcp-config` beside
`--mcp-config`. Before, Claude Code merged the servers it discovers on its own
on top of bernstein's payload, so a spawned agent could start servers nobody
configured for the run, and carry their tools in its context. Those sources are
now left out: servers registered with `claude mcp add` (user or local scope), a
`.mcp.json` checked into the target repo, plugin-provided servers, account
connectors, and subagent servers referenced by name.

This does not keep `~/.claude/mcp.json` out. The claude CLI never reads that
file; bernstein does, and passes its servers in the payload together with the
project's `bernstein.yaml` `mcp_servers` and the bernstein bridge. A task that
declares no servers still gets all of them, so the process count #5965
describes is only reduced by what the CLI used to add, not removed. To give a
spawned agent a server it now misses, declare it in `bernstein.yaml`
`mcp_servers` or `~/.claude/mcp.json`.

The `pi` half of #5965 (`pi -ne`) is not in this change: that flag turns off
every pi extension, not only MCP, and needs its own decision.
