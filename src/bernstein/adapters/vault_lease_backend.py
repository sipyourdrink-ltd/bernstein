"""Vault-lease-backed :class:`CredentialVault` implementation.

Uses HashiCorp Vault dynamic secrets to issue short-lived leases for
credentials. The backend stores the lease metadata locally (in-memory or via
an optional persistence layer) and revokes leases when the credential is
deleted or on explicit revocation.

This backend differs from :class:`FileBackend` and :class:`KeyringBackend`
in that secrets are never stored locally — Vault issues ephemeral credentials
that auto-expire, reducing the blast radius of a compromised credential store.

Usage::

    backend = VaultLeaseBackend(
        vault_url="https://vault.example.com",
        mount_path="secret",
        role="bernstein-agent",
    )
    backend.put("my-provider", StoredSecret(
        secret="...", account="user@example.com", fingerprint="abc123", created_at="..."
    ))
    secret = backend.get("my-provider")
    backend.revoke("my-provider")  # explicitly revoke the lease
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    CredentialVault,
    StoredSecret,
    VaultError,
    VaultNotFoundError,
)

logger = logging.getLogger(__name__)

#: Default Vault API version.
_DEFAULT_API_VERSION = "v1"

#: Default TTL for issued leases in seconds (15 minutes).
_DEFAULT_TTL_SECONDS = 900

#: Maximum lease lifetime to request from Vault.
_MAX_TTL_SECONDS = 86400  # 24 hours


class VaultLeaseError(VaultError):
    """Raised when a Vault lease operation fails."""


class VaultLeaseNotFoundError(VaultNotFoundError):
    """Raised when no lease exists for a given provider."""


@dataclass(frozen=True)
class LeaseInfo:
    """Lease metadata for a stored credential.

    Returned by :meth:`VaultLeaseBackend.lease_info`.
    """

    lease_id: str
    expires_at: datetime
    mount_path: str
    secret_path: str
    created_at: str
    is_valid: bool  # True if expiry has not passed

    @classmethod
    def from_entry(cls, entry: _LeaseEntry) -> LeaseInfo:
        """Build a :class:`LeaseInfo` from an internal lease entry."""
        return cls(
            lease_id=entry.lease_id,
            expires_at=entry.expires_at,
            mount_path=entry.mount_path,
            secret_path=entry.secret_path,
            created_at=entry.created_at,
            is_valid=datetime.now(tz=UTC) < entry.expires_at,
        )


@dataclass(frozen=True)
class _LeaseEntry:
    """Metadata for an active Vault lease."""

    lease_id: str
    expires_at: datetime
    mount_path: str
    secret_path: str
    created_at: str


@dataclass
class _InMemoryStore:
    """Thread-unsafe in-memory store for lease metadata.

    In production, replace with a persistent store (e.g., file backend or
    keyring) if restart-resilience is required.
    """

    _entries: dict[str, _LeaseEntry] = field(default_factory=dict)

    def put(self, provider_id: str, entry: _LeaseEntry) -> None:
        self._entries[provider_id] = entry

    def get(self, provider_id: str) -> _LeaseEntry | None:
        return self._entries.get(provider_id)

    def delete(self, provider_id: str) -> bool:
        return self._entries.pop(provider_id, None) is not None

    def list(self) -> list[str]:
        return list(self._entries.keys())

    def clear(self) -> None:
        self._entries.clear()


class VaultLeaseBackend(CredentialVault):
    """Credential vault backed by HashiCorp Vault dynamic secrets.

    Issues short-lived leases for each stored credential. The actual secret
    value is fetched from Vault at :meth:`get` time and cached locally for
    the lease lifetime. Leases are revoked on :meth:`delete` or
    :meth:`revoke`.

    Args:
        vault_url: Base URL of the Vault server (e.g. ``https://vault.example.com``).
        mount_path: Vault mount path for the secrets engine (e.g. ``secret``).
        role: Vault role name for dynamic secret generation.
        token: Vault token. If ``None``, reads from ``VAULT_TOKEN`` env var.
        token_env_var: Environment variable name containing the Vault token.
            Defaults to ``VAULT_TOKEN``.
        ttl_seconds: Requested TTL for each lease. Vault may issue a shorter
            lease based on its configuration. Defaults to 900 (15 minutes).
        store: Optional persistence layer for lease metadata. Defaults to an
            in-memory store that does not survive restarts.
        http_client: Optional urllib request object for testing.
    """

    backend_id = "vault-lease"

    def __init__(
        self,
        *,
        vault_url: str,
        mount_path: str = "secret",
        role: str = "",
        token: str | None = None,
        token_env_var: str = "VAULT_TOKEN",
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        store: _InMemoryStore | None = None,
        http_client: Any | None = None,
    ) -> None:
        if not vault_url:
            raise VaultLeaseError("vault_url is required")
        self._vault_url = vault_url.rstrip("/")
        self._mount_path = mount_path
        self._role = role
        self._token = token or os.environ.get(token_env_var, "")
        if not self._token:
            raise VaultLeaseError(f"Vault token is required; set {token_env_var} or pass token=...")
        self._ttl_seconds = min(ttl_seconds, _MAX_TTL_SECONDS)
        self._store = store or _InMemoryStore()
        self._http_client = http_client or urllib.request
        self._secret_cache: dict[str, StoredSecret] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api_url(self, path: str) -> str:
        return f"{self._vault_url}/v1/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._api_url(path)
        headers: dict[str, str] = {
            "X-Vault-Token": self._token,
            "Content-Type": "application/json",
        }
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._http_client.urlopen(req) as resp:  # type: ignore[attr-defined]
                return cast(dict[str, Any], json.loads(resp.read()))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise VaultLeaseError(f"Vault {method} {path} failed with HTTP {exc.code}: {body_text}") from exc
        except urllib.error.URLError as exc:
            raise VaultLeaseError(f"Vault {method} {path} failed: {exc.reason}") from exc

    def _issue_lease(self, secret_path: str) -> tuple[str, StoredSecret, datetime]:
        """Request a dynamic credential from Vault and return (lease_id, secret, expires_at)."""
        # Build the request body for dynamic secrets.
        request_body: dict[str, Any] = {}
        if self._role:
            request_body["role_name"] = self._role
        request_body["ttl"] = f"{self._ttl_seconds}s"

        resp = self._request("POST", f"{self._mount_path}/creds/{secret_path}", request_body)
        data = resp.get("data", {})
        lease_id = str(resp.get("lease_id", ""))
        if not lease_id:
            raise VaultLeaseError(f"Vault did not return a lease_id for {secret_path}")

        # Parse expiry from lease_duration.
        lease_duration = int(resp.get("lease_duration", self._ttl_seconds))
        expires_at = datetime.now(tz=UTC) + timedelta(seconds=lease_duration)

        # Build a synthetic StoredSecret from the dynamic credential fields.
        secret_value = json.dumps(data)
        created_at = datetime.now(tz=UTC).isoformat()
        stored = StoredSecret(
            secret=secret_value,
            account=data.get("username", data.get("access_key", "")),
            fingerprint=lease_id,
            created_at=created_at,
            last_used_at=None,
            metadata={"lease_id": lease_id, "expires_at": expires_at.isoformat()},
        )
        return lease_id, stored, expires_at

    def _revoke_lease(self, lease_id: str) -> bool:
        """Revoke a Vault lease by its lease_id."""
        try:
            self._request("POST", "sys/leases/revoke", {"lease_id": lease_id})
            return True
        except VaultLeaseError as exc:
            logger.warning("Failed to revoke lease %s: %s", lease_id, exc)
            return False

    # ------------------------------------------------------------------
    # CredentialVault protocol
    # ------------------------------------------------------------------

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        """Store a credential by requesting a new Vault lease.

        The ``secret.secret`` field is ignored; Vault issues the actual
        credential value dynamically.
        """
        secret_path = provider_id
        lease_id, stored, expires_at = self._issue_lease(secret_path)
        entry = _LeaseEntry(
            lease_id=lease_id,
            expires_at=expires_at,
            mount_path=self._mount_path,
            secret_path=secret_path,
            created_at=stored.created_at,
        )
        self._store.put(provider_id, entry)
        self._secret_cache[provider_id] = stored

    def get(self, provider_id: str) -> StoredSecret:
        """Return the cached credential for ``provider_id``.

        Raises :class:`VaultLeaseNotFoundError` if no lease exists.
        """
        # Check in-memory cache first.
        if provider_id in self._secret_cache:
            return self._secret_cache[provider_id]

        # Re-issue the lease to re-populate the cache.
        entry = self._store.get(provider_id)
        if entry is None:
            raise VaultLeaseNotFoundError(f"No lease for provider {provider_id!r}")
        lease_id, stored, expires_at = self._issue_lease(entry.secret_path)
        # Update the store with the new lease.
        new_entry = _LeaseEntry(
            lease_id=lease_id,
            expires_at=expires_at,
            mount_path=entry.mount_path,
            secret_path=entry.secret_path,
            created_at=entry.created_at,
        )
        self._store.put(provider_id, new_entry)
        self._secret_cache[provider_id] = stored
        return stored

    def delete(self, provider_id: str) -> bool:
        """Remove the lease entry and revoke the Vault lease."""
        entry = self._store.get(provider_id)
        if entry is None:
            self._secret_cache.pop(provider_id, None)
            return False
        self._store.delete(provider_id)
        self._secret_cache.pop(provider_id, None)
        self._revoke_lease(entry.lease_id)
        return True

    def list(self) -> list[CredentialRecord]:
        """Return metadata for all stored credentials."""
        records: list[CredentialRecord] = []
        for provider_id in self._store.list():
            entry = self._store.get(provider_id)
            if entry is None:
                continue
            records.append(
                CredentialRecord(
                    provider_id=provider_id,
                    account="",
                    fingerprint=entry.lease_id,
                    created_at=entry.created_at,
                    last_used_at=None,
                    metadata={"mount_path": entry.mount_path, "expires_at": entry.expires_at.isoformat()},
                )
            )
        return records

    def touch(self, provider_id: str, last_used_at: str) -> None:
        """Update the last-used timestamp (no-op for lease-backed credentials)."""
        # Lease-backed credentials are refreshed on get(); touching is a no-op.
        pass

    # ------------------------------------------------------------------
    # Lease management
    # ------------------------------------------------------------------

    def revoke(self, provider_id: str) -> bool:
        """Explicitly revoke the Vault lease for ``provider_id``."""
        entry = self._store.get(provider_id)
        if entry is None:
            return False
        self._store.delete(provider_id)
        self._secret_cache.pop(provider_id, None)
        self._revoke_lease(entry.lease_id)
        return True

    def revoke_all(self) -> int:
        """Revoke all active leases and clear the store."""
        count = 0
        for provider_id in list(self._store.list()):
            if self.revoke(provider_id):
                count += 1
        return count

    # ------------------------------------------------------------------
    # Lease lifecycle
    # ------------------------------------------------------------------

    def has_lease(self, provider_id: str) -> bool:
        """Return ``True`` when a lease entry exists for ``provider_id``."""
        return self._store.get(provider_id) is not None

    def lease_info(self, provider_id: str) -> LeaseInfo:
        """Return lease metadata for ``provider_id``.

        Raises :class:`VaultLeaseNotFoundError` if no lease entry exists.
        """
        entry = self._store.get(provider_id)
        if entry is None:
            raise VaultLeaseNotFoundError(f"No lease for provider {provider_id!r}")
        return LeaseInfo.from_entry(entry)

    def renew_lease(self, provider_id: str) -> bool:
        """Renew the Vault lease for ``provider_id`` by re-issuing credentials.

        Issues a fresh lease from Vault and updates the stored lease metadata.
        Returns ``True`` on success, ``False`` if the provider has no existing
        lease or the renewal call fails.
        """
        entry = self._store.get(provider_id)
        if entry is None:
            return False
        try:
            lease_id, stored, expires_at = self._issue_lease(entry.secret_path)
            new_entry = _LeaseEntry(
                lease_id=lease_id,
                expires_at=expires_at,
                mount_path=entry.mount_path,
                secret_path=entry.secret_path,
                created_at=stored.created_at,
            )
            self._store.put(provider_id, new_entry)
            self._secret_cache[provider_id] = stored
            return True
        except VaultLeaseError:
            return False

    def is_lease(self, provider_id: str) -> bool:
        """Return ``True`` when ``provider_id`` has a valid (non-expired) lease."""
        entry = self._store.get(provider_id)
        if entry is None:
            return False
        return datetime.now(tz=UTC) < entry.expires_at
