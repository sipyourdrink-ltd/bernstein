# Register Bernstein in Claude Code

Claude Code auto-discovers MCP servers from a project-local `.mcp.json`
file. `bernstein desktop-register --host claude-code` merges a `bernstein`
entry into that file in the current working directory, so any Claude Code
session opened in the project can call Bernstein's tools.

The write is idempotent and backup-first: the existing `.mcp.json` is
copied to a timestamped `.bak` sibling before any mutating write, and
re-running the command when the entry is already correct performs no
write.

## Config path

| Scope | Path |
|-------|------|
| Project (current directory) | `./.mcp.json` |

Because the scope is project-local, run the command from the repository
root you want Bernstein registered in. `bernstein desktop-register --list`
prints the resolved path for the current directory.

## Install

```bash
cd /path/to/your/project
bernstein desktop-register --host claude-code
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
registration time). Unrelated servers and top-level keys in `.mcp.json`
are preserved verbatim.

Reopen the project in Claude Code so it reloads `.mcp.json`.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) from Claude Code into any Sentry-compatible error tracker, add an
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
project. Verify with `bernstein telemetry probe` after reopening the
project.

## Verify

```bash
bernstein desktop-register --list
```

The `claude-code` row should show `Registered: yes` when run from the same
directory. In a Claude Code session opened on the project, the Bernstein
tools appear in the available MCP tools.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open `./.mcp.json` and delete the `bernstein` key under `mcpServers`, or
remove the whole file if Bernstein was the only entry. A backup of the
pre-registration state is available as the `.mcp.json.*.bak` sibling
created during install.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A `.mcp.json` that is not valid JSON is left untouched and the command
  reports an error rather than overwriting it.
- `bernstein init` may also write `.claude/mcp.json` for orchestration
  auto-discovery; the two mechanisms are independent and can coexist.
