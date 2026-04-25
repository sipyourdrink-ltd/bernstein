"""Credential vault protocol and shared dataclasses.

A backend is anything that satisfies the :class:`CredentialVault` protocol.
The intent is to keep the contract small enough that test fakes and a
file-encrypted fallback both fit cleanly:

* :meth:`CredentialVault.put` — store / replace a secret for a provider.
* :meth:`CredentialVault.get` — return the stored secret or raise.
* :meth:`CredentialVault.delete` — remove the entry, idempotent.
* :meth:`CredentialVault.list` — enumerate metadata only (never secrets).
* :meth:`CredentialVault.touch` — update the ``last_used`` timestamp.

The protocol is deliberately synchronous — calls happen on user-facing CLI
paths where async buys nothing and complicates testing. Provider whoami /
revoke calls do hit the network but live one layer up in
:mod:`bernstein.core.security.vault.providers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class VaultError(RuntimeError):
    """Base class for vault errors."""


class VaultNotFoundError(VaultError):
    """Raised when a credential record does not exist for the given provider."""


@dataclass(frozen=True)
class StoredSecret:
    """A secret value plus the metadata stored alongside it.

    The secret payload never appears in :class:`CredentialRecord` (the type
    returned by :meth:`CredentialVault.list`) so callers of ``list`` cannot
    accidentally dump secrets to logs.
    """

    secret: str
    account: str
    fingerprint: str
    created_at: str
    last_used_at: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True)
class CredentialRecord:
    """Metadata-only view of a stored credential.

    Returned from :meth:`CredentialVault.list`. Excludes :attr:`StoredSecret.secret`
    so this type is safe to print and to send across IPC boundaries.
    """

    provider_id: str
    account: str
    fingerprint: str
    created_at: str
    last_used_at: str | None = None
    metadata: dict[str, str] | None = None


@runtime_checkable
class CredentialVault(Protocol):
    """Vault backend contract.

    Implementations MUST be safe to call from a single thread and MUST
    persist enough metadata to populate :class:`CredentialRecord` without a
    second read of the secret value.
    """

    backend_id: str

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        """Store ``secret`` for ``provider_id``, replacing any existing entry."""

    def get(self, provider_id: str) -> StoredSecret:
        """Return the stored :class:`StoredSecret` or raise :class:`VaultNotFoundError`."""

    def delete(self, provider_id: str) -> bool:
        """Remove the entry for ``provider_id``.

        Returns ``True`` if a record was removed, ``False`` if nothing was
        stored. Idempotent: calling twice is fine.
        """

    def list(self) -> list[CredentialRecord]:  # noqa: A003 — protocol method name
        """Return every stored credential as a metadata-only record."""

    def touch(self, provider_id: str, last_used_at: str) -> None:
        """Update the ``last_used_at`` timestamp for ``provider_id``.

        No-op if the provider has no entry.
        """
