# Opt-in operator observability

Bernstein ships with no telemetry enabled.  An operator may opt in to a
strictly bounded event set so the project can surface crash and error
reports that would otherwise only reach maintainers via manual bug
reports.  This document is the full schema, opt-out matrix, and
retention policy.

## TL;DR

For the operator and maintainer-share pipelines described here:

- Default is **off**. Nothing is sent unless you run
  `bernstein telemetry on` (or set `BERNSTEIN_TELEMETRY=1`), or opt in to
  the separate maintainer-share consent described under *Maintainer-share
  consent* below.
- `DO_NOT_TRACK=1` always wins.  No event is sent and no install id is
  generated.
- The install id is a 128-bit UUID v4 created lazily, **after** opt-in
  only.  It is the only identifier we send.
- Every event is mirrored to a local JSONL file so you can audit
  exactly what was sent.  Rotated weekly, queryable via
  `bernstein telemetry export`.

## Opt-out matrix

| Signal                                          | Result                |
|-------------------------------------------------|-----------------------|
| `DO_NOT_TRACK=1` env var                        | off (universal opt-out) |
| `BERNSTEIN_TELEMETRY=0` (or `false/no/off/""`)  | off                   |
| `BERNSTEIN_TELEMETRY=1` (or `true/yes/on`)      | on                    |
| `~/.bernstein/telemetry.yaml` with `enabled: false` | off               |
| `~/.bernstein/telemetry.yaml` with `enabled: true`  | on                |
| `BERNSTEIN_TELEMETRY_SHARE=0` (or `false/no/off/""`) | share off (wins over the TOML file) |
| `BERNSTEIN_TELEMETRY_SHARE=1` (or `true/yes/on`)     | share on (maintainer-share path only) |
| `$XDG_CONFIG_HOME/bernstein/telemetry.toml` with `share_with_maintainer = true` | share on |
| None of the above                               | off (default)         |

Precedence: `DO_NOT_TRACK` > `BERNSTEIN_TELEMETRY` > `telemetry.yaml` > default-off
for the operator pipeline. The maintainer-share flag has its own precedence:
`DO_NOT_TRACK` > `BERNSTEIN_TELEMETRY_SHARE` > `telemetry.toml` > default-off.
Setting `BERNSTEIN_TELEMETRY` does not change the share flag, and the reverse
is also true.

## Maintainer-share consent

The maintainer-share path (RFC #1719) is separate from the default opt-in
above. `bernstein telemetry on` does not enable it. The CLI enables it with
`bernstein telemetry enable --share-with-maintainer`, which prints the event
schema and redaction list and asks for confirmation (skip with `--yes`), and
it is disabled by `bernstein telemetry disable`. The flag is stored in
`telemetry.toml`, and the sink stays inert unless
`BERNSTEIN_TELEMETRY_SHARE_ENDPOINT` is also set. The environment override
`BERNSTEIN_TELEMETRY_SHARE=1` can enable consent independently of the CLI.

The separately configured `BERNSTEIN_TELEMETRY_DSN` side channel has its own
activation path; the operator and maintainer-share opt-in flags above do not
control it. `bernstein telemetry probe` sends a synthetic event through that
side channel when a valid DSN is configured.

## Subcommand surface

```
bernstein telemetry on          # opt in, write config, generate install id
bernstein telemetry off         # opt out, delete install id, write off-marker
bernstein telemetry status      # show current state + which signal won
bernstein telemetry export      # dump the last 30 days of locally queued events
bernstein telemetry enable --share-with-maintainer   # maintainer-share consent (RFC #1719)
bernstein telemetry disable     # revoke maintainer-share consent
bernstein telemetry tail -n 10  # print the next N events that would be sent, offline
bernstein telemetry probe       # ship one synthetic event to BERNSTEIN_TELEMETRY_DSN
```

## Closed event set

| Name                  | Fields                                                              |
|-----------------------|---------------------------------------------------------------------|
| `install_completed`   | `os`, `py_version`, `install_method`, `bernstein_version`            |
| `first_run_started`   | `time_since_install_seconds`                                         |
| `first_run_completed` | `ok`, `duration_ms`, `error_category` (only on failure)              |
| `command_invoked`     | `name_only`, `bernstein_version`                                     |
| `daily_active`        | `day_iso`                                                            |

No other event variant is permitted, and the serializer rejects any
payload whose dataclass does not match the event name.

### Error categories (closed set)

`config_missing | auth_failed | dependency_missing | model_unreachable | timeout | unknown`

## Envelope shape

Every event is wrapped in the same envelope and serialized to a single
JSON line:

```json
{
  "install_id": "<uuid v4 hex>",
  "name": "<one of the event names above>",
  "payload": { ... },
  "schema_version": 1,
  "timestamp": "<RFC 3339 UTC>"
}
```

`install_id` is the only operator-identifying field.  We never send file
contents, args, prompts, paths, or resource names.

## What we do not collect

- No source code.
- No command-line arguments.
- No environment variables.
- No file paths.
- No prompts or model outputs.
- No IP address (the receiver may log one for routing; see Retention).
- No project, session, or agent names.
- No timing data finer than `duration_ms` per first-run.

## Network behaviour

- Endpoint: configurable via `BERNSTEIN_TELEMETRY_ENDPOINT`.  Default is
  an illustrative placeholder host, not a real receiver.
- Maintainer-share endpoint: configurable via
  `BERNSTEIN_TELEMETRY_SHARE_ENDPOINT`.  No default is shipped, and
  `bernstein telemetry status` prints only whether it is configured.
- Single HTTP POST per event, 3-second timeout, no retries.
- All errors are swallowed.  The command always completes normally
  whether or not the POST succeeded.
- Shutdown flush is bounded by a 5-second wallclock cap.

## Install id lifecycle

- Generated lazily after `bernstein telemetry on`.
- Persisted to `~/.bernstein/install-id` with mode `0600` (best effort).
- Deleted on `bernstein telemetry off`.
- Never sent before opt-in.  The library raises if any caller asks for
  the id while telemetry is disabled.

## First-run notice

The first time `bernstein` runs, a one-time notice is printed to stderr:

```
Bernstein collects no telemetry by default.
Run `bernstein telemetry on` to opt in.
This message will not appear again.
```

After printing, a marker is persisted to
`~/.bernstein/first-run-acknowledged` and the notice never appears
again.

## Local queue

Every event that the client emits is also appended to
`~/.bernstein/telemetry-queue.jsonl` so an operator can audit exactly
what was sent.

- One JSON object per line.
- Rotated weekly: old files become
  `telemetry-queue.<YYYY-MM-DD>.jsonl`.
- Queryable via `bernstein telemetry export --days N`.

## Retention policy

| Bucket                 | Server-side retention | Notes                                  |
|------------------------|-----------------------|----------------------------------------|
| Event envelopes        | 90 days               | Aggregated daily, then deleted.        |
| Aggregated counts      | 18 months             | No install id, no per-event detail.    |
| Access logs (IP, UA)   | 30 days               | Held only for abuse mitigation.        |
| Local queue            | 7-day rolling window  | Operator-controlled, ships nothing.    |

Deletion request: open an issue or email the maintainers with your
install id (it is the only thing we can use to find your events) and
they will be purged.

## Threat model

- The endpoint cannot influence the operator's command outcome.  All
  network paths are fail-closed and bounded.
- A compromised endpoint cannot exfiltrate file contents because we
  never include them in the payload.
- A malicious operator cannot trick the library into generating an
  install id before opt-in; `install_id.ensure` raises until
  `is_enabled` returns True.

## Why this design

- Default-off mirrors rustup, homebrew, and the .NET CLI.  Default-on
  telemetry is rejected by the Python tooling community (see the
  GitHub CLI v2.91 controversy).
- A bounded, closed event set means an external reviewer can audit
  every payload variant in one short file (`events.py`).
- Mirroring to a local file means the operator can always verify what
  was sent.  There is no remote-only source of truth.
- Lazy install id generation means the on-disk footprint of "default
  off" is genuinely nothing.

## Configuration files

| Path                                       | Purpose                            |
|--------------------------------------------|------------------------------------|
| `~/.bernstein/telemetry.yaml`              | `enabled: <bool>` opt-in state.    |
| `$XDG_CONFIG_HOME/bernstein/telemetry.toml` | `share_with_maintainer` flag (falls back to `~/.config/bernstein/telemetry.toml`). Separate from `telemetry.yaml`. |
| `~/.bernstein/install-id`                  | UUID v4 hex.  Created on opt-in.   |
| `~/.bernstein/first-run-acknowledged`      | Marker.  Created on first run.     |
| `~/.bernstein/telemetry-queue.jsonl`       | Locally mirrored events (JSONL).   |

## Implementation map

- `src/bernstein/core/telemetry/config.py` - precedence resolver.
- `src/bernstein/core/telemetry/install_id.py` - lazy id generation.
- `src/bernstein/core/telemetry/events.py` - closed event taxonomy.
- `src/bernstein/core/telemetry/client.py` - bounded HTTP client.
- `src/bernstein/core/telemetry/wire.py` - integration helpers.
- `src/bernstein/cli/commands/telemetry_cmd.py` - operator surface.

## Testing

```
uv run pytest tests/unit/telemetry/ \
              tests/property/test_telemetry_properties.py \
              tests/integration/test_telemetry_lifecycle.py
```

The unit suite covers every precedence layer, every event variant,
every fail-closed network path, the local queue rotation, and the
first-run notice idempotence.  Property tests exercise install-id
uniqueness and payload-schema invariants under random inputs.  The
integration suite drives a mock receiver through the full first-run
flow plus opt-in/opt-out flips.
