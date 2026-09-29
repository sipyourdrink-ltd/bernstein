# ADR-013: A Typed Ingest Contract With Plug-in Adapters

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/observability/ingest_contract.py`,
`src/bernstein/core/observability/ingest_profiles/`, `src/bernstein/plugins/hookspecs.py`
(`provide_ingest_adapter`), issues #4962, #4963, #5024

---

## Problem

Once ADR-012 opens the chain to activity Bernstein did not schedule, the next
question is what that surface accepts. A free-form log sink would let anything
in and prove nothing, and it would need a core patch per runtime: one per
operator, none of them ours.

Two constraints shape the answer. First, the events must be typed, so a
receipt over them means something. Second, adding a runtime must not mean
touching core. The existing adapter catalogue grew to dozens of tools because
adapter authors never had to change core; the ingest boundary will attract
the same pressure from the other direction.

## Decision

The ingest boundary is a **typed contract**, and every runtime is an **adapter
behind it**, discovered through the existing pluggy plugin system.

- An adapter declares which event shapes it can produce. Today that closed set
  is `gen_ai_activity` and `untyped_activity` (`INGEST_EVENT_TYPES` in
  `ingest_contract.py`). An adapter emitting a shape it did not declare is
  rejected, so an adapter cannot quietly widen what it claims to observe.
- The declaration is a **static manifest** (`IngestAdapterDeclaration`), not
  derived from the adapter's type annotations. A manifest can be read and
  checked without importing the adapter, it is what the receipt records, and
  it survives an adapter whose annotations drift.
- The OTLP wire format is the first concrete transport (#5024), chosen
  because runtimes already export it. Event mapping is profile-driven rather
  than a branch on a vendor name; a profile carrying a vendor `if` is a load
  failure.
- The receipt names the adapter and its version for every ingested event, so
  a verifier can attribute each record to the thing that produced it.

The boundary is a projection of the chain, not a side index: the seen-set of
ingested batches is re-derived from chain state, so it cannot drift from the
record it describes.

## Rejected alternative: derive the declaration from type annotations

Cheapest to author, and the temptation for a one-off adapter. Rejected
because it makes the contract depend on the runtime's annotation syntax and
version, and it breaks the moment an adapter ships a mismatch between what it
annotates and what it emits. A manifest is checkable before the adapter runs,
which is the property the receipt depends on.

## Rejected alternative: per-vendor adapters in core

Rejected because it puts every vendor's release cadence inside the trust
boundary and makes each new directory a core change. The plugin boundary is
what keeps the surface growing without growing core.

## Consequences

### Benefits

An operator can point an already-instrumented workload at Bernstein and get
chain-anchored, attributable records with no core patch and no second
instrumentation.

### Costs

The closed event-type set is deliberately small. Anything a runtime emits
that has no declared shape is recorded as untyped or rejected, not guessed
into a richer type. That is the price of "what is not declared is not
claimed".
