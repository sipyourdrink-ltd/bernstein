# OTel GenAI span projection (offline)

Bernstein ships a live [OTLP exporter](otlp-export.md) for the run journal,
but an operator who does not run a collector still needs a signed,
checkable span set for a completed run. `bernstein trace project` and
`bernstein trace verify-projection` are the offline counterpart: they
project a run's event journal into a signed OTel GenAI span set on disk, and
verify that projection later, with no OTLP endpoint required.

Span ids are derived from journal entry hashes
(`span_id = H("otel.span", entry_hash)`), so two projections of the same run
export a byte-identical id tree — the projection is a deterministic function
of the journal, not free-standing telemetry.

## Projecting a run

```
bernstein trace project RUN_ID [--workdir DIR] [--no-genai-stability] [--json]
```

Loads `RUN_ID`'s event journal, maps its events onto the OTel GenAI span
layers (`invoke_workflow` root, `invoke_agent`, `execute_tool`, `chat`
using `gen_ai.operation.name`), and signs the resulting span set with the
install-identity Ed25519 key. By default the signed projection is written to
`.sdd/runs/<run_id>/projection.otel.json`; `--json` prints it to stdout
instead. `--no-genai-stability` omits the (Development-stage) GenAI
semantic-convention attributes while keeping every span id journal-anchored
— useful when a downstream tool chokes on an unstable attribute set.

Every span carries `bernstein.journal.entry_hash` (the exact journal row it
projects), `bernstein.audit.anchor` (the first-entry projection anchor the
trace id derives from), and `bernstein.run.id`. Writing the projection also
attempts to append an `otel.projection` event to the HMAC audit chain. That
event binds the run id, journal head, trace id, span count, and SHA-256 of
the canonical projection payload. `--json` only prints the signed document
and deliberately records no audit event.

Exit codes: `0` written, `1` no event journal for the run, or bad input.

## Verifying a projection

```
bernstein trace verify-projection RUN_ID [--workdir DIR] [--projection PATH]
```

Reloads `RUN_ID`'s journal, recomputes every span id from it, checks the
signature against the install identity, and verifies the projection's full
binding against an authenticated `otel.projection` audit event. The binding
includes the run id, journal head, trace id, span count, and canonical
projection digest. Archived audit segments participate in verification.
`--projection` overrides the expected file location (default
`.sdd/runs/<run_id>/projection.otel.json`).

Verification is read-only with respect to audit evidence: it loads the
existing audit key and never creates an audit key, audit directory, or event.
Consequently, output produced only with
`trace project --json` is unverifiable unless a matching audit event already
exists.

Exit codes:

- `0` verified: span ids and signature match the journal, and one
  authenticated audit event matches all five binding fields.
- `1` could not be evaluated: an input, audit key, audit directory, or
  matching event is missing or unreadable, or audit-chain integrity fails.
- `2` verification failed: recomputed ids or the signature diverge, or
  authenticated evidence for the run contradicts the projection binding.

## Guarantees

- **Determinism.** `project_spans` is a pure function of its input events —
  it never reads a clock, environment, or socket. Ed25519 signing is
  deterministic (RFC 8032), so two projections of the same run produce
  byte-identical spans and signature bytes.
- **Journal-anchored identity.** Every span id and the trace id derive from
  journal entry hashes, not from an SDK-generated random id — stripping the
  journal makes the ids unrecomputable, and tampering with a span breaks
  either the entry-hash binding or the signature.
- **Convention-attribute instability is isolated.** The OTel GenAI semantic
  conventions are still Development-stage; a convention rename never
  affects the journal-anchored ids because the attributes sit behind the
  `--no-genai-stability` flag, not inside the id derivation.

## Relationship to the live OTLP exporter

This offline pair covers the whole-run surface: project once, verify
anytime, no collector needed. The live exporter
(`BERNSTEIN_OTEL_ENDPOINT`) streams the same journal-anchored spans as the
run executes, and `bernstein telemetry verify-span` checks one span copied
out of a tracing UI against the journal. See [OTLP export](otlp-export.md)
for the live-streaming and per-span verification workflow.

## Source

`src/bernstein/cli/commands/advanced_cmd.py` (`trace project` /
`trace verify-projection`),
`src/bernstein/cli/commands/_otel_projection_audit.py` (authenticated audit
binding verification),
`src/bernstein/core/observability/otel_projection.py` (span projection,
signing, verification),
`src/bernstein/core/replay/journal.py` (the event journal projected from).
