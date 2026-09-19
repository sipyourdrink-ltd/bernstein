# custom-vault-token-store

example bernstein plugin: backs `ExternalSecretStore` with a real
HashiCorp Vault token role, so a secret reference like
`vault:bernstein-agent` mints a fresh, genuinely short-lived Vault
token on every call instead of handing back a static value.

implements the `provide_secret_store` hookspec
(`src/bernstein/plugins/hookspecs.py`) and the three-method contract in
`src/bernstein/core/security/external_secret_store.py`.

## install

```bash
pip install -e examples/plugins/custom-vault-token-store
```

bernstein discovers the plugin via the `bernstein.plugins` entry-point
group declared in `pyproject.toml`.

## setup

the plugin mints against a Vault **token role**, so the role must exist
before use:

```bash
vault write auth/token/roles/bernstein-agent \
    allowed_policies=default orphan=true renewable=true \
    explicit_max_ttl=600s
```

## configuration

```bash
export BERNSTEIN_VAULT_ADDR="http://127.0.0.1:8200"
export BERNSTEIN_VAULT_TOKEN="<a vault token allowed to create from the role>"
```

both must be set or the plugin opts out of registering (no `vault`
entry shows up in the store registry, rather than a store that fails
on first use).

`BERNSTEIN_VAULT_ADDR` must use a loopback `http://` address (for example
`http://127.0.0.1:8200`) or `https://`. A non-loopback `http://` address
sends the `X-Vault-Token` header in cleartext on the network, so do not
point this plugin at one.

## what it does

* `resolve("bernstein-agent")` checks the role exists and reports its
  configured max TTL.
* `mint_credential("bernstein-agent", audience=..., ttl_seconds=...)`
  calls `auth/token/create/bernstein-agent`, so every task gets its own
  Vault-issued token and accessor. the returned expiry is capped to
  the narrower of the requested ttl and Vault's own lease duration,
  per the contract ("it must not return a longer one").
* `report_revocation(..., upstream_id=...)` answers the question the
  broker actually asks before a mint. The broker passes the value
  `resolve()` returned, which for this store is the role name, so the
  store checks whether the role still exists (a missing role means
  revoked). When handed a minted token's accessor instead, it looks the
  accessor up (`auth/token/lookup-accessor`); only Vault's
  `400 invalid accessor` answer means revoked. A 403, a 5xx, or an
  unreachable server re-raises rather than pretending the token is gone.

## what gets recorded

the token value itself never enters the grant chain. What
`bernstein.core.identity.grants` records when this store issues a
credential is the store id in the reference (`vault`), nothing else from
this plugin. The `client_token`, its accessor, and its lease identity are
held in the broker's in-process registry for the token's lifetime and are
not written to disk by this plugin. The broker checks revocation only
before a mint (the `report_revocation` call), so revoking an already
issued token in Vault does not by itself re-check that token later.

## why plain HTTP, not a vendor client

`VaultHttpTransport` is stdlib `urllib.request`, no `hvac` or other
Vault SDK. Three calls (`auth/token/roles/<path>`,
`auth/token/create/<path>`, `auth/token/lookup-accessor`) do not carry
their weight in a dependency, and it keeps this example matching the
contract's own "no vendor SDKs in core" rule one level further out:
the plugin has no vendor import to isolate in the first place. If a
real deployment wants renewal, namespaces, or another Vault auth
method, swap in `hvac` behind the same `VaultTransport` protocol
(that seam is why it is a `Protocol` and not a concrete class here);
this example just does not need it yet.

## what's real vs stubbed

nothing is stubbed on the Vault side: `VaultHttpTransport` makes real
`urllib` calls, no vendor SDK. `tests/test_store.py` runs against a
fake transport so it needs no network; `tests/test_live_vault.py`
exercises the same store against a real Vault dev server (skipped
unless `BERNSTEIN_VAULT_LIVE_TEST_ADDR`/`_TOKEN` are set) and pins the
full mint -> lookup-self -> revoke -> re-check round trip.

## test

```bash
cd examples/plugins/custom-vault-token-store
pytest tests/ -q

# against a real Vault dev server:
docker run --rm -d -p 8200:8200 -e VAULT_DEV_ROOT_TOKEN_ID=roottoken hashicorp/vault
vault write auth/token/roles/bernstein-agent allowed_policies=default orphan=true renewable=true explicit_max_ttl=600s
BERNSTEIN_VAULT_LIVE_TEST_ADDR=http://127.0.0.1:8200 BERNSTEIN_VAULT_LIVE_TEST_TOKEN=roottoken \
    pytest tests/test_live_vault.py -q
```
