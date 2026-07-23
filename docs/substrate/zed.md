# Register Bernstein in Zed

Zed reads MCP servers (which it calls "context servers") from its
user-global settings file. `bernstein desktop-register --host zed` merges
a `bernstein` entry under the `context_servers` key so every Zed session
can call Bernstein's tools without manual editing.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path

| Scope | Path |
|-------|------|
| User (all projects) | `$XDG_CONFIG_HOME/zed/settings.json` (falls back to `~/.config/zed/settings.json`) |

Run `bernstein desktop-register --list` to print the resolved path on your
machine.

## Install

```bash
bernstein desktop-register --host zed
```

This writes (merging into any existing `context_servers` map):

```json
{
  "context_servers": {
    "bernstein": {
      "command": "/path/to/python",
      "args": ["-m", "bernstein.mcp"]
    }
  }
}
```

`command` is the Python interpreter that runs Bernstein (resolved at
registration time). Unrelated keys (themes, fonts, language servers, etc.)
in `settings.json` are preserved verbatim.

Restart Zed after registering so it reloads its settings.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Zed into any Sentry-compatible error tracker, add an `env`
block to the `bernstein` entry with `BERNSTEIN_TELEMETRY_DSN` set to a
Sentry-compatible DSN:

```json
{
  "context_servers": {
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
project. Verify with `bernstein telemetry probe` after restarting Zed.

## Verify

```bash
bernstein desktop-register --list
```

The `zed` row should show `Registered: yes`. In Zed, the Bernstein tools
appear in the context-servers menu once the editor has restarted.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open the Zed settings file shown by `--list` and delete the `bernstein`
key under `context_servers`. A backup of the pre-registration state is
available as the `*.bak` sibling created during install. Restart Zed
afterwards.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
- Zed uses `context_servers` rather than the `mcpServers` key seen in the
  Claude / Cursor / Continue / Cline configs; both schemas are otherwise
  compatible.
