# Multi-tenant audit-chain export

Bernstein writes one HMAC-chained audit log per orchestrator instance.
When an enterprise operator runs bernstein on behalf of multiple internal
customers, every customer sees the same chain. That is fine for the
operator's internal compliance posture but it does not let them hand a
specific customer (or that customer's external auditor) a slice of the
log without leaking sibling tenants.

`bernstein audit export --tenant <id>` produces a tenant-scoped slice
that:

- Contains only events tagged with the requested `tenant_id`.
- Re-chains those events over a slice-local HMAC so an auditor can
  replay-verify offline using only the operator's HMAC key.
- Carries a tamper-evident SHA-256 anchor over the canonical JSONL bytes
  (catches single-byte flips even without the key).
- (v2, opt-in) Attaches an Ed25519 signature over `head_sha256` so a
  key-less auditor can authenticate the bundle's origin without sharing
  the operator's HMAC key.
- (v2) Cryptographically validates the optional RFC 3161 token
  end-to-end - chain, signature, and `messageImprint` - instead of
  delegating to `openssl ts -verify`.

Bundles are byte-deterministic - the same input window + tenant id (+
same signing key) produces a byte-identical bundle on every run.

## Schema versions

| Version | Status      | Adds                                                                                                            |
|---------|-------------|-----------------------------------------------------------------------------------------------------------------|
| 1.0.0   | shipped 2026-04 | HMAC chain, SHA-256 anchor, optional RFC 3161 token (verifier deferred), optional offline anchor.            |
| 2.0.0   | shipped 2026-05 | Verifiable RFC 3161 chain (PKI walk + CMS signature + messageImprint), optional `head_signature` (Ed25519). |

v1 readers tolerate v2 bundles by ignoring the additional `head_signature`
field - `additionalProperties: true` at the top level of the schema is
intentional. v2 readers verify the new fields when they are present and
the matching trust material is supplied.

## Tagging events with `tenant_id`

The export filters on `details.tenant_id`. To enable per-tenant export,
add the tenant id to every event your code emits:

```python
audit_log.log(
    "task.created",
    actor="alice",
    resource_type="task",
    resource_id="T-1",
    details={"tenant_id": "acme", ...},
)
```

Events that omit `details.tenant_id` are treated as belonging to the
`default` tenant (matching `normalize_tenant_id` in
`src/bernstein/core/security/tenanting.py`). This keeps the rollout
incremental - operators can switch on multi-tenant tagging without
breaking pre-existing chains.

### Accepted tenant identifiers

A tenant id doubles as a directory name - the per-tenant subtree lives at
`.sdd/<tenant_id>/{backlog,metrics}/` - so `normalize_tenant_id` accepts
only values that name a single directory entry:

| Rule | Accepted | Rejected |
|---|---|---|
| Length | 1-64 characters | 65+ |
| Character set | ASCII letters, digits, `_`, `.`, `-` | anything else, including non-ASCII letters and digits |
| First character | ASCII letter or digit | `-`, `.`, `_` |
| Trailing dot | - | `acme.` (Windows strips it, aliasing `acme`) |
| Reserved device names | - | `CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`, `LPT1`-`LPT9`, with or without an extension |

Surrounding whitespace is stripped. Absent, empty, or whitespace-only
input resolves to `default`, which is what keeps tenant-unaware call
sites working.

Anything else raises `InvalidTenantIdError`. It subclasses both
`ValueError` (the id is malformed) and `LookupError` (there is no such
scope to resolve); request surfaces already translate `LookupError` into
`404`, so a malformed `?tenant=` value, `X-Tenant-Id` header, or agent
JWT `tenant_id` claim is refused as a client error rather than surfacing
as a server error.

### The tenant subtree is store-managed, not operator-configurable

`.sdd` itself may be a symlink - where the state directory lives is the
operator's call. Everything below it is layout this store creates and
owns, so from this release each component of
`.sdd/<tenant_id>/{backlog,metrics,runtime,runtime/wal,audit}` is created
and opened relative to a descriptor for its parent, with `O_NOFOLLOW`. A
component that is a symlink is refused rather than followed.

This is a behaviour change for one existing setup. Earlier releases
created the layout with `Path.mkdir(parents=True, exist_ok=True)`, which
treats a symlink to a directory as an existing directory, so a hand-made
link - `.sdd/acme/metrics -> /var/data/acme-metrics`, say - was followed
silently. That link now fails on first use with `OSError` (`ELOOP`, or
`ENOTDIR` on platforms that report it that way) naming the component.

The server does not refuse to start over it. Seed reload - which runs on
startup and again on `SIGHUP` - reports the refusal in its result instead:
the reload payload carries an `error` naming the tenant and the component,
that tenant is left out of the registry, and nothing is written through the
link. A seed naming a tenant whose id is not a valid identifier is reported
the same way. Both are configuration errors to fix and re-signal, not
reasons for the process to fail to come up.

Two ways out, both offline:

1. **Move the data under `.sdd`.** Replace the link with a real
   directory and copy the contents in. Resolve the target with `realpath`
   rather than `readlink`: a relative link target is relative to the link's
   own directory, and `readlink` hands it back unchanged for the shell to
   resolve against the working directory instead. Copy first and swap last,
   so a wrong or missing target costs you nothing:

   ```bash
   link=.sdd/acme/metrics
   test -L "$link" && target=$(realpath "$link") && test -d "$target" \
     && mkdir "$link.new" && cp -a "$target/." "$link.new/" \
     && rm "$link" && mv "$link.new" "$link"
   ```

2. **Move `.sdd` instead.** If the point of the link was to put state on
   another volume, link the whole `.sdd` directory there. That one is
   still followed, because it is the anchor rather than something below
   it.

Checking for the case is a one-liner:

```bash
find .sdd -mindepth 2 -maxdepth 3 -type l
```

#### Where the refusal does not apply

The refusal is `O_NOFOLLOW` on a descriptor-relative open, so it needs a
platform whose `os.open`, `os.mkdir`, `os.stat`, `os.unlink` and
`os.rename` all accept `dir_fd`, and which defines `O_NOFOLLOW`. Linux
and macOS do. Where they are absent - Windows is the case that matters -
`anchored_write.py` reports `ANCHORED_WRITE_SUPPORTED` (and
`ANCHORED_ROTATE_SUPPORTED`) false and falls back to the joined-path
calls that predate the module, which follow a link or a junction exactly
as before.

So on those platforms the tenant subtree is best-effort: nothing here
detects a redirected `.sdd/<tenant>/audit`. Treat multi-tenant storage
that has to resist a local attacker as supported on Linux and macOS, and
keep `.sdd` on a filesystem only the service account can write to
regardless of platform.

The same split applies to file permissions. Files created through the
anchored helper are created `0o600`, which covers the tenant backlog
mirrors, the collector's metric files, and the per-run cost files. That is
a POSIX mode; Windows ignores it and there is no ACL fallback here, so on
Windows those files land under the process umask. The write-ahead log and
the audit chain open their own files and are under the umask everywhere.

## CLI usage

### Bare HMAC chain (most common)

```bash
bernstein audit export \
    --tenant acme \
    --since 2026-08-01T00:00:00+00:00 \
    --until 2026-09-01T00:00:00+00:00 \
    --output .sdd/evidence/
```

### With RFC 3161 third-party timestamp

Get a TimeStampToken from any RFC 3161 TSA (FreeTSA, DigiCert, SwissSign,
etc.). Save the base64-encoded DER token to a file, then:

```bash
bernstein audit export \
    --tenant acme \
    --since 2026-08-01T00:00:00+00:00 \
    --until 2026-09-01T00:00:00+00:00 \
    --signature-kind hmac-chain+rfc3161 \
    --rfc3161-token /path/to/tsa.token.b64 \
    --rfc3161-tsa-url https://freetsa.org/tsr
```

The bundle records the token verbatim. **v2 verifier walks the TSA cert
chain end-to-end** when invoked with `--rfc3161-trusted-tsa-bundle`. The
old "delegate to `openssl ts -verify`" workflow is still supported for
operators who want a redundant external check.

### With Ed25519 signature over `head_sha256` (v2)

```bash
bernstein audit export \
    --tenant acme \
    --since 2026-08-01T00:00:00+00:00 \
    --until 2026-09-01T00:00:00+00:00 \
    --signature-kind hmac-chain+pubkey \
    --head-signing-key-path .sdd/keys/lineage.pem \
    --head-signing-key-id lineage-2026-05
```

The signing key is shared with the lineage signer (PR #1151) - same
rotation cadence, same KMS plumbing (file / env / HSM via the
`KMSAdapter` protocol). Pass `--head-signing-env-var <NAME>` instead of
`--head-signing-key-path` for a K8s `Secret`-mounted key.

The bundle gains a top-level `head_signature` block:

```json
"head_signature": {
    "alg": "EdDSA",
    "key_id": "lineage-2026-05",
    "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "x": "..."},
    "signature_b64": "..."
}
```

A key-less auditor can verify the signature by extracting the embedded
JWK; an auditor who has been handed a pinned JWK out of band can pass it
to the verifier so a key swap is rejected.

### Both layers

```bash
bernstein audit export \
    --tenant acme \
    --since 2026-08-01T00:00:00+00:00 \
    --until 2026-09-01T00:00:00+00:00 \
    --signature-kind hmac-chain+rfc3161+pubkey \
    --rfc3161-token /path/to/tsa.token.b64 \
    --head-signing-key-path .sdd/keys/lineage.pem
```

### Offline anchor (air-gap deployments)

For deployments that cannot reach a public TSA, attach a deterministic
local anchor. Pass `--signature-kind hmac-chain+offline-anchor`. The
anchor is `sha256(head_sha256 || anchored_at_iso)`. It does not certify
wall-clock truth (an attacker with the bundle can recompute it) but it
ties the chain head to a specific operator-attested timestamp inside the
deterministic JSON.

```bash
bernstein audit export \
    --tenant acme \
    --since 2026-08-01T00:00:00+00:00 \
    --until 2026-09-01T00:00:00+00:00 \
    --signature-kind hmac-chain+offline-anchor
```

Note: the default offline anchor uses `datetime.now(UTC)` so two runs
produce different bundles. To get byte-identical air-gap bundles, pass
`offline_anchor_iso` through the Python API directly.

### Dry-run

`--dry-run` builds the bundle in-memory and prints the manifest without
writing to disk. Useful for spot-checking a window before shipping.

## Wire format (v2)

The bundle is a single JSON object that conforms to
`schemas/audit-multitenant-export-v2.json` (JSON Schema draft-07). The
v1 schema (`audit-multitenant-export-v1.json`) is preserved unchanged.

Top-level fields:

| Field            | Required | Type    | Description                                                         |
| ---------------- | -------- | ------- | ------------------------------------------------------------------- |
| `schema_version` | yes      | string  | `1.0.0` or `2.0.0`. v2 readers accept both.                         |
| `tenant_id`      | yes      | string  | Normalized tenant identifier.                                       |
| `audit_window`   | yes      | object  | `{since, until}` - ISO-8601 strings; since < until.                 |
| `chain_anchor`   | yes      | object  | `{genesis_prev_hmac, head_hmac, head_sha256}`.                      |
| `event_count`    | yes      | integer | Number of events in the slice.                                      |
| `events`         | yes      | array   | Slice events in chronological order, with `_original_hmac` witness. |
| `signature`      | yes      | object  | Detached anchor block.                                              |
| `head_signature` | no (v2)  | object  | Ed25519 signature over `head_sha256` (raw 32 bytes).                |

Each event preserves the original orchestrator-wide HMAC at
`details._original_hmac` so an auditor with access to the source log can
cross-reference back. The slice itself is re-chained - `prev_hmac` /
`hmac` link to the slice-local chain, not the orchestrator-wide one.

## Verifying offline

```python
from pathlib import Path

from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_multitenant import verify_tenant_slice
from bernstein.core.security.rfc3161_verifier import load_trusted_tsa_certs

key = load_or_create_audit_key()  # operator's HMAC key
trust = load_trusted_tsa_certs(Path("path/to/freetsa-bundle.pem"))

result = verify_tenant_slice(
    Path("path/to/bundle.json"),
    key=key,
    rfc3161_trusted_tsa_certs=trust,            # opt-in
    head_signature_trusted_jwk={"kty": "OKP", "crv": "Ed25519", "x": "..."},
)
if not result.ok:
    for err in result.errors:
        print("FAIL:", err)
    raise SystemExit(1)
print("OK", result.bundle["event_count"], "events")
```

Or via the CLI:

```bash
bernstein audit verify-multitenant \
    --bundle .sdd/evidence/audit-multitenant-acme-2026-08-01-2026-09-01.json \
    --rfc3161-trusted-tsa-bundle .sdd/trust/freetsa-bundle.pem \
    --head-signing-public-jwk .sdd/keys/lineage.pub.jwk
```

The verifier runs up to seven independent checks (the last two are
opt-in based on which trust material the caller supplies):

1. **Envelope structure** - required fields, schema version, ISO-8601
   ordering of `audit_window`.
2. **Tenant purity** - every event in the slice carries the declared
   `tenant_id`.
3. **Chain integrity** - re-derive each event's HMAC; confirm
   `prev_hmac` linkage; confirm `chain_anchor.head_hmac` equals the
   recomputed tail.
4. **Anchor consistency** - recompute `head_sha256` from canonical
   JSONL bytes and compare.
5. **Signature block sanity** - base64 validity for RFC 3161 tokens;
   `sha256(head_sha256 || anchored_at)` for offline anchors.
6. **(opt) RFC 3161 cryptographic chain** - walks the embedded TSA cert
   chain against `rfc3161_trusted_tsa_certs`, verifies the CMS signer's
   signature over `SignedAttributes`, and confirms
   `TSTInfo.messageImprint == sha256(head_sha256 bytes)`. Skipped with a
   log warning when no trust bundle is supplied.
7. **(opt) Head signature** - verifies the Ed25519 signature over
   `bytes.fromhex(head_sha256)` against the embedded JWK. When
   `head_signature_trusted_jwk` is supplied, the embedded JWK must
   match (key pinning).

A failure on any active check flips `result.ok` to `False` and appends a
human-readable message to `result.errors`.

## Trust bundle for RFC 3161

The verifier needs operator-supplied TSA roots - we deliberately do
**not** honour OS trust stores. Build a bundle once per TSA you trust:

```bash
# FreeTSA example - root + leaf glued together so both are anchors.
curl -sS https://freetsa.org/files/cacert.pem -o trust/freetsa.pem
```

Pass the resulting file to `--rfc3161-trusted-tsa-bundle`. The verifier
walks the embedded cert chain inside the token against the bundle using
`cryptography.x509.verification.PolicyBuilder`. The CA policy requires
`basicConstraints` to be present (criticality is agnostic - many real
TSAs ship non-critical constraints); the EE policy is permissive
(TSA leaf certs do not carry CABF SAN constraints). The
`id-kp-timeStamping` extended-key-usage bit is surfaced on the result so
operators can enforce the policy themselves.

## Compliance mapping (one-line)

- **EU AI Act Art. 12** - covered by `bernstein audit export
  --article-12 ...` (see `docs/security/AUDIT.md`); the multi-tenant
  export complements it for slicing per-customer.
- **DORA Art. 9 / Art. 28** - the slice + RFC 3161 token + `head_signature`
  is now end-to-end-verifiable evidence for third-party register sharing.
- **SR 11-7** - the chain + sha256 anchor + Ed25519 head signature is the
  model audit trail; the auditor verifies authenticity without holding
  the operator's HMAC key.
- **ISO 27001:2022 A.12.4** - the head signature lets the audit log
  artefact be archived independently of the orchestrator runtime.

## References

- W3C Verifiable Credentials Data Model 2.0
  (https://www.w3.org/TR/vc-data-model-2.0). Conceptually similar
  proof-on-claim split. Rejected as the primary wire format because VC
  v2 is RDF/JSON-LD-shaped and forces context resolution at verify
  time. Migration path remains open.
- RFC 3161 - Time-Stamp Protocol
  (https://www.rfc-editor.org/rfc/rfc3161). v2 verifier walks the chain
  end-to-end via `bernstein.core.security.rfc3161_verifier`.
- RFC 5652 - Cryptographic Message Syntax (CMS); the TimeStampToken is
  a CMS `SignedData`.
- RFC 8032 - Ed25519 deterministic signatures.
- RFC 8037 - JOSE OKP curves; the `head_signature.public_key_jwk` block
  uses this encoding.
- IETF SCITT - Supply Chain Integrity, Transparency, and Trust
  (https://datatracker.ietf.org/wg/scitt). The Ed25519 head signature
  drops cleanly into a SCITT envelope when the WG locks v1.
