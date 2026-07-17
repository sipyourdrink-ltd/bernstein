# DSSE / in-toto envelope on the audit bundle

The HMAC chain on disk proves "no record was edited inside Bernstein". A
single-key HMAC alone cannot give a third-party auditor what they need:
verification without trusting the operator's HMAC key. The DSSE + in-toto
v1 envelope wraps an Article 12 evidence bundle in an Ed25519-signed payload
that any party with the public key can verify, using either the in-tree
Python API or a stdlib-only standalone verifier.

For the bundle itself, see
[EU AI Act Article 12 evidence pack](../compliance/eu-ai-act-article-12-bundle.md).
For the underlying chain, see [audit log](audit-log.md). For the
multi-tenant-export flavour of the same envelope, see
[multi-tenant audit-chain export](audit-multitenant.md).

## Wire format

The envelope is shaped per
[DSSE v1.0](https://github.com/secure-systems-lab/dsse) with the in-toto
[Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/README.md)
as the payload. Three fixed identifiers:

| Field | Value |
|---|---|
| DSSE `payloadType` | `application/vnd.in-toto+json` |
| in-toto `_type` | `https://in-toto.io/Statement/v1` |
| in-toto `predicateType` | `https://bernstein.run/attestations/audit/v1` |

The predicate body carries the Article 12 bundle's `manifest.json` plus the
chain anchor (head HMAC + head SHA-256). The `subject` array carries one
entry per artefact in the bundle with its SHA-256 digest, so an auditor can
match an envelope against a separately-archived `.zip` without parsing the
predicate body.

Source: `src/bernstein/core/security/audit_dsse.py`.

## Determinism contract

- Same input bundle bytes → byte-identical envelope payload (canonical JSON,
  sorted keys, no whitespace, fixed field order).
- Same input bundle bytes + same Ed25519 private key → byte-identical
  envelope including signature, because Ed25519 is deterministic by spec
  ([RFC 8032 §5.1.6](https://www.rfc-editor.org/rfc/rfc8032#section-5.1.6)).

The determinism property matters for auditor reproducibility: the operator
re-runs the wrap pipeline a year later, hands the byte-identical envelope
to the auditor, and the auditor's verification against the published public
key produces the same `OK` exit.

## Producing an envelope

```python
from bernstein.core.security.article12_bundle import build_article12_bundle
from bernstein.core.security.audit_dsse import wrap_bundle, write_envelope

bundle = build_article12_bundle(audit_dir, since, until)
envelope = wrap_bundle(bundle, signing_key=ed25519_private_key)
write_envelope(envelope, dest_path)
```

The signing key is the operator's Ed25519 private key. The same key and
rotation cadence used for the multi-tenant `head_signature` block can drive
this envelope; both flow through the `KMSAdapter` protocol when the
operator has plumbed lineage v2 KMS adapters (see
[regulatory lineage](../compliance/regulatory-lineage.md)).

## Verifying with the in-tree API

```python
from bernstein.core.security.audit_dsse import load_envelope, verify_envelope

envelope = load_envelope(envelope_path)
result = verify_envelope(envelope, trusted_public_key=ed25519_public_key)
if not result.ok:
    for err in result.errors:
        print("FAIL:", err)
    raise SystemExit(1)
```

`verify_envelope` runs four independent checks:

1. **Envelope shape** - DSSE structural fields, payload type, signatures
   array, base64 payload validity.
2. **Statement type** - `_type` and `predicateType` match the constants
   above. Out-of-band envelopes from another producer fail this check.
3. **Subject digest** - every artefact in the predicate's bundle manifest
   has a matching SHA-256 entry in the in-toto subject list. A digest
   mismatch surfaces here before any signature work.
4. **Signature** - the DSSE PAE bytes verify against the supplied public
   key. Wrong key, edited payload, edited signature, all fail.

## Verifying with the standalone verifier

The standalone verifier exists for the auditor who does not want to
install bernstein. It depends only on the Python standard library plus
`cryptography` and refuses to import anything from the `bernstein.*`
package - a subprocess-isolated test in CI asserts that
`import bernstein` raises `ModuleNotFoundError` from inside the
verifier's venv.

```bash
python tools/verify_audit_dsse.py \
  --envelope path/to/envelope.json \
  --public-key path/to/operator-pubkey.pem
```

Add `--bundle path/to/bundle.zip --hmac-key path/to/audit.key` and the
verifier additionally re-walks the HMAC chain inside the bundle. Without
the HMAC key, the verifier still validates the envelope's Ed25519
signature and the in-toto subject digests - that is the regulator-class
property: the auditor confirms integrity without holding the operator's
HMAC secret.

Exit codes:

| Code | Meaning |
|------|---------|
| `0`  | Envelope intact (and HMAC chain intact, if `--hmac-key` was supplied). |
| `1`  | One or more verification errors. |
| `2`  | Argument or input-file error (envelope unreadable, key file missing, etc.). |

Pass `--verbose` for a per-check breakdown of the four envelope checks
plus the HMAC chain walk.

Source: `tools/verify_audit_dsse.py`.

## Tamper behaviour

The verifier flags tamper at the most specific layer that catches it:

- A flipped byte inside the `payload` base64 → DSSE signature failure.
- A swap of one bundle artefact for another (matching name, different
  bytes) → subject digest failure on the swapped artefact's SHA-256 entry.
- An edited HMAC chain inside an otherwise-intact bundle → HMAC chain
  failure (only when `--hmac-key` is supplied; without the key, the
  verifier cannot detect chain edits but the envelope's subject digest
  catches a swap of the entire bundle archive).
- A stripped or replaced signature → DSSE structural failure followed by
  signature failure.

The four-check separation is intentional: the auditor's failure report
points at the layer that broke, not just "verification failed".

## Public-key distribution

The operator publishes the Ed25519 public key alongside the bundle archive
and the envelope - typically as a PEM file inside the same evidence pack
zip. For supply-chain hardening, pin the public key in the verifier
harness rather than reading it from the bundle the auditor was handed:

```bash
python tools/verify_audit_dsse.py \
  --envelope bundle/envelope.json \
  --public-key /etc/bernstein/operator-pubkey.pem
```

Pinning the key out-of-band is what stops a forged envelope+key pair from
verifying against itself.

## Compliance mapping (one-line)

- **EU AI Act Art. 12(2)(c)** - third-party-verifiable monitoring of
  high-risk AI systems referred to in Art. 26(5). The envelope's Ed25519
  signature plus the standalone verifier together form the evidence the
  auditor can verify offline.
- **EU AI Act Art. 19(1)** - automatically generated logs, retained ≥6
  months. The DSSE wrapping does not change retention; it makes the
  retained artefact verifiable years later under a key the operator can
  hand off.
- **DORA Art. 9(3)** - integrity of ICT records. The DSSE envelope is the
  integrity proof; the operator pairs it with an immutable backend (S3
  Object Lock, WORM Postgres) for the storage-side guarantee.
- **AIGF `CTRL-AUDIT-TRAIL`** - covered. See
  [FINOS AIGF mapping](../compliance/finos-aigf-mapping.md).

## Related

- [Audit log](audit-log.md) - the HMAC chain the bundle slices.
- [Multi-tenant audit-chain export](audit-multitenant.md) - same DSSE
  primitives applied to a per-tenant slice with optional RFC 3161
  timestamping.
- [Standard verifiable audit receipts](audit-receipt.md) - reuses these DSSE
  primitives to project a chain range into COSE_Sign1, in-toto, and RFC 6962
  transparency envelopes an off-the-shelf verifier validates.
- [EU AI Act Article 12 evidence pack](../compliance/eu-ai-act-article-12-bundle.md)
  - the bundle the envelope wraps.
- [Regulatory lineage](../compliance/regulatory-lineage.md) - the
  customer-key signing path that shares rotation plumbing with this
  envelope.
- Source: `src/bernstein/core/security/audit_dsse.py`,
  `tools/verify_audit_dsse.py`,
  `src/bernstein/core/security/article12_bundle.py`.
