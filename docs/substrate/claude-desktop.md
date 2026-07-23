# Register Bernstein in Claude Desktop

Claude Desktop auto-discovers MCP servers from a single JSON config file.
`bernstein desktop-register --host claude-desktop` merges a `bernstein`
entry into that file so every Claude Desktop session can call Bernstein's
tools without manual editing.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path per OS

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json` (falls back to `~/.config/Claude/...`) |

Run `bernstein desktop-register --list` to print the resolved path on your
machine.

## Install

```bash
bernstein desktop-register --host claude-desktop
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

Restart Claude Desktop after registering so it reloads its config.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Claude Desktop into any Sentry-compatible error tracker, add an
`env` block to the `bernstein` entry with `BERNSTEIN_TELEMETRY_DSN` set to a
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

The `claude-desktop` row should show `Registered: yes`. In Claude Desktop,
the Bernstein tools appear in the MCP tool list once the app has
restarted.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Bernstein does not own the host config, so removal is a manual edit: open
the config file shown by `--list` and delete the `bernstein` key under
`mcpServers`. A backup of the pre-registration state is available as the
`*.bak` sibling created during install. Restart Claude Desktop afterwards.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
