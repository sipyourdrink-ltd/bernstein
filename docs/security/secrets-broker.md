# Secrets broker

A short-lived-token broker that replaces dotfile-in-workspace and
process-env credential patterns for agent spawns. The agent process
receives only a minted token; the raw backing secret never appears in the
spawned environment.

## Lifecycle

1. Operator declares a `security.secrets` block in `bernstein.yaml` with
   a chosen backend.
2. Orchestrator builds the broker once at startup.
3. Each task asks the broker to mint a token for a named secret with a
   TTL. The broker reads the raw value from the backend, generates a new
   opaque token, and registers the mapping in-process.
4. The minted token value is plumbed into the agent's env in place of
   the real credential.
5. On task exit (success or failure) the broker revokes every token
   owned by that task id; tokens also auto-expire at their TTL.
6. The redactor scrubs both the raw backing value and the minted token
   value from any persisted transcript.

## Config

```yaml
security:
  secrets:
    backend: file_encrypted     # vault | aws_secretsmanager | gcp_secret_manager
                                # | macos_keychain | linux_keyring | file_encrypted
    mint:
      ttl_seconds_default: 900  # default token lifetime
      ttl_overrides:
        ANTHROPIC_API_KEY: 1800
        SHORT_LIVED_TOKEN: 60
    backend_settings:           # forwarded to the backend constructor
      path: /var/lib/bernstein/secrets.enc
    grants:                     # scoped per-task credential grants (optional)
      require_grant: false      # refuse to mint without a verifiable grant
      identity_mode: ed25519    # ed25519 (default) | spiffe
```

When `grants.require_grant` is `true`, the orchestrator must supply a
verifiable, chain-anchored grant to `mint()`; the broker fails closed
otherwise. See [Scoped per-task grants](#scoped-per-task-grants) below.

## Backend setup

### vault

Reads `VAULT_ADDR` and `VAULT_TOKEN` from env. Pass `mount` in
`backend_settings` to override the default KV mount (`secret`). Secret
payloads must either contain a `value` field or a single field; the
broker reads exactly one scalar per name.

### aws_secretsmanager

Requires `boto3`. The standard AWS credential resolution chain applies
(env vars, profile, instance role). `secret_name` is the secret name or
ARN. JSON payloads with a `value` field are unwrapped automatically;
plain-string payloads pass through unchanged.

### gcp_secret_manager

Requires `google-cloud-secret-manager`. Reads `GOOGLE_CLOUD_PROJECT`
from env or `project` from `backend_settings`. Always reads the
`latest` version by default; override via `version`.

### macos_keychain

Shells out to the system `security` CLI. Pass `service` in
`backend_settings` to use a non-default service name (default:
`bernstein`). Store an item with:

```
security add-generic-password -s bernstein -a ANTHROPIC_API_KEY -w 'sk-ant-...'
```

### linux_keyring

Uses the `keyring` Python package, which brokers between freedesktop
Secret Service, KWallet, and the other supported backends. Store an
item with:

```python
import keyring
keyring.set_password("bernstein", "ANTHROPIC_API_KEY", "sk-ant-...")
```

### file_encrypted

Zero-dependency fallback: a Fernet-encrypted JSON object on disk.
Requires `cryptography`. The encryption key is supplied via
`BERNSTEIN_BROKER_KEY` (urlsafe base64) or a file path in
`backend_settings.key_path`. Build the store by encrypting a JSON
object that maps secret names to values.

## CLI

```
bernstein secrets list                            # enumerate backend secrets where supported
bernstein secrets mint --task t-42 --secret ANTHROPIC_API_KEY --ttl 900
bernstein secrets mint --task t-42 --secret K --reveal     # print the raw token value
```

`mint` prints a JSON object with the token id, secret name, task id,
TTL, expiry, and a masked token value. Pass `--reveal` to print the raw
token value when piping into an out-of-band agent invocation.

## Audit log

Every mint, resolve, revoke, and expiry event flows through:

- The module logger at INFO level. Only non-secret identifiers (token
  id, secret name, task id, TTL) are logged, never the raw value.
- An optional in-process `AuditSink` callback that the orchestrator can
  wire to the lineage subsystem or any other audit store.

## Redactor coupling

`bernstein.core.security.redactor.redact_text` consults the broker's
in-process registry and scrubs every minted token value and raw backing
value from the text it processes. The registry is updated automatically
on mint and revoke; tests can clear it via
`clear_redaction_registry()`.

## Scoped per-task grants

By default the broker hands any token holder the full backing secret. In
grant-enforcing mode the credential becomes a projection of a
chain-recorded grant, so a worker holds only a scoped, expiring token and
post-incident forensics can prove the exact credential window per task
from the chain alone.

### The grant record

Before a worker spawn the orchestrator issues a scoped grant with
`bernstein.core.identity.grants.GrantLedger.issue_grant`: task id, secret
name, audience, expiry, and capability ceiling. Each grant record carries
two independent tamper anchors:

- an **Ed25519 signature** by the manager identity over the grant body
  (proves who authorized it), and
- an **HMAC** over `prev_hmac` + the canonical record body, keyed by the
  install audit key (the same construction as the delegation receipts in
  `docs/security/manager-auth.md`), so a mutated, deleted, or reordered
  record breaks the chain.

Records land under `<audit>/grants/<run>.jsonl` and reconstruct the full
`grant_issued -> grant_exchanged -> grant_revoked` lifecycle offline.

### Broker exchange

`SecretsBroker.mint(..., grant=grant)` refuses to mint unless the grant
verifies against the chain and matches the `(task_id, secret_name)` pair;
the refusal is itself a `grant_refused` chain record. The minted token
inherits the grant's task id, audience, and expiry, and the token id is
recorded via a `grant_exchanged` record so token-to-grant resolution
works offline. `resolve(token, audience=...)` refuses a token presented
outside its granted audience, recording the refusal as a chain event.
`revoke()` and task-exit auto-revoke append a signed `grant_revoked`
record referencing the grant.

### Verifying offline

```
bernstein secrets grants list run-42            # reconstructed lifecycle
bernstein secrets grants verify run-42          # HMAC + Ed25519 offline
bernstein secrets grants verify run-42 --json   # byte-identical report
bernstein audit verify                          # includes all grant chains
```

`grants verify` exits non-zero and names the first failing record on any
mutation, deletion, or reordering. Two independent verifiers over the
same chain slice produce byte-identical `--json` reports.

### SPIFFE identity mode

With `grants.identity_mode: spiffe` and the optional `spiffe` extra
installed against a reachable SPIRE Workload API socket, new grants carry
the workload's SPIFFE ID as the issuer identity, binding grants to the
workload identity already checkable via `bernstein spiffe
verify-binding`. `bernstein doctor` preflights the extra and socket. With
the extra absent the default Ed25519 identity path is unchanged.

### Migration

Existing broker configs keep working unchanged: `grants.require_grant`
defaults to `false` and `identity_mode` to `ed25519`, so a broker built
without a grant ledger behaves exactly as before. To adopt scoped grants,
add the `grants` block, construct the broker with a `GrantLedger`, and
issue a grant per task before the spawn.
