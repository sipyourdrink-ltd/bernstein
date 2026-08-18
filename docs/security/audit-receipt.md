# Standard verifiable audit receipts

The HMAC audit chain proves "no record was edited inside Bernstein", and the
[DSSE / in-toto envelope](audit-dsse-envelope.md) and the v2
[multi-tenant export](audit-multitenant.md) add key-less origin authentication.
All of them still end with "now install our verifier". An audit receipt closes
that gap: it projects an existing audit-chain range into off-the-shelf envelope
formats an external auditor already knows how to validate, with no Bernstein
install and no shared HMAC secret.

For the underlying chain see [audit log](audit-log.md); for the envelope this
receipt reuses see [DSSE / in-toto envelope](audit-dsse-envelope.md).

## What a receipt is

A receipt is a **projection**, not new evidence. Its subject digest IS the
chain range `head_sha256` (the SHA-256 over the canonical JSONL of the
re-chained slice, computed by the same reader path the multi-tenant export
uses). Every format binds that single subject, and the receipt embeds the
re-chained event range so a standard verifier can recompute the head from the
embedded range and assert it equals the signed subject.

The consequence is the whole point: mutate any embedded chain entry and the
recomputed head diverges, so **every format fails**. Strip or alter the chain
and the receipt stops verifying; it does not merely lose a log line.

## Formats

| Format | Standard | Payload / binding | Offline verifier |
|---|---|---|---|
| `cose` | COSE_Sign1, RFC 9052 (EdDSA) | Signs the raw 32-byte `head_sha256` | any COSE library + `cbor2` |
| `intoto` | DSSE + in-toto Statement v1 | `subject.digest.sha256` = `head_sha256` | any in-toto / DSSE / SLSA verifier |
| `transparency` | RFC 6962 style signed receipt | Merkle inclusion proof of the chain-head event; signed tree head binds root + subject | Merkle inclusion check + Ed25519 |

All three sign through the same Ed25519 KMS adapter that already signs lineage
records and the multi-tenant `head_signature` (see
[`lineage_kms.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/security/lineage_kms.py)) - no new
key material or rotation cadence. The verifying key is embedded as an RFC 8037
JWK (`signing.public_key_jwk`) so the receipt is self-contained; auditors may
pin it out-of-band for additional trust.

The `transparency` format is offline by default (a local Merkle inclusion proof
built with the same domain-separated hashing as `bernstein audit seal`). Online
Rekor submission is strictly opt-in (`--online-rekor`) so air-gapped operators
are never forced onto the network.

## Determinism

For a fixed chain range and signing key the receipt bytes are byte-identical
across independent runs: canonical JSONL for the range, canonical CBOR for
COSE, canonical JSON for the DSSE payload, and RFC 8032 deterministic Ed25519
for every signature. No wall-clock value enters the signed or serialized bytes.

## Exporting

```bash
bernstein audit receipt export \
  --format cose,intoto,transparency \
  --since 2026-01-01T00:00:00Z \
  --until 2026-02-01T00:00:00Z \
  --signing-key-path /path/to/ed25519.pem \
  --output .sdd/evidence
```

The export writes `audit-receipt-<since>-<until>.json` and records an
`audit.receipt_export` event into the HMAC chain (head, window, event count,
receipt hash, format list) so the projection is itself chain-attested. Use
`--dry-run` to print the receipt without writing, `--signing-env-var` to read
the key from an environment variable, and `--online-rekor` to also anchor in a
Rekor transparency log.

## Verifying offline

The standalone verifier imports **no Bernstein code** - only `cryptography` and
`cbor2`, the libraries an auditor already runs:

```bash
python tools/verify_audit_receipt.py --receipt audit-receipt-....json --verbose
```

It recomputes `head_sha256` from the embedded range, asserts every format binds
that value, and validates each envelope with a stock verification path. Pin the
key with `--jwk` or `--public-key`; scope with `--format cose|intoto|transparency|all`.
Exit code `0` means all checks passed, `1` a check failed, `2` bad arguments.

The `bernstein audit receipt verify <path>` subcommand shells out to that
standalone tool on purpose - it proves the receipt needs nothing from Bernstein
to validate.

## Run-attestation projection

`build_run_attestation_receipt(...)` reuses the same COSE, DSSE/in-toto, and
transparency substrate for identity-bound tool-call evidence. Its range is not
chosen by timestamps: construction verifies the source audit chain from
genesis, finds exactly one `identity.spawn_attestation` for the run, and keeps
every source event from that anchor through an authenticated HMAC boundary.
Events belonging to other runs remain interleaved in the embedded range. This
preserves the omission-detection boundary instead of filtering and silently
re-chaining a hand-picked history.

The projection distinguishes two verdicts:

- `dispatch_evidence_verdict` is re-derived from retained identity envelopes,
  attestation references, and enforced-dispatch markers;
- `whole_run_verdict` becomes `complete` only when the retained range ends in
  one still-valid `run.closure` marker that binds the run journal; otherwise
  the receipt remains `observed` and `provisional`.

The second rule is load-bearing. An authenticated snapshot head proves exactly
what was retained, not that the run ended there. The semantic verifier walks
the retained range: absence stays open, duplicate or malformed markers fail
closed, and any later same-run event invalidates the terminal claim. A detached
closure bound to a work ledger remains valid detached-run evidence but cannot
upgrade a receipt whose beginning is anchored to the run journal. A serialized
`complete` claim cannot upgrade itself.

An explicit historical boundary may still scope an observed receipt, but it
cannot establish whole-run completeness. Construction refuses a historical
range that would otherwise upgrade to `complete` unless its boundary is also
the verified source snapshot head, so a caller cannot conceal a later same-run
event behind an earlier closure marker.

Use `tools/verify_audit_receipt.py` to verify the standard receipt formats and
pin the receipt-signing key. `verify_run_attestation_projection(...)` separately
recomputes the run-specific semantic projection from the embedded events. A
self-embedded JWK establishes cryptographic self-consistency only; provenance
requires an independently pinned JWK or public key.

### Operator surface

`bernstein identity attest` projects the receipt from the command line. Both
verbs take the same signing options as `bernstein audit receipt export`, since
building a projection signs one.

```bash
# Project without writing; print both verdicts.
bernstein identity attest show --run <run-id> --signing-key-path key.pem

# Rebuild, verify the projection, and emit the receipt.
bernstein identity attest verify --run <run-id> --signing-key-path key.pem
```

`verify` exits non-zero and names each failing check when the recomputed range
no longer supports the projection. It stages the receipt privately and moves
it into the evidence directory only after semantic verification passes, so a
failed projection is never left behind as normal evidence. `show` creates
neither a receipt nor audit key material. Both verdicts are recomputed from the
retained range on every invocation; neither is read from a field in the receipt,
so a serialized claim cannot promote itself through this surface any more than
it can through the library.

These verbs sit under `identity attest` rather than extending `identity
verify`, which checks an install-rev fingerprint token. The two answer
different questions about different objects and only share a noun.

### Verifying with third-party tooling

Because `intoto` is a plain DSSE envelope wrapping an in-toto v1 Statement, any
DSSE / in-toto / SLSA verifier validates it given the public key; the subject
digest it reports is the chain `head_sha256`. The `cose` block is a standard
COSE_Sign1 structure (`base64` of the CBOR tag-18 bytes) that any COSE library
verifies against the same key. In both cases, checking that the reported
subject digest equals the head recomputed from the embedded range is what ties
the standard envelope back to the intact chain.

## Schema

The receipt conforms to
[`schemas/audit-receipt-v1.json`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/schemas/audit-receipt-v1.json).

## Threat model

- **Tampered chain entry** - recomputed head diverges from the signed subject;
  every format fails.
- **Forged signature** - the receipt embeds the public key but the signatures
  are Ed25519 over the subject / PAE / tree head; an attacker without the
  private key cannot re-sign a mutated subject.
- **Swapped public key** - pin the JWK (`--jwk`) or PEM (`--public-key`) out of
  band; the embedded key must then match or verification fails.
- **No operator secret required** - unlike raw HMAC chain verification, a
  receipt needs no shared HMAC key, so a sovereign auditor validates it alone.
