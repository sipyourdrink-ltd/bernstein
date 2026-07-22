# Journal-anchored OTLP bridge

The OTLP bridge keeps the run journal as the only source of orchestrator span
identity. `otel_projection.project_spans` remains the canonical batch
projection; `otel_bridge.IncrementalSpanProjector` mirrors it as journal rows
append, with equality between incremental and batch output enforced by tests.

The bridge bypasses `Tracer.start_span` and constructs SDK `ReadableSpan`
objects with journal-derived trace, span, and parent ids. `EventJournal`
delivers observer callbacks under its append lock so concurrent writers cannot
reorder the incremental projection. A `BatchSpanProcessor` moves OTLP/gRPC I/O
off the journal append path.

Every wire span carries its journal entry hash, run id, and the first-entry
projection anchor. The trace id is derived from that anchor. At completion,
the `otel.projection` audit event records the same trace id and the final
journal head, providing the stable join that Phase 3 span verification uses.

Export is disabled unless `BERNSTEIN_OTEL_ENDPOINT` is configured. The live
GenAI convention attributes are independently controlled by
`BERNSTEIN_OTEL_GENAI_STABILITY`; this flag never affects span identity or
ordering. Completed runs use the same bridge through
`bernstein telemetry export-otel --run <id>`.

## Threat model

**What leaves the process.** A wire span carries only projection metadata:
the span name (`gen_ai.<operation>`), the GenAI operation label, journal
entry hashes (SHA-256 values over the chained entries -- the payload bytes
themselves are not exported, though a holder can still test a guessed
payload for equality or brute-force a low-entropy field), the run id, the
journal index, and the row's recorded timestamp. Prompts, agent output,
diffs, file paths, and journal payload fields never reach the wire; the
projection maps event *types*, not event bodies. No signature or key
material is exported: the Ed25519 signature and the projection SHA-256 land
only in the local audit chain.

**Collector trust.** `BERNSTEIN_OTEL_ENDPOINT` names an
operator-controlled collector; the bridge treats it as a *sink*, never a
source of truth. A compromised or hostile collector can drop, delay, or
mutate its stored copy of the spans, and it can mint look-alike spans:
every exported span carries its `bernstein.journal.entry_hash`, so the
collector holds those hashes by construction and can recompute the bare
span id from them. Authenticity therefore does not rest on the entry hash
being secret. A span is trusted only when the hash it carries is verified
against the local journal chain and the `otel.projection` audit event,
which pin the genuine trace id and final journal head locally; a span
whose entry hash is absent from the local chain, or whose trace id does
not match the audit event, is rejected however cleanly its id recomputes.
Note the gRPC channel defaults to `insecure=True` (matching the existing
raw exporter); point the endpoint at a collector on a trusted network or
terminate TLS in front of it, and treat span metadata (run ids, event-type
activity timing) as visible to whoever operates the sink.

**Install-key handling.** Run finalization signs the canonical projection
with the install identity key via `core/security/install_key.py` -- the
same path, `BERNSTEIN_CREDENTIAL_SIGNING_KEY` override, 0600 permissions,
and no-strip read semantics the credential CLI uses. The private key is
loaded, used in-process, and never serialized, logged, or attached to a
span. A malformed or unreadable key aborts only the audit anchoring
(logged warning); it can never fail a run or block the journal.

**Journal integrity is upstream of this feature.** The bridge is a
read-only observer: it cannot write, reorder, or wedge journal appends
(observer exceptions are swallowed), and journal paths are derived through
the `run_journal_path` containment barrier, so a crafted run id cannot
export a journal outside the runs root.

## Rollback

Unset `BERNSTEIN_OTEL_ENDPOINT` (or never set it): the factory returns
before any exporter class is imported -- zero initialization, zero network
attempts, journal untouched (test-enforced). Beyond that:

- The bridge holds no state and performs no migration: disabling it
  requires no cleanup. The run journal and the offline
  `projection.otel.json` artifact are byte-identical with the bridge on,
  off, or absent (both are pure functions of the run). The audit chain is
  the one exception: an enabled run appends a single `otel.projection`
  audit event at finalization, so a run executed with export enabled
  carries that extra event while a disabled or bridge-absent run does not.
- To hard-remove the wire dependency, also drop the optional
  `bernstein[otel]` extra; with the package missing the bridge disables
  itself with one logged warning.
- Spans already delivered to a collector need no revocation: a span is
  trusted only if it verifies against the local journal and chain, so
  orphaned or stale copies in an external sink carry no authority.
- Exporter or collector failures mid-run degrade to logged warnings;
  telemetry can never fail, block, or alter a run, so rollback under
  incident conditions is "unset the variable and restart the run service"
  with no repair step.
