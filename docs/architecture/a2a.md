# A2A v1.0 - signed agent cards

Bernstein's task server publishes an A2A v1.0 agent card at
`/.well-known/agent.json` and the public verification keys at
`/.well-known/agent.json/keys`. Programmatic peers (Claude Code, Codex,
third-party orchestrators) can discover the orchestrator's wire formats,
auth schemes, and endpoint inventory, then verify the card was minted by
the operator's own keystore before they trust it.

If you only have time for one sentence: **the card body is JCS-canonical
JSON (RFC 8785), signed with a per-installation Ed25519 key (RFC 8037) as
a detached JWS (RFC 7515), and verifiable from the JWKS at the same
prefix**. Everything else on this page is detail.

The Discord, Slack, and Telegram bot stubs are unrelated. A2A v1.0
applies to programmatic agent peers, not the chat platforms.

---

## What an A2A v1.0 agent card is

A2A v1.0 is a wire contract for one autonomous agent describing itself
to another. The card is a JSON document published at a stable
`.well-known` path; a verifying peer reads it, checks the embedded
signatures against a JWKS, and uses the result to drive its own client.

Bernstein's card carries the v1.0-mandated fields:

| Field | Meaning |
|---|---|
| `name`, `description`, `version` | Server identity and current Bernstein version. |
| `protocolVersion` | Constant `"1.0"`. |
| `url`, `documentationUrl` | Public base URL and human-readable docs. |
| `supportedInterfaces[]` | Wire formats this server speaks. Today only `HTTP+JSON`. |
| `securitySchemes[]` | Bearer JWT (active) plus an `mtls` stub for forward-compat. |
| `capabilities[]` | High-level coarse capabilities (`task-crud`, `bulletin`, `status`). |
| `skills[]` | Finer-grained capability index. |
| `authentication` | Bearer scheme, list of public paths that skip auth. |
| `endpoints[]` | Method + path + summary for every documented route. |
| `signatures[]` | One or more detached JWS objects over the JCS-canonical body. |

Source: `_agent_card_body` in
`src/bernstein/core/routes/well_known.py`.

---

## How the card is signed

The signing flow is three steps:

1. Build the body dict from the in-module `_ENDPOINTS` table.
2. JCS-canonicalise it (RFC 8785) to produce a deterministic byte string.
3. Sign the canonical bytes with the persistent Ed25519 keystore key,
   then attach the resulting detached JWS object to a `signatures[]`
   array on the body.

The detached JWS shape follows RFC 7515 §A.5: the compact form is
`base64url(header)..base64url(signature)` - the payload segment is
empty, so verifiers reconstruct it themselves from the body bytes
they were served. The protected header carries `alg: EdDSA`,
`typ: agent-card+jws`, and the `kid` matching the JWKS entry that
issued the signature.

```
                ┌─────────────────────┐
       body ──▶ │  canonicalize_jcs   │ ──▶  canonical bytes
                └─────────────────────┘             │
                                                    ▼
                                          ┌──────────────────┐
                                Ed25519 ──│  detached JWS    │──▶ signature
                                  key     └──────────────────┘
                                                    │
              body + signatures[] = published payload (also JCS bytes on the wire)
```

The card is served back to the client also as JCS bytes so verifiers
can recompute the JWS signing input bit-perfect after stripping the
`signatures` field. Source: `_agent_card_payload`,
`_sign_canonical_body`, and the route handler `agent_json` in
`src/bernstein/core/routes/well_known.py`. The reusable signer
lives in `src/bernstein/core/security/agent_card_signer.py`.

### Why detached

Embedding the body inside the JWS payload would force every verifier to
parse the JWS twice - once to recover the bytes, once to verify them.
Detached signatures over the JCS-canonical body let the verifier work
directly off the JSON it already had to parse, with no round-trip
through base64url. The `card_hash` field on the underlying
`AgentIdentityCard` keeps its existing internal semantics; the JWS is
the externally-verifiable layer.

### Algorithm choice

Ed25519 over a 32-byte SPKI public key keeps the JWKS document small
(one JWK is well under a kilobyte) and the verification cost low. RFC
8037 fixes the JWS algorithm name to `EdDSA` and the JWK key type to
`OKP` with curve `Ed25519`, so any RFC 8037-compliant verifier reads
the keys without bespoke parsing.

---

## How a verifier consumes the card

```
GET /.well-known/agent.json           → card body + signatures[]
GET /.well-known/agent.json/keys      → JWKS { "keys": [<jwk>, ...] }
```

Verifier loop:

1. Parse the body. Pull `signatures[]` aside.
2. Re-serialise the rest with JCS to recover the canonical bytes.
3. For each signature: look up the public key in the JWKS by `kid`.
4. Run RFC 8037 EdDSA verification on
   `base64url(header) || "." || base64url(canonical_body)`.
5. Accept the card iff at least one signature verifies under a JWK
   currently published in the JWKS.

The JWKS publishes both the current key and any archived key still
inside the rotation grace window (24h by default). A verifier whose
HTTP cache has the previous JWKS continues to validate signatures from
the previous `kid` until its `Cache-Control: max-age=3600` ages out.

For the keystore, rotation, and on-disk semantics, see
[persistent agent-card keystore](../security/keystore.md).

---

## Audience binding

Tokens minted for the orchestrator carry an RFC 8707 resource
indicator. Bearer tokens that pre-date RFC 8707 keep validating; tokens
that present a `resource` claim must list the orchestrator's configured
URI (or include it in their array). Mismatched audiences are rejected
with the RFC 8707 §3 challenge:

```
WWW-Authenticate: Bearer error="invalid_token",
                  error_description="resource indicator mismatch"
```

This blocks an SSO token minted for some other audience from being
replayed against Bernstein. Source: `_resource_indicator_check` in
`src/bernstein/core/security/auth_middleware.py`.

The configured indicator comes from `BERNSTEIN_RESOURCE_INDICATOR`
(comma-separated allowlist) or the `auth.resource_indicators` key in
`bernstein.yaml`.

---

## Cold-start safety

The first `/.well-known/agent.json` request after a fresh boot triggers
two things at once: it lazily binds the keystore to its on-disk
directory, and it loads the cached PEM bytes for the signing key.
Both helpers (`_get_keystore` and `_get_signing_keypair`) take the
same `_KEY_LOCK`, so the lock has to be reentrant - a plain
`threading.Lock` self-deadlocks on the cold path because the outer
holder calls into the inner helper which tries to re-acquire it.

Bernstein uses `threading.RLock` so the nested acquire succeeds. Source:
`_KEY_LOCK = threading.RLock()` in
`src/bernstein/core/routes/well_known.py`.

---

## Endpoints summary

| Path | Auth | Purpose |
|---|---|---|
| `GET /.well-known/agent.json` | none | A2A v1.0 signed agent card. |
| `GET /.well-known/agent.json/keys` | none | JWKS for verifying the card signatures. |
| `GET /llms.txt` | none | Markdown rendering of the same surface for LLM consumers. |

All three live in `AUTH_PUBLIC_PATHS` so any network caller can read
them without provisioning a token. They expose only the public surface;
no task data, no secrets.

For the full index of `.well-known` discovery paths (agent card, JWKS,
`llms.txt`, and the HTTP Message Signatures directory), see
[the `.well-known` service catalog](../protocols/well-known-manifest.md).

---

## Configuration

| Knob | Default | Purpose |
|---|---|---|
| `BERNSTEIN_PUBLIC_BASE_URL` | `http://127.0.0.1:8052` | URL advertised in the card body. |
| `BERNSTEIN_AGENT_CARD_KEY_DIR` | `.bernstein/keys` | On-disk directory backing the keystore. |
| `BERNSTEIN_RESOURCE_INDICATOR` | unset | RFC 8707 audience(s) the orchestrator accepts. |
| `BERNSTEIN_AUTH_DISABLED` | `0` | Local-dev opt-out for bearer auth on protected paths. |

Operators rotate the signing key with
`AgentCardKeystore.rotate()` (or `rotate_agent_card_keys()` from the
route module) when they want to roll the JWKS. The previous key stays
in the JWKS for the grace window so peers verify in flight without a
race.

---

## Limitations

- One signing key per installation. Multi-tenant signing (per-tenant
  `kid`) is not modelled here; tenant isolation lives at the audit-log
  layer.
- JCS canonicalisation in
  `src/bernstein/core/security/agent_card_signer.py` covers the value
  shapes the card produces (strings, bools, ints, floats, lists, dicts)
  but does not implement the full RFC 8785 numeric-edge-case rules.
  Cards do not emit `NaN`, `±Infinity`, or integers past 2^53 today.
- Multi-process deployments share the keystore directory and rely on
  `O_EXCL` on first-run generation. Pre-provision the keypair if you
  want to skip the race.
- `mtls` is published in `securitySchemes[]` as a forward-compat stub.
  Client-cert verification at the middleware layer is not active yet.

---

## Related

- Persistent keystore semantics: [security/keystore.md](../security/keystore.md).
- `.well-known` service catalog (index of discovery paths):
  [protocols/well-known-manifest.md](../protocols/well-known-manifest.md).
- Operator security runbook:
  [operations/security-and-identity.md](../operations/security-and-identity.md).
- Source:
  - `src/bernstein/core/routes/well_known.py` - routes, body builder, JWS attach.
  - `src/bernstein/core/security/agent_card_signer.py` - JCS, JWS, keypair primitives.
  - `src/bernstein/core/security/agent_card_keystore.py` - file-backed keystore.
  - `src/bernstein/core/security/auth_middleware.py` - RFC 8707 audience binding.
