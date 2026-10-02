# ADR-012: Govern What We Schedule, Observe What We Do Not

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/observability/` (`otlp_ingest.py`,
`ingest_contract.py`, `otlp_ingest_receipt.py`), `src/bernstein/core/govern/`,
issues #4962, #4990

---

## Problem

Every governance primitive the project ships - the HMAC-chained audit log, the
lineage spine, Ed25519 receipts, delegation grants, capability ceilings, the
OTel projection - was reachable only for a run the orchestrator scheduled. An
operator who runs one workload through Bernstein and three through something
else has governance for a quarter of their surface and no way to say so.

That is a structural limit, not a missing feature. The record starts at
`Orchestrator.run()`. Activity that never passes through it produces no chain
events, so a verifier reading a receipt cannot tell the difference between
"this did not happen" and "this happened where we could not see it".

The answer could have been to widen "governed" to mean "anything we were told
about". That would have put our signature on claims we cannot support, which
is the most damaging thing a governance layer can ship.

## Decision

The boundary is directional and one-way: **inward**.

1. **Governed** applies only to activity the orchestrator scheduled. For that
   activity we attest what ran, in what order, and that nothing was altered.
2. **Observed** applies to activity reported to us that we did not schedule.
   We record it, anchor it into the same chain, and state plainly that we did
   not drive it and cannot attest it is complete.

The two are never rendered at the same confidence. An ingested event carries
an explicit origin, and the receipt that covers it states the coverage gap
rather than omitting it. "Nothing happened here" and "we were not watching
here" stay distinguishable offline.

We do not become a proxy, and we do not ask anyone to route traffic through
us. A proxy that is not in the path governs nothing; a record that anything
can write into, and that a stranger can verify offline, governs whatever it is
given.

## Rejected alternative: treat reported activity as governed

The tempting collapse is to put scheduled and reported activity behind one
word so the surface looks fully covered. Rejected because it converts a
structural gap into a false negative in the most expensive direction: an
auditor reads "governed" and concludes coverage exists where it does not. The
honesty of the receipt is the product; a receipt that overstates coverage
destroys the one property an external reader depends on.

## Rejected alternative: refuse everything we did not schedule

The narrowest rule is the easiest to police, but it removes nothing. An
operator with four runtimes will still have three ungoverned runtimes; they
just will not be able to say which three, or record what happened there.
Recording with an explicit gap is strictly more useful than silence, and the
gap statement keeps the claim honest.

## Consequences

### Benefits

The receipt becomes the carrier of the boundary: executed, reported and
unobserved portions are named separately, and an offline verifier can
reproduce the distinction with no access to the original run.

The claim "we govern what we schedule" stays exactly true, because nothing
reported is ever asserted as governed.

### Costs

The ingest surface is a second trust boundary to keep honest. Every adapter
that reports activity must also declare what it cannot observe, and the
receipt must refuse to be issued when a known gap would be omitted. That is
more surface than a single scheduler-only path, and it is the price of
extending the chain to activity we do not drive.
