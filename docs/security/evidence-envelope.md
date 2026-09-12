# Evidence envelope (v1)

An evidence envelope is a portable, signed statement about a bounded set of
actions: who acted, under what authority, what was authorised, what was
recorded — and what the envelope does not account for.

- Schema: [`schemas/evidence-envelope-v1.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/schemas/evidence-envelope-v1.json)
- Format surface: `bernstein.core.security.evidence_envelope`
- Golden vector: `tests/fixtures/evidence-envelope-vectors/`

## Status

**The format only.** This is the schema, the canonical encoding and one
committed vector. There is no producer that builds the six-section envelope
from a run, and no verifier that checks one. Until those exist, an envelope
is something you can validate and re-encode, not something the installation
emits.

The canonical encoder itself has one caller outside its own tests:
[compliance evidence packs](../operations/compliance.md) digest their JSON
artefacts (`manifest.json`, `controls.json`, the audit data catalog) through
`canonical_envelope_bytes` rather than a local `json.dumps` convention, so a
pack digest and an envelope digest are checkable the same way (#5504). That
pack is not an evidence envelope — it carries no `principal`, `grants` or
`signature` section — it simply reuses this module's RFC 8785 encoder.

## What an envelope proves

| It proves | Because |
|---|---|
| These six sections were signed together, by the key `principal.key` names | the detached JWS covers every member except `signature`, over the envelope's canonical bytes |
| Nobody edited a section after signing | changing any member changes the preimage, and the signature stops verifying |
| Two parties quoting one digest are quoting one file | the canonical form is byte-deterministic, so `sha256` over it is a stable name |
| The producer stated which parts of its scope it could not account for | `coverage` is a required section with a required `uncovered` list |

## What an envelope does not prove

| It does not prove | Why not |
|---|---|
| That the actions happened | the envelope carries digests and locators, not the material; resolving a locator and checking the digest is a separate step, and no verifier ships yet |
| That the grant chain was valid | `grants` is carried, not evaluated. Whether each link attenuates its parent and was live at the decision's `timestamp` is a verifier's question |
| That nothing is missing | it proves the producer *declared* a gap, not that the declaration is complete. An action absent from both `decisions` and `coverage.uncovered` is invisible to a reader |
| Anything about the hardware | this is software evidence signed by an installation key: no TEE, no TPM, no hardware root of trust |

The third row is the one to read twice. Coverage moves the failure mode from
"silence" to "a claim you can check against the run" — it does not remove it.

## Sections

| Section | Holds |
|---|---|
| `principal` | the acting identity, as a URI reference plus the Ed25519 JWK that makes it checkable |
| `grants` | the authority chain, root first, each link naming its parent and carrying a `not_after` expiry |
| `decisions` | one record per authorisation, each naming a versioned policy. The field surface mirrors `GovernanceDecision` so projecting a recorded decision is a rename, not a re-interpretation |
| `evidence` | `sha256` digests plus locators tying each decision to what was recorded |
| `coverage` | the declared/covered counts, one entry per uncovered action with a reason, and the limitations in prose |
| `signature` | detached JWS over the five sections above |

`grants` may be empty today: the authority plane that populates it is not
built yet. An empty array states "no grant chain was recorded" rather than
implying unbounded authority, and the limitation belongs in `coverage`.

## Canonical form

JCS (RFC 8785), via the same `canonicalize_jcs` the A2A capability card signs
under — see [JCS canonicalization](jcs-canonicalization.md). The signature is
a detached JWS in the capability card's header shape, with the envelope's own
`typ` so the three signature contexts in this repository cannot be replayed
into one another.

The repository has two canonical-JSON conventions: JCS, and the sorted-keys
encoding the [audit-receipt](audit-receipt.md) family shares. The envelope
joins the first rather than adding a third. JCS is the choice because an
envelope is read by parties who hold the spec and not this source tree, and
RFC 8785 is the encoding they can implement from that spec alone.

An envelope's bytes on disk *are* its canonical form — one line, no trailing
newline. Parsing a stored envelope and re-encoding it must reproduce the file
byte for byte; that is what makes a published digest checkable.

## Checking the committed vector

```bash
cd tests/fixtures/evidence-envelope-vectors
shasum -a 256 -c partial-coverage-envelope.sha256
```

The vector covers three of five declared actions and names the other two.
`tests/unit/test_evidence_envelope_format_vectors.py` re-encodes it with
today's canonicaliser, re-verifies its signature offline against the
published key, validates it against the schema, and demonstrates the
coverage rules by removing the sections they require.
