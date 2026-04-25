"""End-to-end vault tests covering audit-chain integrity and resolver flow.

These hit the real :class:`AuditLog` so we can assert
``bernstein audit verify`` keeps its HMAC chain after every vault
connect / read / revoke sequence — which is the v1.9 acceptance bar.
The keyring is mocked with an in-memory backend so no real keychain
prompts appear during ``uv run`` test suites.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bernstein.core.security.audit import AUDIT_KEY_ENV, AuditLog
from bernstein.core.security.vault.audit import audit_event
from bernstein.core.security.vault.backend_keyring import KeyringBackend
from bernstein.core.security.vault.protocol import StoredSecret
from bernstein.core.security.vault.resolver import resolve_secret


class _MemKeyring:
    class _Errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.errors = _MemKeyring._Errors()

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise self.errors.PasswordDeleteError("missing")
        del self.store[(service, account)]


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a sealed audit dir + key under ``tmp_path``.

    Setting ``BERNSTEIN_AUDIT_KEY_PATH`` redirects the global key loader
    to a per-test file so we never collide with the user's real key.
    """
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    # Write a key with the required 0600 mode.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        os.write(fd, b"a" * 64)
    finally:
        os.close(fd)
    return audit_dir


def test_full_lifecycle_keeps_audit_chain_valid(
    isolated_audit: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect -> read -> revoke writes valid HMAC-chained audit events."""
    fake = _MemKeyring()
    backend = KeyringBackend(service="bernstein-test", keyring_module=fake)

    # 1. connect — emit an audit event for the write
    backend.put(
        "github",
        StoredSecret(
            secret="ghp_test",
            account="octocat",
            fingerprint="abcd1234efgh",
            created_at="2026-04-25T12:00:00Z",
        ),
    )
    audit_event(
        action="connect",
        provider_id="github",
        account="octocat",
        fingerprint="abcd1234efgh",
        backend="keyring",
        audit_dir=isolated_audit,
    )

    # 2. read — resolver path with the same audit dir
    monkeypatch.setattr(
        "bernstein.core.security.vault.audit.DEFAULT_AUDIT_DIR",
        isolated_audit,
    )
    resolution = resolve_secret(
        "github",
        vault=backend,
        environ={},
        audit=True,
    )
    assert resolution.found is True
    assert resolution.source == "vault"

    # 3. revoke — emit the third event
    audit_event(
        action="revoke",
        provider_id="github",
        account="octocat",
        fingerprint="abcd1234efgh",
        backend="keyring",
        audit_dir=isolated_audit,
        extra={"local_removed": True, "remote_revoked": False},
    )

    # 4. The HMAC chain across connect / read / revoke must verify.
    log = AuditLog(isolated_audit)
    valid, errors = log.verify()
    assert valid, f"audit chain broken: {errors}"

    events = log.query()
    actions = [e.event_type for e in events]
    # We expect at least connect / read / revoke — the read may add a
    # second event if open_vault_silent finds a backend, so use >=.
    assert "vault.connect" in actions
    assert "vault.read" in actions
    assert "vault.revoke" in actions


def test_resolver_audit_does_not_log_secret(
    isolated_audit: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit log must record the fingerprint, never the raw token."""
    fake = _MemKeyring()
    backend = KeyringBackend(service="bernstein-test", keyring_module=fake)
    backend.put(
        "linear",
        StoredSecret(
            secret="lin_api_supersecret_1234",
            account="alex@example.com",
            fingerprint="ffffaaaa1111",
            created_at="2026-04-25T12:00:00Z",
        ),
    )
    monkeypatch.setattr(
        "bernstein.core.security.vault.audit.DEFAULT_AUDIT_DIR",
        isolated_audit,
    )
    resolve_secret("linear", vault=backend, environ={}, audit=True)

    files = list(isolated_audit.glob("*.jsonl"))
    assert files, "expected at least one audit log file"
    text = files[0].read_text()
    assert "lin_api_supersecret_1234" not in text
    assert "ffffaaaa1111" in text  # fingerprint IS recorded
