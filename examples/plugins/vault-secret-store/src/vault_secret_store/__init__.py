"""HashiCorp Vault backend for the ExternalSecretStore contract (issue #5925).

Each task gets a fresh short-lived Vault token minted via
``auth/token/create/<role>``. The token value never enters Bernstein's
grant chain -- only its accessor and expiry are recorded, which is all
the broker needs to prove which task held which power over which window.

Configuration
=============

Pass the four required knobs to :class:`VaultSecretStore` directly, or
use :meth:`VaultSecretStore.from_env` to read them from environment
variables:

* ``BERNSTEIN_VAULT_ADDR``  -- Vault address (default ``http://127.0.0.1:8200``)
* ``BERNSTEIN_VAULT_TOKEN`` -- Root or limited-privilege token for the minting role
* ``BERNSTEIN_VAULT_ROLE``  -- Token role name (default ``bernstein-agent``)
* ``BERNSTEIN_VAULT_MOUNT`` -- Auth mount path (default ``auth/token``)

Registration
============

When installed as a package, the plugin auto-registers via the
``bernstein.plugins`` entry-point.  For in-process use (tests, scripts):

.. code-block:: python

    from vault_secret_store import VaultSecretStore
    from bernstein.core.security.secret_store_registry import register_secret_store

    register_secret_store("vault", VaultSecretStore.from_env)

Live test
=========

Set ``BERNSTEIN_VAULT_LIVE_TEST_ADDR`` and ``BERNSTEIN_VAULT_LIVE_TEST_TOKEN``
and run ``pytest tests/ -v -m live`` to exercise a real Vault dev server.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json

from bernstein.core.security.external_secret_store import (
    ExternalCredential,
    ExternalSecretStore,
    ExternalStoreError,
    SecretDescriptor,
)
from bernstein.plugins import hookimpl

__all__ = ["VaultSecretStore", "VaultConfig"]

logger = logging.getLogger(__name__)

_DEFAULT_ADDR = "http://127.0.0.1:8200"
_DEFAULT_ROLE = "bernstein-agent"
_DEFAULT_MOUNT = "auth/token"


@dataclass
class VaultConfig:
    """Vault connection parameters.

    Attributes:
        addr: Vault server address.
        token: Token used to call ``auth/token/create/<role>``.
        role: Token role name; governs the policies and TTL of minted tokens.
        mount: Auth-method mount path (without leading or trailing ``/``).
    """

    addr: str = _DEFAULT_ADDR
    token: str = ""
    role: str = _DEFAULT_ROLE
    mount: str = _DEFAULT_MOUNT

    @classmethod
    def from_env(cls) -> VaultConfig:
        """Read ``BERNSTEIN_VAULT_*`` environment variables."""
        return cls(
            addr=os.environ.get("BERNSTEIN_VAULT_ADDR", _DEFAULT_ADDR).rstrip("/"),
            token=os.environ.get("BERNSTEIN_VAULT_TOKEN", ""),
            role=os.environ.get("BERNSTEIN_VAULT_ROLE", _DEFAULT_ROLE),
            mount=os.environ.get("BERNSTEIN_VAULT_MOUNT", _DEFAULT_MOUNT).strip("/"),
        )


class VaultSecretStore(ExternalSecretStore):
    """HashiCorp Vault backend for the :class:`ExternalSecretStore` contract.

    Uses stdlib ``urllib`` only: no ``hvac`` or ``requests`` dependency,
    so this example has no transitive requirements beyond ``bernstein``.
    Replace :meth:`_vault_request` with your preferred HTTP client if you
    need retries, TLS client certificates, or namespace headers.
    """

    store_id: str = "vault"

    def __init__(self, config: VaultConfig | None = None) -> None:
        self._cfg = config or VaultConfig.from_env()

    @classmethod
    def from_env(cls, **_kwargs: Any) -> VaultSecretStore:
        """Factory compatible with :class:`SecretStoreFactory`."""
        return cls(VaultConfig.from_env())

    # ------------------------------------------------------------------
    # ExternalSecretStore interface
    # ------------------------------------------------------------------

    def resolve(self, path: str) -> SecretDescriptor:
        """Look up ``path`` with ``sys/internal/ui/mounts/<path>``-style probe.

        For a token role the canonical check is ``auth/token/lookup``; this
        example uses the ``/v1/auth/token/roles/<role>`` endpoint instead so
        the call works with a dev-server token that lacks ``auth/token/lookup``
        permissions. The upstream_id is the role name, which is stable enough
        to serve as the grant-chain identity.
        """
        role = path.split("/")[-1] if "/" in path else path
        try:
            data = self._vault_request("GET", f"/{self._cfg.mount}/roles/{role}")
        except ExternalStoreError:
            raise ExternalStoreError(f"vault: role {role!r} not found at {self._cfg.addr}")
        explicit_max_ttl = data.get("data", {}).get("explicit_max_ttl", 0)
        expires_at = (time.time() + explicit_max_ttl) if explicit_max_ttl else 0.0
        return SecretDescriptor(
            store_id=self.store_id,
            upstream_id=role,
            revoked=False,
            expires_at=expires_at,
        )

    def mint_credential(self, path: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        """Call ``auth/token/create/<role>`` and return the minted token."""
        role = path.split("/")[-1] if "/" in path else path
        payload: dict[str, Any] = {
            "ttl": f"{ttl_seconds}s",
            "metadata": {"bernstein_audience": audience},
        }
        try:
            data = self._vault_request("POST", f"/{self._cfg.mount}/create/{role}", body=payload)
        except ExternalStoreError as exc:
            raise ExternalStoreError(f"vault: could not mint token for role {role!r}: {exc}") from exc
        auth = data.get("auth") or {}
        token_value = auth.get("client_token", "")
        accessor = auth.get("accessor", "")
        lease_duration = auth.get("lease_duration", ttl_seconds)
        if not token_value:
            raise ExternalStoreError(f"vault: mint response for role {role!r} carried no client_token")
        return ExternalCredential(
            value=token_value,
            expires_at=time.time() + lease_duration,
            upstream_id=accessor,
            audience=audience,
        )

    def report_revocation(self, upstream_id: str) -> bool:
        """Look up the accessor; return True when Vault reports it revoked."""
        if not upstream_id:
            return False
        try:
            data = self._vault_request("POST", f"/{self._cfg.mount}/lookup-accessor", body={"accessor": upstream_id})
        except ExternalStoreError:
            # A 403/404 from Vault means the accessor is gone (revoked).
            return True
        return bool(data.get("data", {}).get("expire_time") == "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _vault_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make one Vault API call; raise :class:`ExternalStoreError` on failure."""
        url = f"{self._cfg.addr}/v1{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, method=method)
        req.add_header("X-Vault-Token", self._cfg.token)
        req.add_header("Content-Type", "application/json")
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as exc:
            raise ExternalStoreError(f"vault: {method} {path} → HTTP {exc.code}") from exc
        except URLError as exc:
            raise ExternalStoreError(f"vault: {method} {path} → {exc.reason}") from exc


class VaultSecretStorePlugin:
    """Pluggy plugin that registers :class:`VaultSecretStore` via the hookspec."""

    @hookimpl
    def provide_secret_store(self) -> tuple[str, Any]:
        """Register the Vault backend under the name ``vault``."""
        return ("vault", VaultSecretStore.from_env)
