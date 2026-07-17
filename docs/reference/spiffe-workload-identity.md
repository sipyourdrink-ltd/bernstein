# SPIFFE-compatible workload identity

Bernstein workloads already hold an Ed25519 install identity (the
orchestrator's agent-card signing key) and per-agent signed identity cards.
Infrastructure teams standardizing on [SPIFFE](https://spiffe.io/) workload
identity can now consume both: the install identity and each card map onto a
deterministic SPIFFE ID, and a [SPIRE](https://spiffe.io/docs/latest/spire-about/)
agent's X.509-SVID can be bound to a card by a receipt anchored in the HMAC
audit chain.

This is an optional integration profile. The self-contained Ed25519 path stays
the default; SPIRE support lives behind the `spiffe` extra, and every existing
flow works unchanged with the extra absent.

## SPIFFE ID scheme (deterministic)

```
spiffe://<trust-domain>/bernstein/<install>/<agent>
```

| Segment | Source | Notes |
|---|---|---|
| `<trust-domain>` | operator input | Validated: lowercase DNS-like `[a-z0-9._-]{1,255}`, no scheme or path. |
| `<install>` | install public key | First 16 hex chars of `SHA-256(install_public_key_pem)`. Stable per install. |
| `<agent>` | agent card id | Validated as a SPIFFE path segment (`[a-zA-Z0-9._-]`, not `.`/`..`). |

The derivation is a pure function. Two operators deriving the id for the same
install and agent obtain the same string, and a verifier re-derives it later to
check a card-to-SVID binding. Derive it offline:

```bash
bernstein spiffe id \
    --install-key .bernstein/keys/agent-card.ed25519.pub \
    --agent backend-1 --trust-domain example.org
# -> spiffe://example.org/bernstein/<install>/backend-1
```

## The card-to-SVID binding is the receipt

When a SPIRE agent issues an X.509-SVID for a workload, the mapping between the
platform identity (the SVID) and the card identity (the agent card) must be
verifiable after the fact. `bind_svid_to_card`:

1. Re-derives the SPIFFE ID deterministically from the install public key and
   the card's agent id.
2. Refuses any SVID whose id disagrees (`BindingError`).
3. Stamps the SPIFFE ID onto the card (`svid_reference`).
4. Appends a `spiffe.svid_binding` event into the HMAC-chained audit log,
   pinning the binding's content hash, the derived SPIFFE ID, the install
   fingerprint, the card hash, and the leaf SVID's content address
   (`sha256:<hex>` over the leaf DER) -- never the SVID private key.

The binding's identity **is** its content hash, and that hash is chained. Strip
the audit chain and the binding loses its meaning, not just its log. Verify it
offline:

```bash
bernstein spiffe verify-binding binding.json \
    --install-key .bernstein/keys/agent-card.ed25519.pub \
    --trust-domain example.org --audit-dir .sdd/audit
# -> valid (chain-anchored)
```

`verify-binding` re-derives the SPIFFE ID from the install key and, with
`--audit-dir`, recomputes the binding content hash and matches it against the
chained `spiffe.svid_binding` receipt. A post-hoc tamper -- a swapped SPIFFE
ID, install fingerprint, card hash, or SVID reference -- fails because the
recomputed hash no longer matches the receipt.

## mTLS on the task server

An `X509Svid` projects onto the existing cluster
[`TLSConfig`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/core/protocols/cluster/cluster_tls.py), so
SPIRE SVIDs drive the same mutual-TLS enforcement the task server already uses
(uvicorn `--ssl-*`). `svid_tls_config` writes the SVID to disk with an
owner-only (`0o600`) private key and returns a ready `TLSConfig` whose
`verify_mode="required"` yields an `ssl.CERT_REQUIRED` context.

## Example SPIRE configuration

Register a Bernstein workload so the SPIRE agent issues it an SVID whose id
matches the derived scheme. Compute `<install>` with `bernstein spiffe id`
first, then register the entry (selectors are illustrative -- use whatever your
node attestation supports):

```bash
# 1. Derive the SPIFFE ID Bernstein will expect for this agent.
SPIFFE_ID=$(bernstein spiffe id \
    --install-key .bernstein/keys/agent-card.ed25519.pub \
    --agent backend-1 --trust-domain example.org)

# 2. Register the workload with SPIRE (unix uid selector shown).
spire-server entry create \
    -spiffeID "$SPIFFE_ID" \
    -parentID spiffe://example.org/spire/agent/x509pop/<node> \
    -selector unix:uid:$(id -u)

# 3. Point Bernstein at the agent's Workload API socket and install the extra.
pip install 'bernstein[spiffe]'
export SPIFFE_ENDPOINT_SOCKET=unix:///run/spire/sockets/agent.sock
```

With the socket reachable, `bernstein.core.identity.spiffe.workload_api`
fetches the SVID, `bind_svid_to_card` binds it to the card and records the
receipt, and `svid_tls_config` wires the SVID into the task server's mTLS.

## Scoped credential grants carry the SPIFFE ID

When the secrets broker runs in grant-enforcing mode with
`grants.identity_mode: spiffe`, `bernstein.core.identity.spiffe.grant_identity`
resolves the workload SPIFFE ID (from the fetched SVID) as the issuer identity
on each scoped credential grant. The grant record's issuer is then the same
`spiffe://` id checkable via `bernstein spiffe verify-binding`, so a
per-task downstream credential is bound to the workload identity, not just to
the install-scoped manager key. `bernstein doctor` preflights the extra and the
Workload API socket; with the extra absent the grant issuer falls back to the
default Ed25519 manager identity and the regression suite is unchanged. See
[the secrets broker guide](../security/secrets-broker.md#scoped-per-task-grants)
for the full grant lifecycle.

## Threat model notes

- **Determinism is the trust root.** The SPIFFE ID is a pure function of the
  install public key and the agent id. An attacker cannot make a card claim a
  different install's SPIFFE ID without holding that install's public key at
  derivation time; a verifier catches the mismatch by re-deriving.
- **The receipt binds identity to identity.** The `spiffe.svid_binding` event
  ties the platform identity (SVID leaf content address) to the card identity
  (card hash) under one content hash. Editing either side after the fact breaks
  the hash match, and the HMAC chain independently detects any edit to the
  event itself.
- **No private key ever leaves the boundary.** The SVID private key is written
  owner-only and is never logged, never projected into a card's SVID reference,
  and never recorded in the audit chain. Errors on the fetch path are logged by
  exception type only.
- **Trust domain is operator-scoped.** Bernstein validates the trust domain and
  namespaces every workload under the reserved `bernstein` path segment, so a
  Bernstein-minted id cannot collide with an operator's other SPIRE
  registrations.
- **Upstream spec churn is contained.** The mapping module is isolated from the
  SPIRE transport; if the agent-workload-identity spec shifts, only the
  derivation and reference-projection functions change, not the audit-chain
  receipt or the mTLS wiring.
- **Optional by construction.** With the `spiffe` extra absent there is no SPIRE
  code path and no new dependency; the regression suite is green because the
  Ed25519 identity path is untouched.
