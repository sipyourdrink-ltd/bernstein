# Register Bernstein in Aider

Aider's config file lives at `~/.aider.conf.yml` and is YAML, not JSON.
Aider does not natively load MCP servers, so this adapter is best-effort:
`bernstein desktop-register --host aider` records a `bernstein` entry
under an `mcp-servers` key in the YAML config. Community wrappers, custom
launch scripts, and Aider plugins can pick that entry up to invoke
Bernstein.

The write is idempotent and backup-first: the existing config is copied to
a timestamped `.bak` sibling before any mutating write, and re-running the
command when the entry is already correct performs no write.

## Config path

| Scope | Path |
|-------|------|
| User (all projects) | `~/.aider.conf.yml` |

Run `bernstein desktop-register --list` to print the resolved path on your
machine.

## Install

```bash
bernstein desktop-register --host aider
```

This writes (merging into any existing top-level keys):

```yaml
model: gpt-4o            # existing keys preserved
auto-commits: false      # existing keys preserved
mcp-servers:
  bernstein:
    command: /path/to/python
    args:
      - -m
      - bernstein.mcp
```

`command` is the Python interpreter that runs Bernstein (resolved at
registration time). Unrelated keys are preserved verbatim.

## Telemetry DSN

To route Bernstein's side-channel telemetry (lineage, cost, run lifecycle,
tracker events) into any Sentry-compatible error tracker when a wrapper launches
Bernstein from Aider, export `BERNSTEIN_TELEMETRY_DSN` in the wrapper's
environment before invoking Bernstein:

```bash
export BERNSTEIN_TELEMETRY_DSN="https://<public_key>@<host>/<project_id>"
python -m bernstein.mcp
```

Or add an equivalent `env` block in your custom launch script. Aider's own
YAML config does not carry env vars for sub-processes, so the DSN must be
set wherever the wrapper actually launches Bernstein. The same env-var
name and wire format are honoured by every host (see
[docs/observability/side-channel.md](../observability/side-channel.md)),
so operators running several hosts in parallel can point them all at one
project. Verify with `bernstein telemetry probe` after relaunching.

## Verify

```bash
bernstein desktop-register --list
```

The `aider` row should show `Registered: yes`. Because Aider has no
built-in MCP loader, the entry will not light up the host's UI on its own;
use it as input to a wrapper script that launches Bernstein when Aider
starts.

For machine-readable output:

```bash
bernstein desktop-register --list --json
```

## Uninstall

Open `~/.aider.conf.yml` and delete the `bernstein` key under
`mcp-servers` (and the `mcp-servers` key itself if Bernstein was the only
entry). A backup of the pre-registration state is available as the
`*.bak` sibling created during install.

## Notes

- Registration never deletes or rewrites entries it did not create.
- A config file that is not valid YAML is left untouched and the command
  reports an error rather than overwriting it.
- If a future Aider release adds first-class MCP support, this adapter
  will move to use the new key without changing the operator workflow.
