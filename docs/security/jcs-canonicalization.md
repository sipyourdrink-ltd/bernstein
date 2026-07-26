# JCS canonicalization and property-name ordering

Bernstein signs and hashes structured payloads over RFC 8785 (JSON
Canonicalization Scheme) bytes. `canonicalize_jcs` in
`bernstein.core.security.agent_card_signer` is the single producer of those
bytes. Everything that has to be verifiable by a third party goes through it:

| Surface | Module |
| --- | --- |
| Agent identity cards (detached JWS) | `core/security/agent_card_signer.py` |
| A2A capability cards and conformance checks | `core/interop/a2a_card.py`, `core/interop/a2a_conformance.py` |
| Capability tokens | `core/security/capability_tokens.py` |
| Delegation scope digests | `core/identity/delegation_scope.py` |
| Payment mandates and transaction receipts | `core/payments/` |
| Tenant showback statements | `core/cost/showback_canonical.py` |
| Update advisories and version pins | `core/distribution/update_advisory.py` |
| `/.well-known/agent.json` and `agent-card.json` | `core/routes/well_known.py` |

## Ordering rule

RFC 8785 section 3.2.3 sorts object property names as **arrays of UTF-16 code
units**. It does not sort by Unicode code point.

The two orders agree for every property name whose characters are below
U+D800, which covers ASCII, Latin, CJK and everything else in the Basic
Multilingual Plane below the surrogate range. They disagree in exactly one
case: a **supplementary-plane** name (a character above U+FFFF, such as an
emoji or a plane-2 CJK ideograph) compared against a name starting in
**U+E000 to U+FFFF**. In UTF-16 the supplementary name begins with a high
surrogate in U+D800 to U+DBFF, which sorts *below* that range, while its code
point sorts *above* it.

```
{"a": 1, "": 2, "\U0001F600": 3}

UTF-16 code units (RFC 8785):  a , U+1F600 , U+E000
Unicode code points:           a , U+E000  , U+1F600
```

`JCS_CANONICALIZATION_VERSION` records which rule the current build applies:

| Version | Ordering | Shipped in |
| --- | --- | --- |
| 1 | Unicode code point | up to v3.10.0 |
| 2 | UTF-16 code units (RFC 8785 conformant) | v3.11.0 onwards |

## What changed and who is affected

Version 2 changes the canonical bytes, and therefore the signature and the
digest, for **any object that carries a supplementary-plane property name
alongside a name in U+E000 to U+FFFF**. Nothing else changes: for every other
payload the bytes are identical to version 1, byte for byte.

Every property name Bernstein itself emits is ASCII, so all self-produced
artefacts (agent cards, capability tokens, mandates, receipts, advisories,
version pins, well-known routes) are unaffected and need no action.

Two surfaces accept caller-supplied property names and can therefore reach the
changed case:

- `AgentIdentityCard.extensions`, a free-form map negotiated at spawn time and
  included in the signed card body.
- Inbound payloads canonicalized for verification: remote A2A agent cards
  (`check_agent_card_v1_conformance`), AGNTCY ADS descriptors, advisory
  documents, version pins, and showback statement trees supplied by a caller.

## Migration

If you have never used a non-ASCII key in `extensions` or in a showback
statement payload, there is nothing to do.

Otherwise:

1. **Find affected artefacts.** An artefact is affected only if one of its
   objects has both a property name above U+FFFF and a property name in
   U+E000 to U+FFFF. `bernstein audit verify` and the A2A conformance check
   report such an artefact as a signature mismatch, not as a reordering, so
   look for verification failures that appear after upgrading while the bytes
   on disk are unchanged.
2. **Re-sign, do not re-order.** Re-issue the artefact with the current build.
   The body content is unchanged; only the canonical byte order of the
   affected object differs.
3. **Verifiers upgrade first.** A verifier still on version 1 rejects a
   version 2 signature over an affected object, and vice versa. Upgrade
   verifying parties before re-signing.

An independent RFC 8785 implementation (for example `rfc8785` in Python or
`canonicalize` in Node) agrees with version 2 and disagreed with version 1,
which is the reason for the change: under version 1 a conformant third-party
verifier recomputing over the same object read a valid Bernstein signature as
invalid.

## Test vectors

`tests/fixtures/showback/canonical_vectors.json` carries cross-language
vectors for the canonical core. The last statement vector is the disagreeing
case; its expected bytes are derived from the RFC rule itself (names sorted by
their UTF-16BE encoding, which is a big-endian serialisation of the code-unit
array) rather than from any implementation, so it pins the specification
rather than a tool.
