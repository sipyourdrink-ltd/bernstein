# Register Bernstein in Continue

Continue auto-discovers MCP servers from a user-global
`~/.continue/config.json` file. `bernstein desktop-register --host continue`
merges a `bernstein` entry into the `mcpServers` map so every Continue
session can call Bernstein's tools without manual editing.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path

| Scope | Path |
|-------|------|
| User (all projects) | `~/.continue/config.json` |

Continue also reads `config.yaml` in some releases; the JSON file remains
the canonical location for MCP server registration and is what
`desktop-register` writes. Run `bernstein desktop-register --list` to
print the resolved path on your machine.

## Install

```bash
bernstein desktop-register --host continue
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
registration time). Unrelated keys (models, slash commands, etc.) in
`config.json` are preserved verbatim.

Restart your editor so Continue reloads `~/.continue/config.json`.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Continue into any Sentry-compatible error tracker, add an
`env` block to the `bernstein` entry with `BERNSTEIN_TELEMETRY_DSN` set to
a Sentry-compatible DSN:

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
project. Verify with `bernstein telemetry probe` after restarting the
editor.

## Verify

```bash
bernstein desktop-register --list
```

The `continue` row should show `Registered: yes`. In a Continue session,
the Bernstein tools appear in the MCP tool list once the editor has
reloaded the config.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open `~/.continue/config.json` and delete the `bernstein` key under
`mcpServers`. A backup of the pre-registration state is available as the
`*.bak` sibling created during install. Restart your editor afterwards.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
- If your installation only uses `config.yaml`, mirror the entry manually;
  `desktop-register` writes to the JSON file by design so the merge
  contract is identical across hosts.
