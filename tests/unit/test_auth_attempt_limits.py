"""Authentication attempt limits on canonical and versioned routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.seed import parse_seed
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.security import auth_rate_limiter
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


ENDPOINTS = [
    ("POST", "/dashboard/auth/login", {}),
    ("GET", "/auth/login", {}),
    ("GET", "/auth/oidc/callback", {}),
    ("POST", "/auth/saml/acs", {}),
    ("POST", "/auth/cli/device", {}),
    ("POST", "/auth/cli/token", {"device_code": "unknown"}),
    ("POST", "/auth/cli/authorize", {"user_code": "unknown"}),
]
PREFIX_PAIRS = [("", ""), ("/api/v1", "/api/v1"), ("", "/api/v1"), ("/api/v1", "")]


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    monkeypatch.delenv("BERNSTEIN_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


def client_for(app: FastAPI, peer: str = "127.0.0.1") -> AsyncClient:
    # Auth limits also apply to loopback, which bypasses generic request limits.
    return AsyncClient(transport=ASGITransport(app=app, client=(peer, 1234)), base_url="http://test")


def assert_limited(response: Any) -> None:
    assert response.status_code == 429, response.text
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("first_prefix,last_prefix", PREFIX_PAIRS)
async def test_default_attempt_limit(
    app: FastAPI, method: str, path: str, body: dict[str, str], first_prefix: str, last_prefix: str
) -> None:
    async with client_for(app) as client:
        for _ in range(10):
            response = await client.request(method, first_prefix + path, json=body)
            assert response.status_code == (200 if path.startswith("/dashboard") else 404)
        assert_limited(await client.request(method, last_prefix + path, json=body))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("requests", [2, 12])
async def test_seed_overrides_default_attempt_limit(
    app: FastAPI, tmp_path: Path, method: str, path: str, body: dict[str, str], requests: int
) -> None:
    seed = tmp_path / "bernstein.yaml"
    seed.write_text(
        f"goal: Test\nrate_limit:\n  auth:\n    requests: {requests}\n"
        "    window_seconds: 17\n    paths: [/auth, /dashboard/auth]\n"
    )
    app.state.seed_config = parse_seed(seed)
    async with client_for(app) as client:
        for _ in range(requests):
            response = await client.request(method, path, json=body)
            assert response.status_code == (200 if path.startswith("/dashboard") else 404)
        response = await client.request(method, "/api/v1" + path, json=body)
        assert_limited(response)
        assert 1 <= int(response.headers["Retry-After"]) <= 17


@pytest.mark.asyncio
async def test_failed_dashboard_logins_back_off_per_client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_DASHBOARD_PASSWORD", "test-password")
    now = [1000.0]
    monkeypatch.setattr(auth_rate_limiter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    async with client_for(app) as alice, client_for(app, "127.0.0.2") as bob:
        path = "/dashboard/auth/login"
        assert (await alice.post(path, json={"password": "wrong"})).status_code == 401
        first = await alice.post(path, json={"password": "wrong"})
        assert_limited(first)
        assert first.headers["Retry-After"] == "1"
        now[0] += 1
        assert (await alice.post(path, json={"password": "wrong"})).status_code == 401
        # Another client's success must leave Alice's lockout in place.
        assert (await bob.post(path, json={"password": "test-password"})).status_code == 200
        second = await alice.post("/api/v1" + path, json={"password": "test-password"})
        assert_limited(second)
        assert second.headers["Retry-After"] == "2"
        now[0] += 2
        assert (await alice.post(path, json={"password": "test-password"})).status_code == 200
        assert (await alice.post(path, json={"password": "wrong"})).status_code == 401
        reset = await alice.post(path, json={"password": "wrong"})
        assert_limited(reset)
        assert reset.headers["Retry-After"] == "1"


@pytest.mark.asyncio
async def test_exhausted_attempt_windows_back_off(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(auth_rate_limiter, "time", SimpleNamespace(monotonic=lambda: now[0]))
    async with client_for(app) as client:
        for expected_delay in (60, 120, 240):
            for _ in range(10):
                assert (await client.get("/auth/login")).status_code == 404
            response = await client.get("/auth/login")
            assert_limited(response)
            assert int(response.headers["Retry-After"]) == expected_delay
            now[0] += expected_delay / 2
            still_blocked = await client.get("/api/v1/auth/login")
            assert_limited(still_blocked)
            assert int(still_blocked.headers["Retry-After"]) == expected_delay // 2
            now[0] += expected_delay / 2


@pytest.mark.asyncio
async def test_clients_and_applications_have_independent_attempt_budgets(app: FastAPI, tmp_path: Path) -> None:
    async with client_for(app) as alice:
        for _ in range(10):
            assert (await alice.get("/auth/login")).status_code == 404
        # Changing auth endpoints must not create a new default budget.
        assert_limited(await alice.post("/auth/cli/device", json={}))
    async with client_for(app, "127.0.0.2") as bob:
        assert (await bob.get("/auth/login")).status_code == 404
    other_app = create_app(jsonl_path=tmp_path / "other" / ".sdd" / "runtime" / "tasks.jsonl")
    async with client_for(other_app) as client:
        assert (await client.get("/auth/login")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True])
async def test_attempt_limit_uses_existing_proxy_identity(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    if trusted:
        monkeypatch.setenv("BERNSTEIN_TRUSTED_PROXY_IPS", "127.0.0.1,10.0.0.2")
    async with client_for(app) as client:
        for index in range(10):
            headers = {"X-Forwarded-For": f"192.0.2.{index},198.51.100.1,10.0.0.2"}
            assert (await client.get("/auth/login", headers=headers)).status_code == 404
        response = await client.get("/api/v1/auth/login", headers={"X-Forwarded-For": "198.51.100.1,10.0.0.2"})
        assert_limited(response)
        other = await client.get("/auth/login", headers={"X-Forwarded-For": "198.51.100.2,10.0.0.2"})
        if trusted:
            assert other.status_code == 404
        else:
            assert_limited(other)
