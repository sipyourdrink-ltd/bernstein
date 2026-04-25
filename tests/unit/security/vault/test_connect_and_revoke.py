"""Tests for the high-level ``perform_connect`` / ``perform_revoke`` flows.

These cover the validation-failure UX (token-paste with a bad PAT), the
revoke idempotency contract, and the OAuth device-code cancellation path.
The HTTP layer is mocked with :mod:`respx` so no network calls happen.
"""

from __future__ import annotations

from typing import cast

import httpx
import pytest
import respx

from bernstein.core.security.vault.connect import (
    perform_connect,
    perform_revoke,
)
from bernstein.core.security.vault.oauth_device import (
    DeviceCodeChallenge,
    OAuthDeviceCancelled,
    OAuthDeviceTimeout,
    poll_device_code,
)
from bernstein.core.security.vault.protocol import (
    CredentialRecord,
    StoredSecret,
    VaultNotFoundError,
)
from bernstein.core.security.vault.providers import require_provider


class _MemoryVault:
    """Fake :class:`CredentialVault` for connect/revoke tests."""

    backend_id = "memory"

    def __init__(self) -> None:
        self.store: dict[str, StoredSecret] = {}

    def put(self, provider_id: str, secret: StoredSecret) -> None:
        self.store[provider_id] = secret

    def get(self, provider_id: str) -> StoredSecret:
        if provider_id not in self.store:
            raise VaultNotFoundError(provider_id)
        return self.store[provider_id]

    def delete(self, provider_id: str) -> bool:
        return self.store.pop(provider_id, None) is not None

    def list(self) -> list[CredentialRecord]:
        return [
            CredentialRecord(
                provider_id=pid,
                account=s.account,
                fingerprint=s.fingerprint,
                created_at=s.created_at,
                last_used_at=s.last_used_at,
            )
            for pid, s in self.store.items()
        ]

    def touch(self, provider_id: str, last_used_at: str) -> None:
        if provider_id in self.store:
            stored = self.store[provider_id]
            self.store[provider_id] = StoredSecret(
                secret=stored.secret,
                account=stored.account,
                fingerprint=stored.fingerprint,
                created_at=stored.created_at,
                last_used_at=last_used_at,
                metadata=stored.metadata,
            )


# ---------------------------------------------------------------------------
# perform_connect — token validation
# ---------------------------------------------------------------------------


@respx.mock
def test_perform_connect_github_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bernstein.core.security.vault.audit.audit_event",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "bernstein.core.security.vault.connect.audit_event",
        lambda **_: None,
    )
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}),
    )
    vault = _MemoryVault()
    provider = require_provider("github")
    result = perform_connect(provider, {"token": "ghp_valid"}, vault=vault)
    assert result.success is True
    assert result.account == "octocat"
    assert result.masked_secret.startswith("ghp_")
    assert "ghp_valid" not in result.masked_secret
    assert vault.get("github").secret == "ghp_valid"


@respx.mock
def test_perform_connect_token_validation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bernstein.core.security.vault.connect.audit_event",
        lambda **_: None,
    )
    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"}),
    )
    vault = _MemoryVault()
    provider = require_provider("github")
    result = perform_connect(provider, {"token": "ghp_bad"}, vault=vault)
    assert result.success is False
    assert "Bad credentials" in result.error or "rejected" in result.error.lower()
    # The vault must NOT receive the bad credential.
    assert "github" not in vault.store


# ---------------------------------------------------------------------------
# perform_revoke — idempotency + remote endpoint
# ---------------------------------------------------------------------------


def test_perform_revoke_no_local_entry_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bernstein.core.security.vault.connect.audit_event",
        lambda **_: None,
    )
    vault = _MemoryVault()
    provider = require_provider("github")
    result = perform_revoke(provider, vault=vault)
    assert result.removed_local is False
    assert result.revoked_remote is False
    # Calling twice must not raise.
    result2 = perform_revoke(provider, vault=vault)
    assert result2.removed_local is False


@respx.mock
def test_perform_revoke_slack_calls_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bernstein.core.security.vault.connect.audit_event",
        lambda **_: None,
    )
    respx.post("https://slack.com/api/auth.revoke").mock(
        return_value=httpx.Response(200, json={"ok": True, "revoked": True}),
    )
    vault = _MemoryVault()
    vault.put(
        "slack",
        StoredSecret(
            secret="xoxb-1",
            account="bot",
            fingerprint="abc",
            created_at="2026-04-25T12:00:00Z",
        ),
    )
    provider = require_provider("slack")
    result = perform_revoke(provider, vault=vault)
    assert result.removed_local is True
    assert result.revoked_remote is True


# ---------------------------------------------------------------------------
# OAuth device code — cancellation + timeout
# ---------------------------------------------------------------------------


def _challenge() -> DeviceCodeChallenge:
    import time

    return DeviceCodeChallenge(
        verification_url="https://example.com/device",
        user_code="WDJB-MJHT",
        device_code="dev-xyz",
        interval_s=0.0,  # tests use sleep_fn injection
        expires_at=time.monotonic() + 60.0,
    )


@respx.mock
def test_poll_device_code_cancellation_raises() -> None:
    respx.post("https://idp.example.com/token").mock(
        return_value=httpx.Response(400, json={"error": "access_denied"}),
    )
    with pytest.raises(OAuthDeviceCancelled):
        poll_device_code(
            _challenge(),
            token_endpoint="https://idp.example.com/token",
            client_id="bernstein-cli",
            client=httpx.Client(),
            sleep_fn=lambda _s: None,
        )


@respx.mock
def test_poll_device_code_expired_raises() -> None:
    respx.post("https://idp.example.com/token").mock(
        return_value=httpx.Response(400, json={"error": "expired_token"}),
    )
    with pytest.raises(OAuthDeviceTimeout):
        poll_device_code(
            _challenge(),
            token_endpoint="https://idp.example.com/token",
            client_id="bernstein-cli",
            client=httpx.Client(),
            sleep_fn=lambda _s: None,
        )


@respx.mock
def test_poll_device_code_success() -> None:
    # First call returns authorization_pending; second returns access_token.
    route = respx.post("https://idp.example.com/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={"access_token": "tok-abc", "token_type": "Bearer"},
            ),
        ],
    )
    tokens = poll_device_code(
        _challenge(),
        token_endpoint="https://idp.example.com/token",
        client_id="bernstein-cli",
        client=httpx.Client(),
        sleep_fn=lambda _s: None,
    )
    assert tokens.access_token == "tok-abc"
    assert cast(int, route.call_count) == 2
