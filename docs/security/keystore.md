# Persistent agent-card keystore

The orchestrator signs its A2A v1.0 agent card with a per-installation
Ed25519 keypair. Without a persistent keystore, every restart would
mint a fresh `kid` and break verifiers that had cached the previous
JWK. This page covers how the keystore lives on disk, what permissions
it expects, and how rotation works so peers stay verified through the
swap.

For the wire-level signing flow, see
[A2A v1.0 - signed agent cards](../architecture/a2a.md).

---

## Filesystem layout

```
.bernstein/keys/
    agent-card.ed25519       0600   PKCS#8 PEM private key (current)
    agent-card.ed25519.pub   0600   SPKI PEM public key   (current)
    archive/
        20260510T141200Z/
            agent-card.ed25519       0600   rotated-out private
            agent-card.ed25519.pub   0600   rotated-out public
            rotated_at.txt                  UTC ISO-8601 timestamp
```

Source: `src/bernstein/core/security/agent_card_keystore.py`.

The default directory is `.bernstein/keys` under the working directory.
Set `BERNSTEIN_AGENT_CARD_KEY_DIR` to point at a mounted secret volume
in a containerised deployment.

The public key is written `0o600` even though it is also published over
the JWKS HTTP endpoint. External consumers fetch it from the network,
not from the local filesystem, so there is no operational reason to
grant other local users filesystem read.

---

## On-disk semantics

| Property | Behaviour |
|---|---|
| First-run create | `os.O_EXCL` on the private file. Two concurrent processes cannot both win. |
| Permissions on create | `0o600` enforced by `os.chmod` even if the umask was wider. |
| Permissions on load | Refuses to load a private file with mode looser than `0o600`. Operator must `chmod 600` and retry. |
| Reload after restart | Reads from disk; no in-process cache survives a restart. |
| Multi-process | Share the directory; `O_EXCL` resolves the cold-start race. Pre-provision to skip it entirely. |
| Envelope encryption | Not in this module. Layer KMS / sops / age on the directory if you need it. |

The `O_EXCL` plus `0o600` pair is the file-level invariant the
orchestrator relies on. A wider mode raises `PermissionError` on load -
the operator sees the misconfiguration immediately rather than silently
running with a too-readable key.

Source: `_generate_atomic` and `_load_existing` in
`src/bernstein/core/security/agent_card_keystore.py`.

---

## Rotation

Operators rotate by calling `AgentCardKeystore.rotate()` or the
route-module helper `rotate_agent_card_keys()`. Rotation is two steps:

1. The current keypair moves under
   `archive/<utc-isoformat>/` together with a `rotated_at.txt`
   timestamp file.
2. A fresh keypair is minted with the same `O_EXCL` plus `0o600`
   semantics as a first-run create.

Both files in the archive directory keep `0o600`. The timestamp file
is written `0o644` because it carries no secret material; the JWKS
endpoint reads it to decide whether the archived public key still
falls inside the grace window.

Source: `rotate` and `_archive_existing` in
`src/bernstein/core/security/agent_card_keystore.py`.

---

## Rotation grace window

Default grace: 24 hours
(`DEFAULT_GRACE_SECONDS = 24 * 60 * 60`). During the window the JWKS
endpoint at `/.well-known/agent.json/keys` publishes both the current
and the archived public key:

```
GET /.well-known/agent.json/keys
{
  "keys": [
    { "kty": "OKP", "crv": "Ed25519", "kid": "agent-bernstein-orchestrator",                "x": "..." },
    { "kty": "OKP", "crv": "Ed25519", "kid": "agent-bernstein-orchestrator-20260510T141200Z", "x": "..." }
  ]
}
```

The archived `kid` encodes the moment the key was rotated out, so a
verifier seeing both keys in the JWKS routes by `kid` without ambiguity.

The agent-card route serves
`Cache-Control: public, max-age=3600`. A verifier that cached the
previous JWKS five minutes before the rotation gets up to 24 hours to
refresh its cache and pick up the new `kid` without any single
in-flight signature failing. Source: `list_archived` in the keystore
module and `agent_json_keys` in `src/bernstein/core/routes/well_known.py`.

Override the grace by constructing the keystore with
`grace_seconds=...` (callers that wrap the route module own this knob).

---

## Verification flow with rotation

```
peer cache hit
   ▼
old kid in JWKS? ── yes ──▶ verify signature against archived JWK ──▶ accept
                                       │
                                       no
                                       ▼
peer fetches fresh JWKS (HTTP cache aged out)
   ▼
new kid present, old kid present (still inside grace) ──▶ verify against either ──▶ accept

   ▼  (after grace expires)
old kid GC'd from JWKS ──▶ verify only against new kid
```

Operators that need a longer-than-24h tail extend the grace before
rotating, then shorten it again afterwards. Operators that want a
faster cutover shorten the grace and accept the risk that a verifier
which fetched JWKS just before the rotation cannot validate a
signature minted just after.

---

## Key lifecycle in production

1. **First boot.** No directory yet. The first
   `/.well-known/agent.json` request lazily creates the directory and
   mints the keypair under `O_EXCL`.
2. **Steady state.** Every signature uses the cached private PEM. The
   JWKS publishes a single key.
3. **Planned rotation.** Operator calls `rotate()`. The old key moves
   to `archive/`. The new key is minted. The JWKS publishes both for
   the grace window.
4. **Grace expiry.** The keystore stops including the archived key
   when `list_archived()` runs. The next JWKS fetch returns only the
   new key.
5. **Garbage collection.** The keystore does not delete archived
   directories; an operator may prune them out-of-band once they are
   well beyond grace.

Source: the public surface `load_or_generate`, `rotate`,
`list_archived` on `AgentCardKeystore` covers steps 1, 3, and 4.

---

## Operator runbook

| Scenario | Action |
|---|---|
| Bootstrap a new install | Let the first request mint the keypair, or pre-write a PKCS#8 PEM at `<key_dir>/agent-card.ed25519` with `0o600`. |
| Move to a mounted secret volume | Set `BERNSTEIN_AGENT_CARD_KEY_DIR=/run/secrets/bernstein-keys`, ensure `0o600` on the private file. |
| Suspect compromise | Rotate immediately. Then forcibly shorten the grace by overwriting `archive/<old>/rotated_at.txt` with a timestamp older than the current grace. |
| Routine rotation | Call `rotate_agent_card_keys()`. Wait 24 hours. Done. |
| Refusal to load with `PermissionError` | `chmod 600 .bernstein/keys/agent-card.ed25519` and restart. |

The keystore is intentionally minimal. KMS / HSM signing lives in
`src/bernstein/core/security/key_custody.py`;
the agent-card path stays plaintext-on-disk on purpose so a deployment
without a KMS still gets a signed card.

---

## Related

- A2A v1.0 contract and signing flow:
  [architecture/a2a.md](../architecture/a2a.md).
- `.well-known` service manifest catalog:
  [protocols/well-known-manifest.md](../protocols/well-known-manifest.md).
- Source: `src/bernstein/core/security/agent_card_keystore.py`,
  `src/bernstein/core/security/agent_card_signer.py`.
