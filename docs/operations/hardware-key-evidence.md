# Offline hardware key evidence

Bernstein verifies hardware key custody and action presence evidence offline through
its existing receipt envelope. The two kinds are `key-attestation/piv` and
`approval/fido2-assertion`. No token access, key generation, registration ceremony,
network lookup or system clock is involved. `cryptography` is the only crypto
runtime dependency; no `fido2` or `yubikey-manager` is required.

A token-held signing key **does not raise the TRACE trust level**. Keep
`runtime.platform: software-only` and `appraisal.status: none`. Touch establishes
presence, not informed intent: the token has no trusted display. A PIN is user
verification, not a claim of biometric verification. This feature makes no TRACE
schema changes and does not produce runtime attestation.

## Trust configuration and deterministic time

Configure `HardwarePolicy` independently from the evidence being checked. By default,
root pins load from the bundled vendor certificates after manifest verification.
Pass `root_sha256` explicitly to replace these pins; an empty set trusts no roots:

```python
from datetime import datetime, timezone
from bernstein.core.identity.hardware_attestation import HardwarePolicy, bundled_root_pins
from bernstein.core.receipts.protocol import verify_receipt

policy = HardwarePolicy(
    at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    root_sha256=bundled_root_pins(),
    f9_sha256=frozenset({"<independently enrolled F9 DER SHA256>"}),
)
result = verify_receipt(receipt_document, context=policy)
assert result.ok, result.errors
```

The optional `at` overrides historical appraisal time. Otherwise a timezone-aware
ISO-8601 `timestamp` in the signed payload is required. No wall-clock fallback
exists. A signer-supplied historical timestamp proves neither freshness nor
current validity. An application requiring freshness must supply a trusted time
and its own replay policy. Identical receipt bytes, policy and counter snapshot
produce identical results. `EvidenceVerdict.to_json()` uses the existing canonical
receipt bytes function. Generic receipt results include errors and warnings.
Absent external policy fails closed. Never accept root/F9/enrollment pins, RP,
origins or UV requirements merely because a receipt supplies them.

The packaged PEM roots are genuine vendor certificates, with DER SHA256 and
original HTTPS source URLs in `core/identity/hardware_roots/manifest.json`.
`bundled_root_pins()` checks their manifest before returning fingerprints. They
are public trust anchors, not synthetic test certificates. Updating trust anchors
is an explicit operator/code review decision. No revocation, metadata-service,
AIA download, OS trust store or current vendor assurance is inferred.

## PIV evidence

The payload contains `chain`, a root-to-leaf list of PEM certificates, optionally
`timestamp`. It includes root, intermediate tier, pinned device F9 and attested
leaf. New Yubico chains can contain **two** intermediate certificates:
Root 1 → Intermediate B 1 → PIV Attestation B 1 → device F9 → leaf.
A four-certificate synthetic/legacy path is also supported.

Every supplied issuer signature and validity interval is verified. Issuer CA,
path-length and optional keyCertSign constraints are enforced. Only critical
BasicConstraints/KeyUsage semantics are supported; every other critical extension
fails closed, including recognized but unenforced name/policy constraints. F9 must be externally pinned and RSA; the leaf signature uses
PKCS#1 v1.5. Other chain links support RSA, ECDSA and Ed25519. The attested SPKI
must equal the actual outer receipt signer. Receipt signing and verification use
the existing shared protocol path, supporting Ed25519 and the P-256 fallback;
other catalog signing callers retain their Ed25519-only default.

Typed vendor extensions decode firmware `.3.3` (three raw octets), serial `.3.7`
(DER INTEGER), PIN/touch `.3.8` (two raw octets), and form factor `.3.9` (one raw
octet), under `1.3.6.1.4.1.41482`. Ed25519 requires firmware ≥5.7. Public verdicts
omit serial and all certificate subjects. Treat retained certificates as private:
leaf certificates can identify a physical token.

`approval=True` is the default and rejects PIN or touch `NEVER`. For custody
inspection without approving an action, explicitly set `approval=False`. `ONCE`
and `CACHED` are reported literally; applications requiring per-action PIN/touch
must enforce `ALWAYS` against the decoded metadata. Approval mode's rejection of
`NEVER` alone does not claim an uncached per-action ceremony.

## FIDO2 approval evidence

Configure `rp_id`, exact allowed `origins`, optional `require_uv`, externally
enrolled `registration_sha256` and an optional immutable `previous_counts`
credential-ID/count snapshot. Credential IDs use unpadded base64url. Registration
and assertion byte strings also use unpadded canonical base64url.

The approval payload fields are:

- `approved_payload`: the exact JSON object approved.
- `credential_id`, `authenticator_data`, `client_data_json`, `signature`.
- `registration`: the retained packed enrollment evidence described below.
- Optional signed `timestamp` when policy has no explicit `at`.

The client data must have `type: webauthn.get`, configured origin, no cross-origin
ceremony, and challenge `base64url(sha256(canonical_receipt_bytes(approved_payload)))`.
The assertion signature covers `authenticatorData || sha256(clientDataJSON)`.
The verifier checks configured RP hash, UP, optional UV, flags, signCount, and
Ed25519 COSE `-8` or ES256 COSE `-7`. Counter zero or a nonincreasing count produces
a warning, not rejection. There is no hidden mutable counter store; persist and
supply counters at the application layer. The signed approved payload must be
matched against the intended action by the application; verification does not
execute or authorize an action automatically.

Retain registration as `fmt: packed`, numeric `alg`, `authenticator_data`,
`client_data_json`, `challenge`, `signature`, and root-to-attestation-leaf PEM
`chain`. This is an explicit JSON transport of packed attestation fields, not a
browser `attestationObject` blob. Independently enroll its
`registration_digest(registration)`; never learn that pin from an assertion.
The verifier validates the retained enrollment's signature and certificate path,
packed X509 profile (including optional AAGUID binding), RP, origin and creation
type, and reads credential ID and COSE key directly from signed authenticator
data. Subsequent assertions must use that exact credential and key.

Only bounded definite-length CBOR and these two COSE algorithms are supported.
Self/none attestation, unsupported algorithms and authenticator extensions fail
closed. Full browser ceremony, metadata certification, revocation, backup policy,
credential registration and real hardware interaction remain outside this offline
verifier. Tests use synthetic certificates and signatures only.

Vendor references: [PIV attestation](https://developers.yubico.com/PIV/Introduction/PIV_attestation.html),
[attestation chain](https://developers.yubico.com/yubico-piv-tool/Attestation.html),
[public PKI](https://developers.yubico.com/PKI/).
