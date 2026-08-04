# Clean-run attestation

## Scope

The clean-run attestation (issue #2930) makes eval-run isolation provable
instead of assumed: a signed artefact that binds a golden task's ground-truth
to the run's complete journaled activity and proves the two never intersected
and that no read escaped the task worktree. It is additive and default off:
without an attestation, `evaluate_task` and the multiplicative score are
byte-for-byte unchanged.

The ground-truth (task id, title, completion signals, expected test commands,
reference-solution contents) is sealed into a `ContrabandSet` of keyed HMAC
digests under the operator's audit key — the attestation commits to the
answer without ever carrying it, so publishing it cannot leak the solution.
The scope boundary comes from the substrate, not from config: the task's
worktree root plus the `NetworkPolicy` endpoint allowlist. Without a bounded
worktree root the builder refuses to sign (`CleanRunBoundaryError`), because
the "closed universe of in-scope reads" would be undefined and a `CLEAN`
claim vacuous.

## Retained evidence

The activity set is drawn from the run's Merkle-chained `EventJournal` rows,
optionally joined with the run's HMAC audit-chain slice; the attestation
records both heads, so an omitted or mutated contaminating access breaks the
anchor rather than silently trimming the set. `scan_activity` is a pure
membership pass over sealed digests: verdict `CLEAN` iff zero contraband
matches and zero out-of-scope accesses, with matches recorded as
`(journal index, match class)` positions — never plaintext. The canonical
binding is anchored in the dedicated `eval-clean-run` lineage-spine run and
mirrored into the HMAC audit chain as an `eval.clean_run_attestation` event
(hashes and the verdict only), so a tampered attestation fails
`bernstein audit verify` like any tampered chain entry. A `DIRTY` verdict
zeroes the multiplicative `Safety` factor at scoring time.

## Verification re-derives, never trusts

`verify_clean_run_attestation()` (surfaced as `bernstein eval clean-run
verify <hash>`) recomputes the attestation hash from the stored body,
re-derives the verdict and match positions from the embedded activity digests
and contraband commitment alone — rejecting a stored `CLEAN` whose embedded
evidence contains a match, even when the hashes are internally consistent —
then requires the journal rows to chain to the recorded head (an activity set
that does not is rejected as *unanchored*, and a missing journal fails closed
the same way), re-derives the sealed activity set from those anchored rows,
and re-checks the spine anchor. `project_clean_run_receipt()` reuses the
audit-receipt projection (COSE / in-toto / transparency, same KMS adapter, no
new key material) over the chain range covering the mirror, checkable by a
verifier holding neither the plaintext ground-truth nor the HMAC key.
