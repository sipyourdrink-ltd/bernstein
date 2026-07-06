# Adapter conformance + capability report

`bernstein adapters check` answers one question:

> Of the adapters this install declares, which ones are reachable on
> this machine, which ones conform to the contract, and which ones are
> missing binaries or have drifted?

The command is a one-shot inventory. Re-running it is cheap (no
network, no agent spawn) and the output is consumable both by humans
(Rich table) and by CI dashboards (`--format json`).

## TL;DR

| Command                                         | When to use it |
|-------------------------------------------------|----------------|
| `bernstein adapters list`                       | Quick "what ships?" listing - one line per adapter, no contract probing |
| `bernstein adapters list-status`                | Compact conformance status per adapter (Rich table) |
| `bernstein adapters check`                      | Full table - binary path, version, capabilities, conformance, notes |
| `bernstein adapters check <name>`               | Drill down to one adapter |
| `bernstein adapters check --format json`        | CI-consumable payload keyed on `adapters` and `summary` |
| `bernstein adapters check --strict`             | Exit non-zero on any `conformance == "fail"` row |

## What every row contains

```
AdapterStatus(
    name="claude",
    module_path="bernstein/adapters/claude.py",
    binary_resolved="/usr/local/bin/claude",
    version_string="1.0.42",
    capabilities=frozenset({"--model", "--effort", "--permission-mode", ...}),
    conformance="ok",
    conformance_detail="",
    last_modified_utc="2026-05-17T16:15:21.849480+00:00",
    contract_hash="8a3f42703dc55c2442ca8c0620bd25fd1823748e970d03519434ac885439dbc5",
)
```

Field-by-field:

* **name** - registry key.
* **module_path** - repo-relative source path of the adapter module.
* **binary_resolved** - `shutil.which(binary)` output, or `None` when
  the binary is not on `PATH` (or the adapter is a no-binary wrapper
  such as `mock`, `generic`, or `openai_agents`).
* **version_string** - first non-empty trimmed line of
  `<binary> --version`, captured with a 5-second timeout. `None` when
  the call fails, times out, or the binary is missing.
* **capabilities** - flags + subcommands the contract YAML declares
  as the required surface. Empty when no contract exists.
* **conformance** - one of `ok` / `fail` / `skip`. See verdict table
  below.
* **conformance_detail** - human-readable reason. Empty on `ok`.
* **last_modified_utc** - ISO-8601 UTC mtime of the adapter module
  file. Useful as a cache key in dashboards.
* **contract_hash** - SHA-256 of the loaded contract bytes. Empty
  when the adapter has no contract on disk.

## Verdict table

| Verdict | When? |
|---------|-------|
| `ok`    | Binary present, `<binary> --help` advertises every required flag and subcommand |
| `fail`  | Binary present but help text is missing at least one required token (contract drift) |
| `skip`  | No contract on disk, or binary missing, or `--help` failed / timed out |

`skip` is the default state for adapters that simply have no contract
yet - operators see "we know about it, but we haven't promised what
it does". `fail` is the only verdict that should ever turn red on a
healthy install.

## JSON contract

```json
{
  "adapters": [
    {
      "name": "claude",
      "module_path": "bernstein/adapters/claude.py",
      "binary_resolved": "/usr/local/bin/claude",
      "version_string": "1.0.42",
      "capabilities": ["--effort", "--include-hook-events", "--model"],
      "conformance": "ok",
      "conformance_detail": "",
      "last_modified_utc": "2026-05-17T16:15:21.849480+00:00",
      "contract_hash": "8a3f4270..."
    }
  ],
  "summary": {
    "total": 44,
    "reachable": 10,
    "conform": 6,
    "fail": 0,
    "skip": 38
  }
}
```

Capabilities are emitted as a sorted list so downstream diffs are
stable.

## Strict mode

`--strict` flips the process exit code:

* No `fail` rows -> exit `0`.
* Any `fail` row -> exit `1`.

`skip` rows are tolerated - they typically reflect "operator has not
installed this CLI yet", which is informational not actionable.

## Conformance check details

The check is in-process and never spawns pytest:

1. `shutil.which(binary)` resolves the upstream CLI.
2. If reachable, `<binary> --version` is captured with a 5s timeout.
3. The contract YAML (when present) is loaded and hashed.
4. The contract's required flags + subcommands are matched against
   `<binary> --help` output (token-boundary regex; ANSI escapes are
   stripped).

Adapters that lack a contract YAML fall through with `conformance ==
"skip"` and an empty capability set. This is intentional - the
contract suite ships with curated coverage of the load-bearing CLIs.

## CI integration

```yaml
# .github/workflows/adapters-check.yml
- name: Adapter conformance gate
  run: bernstein adapters check --strict --format json | tee adapters.json
```

Dashboards parse `adapters.json` and surface per-adapter trend lines.
The `contract_hash` field is the recommended cache key when stitching
historical reports.

## Related commands

* `bernstein adapters list` - quick "what ships?" enumeration; no
  contract probing.
* `bernstein adapters contract-check <name>` - deeper contract check
  for a single adapter (includes model-list assertions when an auth
  secret is set).
* `bernstein doctor` - host-readiness checks; surfaces a subset of the
  adapter status alongside other environment signals.
