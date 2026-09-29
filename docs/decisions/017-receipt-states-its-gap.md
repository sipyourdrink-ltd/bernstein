# ADR-017: A Receipt States Its Own Coverage Gap

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/security/run_closure.py`,
`src/bernstein/core/security/audit_receipt.py`,
`src/bernstein/core/observability/otlp_ingest_receipt.py`,
`src/bernstein/compliance/article12.py`, issues #4968, #4977

---

## Problem

Our receipts were trustworthy because everything they covered passed through
our scheduler. Ingested activity breaks that assumption: it is something we
were told, not something we did. A receipt that presents both at the same
confidence puts our signature on a claim we cannot support.

The failure mode already had a name in the codebase: an unanswered query is
not a negative answer. A receipt that omits what it could not observe reports
an unmonitored surface as a clean one, and an evidence pack over a
partially-instrumented estate that omits the ungoverned portion answers an
auditor's question wrongly in the most expensive direction.

## Decision

A receipt covering ingested activity states its own coverage, in
machine-readable form, rather than by omission.

- It separates what was executed under our scheduler from what was reported
  to us, and names the adapter and version that reported each portion.
- It records what the adapter declared it cannot observe.
- It names the coverage gap explicitly, so an offline verifier can
  distinguish "nothing happened here" from "we were not watching here" with
  no access to the original run.
- An evidence pack over a mixed period includes ingested activity and states
  the executed, reported and unobserved portions separately. A pack whose
  period contains an unobserved gap cannot be emitted without stating it.

The coverage statement is a fact, not a judgement. Nothing here declares a
given coverage level acceptable; it supplies the facts an operator needs to
judge it.

## Rejected alternative: render executed and reported at one confidence

The shortest implementation is to fold both into "covered". Rejected because
it is the one outcome that makes the receipt worse than useless: it asserts
coverage where there is none, in exactly the direction an auditor will not
catch until it matters.

## Rejected alternative: refuse to cover any ingested activity

Refusing keeps the claim simple, but it makes the receipt unable to describe
a mixed estate at all, which is the estate an operator with one governed
workload and three ungoverned ones actually has. Stating the gap is strictly
more useful than refusing to speak about it.

## Consequences

### Benefits

The receipt and the evidence pack stay honest over a partially-instrumented
estate, and the distinction survives offline verification.

### Costs

Every surface that emits a receipt must now carry a coverage statement, and a
path that would omit a known gap must fail rather than degrade silently. That
is more machinery, and it is the price of never reporting an unmonitored
surface as a clean one.
