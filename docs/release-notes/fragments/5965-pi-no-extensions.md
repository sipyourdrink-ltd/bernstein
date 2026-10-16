## Spawned pi agents no longer load the operator's extensions

Pi has no MCP of its own; MCP servers and other tools reach it as
extensions, which it discovers in `~/.pi/agent/extensions/` and in a
repository's `.pi/extensions/`. A spawned `pi` agent loaded all of them, so
every concurrent run started the operator's personal MCP servers and carried
their tools in its context.

The `pi` adapter now passes `--no-extensions`. Bernstein passes pi no
extension of its own, so nothing a governed run relied on is lost. The
`claude` half of this issue shipped in #5981 (`--strict-mcp-config`).
