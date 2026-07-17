# Named sandbox pools

A pool is a named, governed placement contract for agent work. Operators define
it once; recipe authors (human or agent) may turn only the knobs the pool
exposes; and the placement of every run is provable after the fact, offline,
from the audit chain alone.

## Why pools

Without a pool, placement is implicit and per-run: whoever writes the recipe
controls every infra knob the workspace manifest exposes (backend, mounts,
environment, credential env vars). With agent-authored recipes in the loop, an
agent can request more network or credentials than the operator intended, and
the chain records what ran, not what was allowed to run.

A pool fixes all three at once:

- **Authors turn a small surface.** The pool declares the exact override fields
  it exposes. Anything else is refused before a sandbox is created.
- **Placement is a receipt.** Every dispatch seals
  `(pool_hash, template_hash, overrides_hash, effective_manifest_hash,
  selector_inputs, chosen_backend)`; two hosts holding the same pool and recipe
  seal a byte-identical receipt.
- **Ceilings are tamper-evident.** A widened egress class, an out-of-allowlist
  credential env var, or a capability above the ceiling is refused with a
  chained refusal receipt, not merely absent from a log.

## The pool manifest

A pool manifest is a frozen, canonicalized document. Its identity is its
`pool_hash`: a SHA-256 over the canonical JSON of every other field. The manifest
declares:

| Field | Meaning |
|---|---|
| `name` | The name a recipe targets. |
| `backend_allowlist` | Backends placement may choose from, in preference order. |
| `template` | Base workspace template: `root`, `env`, `timeout_seconds`. |
| `exposed_fields` | The exact override keys recipes may set. |
| `max_concurrency` | Pool-level concurrency ceiling. |
| `capability_ceiling` | Maximum sandbox capabilities an effective placement may require. |
| `network_egress_class` | Widest egress class placement may request (`none` < `loopback` < `restricted` < `open`). |
| `credential_env_allowlist` | Credential env-var names a recipe override may introduce. |

Register a pool from a JSON spec (the manifest without a hash):

```bash
bernstein pool register pool.json
bernstein pool list
bernstein pool show ci-linux
bernstein pool verify
```

Registration appends a `pool.registered` (or `pool.updated`) event to the HMAC
audit chain and writes the manifest body to a content-addressed store. The
runtime registry is a deterministic projection rebuilt by replaying those
events; there is no side database to drift. `bernstein pool verify` re-derives
the projection, re-hashes every stored body, and re-checks every stored
placement receipt.

## Governed overrides

A recipe targets a pool by name and may set only the fields the pool exposes.
Overrides are schema-validated and merged into the base template by a pure
function, and the result is canonicalized into an `effective_manifest_hash`. The
merge is fail-closed:

- An override on a non-exposed field is refused.
- A requested capability above the ceiling is refused.
- A requested egress class wider than the ceiling is refused.
- A credential env var outside the allowlist is refused.

Each refusal is a chained `pool.override_refused` receipt; no sandbox is created.

## Worker enrolment

A worker joins a pool with a signed ceremony:

```bash
bernstein worker --pool ci-linux --server http://central:8052
```

The worker signs an enrolment receipt binding its Ed25519 install identity to
the target `pool_hash`, then continues to poll the server outbound-only. JWT
cluster scopes remain the transport gate; the Ed25519 signature is the
attribution layer. Every subsequent claim is a signed receipt, so the execution
host of any run is cryptographically attributable and `bernstein audit verify`
proves it offline. Flipping one byte of a recorded effective manifest breaks
chain verification.

## Warm claim-ahead

Warm pre-provisioned slots are keyed by `effective_manifest_hash`: a slot
attaches to a dispatch only when the hashes are equal. Infra drift between
provisioning and claim surfaces as hash divergence, which quarantines the slot
with a receipt and falls back to cold provisioning, so warm reuse never weakens
the manifest contract.

See also: [Backend selector](../concepts/sandbox-selector.md) and
[Fleet operations](fleet.md).
