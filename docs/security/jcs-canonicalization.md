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

## Number rule

RFC 8785 section 3.2.2.3 does not define its own number format. It defers to
ECMAScript's `Number::toString` (ECMA-262 7.1.12.1), the same rule
`JSON.stringify` applies in a browser.

Python's `repr` generates the same shortest-round-trip digits that rule asks
for, but lays them out differently in three places:

| Value | Python `repr` | ES6 / RFC 8785 |
| --- | --- | --- |
| `10.0` | `10.0` | `10` |
| `-0.0` | `-0.0` | `0` |
| `1e-7` | `1e-07` | `1e-7` |
| `1e20` | `1e+20` | `100000000000000000000` |
| `1e21` | `1e+21` | `1e+21` |

ES6 writes a number in plain decimal while the decimal point sits in
`-6 < n <= 21`, and switches to scientific notation outside that window with
an unpadded, explicitly signed exponent. An integer-valued float has no
fractional part to write, so it is written as an integer: under this rule
`1.0`, `1e0` and `100e-2` all canonicalise to the same bytes as `1`, which is
the point of a canonical form.

Integers are the one deliberate departure from the double model. They are
emitted exactly rather than routed through a double, so a count past `2**53`
keeps the value the caller signed. Tenant showback statements rely on this:
their `nano_usd` values deliberately exceed the double-exact integer range.

NaN and the infinities have no RFC 8785 encoding, and `canonicalize_jcs`
raises `ValueError` on them as it always has.

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

| Version | Ordering | Numbers | Shipped in |
| --- | --- | --- | --- |
| 1 | Unicode code point | Python `repr` | up to v3.10.0 |
| 2 | UTF-16 code units (RFC 8785 conformant) | Python `repr` | v3.11.0 to v3.19.x |
| 3 | UTF-16 code units (RFC 8785 conformant) | ES6 `Number::toString` (RFC 8785 conformant) | v3.20.0 onwards |

## What changed in version 2, and who is affected

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

## Migration to version 2

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

## What changed in version 3, and who is affected

Version 3 changes the canonical bytes, and therefore the signature and the
digest, for **any payload carrying a float that the ES6 rule lays out
differently**: one that is integer-valued, one that falls on the other side of
the scientific-notation thresholds, or negative zero. A payload holding no
float at all is byte-identical to version 2.

This is a wider blast radius than version 2. Version 2 needed a
supplementary-plane property name to bite; version 3 bites on `5.0`.

Self-produced artefacts that carry a float are affected whenever that float
lands on one of those values:

- `AgentIdentityCard` bodies: `max_budget_usd` is the common case, since an
  operator writing a budget writes `5.0` rather than `5.0000001`.
  `created_at` and `expires_at` come from `time.time()` and land on an
  integer boundary only by coincidence, but they can.
- Any capability token, mandate, receipt, advisory or trust record whose body
  carries a float on such a value.

Tenant showback statements are **not** affected. Their canonical vectors were
generated with `rfc8785` (Python) and reproduced with `canonicalize` (Node),
both conformant, so they already expected version 3's bytes; their money
values travel as decimal strings and integers rather than floats.

## Migration to version 3

1. **Find affected artefacts.** An artefact is affected only if one of its
   numbers is a float on one of the values in the table above. As with
   version 2, `bernstein audit verify` and the A2A conformance check report
   such an artefact as a signature mismatch rather than as a formatting
   change, so look for verification failures that appear after upgrading
   while the bytes on disk are unchanged.
2. **Re-sign, do not rewrite.** Re-issue the artefact with the current
   build. The body content is unchanged; only how one of its numbers is
   spelled in the canonical bytes differs.
3. **Verifiers upgrade first.** A verifier still on version 2 rejects a
   version 3 signature over an affected body, and vice versa. Upgrade
   verifying parties before re-signing.

An independent RFC 8785 implementation agrees with version 3 and disagreed
with version 2, which is the reason for the change, and the same reason
version 2 existed: under version 2 a conformant third-party verifier
recomputing over a card whose budget was `5.0` read a valid Bernstein
signature as invalid.

## Test vectors

`tests/unit/test_canonicalize_jcs_numbers.py` pins the number rule, and
`tests/property/test_a2a_card_bughunt.py` checks it against the published RFC
8785 reference vectors (`arrays.json`, `french.json`, `values.json`,
`structures.json`). The expected strings in both are the rule applied by
hand rather than any implementation's output.

`tests/fixtures/showback/canonical_vectors.json` carries cross-language
vectors for the canonical core. The last statement vector is the disagreeing
case; its expected bytes are derived from the RFC rule itself (names sorted by
their UTF-16BE encoding, which is a big-endian serialisation of the code-unit
array) rather than from any implementation, so it pins the specification
rather than a tool.

`tests/fixtures/agent-card-utf16-vector/` closes the one gap those leave: an
`AgentIdentityCard` signed through the real `sign_agent_card` path, with a
disagreeing property-name pair set on `extensions` -- the caller-supplied
surface this page names above. `test_canonicalize_jcs_key_order.py` and the
RFC reference vectors prove the canonicaliser is correct against hand-built
input; this vector proves it against a record the production signing path
actually emitted, which is the property an independent verifier checking
Bernstein's real output needs (#5551).
