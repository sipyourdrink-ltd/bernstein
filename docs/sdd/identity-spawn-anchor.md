# Identity spawn anchor

## Scope

`IdentitySpawnAnchor` implements the first acceptance criterion of issue #2931:
one Bernstein run may retain exactly one verified, signed agent identity. It is
an additive security primitive and is not yet wired into every spawn path.

The anchor accepts a signed `AgentIdentityCard`, resolves the JWS `kid` only
against a Bernstein-controlled trust mapping, validates the card at spawn time,
and records an `identity.spawn_attestation` event in the HMAC audit chain. A key
asserted by the card itself is never a trust source.

## Retained evidence

The event freezes the material required for later offline verification:

- a snapshot of the card and detached JWS;
- the exact validation timestamp;
- the public Ed25519 JWK selected from Bernstein's trusted key mapping;
- a canonical SHA-256 digest of that JWK;
- the card digest, SVID reference, and run-journal head.
- when configured, the lineage tool-signing `kid`, canonical public Ed25519
  JWK, and JWK digest used for every identity-bound tool call in that run.

Historical reconstruction verifies the HMAC chain, signed-card digest, frozen
JWK digest and metadata, detached signature, and the card's validity at the
recorded validation time. It does not consult the current clock or require the
live JWKS to retain a rotated-out key. The retained JWK is public verification
material; no private signing key is written to the event.

The optional tool key is supplied as public `AgentCard` material and must name
the same agent as the signed spawn card. Once anchored, a different tool key,
`kid`, agent, or journal head is a run conflict—not a rotation. The caller must
start and anchor a new run. This makes later verification independent of live
key discovery while preventing a mid-run identity substitution.

## Transaction and retry invariants

The lookup for an existing run identity and the append happen inside one
`chain_transaction()`. The transaction is exclusive across threads and
processes, so two orchestrators cannot bind different identities to the same
run.

An identical retry is idempotent and produces no second event. Any competing
identity is rejected. A retry carrying a different `run_journal_head` is
reported as a moved-head conflict and is never silently re-anchored, including
after a legitimate crash during spawn.

This slice does not make authorization decisions, issue identities, or select
an external identity provider. It only freezes public verification material
for the native tool-call identity layer.
