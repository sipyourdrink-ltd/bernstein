"""A minimal in-memory credential vault double for the ssh secret path.

Satisfies :class:`~bernstein.core.security.vault.protocol.CredentialVault`
enough for :func:`~bernstein.core.security.vault.resolver.resolve_secret`: the
resolver calls ``get`` and ``touch`` and reads ``backend_id``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from bernstein.core.security.vault.protocol import StoredSecret, VaultNotFoundError
from bernstein.core.security.vault.resolver import fingerprint


class FakeVault:
    """In-memory vault mapping provider id -> secret value."""

    backend_id = "fake"

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)

    def get(self, provider_id: str) -> StoredSecret:
        if provider_id not in self._secrets:
            raise VaultNotFoundError(provider_id)
        value = self._secrets[provider_id]
        return StoredSecret(
            secret=value,
            account="acct",
            fingerprint=fingerprint(value),
            created_at="2026-01-01T00:00:00Z",
        )

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        self._secrets[provider_id] = secret.secret

    def delete(self, provider_id: str) -> bool:
        return self._secrets.pop(provider_id, None) is not None

    def list(self) -> list:
        return []

    def touch(self, provider_id: str, last_used_at: str) -> None:
        return None


def make_git_repo(path: Path) -> Path:
    """Initialise a one-commit git repo at *path* and return it."""
    path.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@example.com")
    _git("config", "user.name", "t")
    (path / "seed.txt").write_text("seed\n")
    _git("add", "seed.txt")
    _git("commit", "-q", "-m", "init")
    return path


__all__ = ["FakeVault", "make_git_repo"]
