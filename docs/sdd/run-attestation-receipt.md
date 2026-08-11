# Authenticated run-attestation receipt

## Purpose

Issue #2931 needs a portable receipt for the signed identity and enforced
dispatch evidence already recorded in Bernstein's HMAC audit chain. This slice
projects that evidence into the existing COSE, DSSE/in-toto, and transparency
formats without inventing a second receipt cryptosystem or signing-key
lifecycle.

The receipt remains provisional until the retained range contains one valid
`run.closure` marker for the requested run. A snapshot head is authenticated,
but it is not proof that a foreground or otherwise unsealed run ended there.
Closure is derived from retained evidence and never inferred from silence.

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

An explicit historical boundary may produce an observed receipt, but it cannot
prove whole-run completeness. If that retained range would otherwise upgrade
to `complete`, construction refuses it unless the selected boundary is also
the verified source snapshot head. This prevents a caller from hiding a later
same-run event behind an earlier closure marker.

Timestamp fields remain observational. They are never used to decide range
membership, so collisions or forged wall-clock values cannot add or remove an
event from the declared interval.

## Two verdicts

`dispatch_evidence_verdict` describes only the tool-call evidence present in
the retained interval. It is `complete` when each retained enforced dispatch
has one preceding identity-valid attestation and the existing replay,
substitution, ordering, and coverage rules pass.

`whole_run_verdict` becomes `complete` only when the retained range contains
exactly one valid, still-terminal `run.closure` marker that binds a verified
run-journal head and positive event count. Otherwise it is `observed`,
`provisional` remains true, and `terminal_boundary` remains null. A detached
RunService closure that binds a work ledger is valid for that execution path,
but cannot upgrade an identity receipt whose beginning is anchored to a run
journal: the two ends must name the same execution object.

The verifier walks forward from the marker. A later event for the same run
invalidates completeness; events for other runs may remain interleaved without
doing so. Duplicate markers, conflicting outcomes, invalid anchors, or audit
chain corruption also refuse the upgrade. The serialized verdict is never
trusted.

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
boundary witnesses, recomputes the dispatch verdict, walks the closure state,
and accepts a whole-run upgrade only from a still-valid run-journal closure.
The cryptographic and semantic checks are separate so neither signature
validity nor a stored verdict is mistaken for the other.

## Failure behavior

- Missing, duplicate, or conflicting run anchors refuse construction.
- A requested boundary before the anchor or absent from the authenticated
  chain refuses construction.
- A historical boundary that would claim whole-run completeness refuses
  construction unless it is also the verified source snapshot head.
- Source-chain corruption refuses construction before any receipt is emitted.
- Removing a prefix, middle event, or suffix changes the recomputed range head
  and invalidates every standard receipt format.
- Missing or invalid identity envelopes, duplicated references, reordered
  calls, or mismatched intent bindings downgrade dispatch evidence to
  `observed`.
- A missing closure stays `open`; a later same-run event invalidates an earlier
  marker rather than being hidden.
- Construction never appends, truncates, acknowledges, or repairs the source
  audit chain.

## Explicit exclusions

Closure does not prove that journaled claims are true, that effects bypassing
Bernstein were observed, or that an HMAC key holder was honest. It proves that
the authenticated chain contains a terminal statement for a specific verified
state anchor and no later retained event for that run. `LineageGate`, external
identity, result/effect attestation, revocation, and transparency publication
remain separate boundaries. Closure reuses the existing audit key and receipt
formats; it creates no new key lifecycle.
