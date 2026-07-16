# `.well-known` service catalog

Bernstein's task server publishes a small set of unauthenticated discovery endpoints so external agents and scanners can find the orchestrator's public surface without hand configuration. This page is the index; the agent-card contract itself is documented in [A2A v1.0 - signed agent cards](../architecture/a2a.md).

| Path | Purpose | Reference |
|---|---|---|
| `GET /.well-known/agent.json` | A2A v1.0 signed agent card (JCS-canonical body, detached Ed25519 JWS). | [a2a.md](../architecture/a2a.md) |
| `GET /.well-known/agent.json/keys` | JWKS for verifying the agent-card signatures. | [a2a.md](../architecture/a2a.md#how-a-verifier-consumes-the-card) |
| `GET /llms.txt` | Markdown summary of the same public surface for LLM consumers. | [a2a.md](../architecture/a2a.md#endpoints-summary) |
| `GET /.well-known/http-message-signatures-directory` | JWKS for verifying the RFC 9421 HTTP Message Signatures on Bernstein's outbound agent-facing requests (install-identity keypair). | `src/bernstein/core/routes/well_known.py` |
| `.well-known/security.txt` | Security contact and disclosure policy (RFC 9116), served from the repo/site root. | [SECURITY.md](../../SECURITY.md) |

All task-server endpoints above are unauthenticated and listed in the auth-middleware public-path allowlist; they expose only the public surface (no task data, no secrets).

## How to use it

```bash
curl http://127.0.0.1:8052/.well-known/agent.json
curl http://127.0.0.1:8052/.well-known/agent.json/keys
curl http://127.0.0.1:8052/llms.txt
```

The agent card is A2A v1.0: a JCS-canonical (RFC 8785) JSON body signed with a per-installation Ed25519 key (RFC 8037) as a detached JWS (RFC 7515), verifiable from the JWKS at the same prefix. For the card fields, signing flow, verifier loop, key rotation, and configuration knobs (`BERNSTEIN_PUBLIC_BASE_URL`, `BERNSTEIN_AGENT_CARD_KEY_DIR`, `BERNSTEIN_RESOURCE_INDICATOR`), see [A2A v1.0 - signed agent cards](../architecture/a2a.md).

## Limitations

- One global manifest per server; the task server publishes only its own surface, not aggregated plugin or adapter manifests.
- There is no central registry of running Bernstein instances; each server serves its own `.well-known` surface on its own host.

## Related

- [A2A v1.0 - signed agent cards](../architecture/a2a.md) - the agent-card contract, signing, and verification.
- [Persistent agent-card keystore](../security/keystore.md) - key storage and rotation.
- Source: `src/bernstein/core/routes/well_known.py`.
