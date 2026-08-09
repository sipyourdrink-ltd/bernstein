# Provisional run-attestation receipt

## Purpose

Issue #2931 needs a portable receipt for the signed identity and enforced
dispatch evidence already recorded in Bernstein's HMAC audit chain. This slice
projects that evidence into the existing COSE, DSSE/in-toto, and transparency
formats without inventing a second receipt cryptosystem or signing-key
lifecycle.

The receipt is intentionally provisional. No event currently closes every
Bernstein execution path: `run.lifecycle transition=completed` belongs to
detached `RunService` runs, while `intent.journal_seal` belongs to
capsule-governed runs. A snapshot head is authenticated, but it is not proof
that a foreground or otherwise unsealed run ended there.

## Construction boundary

`build_run_attestation_receipt`:

1. opens the source audit chain with the operator HMAC key;
2. verifies the chain from genesis while holding the cross-process append
   transaction;
3. requires exactly one `identity.spawn_attestation` for the requested run;
4. selects from that anchor through an explicit authenticated HMAC, or the
   verified snapshot head when no boundary is supplied;
5. preserves every event in that contiguous interval, including events for
   other runs;
6. rebuilds the interval through the established slice-local chain path and
   retains each source HMAC as `details._original_hmac`;
7. re-derives the target run's identity/dispatch verdict from the retained
   evidence; and
8. signs the resulting `head_sha256` through the shared receipt substrate.

Timestamp fields remain observational. They are never used to decide range
membership, so collisions or forged wall-clock values cannot add or remove an
event from the declared interval.

## Two verdicts

`dispatch_evidence_verdict` describes only the tool-call evidence present in
the retained interval. It is `complete` when each retained enforced dispatch
has one preceding identity-valid attestation and the existing replay,
substitution, ordering, and coverage rules pass.

`whole_run_verdict` is always `observed` in this slice. `provisional` is always
true and `terminal_boundary` is always null. Semantic verification ignores any
serialized attempt to say otherwise and reports an unsupported completeness
claim.

This distinction lets a receipt truthfully say, "all retained enforced
dispatch evidence checks out," without silently changing that into, "the
receipt contains every dispatch the run ever made."

## Offline verification and trust

The standalone audit-receipt verifier recomputes the embedded range head and
checks the COSE, DSSE/in-toto, and transparency signatures. A verifier with no
out-of-band key may use the embedded public JWK to establish internal
self-consistency. It must not call that signer trusted. Pinning the expected JWK
or public key adds signer provenance.

`verify_run_attestation_projection` performs the semantic half: it derives the
run from the first retained identity anchor, checks the source anchor and
boundary witnesses, recomputes the dispatch verdict, and refuses a whole-run
upgrade. The cryptographic and semantic checks are separate so neither
signature validity nor a stored verdict is mistaken for the other.

## Failure behavior

- Missing, duplicate, or conflicting run anchors refuse construction.
- A requested boundary before the anchor or absent from the authenticated
  chain refuses construction.
- Source-chain corruption refuses construction before any receipt is emitted.
- Removing a prefix, middle event, or suffix changes the recomputed range head
  and invalidates every standard receipt format.
- Missing or invalid identity envelopes, duplicated references, reordered
  calls, or mismatched intent bindings downgrade dispatch evidence to
  `observed`.
- Construction never appends, truncates, acknowledges, or repairs the source
  audit chain.

## Explicit exclusions

This slice adds no universal run-closure event, `LineageGate` or janitor
coupling, CLI command, external identity provider, revocation service,
result/effect attestation, Rekor dependency, dispatch-policy change, or new key
lifecycle. Those remain separate boundaries. A later closure-marker slice can
make whole-run `complete` reachable without replacing this receipt format or
its authenticated range.
