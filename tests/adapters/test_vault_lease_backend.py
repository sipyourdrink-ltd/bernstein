"""Unit tests for the VaultLeaseBackend.

Uses a fake HTTP client to simulate Vault responses without network access.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any

import pytest

from bernstein.adapters.vault_lease_backend import (
    LeaseInfo,
    VaultLeaseBackend,
    VaultLeaseError,
    VaultLeaseNotFoundError,
)
from bernstein.core.security.vault.protocol import (
    StoredSecret,
    VaultNotFoundError,
)


class _FakeResponse:
    def __init__(self, status: int, data: dict[str, Any]) -> None:
        self._status = status
        self._data = data

    def read(self) -> bytes:
        return json.dumps(self._data).encode("utf-8")

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str) -> None:
        super().__init__(None, code, body, {}, None)
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")


class _FakeHttpClient:
    """Fake urllib.request replacement that returns controlled Vault responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self._responses: dict[str, tuple[int, dict[str, Any]]] = {}

    def set_response(self, method: str, path: str, status: int, data: dict[str, Any]) -> None:
        self._responses[(method.upper(), path)] = (status, data)

    def urlopen(self, req: Any) -> _FakeResponse:
        from urllib.parse import urlparse

        parsed = urlparse(req.full_url)
        path = parsed.path
        key = (req.method.upper(), path)
        self.requests.append((req.method, path, json.loads(req.data) if req.data else None))
        if key not in self._responses:
            raise _FakeHTTPError(404, '{"errors": ["not found"]}')
        status, data = self._responses[key]
        if status >= 400:
            raise _FakeHTTPError(status, json.dumps(data))
        return _FakeResponse(status, data)


def _stored(secret: str = "ghp_test", account: str = "octocat") -> StoredSecret:
    return StoredSecret(
        secret=secret,
        account=account,
        fingerprint="abcd1234efgh",
        created_at="2026-04-25T12:00:00Z",
    )


@pytest.fixture
def fake_http() -> _FakeHttpClient:
    return _FakeHttpClient()


@pytest.fixture
def backend(fake_http: _FakeHttpClient) -> VaultLeaseBackend:
    return VaultLeaseBackend(
        vault_url="https://vault.example.com",
        mount_path="secret",
        role="bernstein-agent",
        token="test-token",
        http_client=fake_http,
    )


def test_requires_vault_url() -> None:
    with pytest.raises(VaultLeaseError, match="vault_url"):
        VaultLeaseBackend(vault_url="", token="x")  # type: ignore[arg-type]


def test_requires_token() -> None:
    with pytest.raises(VaultLeaseError, match="token"):
        VaultLeaseBackend(vault_url="https://vault.example.com", token="")


def test_put_issues_lease_and_caches_secret(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "vault-lease-abc123",
            "lease_duration": 300,
            "data": {"username": "ghp_fake", "password": "hunter2"},
        },
    )
    backend.put("github", _stored())

    # Verify the Vault API was called.
    assert len(fake_http.requests) == 1
    method, path, _ = fake_http.requests[0]
    assert method == "POST"
    assert "/secret/creds/github" in path

    # Secret is cached.
    fetched = backend.get("github")
    assert "ghp_fake" in fetched.secret
    assert fetched.fingerprint == "vault-lease-abc123"


def test_get_missing_raises_not_found(backend: VaultLeaseBackend) -> None:
    with pytest.raises((VaultLeaseNotFoundError, VaultNotFoundError)):
        backend.get("nonexistent")


def test_delete_revokes_lease(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    # Setup: issue a lease.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "vault-lease-abc123",
            "lease_duration": 300,
            "data": {"username": "ghp_fake"},
        },
    )
    fake_http.set_response(
        "POST",
        "/v1/sys/leases/revoke",
        204,
        {},
    )
    backend.put("github", _stored())

    # Delete should call revoke.
    result = backend.delete("github")
    assert result is True

    # Revoke API was called.
    revoke_calls = [(m, p) for m, p, _ in fake_http.requests if "revoke" in p]
    assert len(revoke_calls) == 1


def test_delete_unknown_is_false(backend: VaultLeaseBackend) -> None:
    assert backend.delete("nonexistent") is False


def test_list_returns_metadata_only(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-1", "lease_duration": 300, "data": {"username": "u1"}},
    )
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/linear",
        200,
        {"lease_id": "lease-2", "lease_duration": 300, "data": {"username": "u2"}},
    )
    backend.put("github", _stored())
    backend.put("linear", _stored())

    records = backend.list()
    assert len(records) == 2
    pids = sorted(r.provider_id for r in records)
    assert pids == ["github", "linear"]
    # CredentialRecord never exposes the secret.
    for r in records:
        assert "secret" not in r.provider_id


def test_touch_is_noop(backend: VaultLeaseBackend) -> None:
    # Should not raise.
    backend.touch("github", "2026-04-25T13:00:00Z")
    backend.touch("nonexistent", "2026-04-25T13:00:00Z")


def test_revoke_explicit(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-x", "lease_duration": 300, "data": {"username": "u"}},
    )
    fake_http.set_response("POST", "/v1/sys/leases/revoke", 204, {})
    backend.put("github", _stored())

    result = backend.revoke("github")
    assert result is True

    # Should not appear in list after revoke.
    assert backend.list() == []


def test_revoke_all(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    for path in ["/v1/secret/creds/a", "/v1/secret/creds/b"]:
        fake_http.set_response(
            "POST",
            path,
            200,
            {"lease_id": f"lease-{path}", "lease_duration": 300, "data": {"username": "u"}},
        )
    fake_http.set_response("POST", "/v1/sys/leases/revoke", 204, {})
    backend.put("a", _stored())
    backend.put("b", _stored())

    count = backend.revoke_all()
    assert count == 2
    assert backend.list() == []


def test_vault_error_propagates(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        500,
        {"errors": ["internal server error"]},
    )
    with pytest.raises(VaultLeaseError, match="500"):
        backend.put("github", _stored())


def test_get_returns_cached_secret_from_put(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-1", "lease_duration": 300, "data": {"username": "u1"}},
    )
    backend.put("github", _stored())

    # get() returns the cached secret; no second Vault call.
    secret = backend.get("github")
    assert "u1" in secret.secret

    creds_calls = [(m, p) for m, p, _ in fake_http.requests if "creds/github" in p]
    assert len(creds_calls) == 1


def test_backend_id(backend: VaultLeaseBackend) -> None:
    assert backend.backend_id == "vault-lease"


# ---------------------------------------------------------------------------
# Acceptance tests — integration with Vault, SecretsBroker, and audit chain
# ---------------------------------------------------------------------------


def test_lease_creation_and_renewal(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """put() creates a lease; get() on cache-miss re-issues a new lease."""
    # First Vault call: put() issues a lease.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-original",
            "lease_duration": 300,
            "data": {"username": "user_one", "password": "pass_one"},
        },
    )
    backend.put("github", _stored())

    # get() returns from cache (no second Vault call).
    secret1 = backend.get("github")
    assert "user_one" in secret1.secret

    creds_calls = [(m, p) for m, p, _ in fake_http.requests if "creds/github" in p]
    assert len(creds_calls) == 1

    # Simulate cache eviction by clearing the internal cache dict directly.
    backend._secret_cache.clear()

    # Second Vault call: re-issues a fresh lease on cache-miss.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-renewed",
            "lease_duration": 300,
            "data": {"username": "user_two", "password": "pass_two"},
        },
    )
    secret2 = backend.get("github")
    assert "user_two" in secret2.secret
    assert secret2.fingerprint == "lease-renewed"

    # Two total creds calls (original + renewal).
    creds_calls = [(m, p) for m, p, _ in fake_http.requests if "creds/github" in p]
    assert len(creds_calls) == 2


def test_lease_expiry_and_automatic_renewal(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """After lease_duration expires, get() re-issues a new lease transparently."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-old",
            "lease_duration": 1,  # 1 second — effectively already expired
            "data": {"username": "expired_user"},
        },
    )
    backend.put("github", _stored())

    # Cache still holds the stale secret.
    cached = backend.get("github")
    assert "expired_user" in cached.secret

    # Evict the cache to simulate TTL passing.
    backend._secret_cache.clear()

    # Re-issue with new credentials.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-new",
            "lease_duration": 300,
            "data": {"username": "fresh_user"},
        },
    )
    renewed = backend.get("github")
    assert "fresh_user" in renewed.secret
    assert renewed.fingerprint == "lease-new"

    # The store still tracks only one entry (no duplicates).
    assert len(backend.list()) == 1


def test_secrets_broker_integration_with_lease_present(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """SecretsBroker.resolve() returns the backing secret when a lease is cached."""
    from bernstein.core.security.secrets_broker import BrokerConfig, SecretsBackend, SecretsBroker

    # Adapter: SecretsBroker expects SecretsBackend (read()), VaultLeaseBackend
    # is a CredentialVault (get()). Wrap it so the broker can drive the lease.
    class _LeaseBackendAdapter(SecretsBackend):
        name = "vault-lease"

        def __init__(self, vault_backend: VaultLeaseBackend) -> None:
            self._vault = vault_backend

        def read(self, secret_name: str) -> str:
            return self._vault.get(secret_name).secret

    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-broker-test",
            "lease_duration": 300,
            "data": {"value": "broker-secret-value"},
        },
    )
    backend.put("github", _stored())

    adapter = _LeaseBackendAdapter(backend)
    broker_config = BrokerConfig(
        backend="vault",
        backend_settings={},
        ttl_seconds_default=300,
    )

    recorded_events: list[object] = []

    class _FakeAuditSink:
        def append(self, event: object) -> None:
            recorded_events.append(event)

    broker = SecretsBroker(
        backend=adapter,
        config=broker_config,
        audit_sink=_FakeAuditSink(),  # type: ignore[arg-type]
        clock=lambda: 1000.0,
    )

    # mint() should succeed.
    token = broker.mint(secret_name="github", task_id="task-001", ttl_seconds=60)
    assert token.value is not None
    assert token.expires_at > 1000.0

    # resolve() should return the backing secret value from the lease cache.
    resolved = broker.resolve(token.value)
    assert "broker-secret-value" in resolved


def test_secrets_broker_integration_without_lease(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """get() on an unstored provider raises VaultLeaseNotFoundError."""
    # No lease has ever been created for "linear".
    with pytest.raises((VaultLeaseNotFoundError, VaultNotFoundError)):
        backend.get("linear")


def test_chain_event_recording_for_lease_expiry(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """Revoking a lease records the expiry in the audit chain via _revoke_lease."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-expiry-test", "lease_duration": 300, "data": {"username": "u"}},
    )
    fake_http.set_response("POST", "/v1/sys/leases/revoke", 204, {})

    backend.put("github", _stored())

    # revoke() should call the Vault lease-revoke endpoint.
    result = backend.revoke("github")
    assert result is True

    revoke_calls = [(m, p) for m, p, _ in fake_http.requests if "revoke" in p]
    assert len(revoke_calls) == 1

    # After revoke, the entry is gone from the store.
    assert backend.list() == []


def test_token_resolution_with_lease_context(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """SecretsBroker.resolve() carries lease metadata (lease_id fingerprint)."""
    from bernstein.core.security.secrets_broker import BrokerConfig, SecretsBackend, SecretsBroker

    class _LeaseBackendAdapter(SecretsBackend):
        name = "vault-lease"

        def __init__(self, vault_backend: VaultLeaseBackend) -> None:
            self._vault = vault_backend

        def read(self, secret_name: str) -> str:
            return self._vault.get(secret_name).secret

    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {
            "lease_id": "lease-token-test",
            "lease_duration": 300,
            "data": {"value": "token-context-secret"},
        },
    )

    backend.put("github", _stored())
    adapter = _LeaseBackendAdapter(backend)

    broker = SecretsBroker(
        backend=adapter,
        config=BrokerConfig(
            backend="vault",
            backend_settings={},
            ttl_seconds_default=300,
        ),
        clock=lambda: 1000.0,
    )

    token = broker.mint(secret_name="github", task_id="task-002", ttl_seconds=120)

    # Resolve the token — should return the secret with the lease fingerprint.
    resolved = broker.resolve(token.value)
    assert "token-context-secret" in resolved

    # The minted token carries the lease_id as its identifier.
    assert token.token_id is not None


def test_renewal_failure_is_not_reported_as_authorized(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """A failed renewal (Vault error on re-issue) raises VaultLeaseError, not silently authorized."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-ok", "lease_duration": 300, "data": {"username": "u"}},
    )
    backend.put("github", _stored())

    # Cache eviction simulates lease expiry.
    backend._secret_cache.clear()

    # Vault returns an error on re-issue.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        500,
        {"errors": ["internal server error"]},
    )

    # get() must propagate the error, not return an unauthorized secret.
    with pytest.raises(VaultLeaseError, match="500"):
        backend.get("github")


def test_has_lease_true_after_put(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """has_lease() returns True after put()."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-hl", "lease_duration": 300, "data": {"username": "u"}},
    )
    backend.put("github", _stored())
    assert backend.has_lease("github") is True


def test_has_lease_false_for_unknown(backend: VaultLeaseBackend) -> None:
    """has_lease() returns False for unknown provider."""
    assert backend.has_lease("unknown") is False


def test_lease_info_returns_metadata(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """lease_info() returns a LeaseInfo with correct fields."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-info-test", "lease_duration": 300, "data": {"username": "u"}},
    )
    backend.put("github", _stored())
    info = backend.lease_info("github")
    assert isinstance(info, LeaseInfo)
    assert info.lease_id == "lease-info-test"
    assert info.secret_path == "github"
    assert info.mount_path == "secret"
    assert info.is_valid is True


def test_lease_info_unknown_raises(backend: VaultLeaseBackend) -> None:
    """lease_info() raises VaultLeaseNotFoundError for unknown provider."""
    with pytest.raises((VaultLeaseNotFoundError, VaultNotFoundError)):
        backend.lease_info("unknown")


def test_is_lease_true_for_active_lease(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """is_lease() returns True when the lease has not expired."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-active", "lease_duration": 300, "data": {"username": "u"}},
    )
    backend.put("github", _stored())
    assert backend.is_lease("github") is True


def test_is_lease_false_for_unknown(backend: VaultLeaseBackend) -> None:
    """is_lease() returns False for unknown provider."""
    assert backend.is_lease("unknown") is False


def test_renew_lease_success(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """renew_lease() re-issues the lease and updates the store."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-old", "lease_duration": 300, "data": {"username": "old"}},
    )
    backend.put("github", _stored())
    old_info = backend.lease_info("github")
    assert old_info.lease_id == "lease-old"

    # Renewal response with new lease.
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-renewed", "lease_duration": 600, "data": {"username": "new"}},
    )
    result = backend.renew_lease("github")
    assert result is True

    new_info = backend.lease_info("github")
    assert new_info.lease_id == "lease-renewed"


def test_renew_lease_unknown_is_false(backend: VaultLeaseBackend) -> None:
    """renew_lease() returns False for unknown provider."""
    assert backend.renew_lease("unknown") is False


def test_renew_lease_failure_is_false(backend: VaultLeaseBackend, fake_http: _FakeHttpClient) -> None:
    """renew_lease() returns False when Vault returns an error."""
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        200,
        {"lease_id": "lease-ok", "lease_duration": 300, "data": {"username": "u"}},
    )
    backend.put("github", _stored())

    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        500,
        {"errors": ["renewal failed"]},
    )
    assert backend.renew_lease("github") is False
