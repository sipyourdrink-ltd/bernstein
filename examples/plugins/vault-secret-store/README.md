# vault-secret-store

A bernstein example plugin that backs the
[`ExternalSecretStore`](../../../../src/bernstein/core/security/external_secret_store.py)
contract with a real [HashiCorp Vault](https://www.vaultproject.io/) token role.

Each task gets a **fresh, short-lived Vault token** minted via
`auth/token/create/<role>`. The token value never enters bernstein's grant chain;
only the accessor and expiry are recorded.

## Quick start

```bash
# 1. Start a Vault dev server
vault server -dev &
export VAULT_ADDR=http://127.0.0.1:8200
export VAULT_TOKEN=root

# 2. Create the token role bernstein will mint from
vault write auth/token/roles/bernstein-agent \
    allowed_policies=default \
    orphan=true \
    renewable=true \
    explicit_max_ttl=600s

# 3. Install the plugin
pip install -e .

# 4. Configure and run
export BERNSTEIN_VAULT_ADDR=http://127.0.0.1:8200
export BERNSTEIN_VAULT_TOKEN=root
export BERNSTEIN_VAULT_ROLE=bernstein-agent
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `BERNSTEIN_VAULT_ADDR` | `http://127.0.0.1:8200` | Vault server URL |
| `BERNSTEIN_VAULT_TOKEN` | _(required)_ | Token with `auth/token/create/<role>` permission |
| `BERNSTEIN_VAULT_ROLE` | `bernstein-agent` | Token role name |
| `BERNSTEIN_VAULT_MOUNT` | `auth/token` | Auth-method mount path |

## Tests

```bash
# Unit tests (no Vault required)
pytest tests/ -v

# Live round-trip (requires a running dev server)
BERNSTEIN_VAULT_LIVE_TEST_ADDR=http://127.0.0.1:8200 \
BERNSTEIN_VAULT_LIVE_TEST_TOKEN=root \
pytest tests/ -v -m live
```

## Relationship to `vault_injector.py`

`bernstein.core.security.vault_injector` handles spawn-time dynamic-secret
injection with Vault lease bookkeeping; it lives inside core and runs before
the task starts. This plugin implements the **broker's grant-chain accounting**
path: the broker calls `mint_credential` when a task requests a named secret
at runtime, records the grant, and calls `report_revocation` when the task
ends. The two paths are complementary and intentionally separate.
