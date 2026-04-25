"""AES-GCM file-backend tests covering encryption and the passphrase guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.vault.backend_file import (
    FileBackend,
    FileBackendUnavailable,
)
from bernstein.core.security.vault.protocol import (
    StoredSecret,
    VaultNotFoundError,
)


def _stored(secret: str = "lin_api_xyz") -> StoredSecret:
    return StoredSecret(
        secret=secret,
        account="alex@example.com",
        fingerprint="abc123abc123",
        created_at="2026-04-25T12:00:00Z",
    )


def test_file_backend_requires_passphrase(tmp_path: Path) -> None:
    """Booting the file backend without the passphrase env-var is a hard error."""
    with pytest.raises(FileBackendUnavailable):
        FileBackend(passphrase_env="VAULT_PASS", path=tmp_path / "vault.enc", environ={})


def test_file_backend_roundtrip(tmp_path: Path) -> None:
    """Put then get a credential through a fresh AES-GCM file vault."""
    env = {"VAULT_PASS": "correct horse battery staple"}
    path = tmp_path / "vault.enc"
    backend = FileBackend(passphrase_env="VAULT_PASS", path=path, environ=env)
    backend.put("linear", _stored())
    assert path.exists()

    backend2 = FileBackend(passphrase_env="VAULT_PASS", path=path, environ=env)
    fetched = backend2.get("linear")
    assert fetched.secret == "lin_api_xyz"
    assert fetched.account == "alex@example.com"


def test_file_backend_wrong_passphrase_fails(tmp_path: Path) -> None:
    """A second backend with a different passphrase cannot decrypt the file."""
    env = {"VAULT_PASS": "correct horse battery staple"}
    path = tmp_path / "vault.enc"
    backend = FileBackend(passphrase_env="VAULT_PASS", path=path, environ=env)
    backend.put("linear", _stored())

    bad_backend = FileBackend(
        passphrase_env="VAULT_PASS",
        path=path,
        environ={"VAULT_PASS": "wrong-passphrase"},
    )
    with pytest.raises(FileBackendUnavailable, match="decryption failed"):
        bad_backend.get("linear")


def test_file_backend_delete_idempotent(tmp_path: Path) -> None:
    env = {"VAULT_PASS": "p"}
    backend = FileBackend(passphrase_env="VAULT_PASS", path=tmp_path / "vault.enc", environ=env)
    backend.put("github", _stored("ghp_a"))
    assert backend.delete("github") is True
    assert backend.delete("github") is False
    with pytest.raises(VaultNotFoundError):
        backend.get("github")


def test_file_backend_list_returns_metadata_only(tmp_path: Path) -> None:
    env = {"VAULT_PASS": "p"}
    backend = FileBackend(passphrase_env="VAULT_PASS", path=tmp_path / "vault.enc", environ=env)
    backend.put("github", _stored("ghp_secret_should_not_leak"))
    [record] = backend.list()
    assert record.provider_id == "github"
    # The list result is a CredentialRecord which does not carry a secret
    # field by construction. Verify by attribute presence.
    assert not hasattr(record, "secret")


def test_file_backend_touch_updates_last_used(tmp_path: Path) -> None:
    env = {"VAULT_PASS": "p"}
    backend = FileBackend(passphrase_env="VAULT_PASS", path=tmp_path / "vault.enc", environ=env)
    backend.put("github", _stored())
    backend.touch("github", "2026-04-25T13:00:00Z")
    assert backend.get("github").last_used_at == "2026-04-25T13:00:00Z"
