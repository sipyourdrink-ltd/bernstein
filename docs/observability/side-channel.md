# Side-channel telemetry

Bernstein emits its own observability stream (lineage tamper alerts, cost
events, run lifecycle, tracker deliveries, captured errors). When Bernstein
runs embedded inside a host application (Claude Desktop, Cursor, and similar)
that stream travels over a side channel rather than the host's stdout, so the
host neither intercepts nor forwards it. This page documents the portable
contract that makes the side channel behave identically across every host.

## TL;DR

- One env var everywhere: `BERNSTEIN_TELEMETRY_DSN` (a Sentry-compatible URL).
- One wire format: the Sentry store protocol.
- One backend contract: any Sentry-compatible sink behind a DSN.
- Default state is off. With no DSN set, nothing is emitted and no network is
  touched.
- Fail-closed: a misconfigured DSN or an unreachable backend never crashes a
  run.
- Verify the wiring with `bernstein telemetry probe`.

## Configuration

| Env var | Purpose | Default |
|---|---|---|
| `BERNSTEIN_TELEMETRY_DSN` | Sentry-compatible DSN for the side channel. | (unset = disabled) |
| `BERNSTEIN_TELEMETRY_BACKPRESSURE` | Behaviour when the queue is full: `drop` or `queue`. | `drop` |
| `BERNSTEIN_TELEMETRY_QUEUE_MAXSIZE` | Bounded in-memory queue depth. | `256` |

The DSN has the standard Sentry shape:

```
<scheme>://<public_key>@<host>[:<port>]/<project_id>
```

For example:

```
BERNSTEIN_TELEMETRY_DSN=https://abc123@errors.example.com/42
```

The store endpoint derived from that DSN is
`https://errors.example.com/api/42/store/`, and each event carries an
`X-Sentry-Auth` header so any Sentry-protocol backend accepts it.

`BERNSTEIN_TELEMETRY_DSN` is the single, host-agnostic contract.

## Event contract

Every event rendered onto the side channel is a Sentry store-protocol body.

### Required fields

| Field | Meaning |
|---|---|
| `event_id` | A unique hex id (auto-generated per event). |
| `timestamp` | ISO-8601 UTC time the event was created. |
| `level` | One of `fatal`, `error`, `warning`, `info`, `debug`. |
| `logger` | `bernstein.<category>`, e.g. `bernstein.lineage`. The category identifies the emitter (`lineage`, `cost`, `run`, `tracker`, `probe`, ...). |
| `message` | Human-readable event description. |
| `platform` | Always `python`. |

### Optional fields

| Field | Meaning |
|---|---|
| `tags` | Flat string-to-string map for backend-side filtering. Always includes `bernstein.category`. |
| `extra` | Structured, free-form context (counts, ids, status codes). |
| `sdk` | Emitter identity: `{ "name": "bernstein.sidechannel", "version": "1" }`. |

No prompts, file contents, or secrets are placed in any field. Emitters pass
only the structured context relevant to the event.

## Backpressure

The sink owns a bounded in-memory queue and a single background worker that
posts events to the backend. The queue hand-off is the only place
backpressure applies:

| Policy | Behaviour when the queue is full |
|---|---|
| `drop` (default) | The newest event is discarded and counted; the producer never blocks. |
| `queue` | The producer blocks until a slot frees up, for at most ~1 second, then drops the event rather than wedging the run. |

Pick `drop` when liveness of the orchestrator matters more than completeness
of the stream (the common case). Pick `queue` when you want to preserve as
many events as possible and can tolerate brief producer-side blocking under a
slow backend.

## CLI

```
bernstein telemetry probe                      # emit one synthetic event
bernstein telemetry probe --message "ci check" # custom message body
```

`probe` reads `BERNSTEIN_TELEMETRY_DSN`, ships a single synthetic event with
`logger=bernstein.probe`, and reports whether it was queued for delivery. If
no DSN is configured it prints a hint and exits cleanly. Use it after pointing
a host-embedded Bernstein at your DSN to confirm the backend received the
stream.

## Per-host install

The contract is identical across hosts; only the mechanism for setting the
environment variable differs.

### Generic shell / CI

```
export BERNSTEIN_TELEMETRY_DSN="https://<public_key>@<host>/<project_id>"
bernstein telemetry probe
```

### Claude Desktop / Cursor (MCP-style host config)

Hosts that launch Bernstein as a subprocess accept an `env` block in their
server configuration. Add the DSN there so every embedded run inherits it:

```json
{
  "mcpServers": {
    "bernstein": {
      "command": "bernstein",
      "args": ["mcp"],
      "env": {
        "BERNSTEIN_TELEMETRY_DSN": "https://<public_key>@<host>/<project_id>"
      }
    }
  }
}
```

### Docker / Compose

Pass the DSN through the container environment:

```yaml
services:
  bernstein:
    image: bernstein:latest
    environment:
      BERNSTEIN_TELEMETRY_DSN: "https://<public_key>@<host>/<project_id>"
```

Because the variable name and wire format are the same everywhere, an operator
running several hosts in parallel can point them all at one error-tracker
project and review every agent's activity in a single stream.

## Failure behaviour

- No DSN: the side channel is a no-op. Nothing is emitted; no network is used.
- Invalid DSN: a warning is logged once and the side channel falls back to the
  no-op sink. The run continues.
- Unreachable backend: the background worker drops the event after the HTTP
  timeout. The producer is never blocked beyond the backpressure budget.

## Scope

- Backend is any Sentry-compatible sink behind a DSN. Other wire formats are
  a follow-up.
- The lineage chain itself stays in `core/lineage/`. Tamper detections are
  mirrored onto the side channel, but the chain is not re-implemented here.

## Related: maintainer-share consent

The side channel above is operator-controlled. A separate, additive consent
flag (`share_with_maintainer`) is documented in
[telemetry-share.md](./telemetry-share.md). That flag gates an opt-in path
to a maintainer endpoint supplied via `BERNSTEIN_TELEMETRY_SHARE_ENDPOINT`
and is off by default. The two surfaces are independent.
