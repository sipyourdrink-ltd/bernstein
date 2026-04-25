"""Keyring-backed :class:`CredentialVault` implementation.

Delegates secret storage to the OS-native keychain via the ``keyring``
package:

* macOS  — Keychain Services
* Linux  — Secret Service / libsecret (when a desktop environment is running
  or ``dbus-daemon`` + a backend like ``gnome-keyring`` is reachable)
* Windows — Credential Manager via DPAPI

A single keychain entry per provider holds a JSON envelope::

    {
      "secret": "...",
      "account": "alex@example.com",
      "fingerprint": "ab12...",
      "created_at": "2026-04-25T12:00:00Z",
      "last_used_at": null,
      "metadata": {"scope": "repo,issues"}
    }

The envelope shape lets us evolve metadata without breaking the keyring
read path — older callers see new keys and ignore them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    CredentialVault,
    StoredSecret,
    VaultError,
    VaultNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Service name used for every keychain entry. Each provider gets a separate
#: account string under this service so users can spot Bernstein-owned
#: entries in their keychain UI.
SERVICE_NAME = "bernstein"

#: Internal "provider index" entry. The OS keychain APIs do not expose a
#: namespaced "list all entries for service X" operation in a portable way,
#: so we maintain a small JSON list of provider ids alongside the
#: per-provider entries.
_INDEX_ACCOUNT = "__bernstein_provider_index__"


class KeyringUnavailable(VaultError):
    """Raised when the ``keyring`` package or its backend cannot be reached."""


def _import_keyring() -> Any:
    """Import the ``keyring`` package or raise :class:`KeyringUnavailable`.

    Import is deferred so the rest of Bernstein still works on systems that
    can't load a keyring backend (typically containers).
    """
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - covered via tests with monkeypatch
        raise KeyringUnavailable(
            "The 'keyring' package is not installed; install it or use --backend file."
        ) from exc
    return keyring


class KeyringBackend(CredentialVault):
    """Default :class:`CredentialVault` backed by the OS keychain.

    Args:
        service: Override the keychain service name. Tests pass a per-test
            value so concurrent test runs don't collide on a real keychain.
        keyring_module: Optional injection point for tests. When ``None`` the
            real :mod:`keyring` package is imported lazily.
    """

    backend_id = "keyring"

    def __init__(
        self,
        *,
        service: str = SERVICE_NAME,
        keyring_module: Any | None = None,
    ) -> None:
        self._service = service
        self._keyring = keyring_module if keyring_module is not None else _import_keyring()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_envelope(self, account: str) -> dict[str, Any] | None:
        try:
            raw = self._keyring.get_password(self._service, account)
        except Exception as exc:  # pragma: no cover - depends on backend
            raise KeyringUnavailable(f"Keyring backend failed during read: {exc}") from exc
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VaultError(
                f"Stored credential for {account!r} is corrupted (not JSON): {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise VaultError(f"Stored credential for {account!r} is not a JSON object.")
        return cast(dict[str, Any], data)

    def _write_envelope(self, account: str, envelope: dict[str, Any]) -> None:
        try:
            self._keyring.set_password(self._service, account, json.dumps(envelope))
        except Exception as exc:  # pragma: no cover - depends on backend
            raise KeyringUnavailable(f"Keyring backend failed during write: {exc}") from exc

    def _delete_account(self, account: str) -> bool:
        try:
            self._keyring.delete_password(self._service, account)
        except self._keyring.errors.PasswordDeleteError:
            return False
        except Exception as exc:  # pragma: no cover - depends on backend
            raise KeyringUnavailable(f"Keyring backend failed during delete: {exc}") from exc
        return True

    def _read_index(self) -> list[str]:
        envelope = self._read_envelope(_INDEX_ACCOUNT)
        if envelope is None:
            return []
        ids = envelope.get("providers")
        if not isinstance(ids, list):
            return []
        return [str(item) for item in ids if isinstance(item, str)]

    def _write_index(self, providers: Iterable[str]) -> None:
        unique = sorted(set(providers))
        self._write_envelope(_INDEX_ACCOUNT, {"providers": unique})

    # ------------------------------------------------------------------
    # CredentialVault protocol
    # ------------------------------------------------------------------

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        envelope: dict[str, Any] = {
            "secret": secret.secret,
            "account": secret.account,
            "fingerprint": secret.fingerprint,
            "created_at": secret.created_at,
            "last_used_at": secret.last_used_at,
            "metadata": secret.metadata or {},
        }
        self._write_envelope(provider_id, envelope)
        index = self._read_index()
        if provider_id not in index:
            index.append(provider_id)
            self._write_index(index)

    def get(self, provider_id: str) -> StoredSecret:
        envelope = self._read_envelope(provider_id)
        if envelope is None:
            raise VaultNotFoundError(f"No vault entry for provider {provider_id!r}")
        return _envelope_to_stored(envelope)

    def delete(self, provider_id: str) -> bool:
        removed = self._delete_account(provider_id)
        if removed:
            index = [pid for pid in self._read_index() if pid != provider_id]
            self._write_index(index)
        return removed

    def list(self) -> list[CredentialRecord]:  # noqa: A003 — protocol method
        records: list[CredentialRecord] = []
        for provider_id in self._read_index():
            envelope = self._read_envelope(provider_id)
            if envelope is None:
                continue
            stored = _envelope_to_stored(envelope)
            records.append(
                CredentialRecord(
                    provider_id=provider_id,
                    account=stored.account,
                    fingerprint=stored.fingerprint,
                    created_at=stored.created_at,
                    last_used_at=stored.last_used_at,
                    metadata=stored.metadata,
                )
            )
        return records

    def touch(self, provider_id: str, last_used_at: str) -> None:
        envelope = self._read_envelope(provider_id)
        if envelope is None:
            return
        envelope["last_used_at"] = last_used_at
        self._write_envelope(provider_id, envelope)


def _envelope_to_stored(envelope: dict[str, Any]) -> StoredSecret:
    metadata = envelope.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        metadata = None
    return StoredSecret(
        secret=str(envelope.get("secret", "")),
        account=str(envelope.get("account", "")),
        fingerprint=str(envelope.get("fingerprint", "")),
        created_at=str(envelope.get("created_at", "")),
        last_used_at=(str(envelope["last_used_at"]) if envelope.get("last_used_at") else None),
        metadata=cast(dict[str, str], metadata) if metadata else None,
    )
