# Verifier ladder

## Scope

The verifier ladder (issue #2927) extends the pre-merge gate's single boolean
into per-tier, re-derivable receipts. It is additive and default off: the
janitor seals nothing unless handed a `VerifierLadderContext`, and it records
what ran — it never decides what runs. Gate policy, tier ordering, and when a
later rung is consulted are unchanged, and nothing here invokes a human
review.

Three ordered rungs form the ladder: `deterministic` (completion signals,
attribution, guardrails), `judge` (the LLM judge), `human`
(consensus/review). Every rung that runs seals a frozen `TierRecord` —
`config_hash`, `inputs_hash`, `evidence_hash`, `verdict` — into the lineage
spine under the dedicated `verifier-ladder` run id, kept apart from per-task
journals exactly as `eval-gate` is. A judge tier that was consulted but never
adjudicated (prerequisites failed, LLM call failed, unparseable output, a
tripped circuit breaker) records verdict `skip` rather than vanishing.

## Retained evidence

The composite `LadderReceipt` binds the task id, the ordered tier records
with their spine anchors, the required-tier policy, a timestamp, and a
`merge_eligible` claim derived by the pure `derive_ladder_verdict()`:
fail-closed, so an empty record set, an absent required rung, or any
non-`pass` verdict — a `fail` *or* a `skip` — blocks eligibility. Each sealed
tier is also mirrored into the HMAC audit chain as a `verifier.tier` event
carrying hashes and the verdict only, never raw diff, rubric, or model
output.

## Verification re-derives, never trusts

`verify_ladder_receipt()` (surfaced as `bernstein verify ladder
<receipt-hash>`) re-hashes the stored body, re-runs the verdict derivation
over the stored tier records — rejecting a stored `merge_eligible` those
verdicts do not entail, even when the receipt's hashes are internally
consistent — and re-checks every `spine_entry_hash` against the spine
entry's content hash, proving each tier sealed exactly the evidence the
receipt claims. With the spine removed or tampered, the "verified" claim
fails closed rather than passing trivially: the composite verdict is
meaningless without the substrate.

Tier-local receipts (`gate.adjudication`, `review.receipt`) remain the
per-tier authorities; the ladder is the coverage binder across them.
