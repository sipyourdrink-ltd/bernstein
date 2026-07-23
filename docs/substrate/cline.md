# Register Bernstein in Cline

Cline (a VS Code extension) auto-discovers MCP servers from a
`cline_mcp_settings.json` file inside the extension's VS Code global
storage. `bernstein desktop-register --host cline` merges a `bernstein`
entry into that file so every Cline session can call Bernstein's tools
without manual editing.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path per OS

| OS | Path |
|----|------|
| macOS | `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` |
| Windows | `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json` |
| Linux | `$XDG_CONFIG_HOME/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` (falls back to `~/.config/Code/...`) |

Run `bernstein desktop-register --list` to print the resolved path on your
machine.

## Install

```bash
bernstein desktop-register --host cline
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

Reload the VS Code window so Cline picks up the new MCP server.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Cline into any Sentry-compatible error tracker, add an `env`
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
project. Verify with `bernstein telemetry probe` after reloading VS Code.

## Verify

```bash
bernstein desktop-register --list
```

The `cline` row should show `Registered: yes`. In Cline, the Bernstein
tools appear in the MCP tool list once VS Code has reloaded.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open the config path shown by `--list` and delete the `bernstein` key
under `mcpServers`. A backup of the pre-registration state is available
as the `*.bak` sibling created during install. Reload VS Code afterwards.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
- VS Code Insiders and other variants store the extension data under a
  different parent directory; in that case point the operator at the
  canonical settings file (Cline exposes it via its UI).
