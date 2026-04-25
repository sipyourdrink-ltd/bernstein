"""Unit tests for the keyring-backed vault.

We never touch the real OS keychain — the ``keyring`` module is replaced
with an in-memory fake so the round-trip is byte-identical to what a real
keyring would do (the package's contract is just ``get_password`` /
``set_password`` / ``delete_password``).
"""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core.security.vault.backend_keyring import (
    KeyringBackend,
    KeyringUnavailable,
)
from bernstein.core.security.vault.protocol import (
    StoredSecret,
    VaultNotFoundError,
)


class _FakeKeyringErrors:
    class PasswordDeleteError(Exception):
        pass


class _FakeKeyring:
    """Drop-in replacement for the ``keyring`` package surface we use."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.errors = _FakeKeyringErrors()

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise self.errors.PasswordDeleteError("not found")
        del self.store[(service, account)]


def _stored(secret: str = "ghp_test", account: str = "octocat") -> StoredSecret:
    return StoredSecret(
        secret=secret,
        account=account,
        fingerprint="abcd1234efgh",
        created_at="2026-04-25T12:00:00Z",
    )


def test_put_and_get_roundtrip() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    backend.put("github", _stored())
    fetched = backend.get("github")
    assert fetched.secret == "ghp_test"
    assert fetched.account == "octocat"


def test_get_missing_raises() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    with pytest.raises(VaultNotFoundError):
        backend.get("github")


def test_list_includes_index_after_put() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    backend.put("github", _stored("ghp_a", "octocat"))
    backend.put("linear", _stored("lin_b", "alex@example.com"))
    records = backend.list()
    pids = sorted(r.provider_id for r in records)
    assert pids == ["github", "linear"]
    # No record contains the secret value (list returns metadata only).
    for r in records:
        assert "ghp_" not in r.fingerprint
        assert "lin_" not in r.fingerprint


def test_delete_idempotent() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    backend.put("github", _stored())
    assert backend.delete("github") is True
    # Second delete returns False but does not raise.
    assert backend.delete("github") is False


def test_touch_updates_last_used() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    backend.put("github", _stored())
    backend.touch("github", "2026-04-25T13:00:00Z")
    fetched = backend.get("github")
    assert fetched.last_used_at == "2026-04-25T13:00:00Z"


def test_touch_unknown_provider_is_noop() -> None:
    backend = KeyringBackend(service="bernstein-test", keyring_module=_FakeKeyring())
    backend.touch("github", "2026-04-25T13:00:00Z")  # nothing stored
    # No exception, no entry created.
    assert backend.list() == []


def test_keyring_unavailable_raises_when_module_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the import inside _import_keyring to fail.
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "keyring":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(KeyringUnavailable):
        KeyringBackend(service="bernstein-test")
