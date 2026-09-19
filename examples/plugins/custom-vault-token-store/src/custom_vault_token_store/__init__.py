"""custom-vault-token-store - HashiCorp Vault backend for ExternalSecretStore.

Worked example of the ``provide_secret_store`` hookspec. Registers a
:class:`VaultTokenRoleStore` under the name ``vault`` so an operator's
own Vault server becomes a broker backend: ``secret_name="vault:<role>"``
resolves against a Vault token role and every mint is a fresh,
Vault-issued, genuinely short-lived token.

Configuration is read from the environment so the package can be
dropped into a runtime image and pointed at a Vault server without a
code change:

* ``BERNSTEIN_VAULT_ADDR`` - Vault base URL (``http://127.0.0.1:8200``).
* ``BERNSTEIN_VAULT_TOKEN`` - Vault token used to authenticate every
  broker call. Never logged, never recorded in the grant chain (the
  contract in ``external_secret_store.py`` covers that already; this
  plugin only holds it in memory).

Both must be set for the store to register; an operator who has not
configured Vault sees no ``vault`` entry in the registry rather than a
registration that fails on first use.
"""

from __future__ import annotations

import os

from bernstein.core.security.secret_store_registry import SecretStoreRegistration
from bernstein.plugins import hookimpl
from custom_vault_token_store._store import (
    VaultHttpError,
    VaultHttpTransport,
    VaultTokenRoleStore,
    VaultTransport,
)

__all__ = [
    "VaultHttpError",
    "VaultHttpTransport",
    "VaultTokenRoleStore",
    "VaultTokenStorePlugin",
    "VaultTransport",
]


class VaultTokenStorePlugin:
    """Plugin entry point: advertises the ``vault`` secret store."""

    plugin_name = "custom-vault-token-store"
    hook_target = "provide_secret_store"

    @hookimpl
    def provide_secret_store(self) -> SecretStoreRegistration | None:
        addr = os.environ.get("BERNSTEIN_VAULT_ADDR", "")
        token = os.environ.get("BERNSTEIN_VAULT_TOKEN", "")
        if not addr or not token:
            return None

        def _factory(**_kwargs: object) -> VaultTokenRoleStore:
            return VaultTokenRoleStore(transport=VaultHttpTransport(addr=addr, token=token))

        return SecretStoreRegistration(
            name="vault",
            factory=_factory,
            summary=f"HashiCorp Vault token-role store at {addr}",
        )
