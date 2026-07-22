# Live OTLP export of journal-anchored spans

Bernstein streams its orchestrator spans to any standard OpenTelemetry
pipeline over OTLP/gRPC - and every span on the wire is checkable evidence
against the run journal, not free-standing telemetry (issue #2526).

## Why these spans are different

Most agent frameworks emit spans with random SDK-generated ids: nothing
distinguishes a genuine orchestrator span from any process emitting
look-alike attributes. Bernstein instead treats the OTLP wire path as a
*transport* for the deterministic span projection of the run's
Merkle-chained event journal:

- **Span ids are journal-derived.** Every span id is a hash of the journal
  entry it projects (`span_id = H("otel.span", entry_hash)`); the trace id
  derives from the run's first entry hash. Two replays of the same run
  export byte-identical trace and span id trees.
- **Every span carries its evidence.** `bernstein.journal.entry_hash`
  pins the exact journal row a span projects, `bernstein.audit.anchor`
  carries the first-entry projection anchor from which the trace id derives,
  and `bernstein.run.id` locates the journal. The completed
  `otel.projection` audit event records that trace id together with the
  journal's final head.
- **No wall clock in identity.** Span timestamps come from the journal
  rows' recorded `ts` (excluded from the hash chain); nothing about span
  identity or ordering reads a clock, an LLM output, or the network.
- **The GenAI layers are intact.** Spans keep the OTel GenAI semantic
  conventions (`gen_ai.operation.name`: `invoke_workflow`, `invoke_agent`,
  `execute_tool`, `chat`) with the full parent tree, so stock tracing UIs
  group Bernstein traffic with no bespoke parsing. The convention
  attributes stay behind the projection's stability flag; a convention
  rename never affects the journal-anchored ids.

## Enabling live export

Export is **off by default**: with no endpoint configured, no exporter is
initialised and no network attempt is ever made.

```bash
export BERNSTEIN_OTEL_ENDPOINT="http://otel-collector:4317"   # OTLP/gRPC
export BERNSTEIN_OTEL_SERVICE_NAME="bernstein"                # optional
export BERNSTEIN_OTEL_GENAI_STABILITY="false"                 # optional: omit Development-stage GenAI attrs
pip install 'bernstein[otel]'                                  # gRPC exporter extra
```

With the endpoint set, every run streams its journal-anchored spans as
journal entries append (batched off the orchestrator's hot path). At run
completion Bernstein records an `otel.projection` audit event binding the
exported trace - its trace id, span count, and the SHA-256 of the signed
canonical span set - to the audit chain.

### Example collector config

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317

exporters:
  debug:
    verbosity: detailed

service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [debug]
```

## Backfilling a completed run

```bash
bernstein telemetry export-otel --run <run_id>                 # uses BERNSTEIN_OTEL_ENDPOINT
bernstein telemetry export-otel --run <run_id> --endpoint http://otel-collector:4317
bernstein telemetry export-otel --run <run_id> --dry-run       # print OTLP/JSON, no network
```

Because the projection is a pure function of the journal, backfilling a
run exports byte-identical spans to what the live stream emitted - same
ids, same attributes, same timestamps. `--dry-run` prints the spans as
OTLP/JSON and records nothing.

## Verifying an exported span

Copy a span out of your tracing UI (its id plus attributes, as JSON) and
prove it against the run journal and the audit chain:

```bash
bernstein telemetry verify-span --run <run_id> --span span.json   # from a file
pbpaste | bernstein telemetry verify-span --run <run_id> --span -  # from stdin
```

`--span` accepts a file path, or `-` / `@-` to read the span JSON from
stdin. The span JSON may be the OTLP/JSON object your collector emits
(`spanId` plus an `attributes` list, exactly what `export-otel --dry-run`
prints), so a round-trip through the wire stays checkable.

The command recomputes the span's identity with the *same* derivation the
export bridge used and prints one verdict:

- **genuine** (exit 0) - the span id recomputes from the
  `bernstein.journal.entry_hash` it carries, that entry exists in the run's
  journal, and `bernstein.audit.anchor` derives the trace id recorded by the
  run's `otel.projection` audit event.
- **forged** (exit 1) - the span id does not recompute, the referenced entry
  is absent from the journal, or the anchor does not match the chain. A real
  rejection is a hard failure, never a warning.
- **unverifiable** (exit 1) - the run's journal is absent, or the run was
  never anchored into the audit chain, so the span cannot be proven either
  way.

The offline projection tooling remains available for the whole-run surface:

```bash
bernstein trace project <run_id>            # signed projection.otel.json + audit event
bernstein trace verify-projection <run_id>  # recompute ids from the journal, check signature
```

## Relationship to the raw GenAI exporter

`bernstein.core.observability.otlp_exporter` (the env-gated adapter-level
GenAI span emitter) still exists for per-call model/token spans. The
journal bridge (`bernstein.core.observability.otel_bridge`) is the
orchestrator-truth path: if you need to reconstruct what a run actually
did, trust only spans that verify against the journal.
