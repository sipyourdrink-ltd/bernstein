## Spawned Kilo agents start again

The `kilo` adapter ran `kilo run --prompt <task> --model <m> --yes`, plus
`--mcp <json>` when the task had MCP servers. `kilo run` (`@kilocode/cli`
7.8) has none of those flags. It printed its usage and exited 1, so every
spawned Kilo agent failed before it ran. The adapter now runs
`kilo run --model <m> --auto -- <prompt>`. MCP servers come from Kilo's own
configuration (`kilo mcp`), and servers passed for a task are logged as not
delivered. The install line in the adapter guide now names `@kilocode/cli`;
the `kilocode` npm package is an empty placeholder.
