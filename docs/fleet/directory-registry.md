# Fleet directory registry

The fleet directory registry is a filesystem-as-service-registry for
running several Bernstein instances on a single host without editing
a central supervisor config. Drop a per-instance directory under the
fleet root and the supervisor picks it up on the next reload.

## One-liner setup

```bash
mkdir -p ~/.bernstein/fleet/my-app && \
  printf "path: $HOME/code/my-app\n" > ~/.bernstein/fleet/my-app/bernstein.yaml && \
  bernstein fleet reload
```

That is the whole flow: a directory, a one-line manifest, a rescan.

## Layout

```
$BERNSTEIN_FLEET_ROOT/
  my-app/
    bernstein.yaml      # required: at least empty
    logs/               # optional, supervisor writes here
    .disabled           # optional: presence skips this instance
  another-tenant/
    bernstein.yaml
```

`$BERNSTEIN_FLEET_ROOT` defaults to `~/.bernstein/fleet/`.

## Manifest fields

`bernstein.yaml` is a YAML mapping. Every field is optional.

| Field             | Default                            | Notes                                                                 |
|-------------------|------------------------------------|-----------------------------------------------------------------------|
| `name`            | directory basename                 | Must be unique across the fleet root.                                 |
| `path`            | the instance directory             | Absolute or relative to the instance directory.                       |
| `task_server_url` | `http://127.0.0.1:8052`            | Base URL of this instance's task server.                              |
| `workers`         | `[]`                               | Optional worker capability matrix for routing eligibility.             |

A completely empty file is valid and yields an instance named after
its directory with default values for the rest.

```yaml
workers:
  - worker_id: python-local
    task_types: [standard, fix]
    dispatch_tiers: [local]
    capabilities: [python, testing]
```

Each row requires a nonempty `worker_id`, unique within the instance, and
nonempty `task_types` and `dispatch_tiers` lists of existing `TaskType` and
`DispatchTier` values. Optional `capabilities` defaults to `[]`; entries must
be nonempty, case-sensitive strings. Unknown row fields, wrong types, invalid
enum values, or duplicate IDs invalidate the entire matrix, even if another
row matches. The registry collects a `FleetConfigError`, clears routing
eligibility, and preserves the instance's dashboard registration.

`select_fleet_instance(task, instances)` selects enabled instances where one
worker matches the task's type, exact dispatch tier and every `requires`
string. It never combines rows or infers capabilities from dashboard roles.
The smallest instance name lexicographically wins; no match returns `None`.
Absent or empty `workers` means dashboard-only participation. Declarations
grant no permissions, credentials, capacity or spending authority.

## Disabling an instance

Drop a `.disabled` file inside the instance directory. The registry
keeps it in the scan result under `disabled`, the supervisor skips it,
and the fleet view surfaces a note so operators see why.

```bash
touch ~/.bernstein/fleet/my-app/.disabled
bernstein fleet reload
```

Remove the file to re-enable the instance.

## CLI

| Command                  | Purpose                                                            |
|--------------------------|--------------------------------------------------------------------|
| `bernstein fleet list`   | Show every active instance discovered under the fleet root.       |
| `bernstein fleet reload` | Rescan the fleet root and report active, disabled, and errors.    |

Both subcommands accept `--root` to override the fleet root for a
single invocation. `bernstein fleet reload --json` emits machine-readable
output suitable for piping into a watcher.

## Errors

The registry never crashes on a single malformed instance. Bad
manifests (unparseable YAML, wrong types, duplicate names) become
non-fatal errors that surface in the CLI output and the fleet
dashboard footer. The supervisor continues running the instances that
parsed correctly.
