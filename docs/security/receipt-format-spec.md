# Audit receipt format specification (v1)

This document is the **normative** format specification for the Bernstein audit
receipt. It is written to be self-contained: a reader who has never opened
`src/` can implement a verifier from this page alone, using only a JSON parser,
SHA-256, Ed25519, and (for the `cose` format) a CBOR encoder/decoder.

The receipt projects an HMAC-chained audit-log range into standard,
offline-verifiable envelope formats — COSE_Sign1 (RFC 9052), DSSE / in-toto v1,
and an RFC 6962 style transparency receipt. It is a **projection**, not new
evidence: the subject digest IS the chain range `head_sha256`, and every format
binds that single value.

For the operational workflow (how to export and verify a receipt) see
[Standard verifiable audit receipts](audit-receipt.md). This page defines the
bytes; that page defines the commands. For the run-attestation projection's
design rationale see
[Authenticated run-attestation receipt](../sdd/run-attestation-receipt.md).

## Scope and conformance

- **In scope.** The wire format of `schema_version` `1.0.0` receipts: the
  canonicalization profile, the subject binding, the three envelope formats,
  the embedded signing key, the trust tiers, and the verification exit-code
  contract.
- **Out of scope.** Changing the receipt format itself. This document describes
  the format as it exists; it does not propose revisions. The separate
  `verify run` run-receipt (which carries `journal` and `spine` heads) is a
  different document type and is not covered here.

The reference implementation is `src/bernstein/core/security/audit_receipt.py`.
The standalone verifier `tools/verify_audit_receipt.py` re-implements every
primitive below with zero Bernstein imports; it is the conformance oracle for
this specification.

## Receipt document structure

A receipt is a single JSON object with these top-level fields:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | string | `"1.0.0"` (pinned). |
| `receipt_type` | string | `"https://bernstein.run/attestations/audit-receipt/v1"` (or a projection-specific subtype, see below). |
| `subject` | object | The single attested subject. `subject.digest.sha256` IS the chain range `head_sha256`. |
| `range` | object | The projected chain range metadata (window, head HMAC, head SHA-256, event count). |
| `events` | array | The embedded re-chained event range. A verifier recomputes `head_sha256` from these bytes. |
| `signing` | object | The Ed25519 verifying key shared by every format, embedded as an RFC 8037 JWK. |
| `formats` | object | One or more of `cose`, `intoto`, `transparency` — each a standard-envelope projection of the same subject. |

The JSON schema is
[`schemas/audit-receipt-v1.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/schemas/audit-receipt-v1.json)
(see [JSON schema reference](#json-schema-reference)).

## Canonicalization profile

Every hash and signature in the receipt is computed over **canonical** bytes.
There is exactly one canonical form for each object type.

### Canonical JSON

For any JSON object, the canonical form is produced by sorting keys
lexicographically and emitting compact separators, then UTF-8 encoding:

```python
def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

This is the Python `json.dumps(..., sort_keys=True, separators=(",", ":"))`
profile. Key order is alphabetical; there is no whitespace between tokens;
`", "` and `": "` separators are replaced by `,` and `:`. All strings are
UTF-8. Numbers are emitted in their shortest round-trip form.

### Canonical JSONL for the event range

The event range is serialized as canonical JSONL: each event is canonicalized
individually, the lines are joined with a single `\n`, and a trailing `\n` is
appended. An empty range serializes to the empty byte string.

```python
def canonical_jsonl(events) -> bytes:
    if not events:
        return b""
    lines = [json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events]
    return ("\n".join(lines) + "\n").encode("utf-8")
```

## Subject binding

The receipt's single subject digest is the chain range **`head_sha256`**:

```python
head_sha256 = sha256(canonical_jsonl(events)).hexdigest()
```

where `events` is the embedded, re-chained event range. This value appears in
three places that must agree:

1. `subject.digest.sha256` — the signed subject;
2. `range.head_sha256` — the range metadata;
3. the recomputed value derived from `events` by a verifier.

### `head_sha256` is never independently recomputed

`head_sha256` is **not** a fresh hash computed by the receipt builder. It is
derived from the same reader path the multi-tenant export uses
(`_read_audit_events` → `_rebuild_slice_chain` → `_events_jsonl_bytes`): the
source events are read, filtered to the window, stably sorted, re-chained into
a slice-local HMAC chain, and the head is the SHA-256 over the canonical JSONL
of that re-chained slice. The receipt binds to exactly the value an offline
replay of the same events would produce. There is no second, independent head
computation that could diverge.

The consequence is the tamper-collapse property: mutate any embedded event and
the recomputed head diverges from the signed subject, so **every** format
fails. Strip or alter the chain and the receipt stops verifying; it does not
merely lose a log line.

### Range metadata

`range` carries the window and chain anchors:

| Field | Type | Meaning |
|---|---|---|
| `since` | string | Inclusive ISO-8601 lower bound of the projected range. |
| `until` | string | Exclusive ISO-8601 upper bound of the projected range. |
| `genesis_prev_hmac` | string | Sentinel `prev_hmac` of the first re-chained event — always `0`×64. |
| `head_hmac` | string | HMAC of the final event in the re-chained range (`0`×64 when empty). |
| `head_sha256` | string | SHA-256 over the canonical JSONL of the re-chained range. Equal to `subject.digest.sha256`. |
| `event_count` | integer | Number of events in the projected range. |

### Run-attestation projection binding

The run-attestation projection
(`src/bernstein/core/security/run_attestation_receipt.py`) reuses the same
substrate but selects its range by **authenticated chain position**, not by
timestamp. Its `receipt_type` is
`https://bernstein.run/attestations/run-attestation-receipt/v1`. The following
values bind into the range:

- **`run_id`** — the identity-bound run being attested. It is carried in the
  `run_attestation` projection block and is re-derived by a verifier from the
  first retained `identity.spawn_attestation` event.
- **`identity_anchor_hmac`** — the authenticated HMAC of the run's unique
  `identity.spawn_attestation` anchor. It is the first event of the retained
  range and is mirrored in `range.source_start_hmac`.
- **`through_hmac`** — the authenticated HMAC boundary at which the range ends
  (the verified snapshot head when no explicit boundary is supplied). It is
  mirrored in `range.source_end_hmac`.

The range block additionally carries `selection: "authenticated-chain-position"`
and the two source-boundary witnesses `source_start_hmac` / `source_end_hmac`,
so a verifier can confirm the retained range begins and ends at the declared
authenticated witnesses. The `run_attestation` projection block records the
derived verdicts (`dispatch_evidence_verdict`, `whole_run_verdict`,
`provisional`, `terminal_boundary`); a verifier must re-derive these from the
retained events rather than trust the serialized values.

> **Note.** The run-attestation projection does **not** carry `journal_head` or
> `spine_head`. Those fields belong to the separate `verify run` run-receipt
> (a different document type). The run-attestation receipt binds only the
> audit-chain witnesses listed above.

## Signing key

Every format signs with the same Ed25519 key, embedded in the receipt as an
RFC 8037 JWK:

```json
"signing": {
  "alg": "EdDSA",
  "key_id": "<key-id>",
  "public_key_jwk": {
    "kty": "OKP",
    "crv": "Ed25519",
    "alg": "EdDSA",
    "x": "<urlsafe-base64-no-padding-32-byte-public-key>",
    "kid": "<key-id>"
  }
}
```

`key_id` is the JWK `kid` when present, else the literal `"audit-receipt-key"`.
The `x` member is the 32-byte Ed25519 public key, base64url-encoded without
padding. Signatures are RFC 8032 deterministic Ed25519 — for a fixed key and
message the signature bytes are always identical.

## Format 1: COSE_Sign1 (RFC 9052)

The `cose` format block is:

```json
"cose": {
  "alg": "EdDSA",
  "key_id": "<key-id>",
  "content_type": "application/vnd.bernstein.audit-receipt+json",
  "cose_sign1_b64": "<base64 of the CBOR tag-18 COSE_Sign1 structure>"
}
```

`cose_sign1_b64` is the base64 (standard alphabet) of the canonical CBOR bytes
of a CBOR tag-18 (`COSE_Sign1`) structure:

```
COSE_Sign1 = CBORTag(18, [
    protected,      # bstr: canonical CBOR of the protected header map
    unprotected,    # map: empty {}
    payload,        # bstr: the raw 32-byte head_sha256 digest
    signature       # bstr: Ed25519 signature
])
```

The protected header map (CBOR labels per RFC 9052 §3.1):

| Label | Value | Meaning |
|---|---|---|
| `1` | `-8` | `alg` = EdDSA (RFC 9053 / IANA COSE Algorithms). |
| `3` | `"application/vnd.bernstein.audit-receipt+json"` | `content_type`. |
| `4` | `<key_id>` as UTF-8 bytes | `kid`. |

The signature is computed over the COSE `Sig_structure` for `COSE_Sign1`
(RFC 9052 §4.4):

```
Sig_structure = [
    "Signature1",          # context string
    protected,             # the protected header bstr (exact bytes)
    b"",                   # external_aad: empty
    payload                # the raw 32-byte head_sha256 digest
]
```

```python
def build_cose_sign1(head_sha256: str, key_id: str, sign) -> bytes:
    protected = cbor2.dumps(
        {1: -8, 3: "application/vnd.bernstein.audit-receipt+json", 4: key_id.encode()},
        canonical=True,
    )
    payload = bytes.fromhex(head_sha256)          # raw 32 bytes
    sig_structure = ["Signature1", protected, b"", payload]
    to_sign = cbor2.dumps(sig_structure, canonical=True)
    signature = sign(to_sign)                     # Ed25519
    cose = cbor2.CBORTag(18, [protected, {}, payload, signature])
    return cbor2.dumps(cose, canonical=True)
```

The signed payload is the raw 32-byte digest, not its hex string. A verifier
must assert `payload.hex() == recomputed_head`.

## Format 2: DSSE / in-toto v1

The `intoto` format block is a standard DSSE envelope:

```json
"intoto": {
  "payload": "<base64 of the canonical in-toto Statement JSON>",
  "payloadType": "application/vnd.in-toto+json",
  "signatures": [
    { "keyid": "<key-id>", "sig": "<base64 of the Ed25519 signature>" }
  ]
}
```

### DSSE Pre-Authentication Encoding (PAE)

The signature is over the DSSE PAE of the payload:

```
PAE(payload_type, payload) =
    b"DSSEv1 "
    + str(len(payload_type_bytes)) + b" "
    + payload_type_bytes
    + b" "
    + str(len(payload)) + b" "
    + payload
```

```python
def pae(payload_type: str, payload: bytes) -> bytes:
    t = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(t)).encode("ascii") + b" " + t
        + b" "
        + str(len(payload)).encode("ascii") + b" "
        + payload
    )
```

The `payloadType` is `application/vnd.in-toto+json`. The signature is
`Ed25519(pae(payload_type, payload))`.

### in-toto v1 Statement payload

The payload is the canonical JSON of an in-toto v1 Statement:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    { "name": "<subject-name>", "digest": { "sha256": "<head_sha256>" } }
  ],
  "predicateType": "https://bernstein.run/attestations/audit-receipt/v1",
  "predicate": {
    "schema_version": "1.0.0",
    "kind": "audit-receipt",
    "range": { "since": "...", "until": "...", "genesis_prev_hmac": "...",
               "head_hmac": "...", "head_sha256": "...", "event_count": 0 }
  }
}
```

The `subject[].digest.sha256` is the chain `head_sha256`. A verifier must
assert that the recomputed head appears among the statement's subject digests.
The `predicateType` is `https://bernstein.run/attestations/audit-receipt/v1`
for the base receipt; the run-attestation projection uses
`https://bernstein.run/attestations/run-attestation-receipt/v1` and adds a
`run_attestation` block to the predicate.

## Format 3: Transparency (RFC 6962 style)

The `transparency` format block is an offline Merkle inclusion receipt:

```json
"transparency": {
  "log_algorithm": "RFC6962-SHA256",
  "signed_tree_head": {
    "tree_size": 0,
    "root_hash": "<64 hex>",
    "subject_sha256": "<head_sha256>",
    "signature_b64": "<base64 of the Ed25519 signature>"
  },
  "inclusion_proof": {
    "leaf_index": 0,
    "leaf_hash": "<64 hex>",
    "audit_path": [ { "hash": "<64 hex>", "left": true } ]
  },
  "rekor": null
}
```

### Merkle hashing

Leaves are the range's per-event canonical bytes, domain-separated with a
`0x00` tag:

```python
def leaf_digest(event) -> str:
    return sha256(b"\x00" + canonical_json(event)).hexdigest()
```

Internal nodes are domain-separated with a `0x01` tag:

```python
def combine_internal(left: str, right: str) -> str:
    return sha256(b"\x01" + left.encode() + right.encode()).hexdigest()
```

A **lone odd node is promoted unchanged** to the next level — it is never
self-paired. The empty tree root is the sentinel
`sha256(b"empty-tree").hexdigest()`.

```python
def merkle_root(leaves) -> str:
    if not leaves:
        return sha256(b"empty-tree").hexdigest()
    level = list(leaves)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(combine_internal(level[i], level[i + 1]))
            else:
                nxt.append(level[i])          # lone odd node promoted
        level = nxt
    return level[0]
```

### Signed tree head

The signed tree head binds the Merkle root and the subject digest:

```python
sth = {"tree_size": len(leaves), "root_hash": root, "subject_sha256": head_sha256}
sth_signature = sign(canonical_json(sth))     # Ed25519
```

The `signature_b64` is the base64 of that signature. `subject_sha256` must
equal the recomputed head.

### Inclusion proof

The proof places the chain-head (last) event's leaf in the tree. `leaf_index`
is `len(leaves) - 1` (or `-1` for an empty tree). `audit_path` is a list of
`{hash, left}` steps from leaf to root, where `left` is `true` when the sibling
is the left child (so the current node is the right child). A verifier folds
the proof up to the root:

```python
def root_from_inclusion(leaf_hash, audit_path) -> str:
    node = leaf_hash
    for step in audit_path:
        sibling = step["hash"]
        node = combine_internal(sibling, node) if step["left"] else combine_internal(node, sibling)
    return node
```

and asserts the folded root equals `signed_tree_head.root_hash`, and that the
rebuilt Merkle root over the embedded events equals it too.

### Rekor (optional, online)

`rekor` is `null` by default. When online submission was explicitly requested
and succeeded, it records `{log_index, log_id, inclusion_proof}` from a Rekor
transparency log. It is strictly opt-in and never required for offline
verification.

## Verification algorithm

A verifier recomputes the head from the embedded range and asserts every format
binds it. Pseudocode:

```python
def verify(receipt, pinned_key=None):
    # 1. Resolve the verifying key.
    embedded_jwk = receipt["signing"]["public_key_jwk"]
    if pinned_key is not None:
        assert embedded_jwk matches pinned_key   # else: provenance fails
    public_key = ed25519_public_key_from_jwk(embedded_jwk)

    # 2. Recompute the chain head from the embedded range.
    recomputed = sha256(canonical_jsonl(receipt["events"])).hexdigest()

    # 3. Subject binding.
    assert receipt["subject"]["digest"]["sha256"] == recomputed
    assert receipt["range"]["head_sha256"] == recomputed

    # 4. Per-format checks.
    if "cose" in receipt["formats"]:
        verify_cose(receipt["formats"]["cose"], public_key, recomputed)
    if "intoto" in receipt["formats"]:
        verify_intoto(receipt["formats"]["intoto"], public_key, recomputed)
    if "transparency" in receipt["formats"]:
        verify_transparency(receipt["formats"]["transparency"], public_key, recomputed)
```

Per-format checks:

- **`cose`** — decode the CBOR tag-18 structure; verify the Ed25519 signature
  over the `Sig_structure`; assert `payload.hex() == recomputed`.
- **`intoto`** — verify the Ed25519 signature over `pae(payloadType, payload)`;
  decode the Statement; assert `recomputed` is among the subject digests.
- **`transparency`** — verify the STH signature over the canonical
  `{tree_size, root_hash, subject_sha256}`; assert `subject_sha256 == recomputed`;
  rebuild the Merkle root over the embedded events and assert it equals
  `root_hash`; fold the inclusion proof and assert it equals `root_hash`.

## Trust tiers: embedded-JWK vs pinned-key

The receipt embeds its own Ed25519 public key. This yields two distinct
verification tiers:

| Tier | How reached | What it proves |
|---|---|---|
| **Integrity-only** (trust-on-first-use) | No pin supplied; the embedded `signing.public_key_jwk` is used as-is. | The bytes were signed by the embedded key — internal self-consistency. It does **not** prove the signer is a known, trusted party. A forged receipt+key pair verifies against itself. |
| **Provenance** | A trusted key is pinned out-of-band via `--jwk` or `--public-key`; the embedded key must match it. | The bytes were signed by a known, trusted key. This is the regulator-grade property: the auditor supplies the key, so a forged embedded key cannot pass. |

Pinning is what stops a swapped-key attack. Without a pin, verification
establishes integrity only; with a pin, it establishes provenance. The
verifier names the tier it reached on the `public_key` line (`pinned-pem`,
`pinned-jwk`, or `trust-on-first-use`), so a caller gating on provenance
supplies the pin and reads that line rather than inferring it from the exit
code (see [Exit-code contract](#exit-code-contract)).

## Exit-code contract

`bernstein audit receipt verify <path>` shells to
`tools/verify_audit_receipt.py` and propagates its exit code, so the operator
surface and the auditor's standalone surface share one contract:

| Code | Meaning |
|---|---|
| `0` | **Verified** — every enabled check passed (either tier). |
| `1` | **Failed** — a check failed: unreadable or unparseable receipt body, missing or invalid signing key, embedded key that does not match the pin, recomputed head that does not match the signed subject, a signature that does not verify, a Merkle root or inclusion proof mismatch, or no recognised format present. |
| `2` | **Bad arguments** — a path argument is missing or unreadable, or `--jwk` is not a JSON object. |

> **Not the run-receipt command.** `bernstein verify receipt <path>` verifies a
> *run* receipt (`https://bernstein.run/attestations/run-receipt/v1`) — a
> different document with its own `0/1/2/3` contract. Pointed at an audit
> receipt it exits `1` (MALFORMED), because an audit receipt carries no
> `run_id`. The two surfaces are not interchangeable.

## Worked example

Test vectors live at:

- `tests/fixtures/receipt-vectors/valid-receipt.json` — a receipt whose
  embedded range is intact.
- `tests/fixtures/receipt-vectors/tampered-receipt.json` — the same receipt
  with one embedded event mutated.
- `tests/fixtures/receipt-vectors/valid-receipt-key.pem` — the Ed25519 public
  key both were signed under.

```console
$ python tools/verify_audit_receipt.py \
    --receipt tests/fixtures/receipt-vectors/valid-receipt.json \
    --public-key tests/fixtures/receipt-vectors/valid-receipt-key.pem
```

Verifying `valid-receipt.json` must exit `0`: the recomputed head equals the
signed subject, and every format's signature and binding check passes. With the
key pinned as above it reaches the provenance tier; without one it reaches the
integrity-only tier.

Verifying `tampered-receipt.json` must exit `1`: mutating one embedded event
changes the recomputed head, which no longer matches the signed subject, so the
subject-binding check and every format that binds it fail. The receipt does not
merely lose a line — it stops verifying.

`tests/unit/test_audit_receipt_format_vectors.py` asserts both verdicts against
these committed files on every push, and re-signs the frozen event range with
the current encoder to assert byte-equality with the committed receipt — so a
change to any encoding in this document fails CI instead of silently
invalidating evidence already handed to an auditor.

## Determinism

For a fixed chain range and signing key, the receipt bytes are byte-identical
across independent runs: canonical JSONL for the range, canonical CBOR
(`cbor2` deterministic mode) for COSE, canonical JSON for the DSSE payload, and
RFC 8032 deterministic Ed25519 for every signature. No wall-clock value enters
the signed or serialized bytes. This is what makes auditor reproducibility
possible: the operator re-runs the projection a year later and hands over
byte-identical evidence.

## JSON schema reference

The receipt conforms to
[`schemas/audit-receipt-v1.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/schemas/audit-receipt-v1.json)
(JSON Schema draft-07). The schema pins the required top-level fields, the
`schema_version`/`receipt_type` constants, the `subject`/`range`/`signing`
shapes, and the per-format block shapes. It is the machine-readable companion
to this document; where the two disagree, the schema is authoritative for
structural validation and this document is authoritative for the byte-level
algorithms.

## Related

- [Standard verifiable audit receipts](audit-receipt.md) — operational
  workflow (export, verify, run-attestation surface).
- [DSSE / in-toto envelope](audit-dsse-envelope.md) — the DSSE primitives this
  receipt reuses.
- [Authenticated run-attestation receipt](../sdd/run-attestation-receipt.md) —
  design rationale for the run-attestation projection.
- [Audit log](audit-log.md) — the HMAC chain the receipt slices.
- Source: `src/bernstein/core/security/audit_receipt.py`,
  `src/bernstein/core/security/run_attestation_receipt.py`,
  `tools/verify_audit_receipt.py`, `schemas/audit-receipt-v1.json`.
