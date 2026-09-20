# ADR-016: TRACE Conformance Is Claimed at Level 0 Only

**Status**: Accepted
**Date**: 2026-09-20
**Context**: `src/bernstein/core/security/audit_receipt_conformance.py`,
`docs/observability/trace-export.md`, `schemas/trace-spec/0.2/`, issues #4987, #4976

---

## Problem

A receipt that a reader can check without running our software is necessary
but not sufficient for the record format to be useful beyond this project.
Nothing stated what an implementation must do to produce a receipt a
conforming verifier accepts, so "produces a valid receipt" was defined by
whatever our own writer happened to emit. A format defined by one
implementation is an implementation detail with a schema attached.

The exporter also produced two claims with very different weight: Level 0
(schema and signature conformance) and Level 1 (hardware TEE evidence). Leaving
the two undifferentiated invites an operator to claim more than a software-only
install can support.

## Decision

1. **Conformance is executable, with no privileged path.** A versioned profile
   plus a test corpus states what a conforming producer must emit and what a
   conforming verifier must reject. `bernstein trace conform` runs the corpus
   against any implementation and reports pass or fail per requirement. Our
   own writer is measured against the same corpus as an external one; if our
   writer fails, the corpus is right and the writer is wrong.

2. **We claim Level 0 only.** A software-only install exports a TRACE 0.2
   record that passes Level 0. Level 1 requires properties of the deployment
   - a hardware TEE and a verifier-supplied nonce - that a software-only
   install does not have. The all-zero runtime digest is the honest way to say
   "no hardware measurement exists", and it is not claimed to be anything
   else.

## Rejected alternative: claim the highest level the format allows

Rejected because a level that requires hardware evidence cannot be earned by a
software-only record, and asserting it would convert an honest limitation into
a false claim. Level 0 is what ships; Level 1 is what a different deployment
can reach.

## Rejected alternative: keep conformance as prose

A conformance profile that only exists as documentation is unenforceable and
drifts. Rejected in favour of a corpus where adding a requirement without a
matching case is a build failure, and a non-conforming producer names the
requirement it violated rather than a single pass/fail bit.

## Consequences

### Benefits

"Produces a valid receipt" is checkable by running the corpus, and our writer
gets no exemption from the standard an external implementer must meet.

### Costs

The claim surface is narrower and honest: software-only deployments state
Level 0 and do not assert TEE properties they do not have. That is the price
of keeping the conformance claim checkable offline.
