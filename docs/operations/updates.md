# Updates: check, verify, apply

`bernstein self` is the update lifecycle. It is built around one rule: a
version is a claim until it is verified, so nothing here recommends or
installs a release whose provenance has not been checked first.

Three properties follow from that rule, and each is enforced below the CLI so
a different caller cannot skip it:

| Property | What it means in practice |
|---|---|
| Offline-first, opt-in | No network call for update purposes unless you ran a command or set `BERNSTEIN_UPDATE_CHECK=1`. |
| Air-gap hard off | Under `--profile airgap` / `--profile sovereign` the remote path is refused, and the fetcher re-checks the egress policy immediately before opening a socket. |
| Verified before install | The wheel on disk is re-hashed against the provenance-verified advisory before `pip` is invoked. A mismatch aborts with nothing installed. |

## Quick start

```bash
# One-time: install the release trust root (the project's signing identity)
cp release-trust-root.pem ~/.bernstein/release-trust-root.pem

# Point at the signed release feed (a mirrored file, or the published URL)
export BERNSTEIN_RELEASE_FEED=/srv/mirror/release-feed.json

bernstein self check-update          # verify the feed, seal an advisory
bernstein self update                # refuse mid-run, verify the wheel, install
bernstein self pin 3.9.0             # standardise on a version
bernstein self rollback              # return to the previous receipted version
```

## Commands

| Command | What it does |
|---|---|
| `bernstein self check-update` | Verifies the signed release feed offline against the trust root, classifies the gap by surface, seals a signed `update.advisory` and anchors it in the audit chain. |
| `bernstein self check-update --cached` | Prints the last advisory. Never touches the network. |
| `bernstein self check-update --verify <file>` | Recomputes a sealed advisory offline. No network, no state written. |
| `bernstein self update` | Installs the verified candidate. Refuses while a run is active; verifies the wheel hash before pip; emits an install receipt. |
| `bernstein self pin <version>` | Writes a signed version pin the updater will not cross. |
| `bernstein self unpin` | Removes the pin. |
| `bernstein self rollback` | Returns to the previous receipted version, its wheel re-verified against the cached signed feed. |
| `bernstein self-update` | Compatibility alias. `--check`, `--rollback`, `-y` map onto the commands above. |

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `BERNSTEIN_RELEASE_FEED` | unset | Signed release feed: a local mirrored file path, or an `https://` URL. Required. |
| `BERNSTEIN_RELEASE_TRUST_ROOT` | `~/.bernstein/release-trust-root.pem` | SPKI PEM of the release signing identity. Without it, every check **fails closed**. |
| `BERNSTEIN_UPDATE_CHECK` | unset | Set to `1` to let a non-interactive path make a remote check. Unset means zero network calls. |
| `--require-attestation` | off | Also require the Sigstore build-provenance attestation to verify (needs `gh` on PATH). |
| `--override-pin` | off | Cross an installed version pin. |

Trust-root resolution order: `--trust-root` → `$BERNSTEIN_RELEASE_TRUST_ROOT`
→ `~/.bernstein/release-trust-root.pem`. If none is found the command refuses
rather than trusting the feed it just downloaded.

## The lifecycle

### 1. Check

`bernstein self check-update` loads the release feed, then:

1. **Anchors the key.** The feed carries its signing public key; that key must
   equal your configured trust root. A feed that verifies only against the key
   it carries proves self-consistency, not authorship — anyone who can rewrite
   the feed can also mint a fresh keypair for it.
2. **Verifies the content hash** of the canonical feed body.
3. **Verifies the detached Ed25519 JWS** over those exact bytes.

Any failure means **no candidate is produced**. A tampered feed does not
surface a release with a warning; it surfaces nothing.

Only then is a candidate selected and the gap classified.

### 2. Surface delta

Each release entry in the feed carries a signed `surface` tag:

| Tag | Meaning |
|---|---|
| `verification` | The release changed the audit chain, lineage, or replay format. |
| `security` | The release carried a security fix. |
| `feature` | Ordinary feature or fix release. |

The advisory reports counts per surface and the highest surface in the gap, so
you see "two releases behind, highest surface: verification" rather than a bare
version number. The classification reads the signed field — it never parses
release-note prose.

### 3. The advisory is a receipt

The advisory is a canonical document binding:

```
installed_version, candidate_version, candidate_wheel_sha256,
provenance_verified, provenance_source, trust_root_fingerprint,
surface_delta, feed_sha256, checked_at, checked_at_chain_anchor,
pinned_version, offline_profile
```

It is sealed with a detached Ed25519 JWS over its JCS-canonical bytes using the
install identity, cached at `~/.bernstein/update-advisory.json`, and mirrored
into the HMAC audit chain as an `update.advisory` event.

Re-check one at any time, offline:

```bash
bernstein self check-update --verify ~/.bernstein/update-advisory.json
```

Verification requires **all** of: a matching content hash, a valid signature,
and a non-empty chain anchor. Strip the signature or blank the anchor and it
fails — what is left is a version diff, which is not what this command
produces.

### 4. Apply

`bernstein self update`:

1. **Refuses while work is in flight.** A deterministic orchestrator must not
   replace its own coordination code under a live workflow: the journal would
   record one version and the remaining steps would run under another. Three
   signals are consulted — a live detached-run supervisor, a run whose ledger
   projection still has in-flight or scheduled tasks, and a live
   server / spawner / watchdog pid. If the run store cannot be read, the
   command fails closed rather than updating blind.
2. **Honours the pin.** A signed pin caps the candidate; crossing it needs
   `--override-pin`.
3. **Downloads without installing** (`pip download --no-deps`), so there is an
   artefact to verify.
4. **Re-hashes the wheel** and compares it to the advisory's
   provenance-verified hash. A mismatch aborts — pip is never invoked.
5. **Re-checks the Sigstore attestation** the release pipeline emits. It can
   only escalate a failure, never mask the hash check. With
   `--require-attestation`, a skipped attestation is also a refusal.
6. **Installs the verified local wheel** and emits a `self.update` receipt into
   the audit chain, stored content-addressed under
   `~/.bernstein/update-receipts/`.

### 5. Pin and roll back

```bash
bernstein self pin 3.9.0 --reason "validated against our compliance suite"
bernstein self update            # will not move past 3.9.0
bernstein self update --override-pin
bernstein self unpin
```

`bernstein self rollback` takes its target from the **receipted** install
history rather than a plaintext breadcrumb, and resolves that target's wheel
hash from the cached signed feed — re-verified against the trust root first.
Rollback therefore needs no network access and is provenance-checked exactly
like a forward install. If the cached feed carries no entry for the target,
the command refuses rather than installing an unverified wheel.

## Air-gap operation

Under `--profile airgap` (or `--profile sovereign`, which composes it):

- `resolve_check_permission` refuses the remote path regardless of
  `BERNSTEIN_UPDATE_CHECK`; the declared posture outranks the environment.
- `fetch_release_feed` re-checks the live `NetworkPolicy` immediately before
  opening a socket, so a deny-all posture blocks the request even if a caller
  reaches the fetcher directly.
- Point `BERNSTEIN_RELEASE_FEED` at a mirrored feed file. Reading a file is not
  egress, so the whole check — verify, classify, seal, anchor — runs offline,
  and the advisory records `offline_profile: true`.

## Release feed format

```json
{
  "feed": {
    "schema_version": 1,
    "kind": "bernstein.release_feed",
    "package": "bernstein",
    "generated_at": "2026-07-01T00:00:00Z",
    "releases": [
      {
        "version": "3.10.0",
        "wheel_name": "bernstein-3.10.0-py3-none-any.whl",
        "wheel_sha256": "<64 lowercase hex>",
        "surface": "verification",
        "released_at": "2026-06-20T00:00:00Z",
        "yanked": false
      }
    ]
  },
  "feed_sha256": "<sha256 of the canonical feed body>",
  "signature": "<detached JWS, typ=bernstein-release-feed+jws>",
  "public_key": "-----BEGIN PUBLIC KEY-----…"
}
```

The body is canonicalised with JCS (RFC 8785), so a feed generated twice from
the same inputs is byte-identical and both parties can compare it by hash. A
malformed entry — an unknown `surface`, a `wheel_sha256` that is not 64 hex
characters, an unsupported `schema_version` — is refused during parsing,
before any signature work.

Build one with
`bernstein.core.distribution.update_advisory.build_release_feed_document`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `no release trust root installed` | No PEM found in any of the three locations. | Install the signing identity, or pass `--trust-root`. |
| `no release feed configured` | `BERNSTEIN_RELEASE_FEED` unset and no `--feed`. | Set the variable or pass `--feed`. |
| `release feed signing key does not match the configured trust root` | The feed was signed by a key you do not trust. | Do not proceed. Confirm which identity is meant to sign releases. |
| `release feed content hash does not match its body` | The feed was altered in transit or at rest. | Re-fetch from a trusted mirror. |
| `air-gap/sovereign profile active` | Remote check attempted under an offline posture. | Point `--feed` at a mirrored file. |
| `wheel hash mismatch` | The downloaded wheel is not the artefact the signed feed names. | Nothing was installed. Investigate the index you are pulling from. |
| `Refusing to self-update: work is in flight` | A run or supervisor is live. | Finish or stop the run, then retry. |
| `version pin X blocks Y` | A signed pin caps the candidate. | `bernstein self unpin`, or `--override-pin`. |
| `no receipted predecessor to roll back to` | No install receipt exists yet. | Rollback becomes available after the first receipted install. |

## Source

- `src/bernstein/core/distribution/update_advisory.py` — feed verification,
  advisory sealing and offline verification, surface classification, gating,
  pin, receipts.
- `src/bernstein/cli/commands/self_update_cmd.py` — the `bernstein self`
  command group and the `self-update` compatibility alias.
- `src/bernstein/core/security/audit_chain.py` — `update.advisory` and
  `self.update` events.
