# Register Bernstein in Cursor

Cursor auto-discovers MCP servers from a user-global `~/.cursor/mcp.json`
file (and an optional project-local `./.cursor/mcp.json`).
`bernstein desktop-register --host cursor` merges a `bernstein` entry into
the user-global file so every Cursor session can call Bernstein's tools
without manual editing.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path

| Scope | Path |
|-------|------|
| User (all projects) | `~/.cursor/mcp.json` |

Run `bernstein desktop-register --list` to print the resolved path on your
machine.

## Install

```bash
bernstein desktop-register --host cursor
```

This writes (merging into any existing `mcpServers` map):

```json
{
  "mcpServers": {
    "bernstein": {
      "command": "/path/to/python",
      "args": ["-m", "bernstein.mcp"]
    }
  }
}
```

`command` is the Python interpreter that runs Bernstein (resolved at
registration time). Unrelated servers and top-level keys are preserved
verbatim.

Restart Cursor (or reload the MCP servers panel) so it reloads the config.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Cursor into any Sentry-compatible error tracker, add an `env`
block to the `bernstein` entry with `BERNSTEIN_TELEMETRY_DSN` set to a
Sentry-compatible DSN:

```json
{
  "mcpServers": {
    "bernstein": {
      "command": "/path/to/python",
      "args": ["-m", "bernstein.mcp"],
      "env": {
        "BERNSTEIN_TELEMETRY_DSN": "https://<public_key>@<host>/<project_id>"
      }
    }
  }
}
```

The same env-var name and wire format are honoured by every host
(see [docs/observability/side-channel.md](../observability/side-channel.md)),
so operators running several hosts in parallel can point them all at one
project. Verify with `bernstein telemetry probe` after restarting the host.

## Verify

```bash
bernstein desktop-register --list
```

The `cursor` row should show `Registered: yes`. In Cursor, the Bernstein
tools appear in the MCP tool list once the editor has restarted.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open `~/.cursor/mcp.json` and delete the `bernstein` key under
`mcpServers`. A backup of the pre-registration state is available as the
`*.bak` sibling created during install. Restart Cursor afterwards.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
- Project-local overrides under `./.cursor/mcp.json` are not touched by
  the user-scoped registration; edit them manually if you need a
  per-project entry.
