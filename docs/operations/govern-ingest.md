# Anchoring activity Bernstein did not schedule

Bernstein's record starts at `Orchestrator.run()`. An operator who runs one
workload through Bernstein and three through something else has governance for
a quarter of their surface, and a verifier reading a receipt cannot tell "this
did not happen" apart from "this happened where we could not see it".

`bernstein governance ingest` is the boundary that closes the gap. It accepts
OTLP/JSON spans reported by a runtime Bernstein did not schedule, records them
in the same HMAC audit chain as activity Bernstein drove, and prints a signed
receipt that states its own coverage.

Modules: `src/bernstein/core/observability/otlp_ingest.py` (span parsing),
`src/bernstein/core/observability/otlp_ingest_receipt.py` (anchoring and
receipts), `src/bernstein/core/observability/ingest_profiles/` (attribute
mapping). CLI group: `bernstein governance`.

## CLI

```
bernstein governance ingest --spans <file|-> --source <label> [--profile <name>] [--workdir <path>] [--json]
```

| Option | Meaning |
|---|---|
| `--spans` | OTLP/JSON payload: a span object, or a list of them. `-` reads stdin. |
| `--source` | Identity of the reporting source. Required: it is part of the signed receipt binding, so two sources can never produce an interchangeable receipt. |
| `--profile` | Ingest profile driving attribute mapping (`generic`, `otel_collector`, `agent_direct`). |
| `--workdir` | Project root holding `.sdd/`. The chain is `.sdd/audit`. |
| `--json` | Print the receipt as JSON and nothing else. |

Exit codes: `0` anchored, `1` payload rejected.

```bash
# From a file the collector wrote.
bernstein governance ingest --spans spans.json --source otel-collector-prod

# Straight off a pipe.
your-exporter --format otlp-json | bernstein governance ingest --spans - --source agent-host-7
```

## What the receipt says, and what it refuses to say

The receipt binds the source label, the profile, the batch digest, the arrival
index, the order the source claimed, the chain head it was minted against, and
an Ed25519 signature over all of it. It also carries an explicit coverage
statement: the activity was **reported**, not scheduled here. Bernstein cannot
attest that a foreign runtime reported everything it did, and a receipt that
implied otherwise would be the overclaim this boundary exists to prevent.

## Rejection is whole-payload

The payload is parsed before anything is appended. A span missing its
`traceId` or `spanId`, a malformed attribute list, or JSON that does not parse
fails the whole submission and leaves the chain untouched. There is no partial
ingest to reason about afterwards.

## Re-submitting is safe

Transports retry. A collector that saw no acknowledgement replays its batch,
and an operator unsure whether a file was consumed runs the command again. If
every submission appended, the chain would stop describing what the foreign
runtime did and start describing how many times the transport tried, and every
count projected over it would be wrong.

So every record the boundary writes is addressed by the SHA-256 of what was
reported, scoped to the source and profile that reported it:

- Re-reporting the same span appends nothing.
- Re-submitting the same batch appends nothing and returns the receipt that
  batch was already anchored with, byte-for-byte.
- Reporting **different** bytes is a different address and a new record, so a
  correction is kept alongside the earlier claim rather than collapsed into it.
- The same span reported by two sources is two records. Each receipt binds its
  own source identity, and collapsing them would erase which source told us.

The seen-set is a query over the chain, not a side index, so it cannot drift
from the chain it describes. Copy the audit directory and the same replay
behaviour follows the copy.

## Limits of this surface

- The transport is a file or stdin. There is no listener yet; point a collector
  at a file drop or pipe into the command.
- The boundary observes and records. It does not gate or block what it did not
  schedule.
