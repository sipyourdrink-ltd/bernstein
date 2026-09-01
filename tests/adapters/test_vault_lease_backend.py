"""Unit tests for the VaultLeaseBackend.

Uses a fake HTTP client to simulate Vault responses without network access.
"""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.vault_lease_backend import (
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


def test_put_issues_lease_and_caches_secret(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
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
    method, path, body = fake_http.requests[0]
    assert method == "POST"
    assert "/secret/creds/github" in path

    # Secret is cached.
    fetched = backend.get("github")
    assert "ghp_fake" in fetched.secret
    assert fetched.fingerprint == "vault-lease-abc123"


def test_get_missing_raises_not_found(backend: VaultLeaseBackend) -> None:
    with pytest.raises((VaultLeaseNotFoundError, VaultNotFoundError)):
        backend.get("nonexistent")


def test_delete_revokes_lease(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
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


def test_list_returns_metadata_only(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
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


def test_revoke_explicit(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
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


def test_vault_error_propagates(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
    fake_http.set_response(
        "POST",
        "/v1/secret/creds/github",
        500,
        {"errors": ["internal server error"]},
    )
    with pytest.raises(VaultLeaseError, match="500"):
        backend.put("github", _stored())


def test_get_returns_cached_secret_from_put(
    backend: VaultLeaseBackend, fake_http: _FakeHttpClient
) -> None:
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
