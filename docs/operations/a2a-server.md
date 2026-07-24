# A2A JSON-RPC server surface

Bernstein already speaks [A2A v1.0](https://a2a-protocol.org/) as a client, an
emitter, and a federation peer. This surface makes one instance a **callable,
discoverable node** other agents and apps can delegate work *into*: a signed
agent card is its identity, a JSON-RPC 2.0 binding is how it is called, and
every completed task comes back with a lineage receipt the caller verifies
offline.

It is **off by default.** Nothing below is reachable, and the agent card
advertises none of it, until you set `BERNSTEIN_A2A_SERVER_ENABLED=1`.

## What it gives you

| Capability | Detail |
|---|---|
| Discovery at both well-known paths | The signed card is served identically at `/.well-known/agent-card.json` (A2A v1.0 canonical) and `/.well-known/agent.json` (legacy), so a client built for either name resolves it. |
| JSON-RPC 2.0 binding | One endpoint, `POST /a2a/v1`, dispatches `message/send`, `tasks/get`, and `message/stream` (SSE). |
| Degrade-gracefully polling | A client with no streaming and no artifact support still reaches every result by polling `tasks/get`. |
| Card-declared auth | Two schemes, both in the card: a static **API key** and an **OAuth2 client-credentials** grant. |
| Verifiable answers | A completed task returns an `Artifact` whose parts carry the result text **and** a lineage-receipt reference, so the caller proves the answer offline with `bernstein a2a verify`. |
| Audited callers | The authenticated caller is anchored in the audit chain on every accepted task. |

## Enable it

```bash
export BERNSTEIN_A2A_SERVER_ENABLED=1

# Static API keys: caller_id=key, comma-separated.
export BERNSTEIN_A2A_API_KEYS="partner-a=<key-a>,partner-b=<key-b>"

# OAuth2 client-credentials clients: client_id=client_secret, comma-separated.
export BERNSTEIN_A2A_OAUTH_CLIENTS="app-x=<secret-x>"

# Optional: pin the token-signing secret (else one is derived from the client
# set so a restart keeps validating previously issued tokens).
export BERNSTEIN_A2A_OAUTH_SIGNING_SECRET="<random-secret>"

# Optional: the public base URL advertised in the card.
export BERNSTEIN_PUBLIC_BASE_URL="https://node.example"
```

The JSON-RPC endpoints run their own auth, so they are exempt from the server's
bearer middleware — a call with no A2A credentials is still rejected per spec.

## Discovery and offline card verification

```bash
curl -s https://node.example/.well-known/agent-card.json
```

The card is JCS-canonical (RFC 8785) with a detached JWS (RFC 7515) whose key
is published at `/.well-known/agent.json/keys`. When the surface is on, the card
also carries:

- `supportedInterfaces` including `"JSONRPC"` and an `additionalInterfaces`
  entry pointing at `/a2a/v1`; and
- `securitySchemes` for `a2a-api-key` (apiKey, header `X-API-Key`) and
  `a2a-oauth2` (oauth2 clientCredentials, with the token URL).

The embedded `capabilityCard` verifies offline with
`verify_capability_card()`; a byte-tampered card fails.

## Authentication

### API key

```
POST /a2a/v1
X-API-Key: <key-a>
```

### OAuth2 client-credentials

```bash
curl -s -X POST https://node.example/a2a/v1/oauth/token \
  -d grant_type=client_credentials \
  -d client_id=app-x -d client_secret=<secret-x>
# -> {"access_token": "...", "token_type": "Bearer", "expires_in": 3600}
```

```
POST /a2a/v1
Authorization: Bearer <access_token>
```

Rejections follow the spec: a missing or invalid credential gets `401` with an
RFC 6750 `WWW-Authenticate` challenge; a bad client secret at the token
endpoint gets an OAuth2 §5.2 `invalid_client` body. Tokens are stateless
(self-describing, HMAC-tagged), so they survive a restart and are validated
offline on every call.

## Calling the node

`message/send` — submit work; returns an A2A `Task`:

```json
{"jsonrpc":"2.0","id":1,"method":"message/send",
 "params":{"message":{"role":"user","parts":[{"kind":"text","text":"review the auth module"}],"messageId":"m1"}}}
```

`tasks/get` — poll to completion:

```json
{"jsonrpc":"2.0","id":2,"method":"tasks/get","params":{"id":"<taskId>"}}
```

When the task completes, the result carries an artifact:

```json
{"artifacts":[{"artifactId":"<taskId>-result","name":"result",
  "parts":[
    {"kind":"text","text":"<result summary>"},
    {"kind":"data","data":{
       "attested":{"taskId":"<taskId>","result":"<result summary>"},
       "lineageReceipt":{"entry_hash":"...","content_hash":"sha256:...","operator_hmac":"...","head_signature":{...},"kid":"..."}
    }}
  ]}]}
```

`message/stream` — the same accepted task and a status snapshot over SSE
(`text/event-stream`); terminal results still come from `tasks/get`.

## Task-state mapping

| A2A state | Bernstein status |
|---|---|
| `submitted` | `open` / `planned` |
| `working` | `claimed` / `in_progress` / `waiting_for_subtasks` |
| `input-required` | `blocked` (needs more from the caller) |
| `auth-required` | `blocked` (needs downstream credentials) |
| `completed` | `done` / `closed` |
| `failed` | `failed` / `refused` / `abandoned` |
| `canceled` | `cancelled` |

## Verify an answer offline

The `data` part carries the receipt and the exact bytes it attests. Extract
them and run the shipped verifier — no access to the node required:

```bash
bernstein a2a verify --receipt receipt.json --response attested.json
```

- `attested.json` — the `parts[].data.attested` object.
- `receipt.json` — the `parts[].data.lineageReceipt` object.

Verification recomputes the content hash and checks the head signature over the
binding digest against the audit chain head. Tamper one byte of the answer and
it is rejected; strip the receipt and the answer is *unverifiable*, not merely
unlogged. Pass `--trusted-jwk` to pin the signing key and establish provenance
against the operator's published key rather than trust-on-first-use.

## Publish for discovery

`bernstein a2a publish --endpoint https://node.example/a2a` projects the node's
signed capability card into agent-registry manifests, each carrying a
verifiable publisher fingerprint, so peers discover the node by verifiable
capability rather than by an opaque URL.

Three surfaces are supported; pass `--surface` (repeatable) to select:

| Surface | Record shape | Trust root |
| --- | --- | --- |
| `a2a-card` | The signed capability card (JWS per RFC 7515 over RFC 8785 bytes). | The card's own Ed25519 key. |
| `mcp-registry` | A `server.json` carrying the `ed25519/<fp>` publisher block the MCP verifier already parses. | The card's own Ed25519 key. |
| `agntcy-ads` | An OASF capability descriptor with Sigstore provenance. | A distinct provenance key (Sigstore, or a local Ed25519 fallback). |

The default publish emits `a2a-card` and `mcp-registry`. The AGNTCY ADS surface
is opt-in — it signs its OASF descriptor with a provenance key distinct from the
card key — so request it explicitly:

```bash
bernstein a2a publish --endpoint https://node.example/a2a --surface agntcy-ads
```

The OASF descriptor is a deterministic projection of the signed card, pinned to
a stated OASF schema version: `advertised_tools` map onto OASF skills and the
card's policy block (cost cap, redaction tier, sandbox profile) maps onto a
`bernstein.policy` extension. The record carries a Sigstore provenance
attestation over the descriptor; when the `sigstore` package or network is
unavailable it falls back to a local Ed25519 signature, recorded as the
provenance `trust_root`. `verify_publication_record()` verifies an ADS record
offline — it rebuilds the descriptor from the embedded card, rejects any byte
that does not match the deterministic projection, and checks the provenance
signature — so a tampered descriptor is rejected without contacting the node.
The provenance key is minted once and reused (persisted beside the card), so
republishing an unchanged node rewrites byte-identical descriptor bytes.

## Related

- [Signed A2A message receipts](a2a-message-receipts.md)
- [A2A interop (peer cards)](../interop/a2a.md)
- [A2A architecture](../architecture/a2a.md)
