"""Authentication attempt budgets and failure backoff for A2A routes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from bernstein.core.routes import a2a_jsonrpc
from bernstein.core.security import auth_rate_limiter
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

TOKEN_PATH = "/a2a/v1/oauth/token"
RPC_PATH = "/a2a/v1"
PREFIX_PAIRS = [("", ""), ("/api/v1", "/api/v1"), ("", "/api/v1"), ("/api/v1", "")]
RPC_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "unknown"}}


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("BERNSTEIN_A2A_SERVER_ENABLED", "1")
    monkeypatch.setenv("BERNSTEIN_A2A_API_KEYS", "alice=test-key")
    monkeypatch.setenv("BERNSTEIN_A2A_OAUTH_CLIENTS", "client=test-secret")
    monkeypatch.setenv("BERNSTEIN_A2A_OAUTH_SIGNING_SECRET", "test-signing-secret")
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.setenv("BERNSTEIN_AGENT_CARD_KEY_DIR", str(tmp_path / "keys"))
    monkeypatch.delenv("BERNSTEIN_TRUSTED_PROXY_IPS", raising=False)
    return create_app(jsonl_path=tmp_path / ".sdd" / "tasks.jsonl")


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [1000.0]
    monkeypatch.setattr(auth_rate_limiter, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


def client_for(app: FastAPI, ip: str = "198.51.100.1") -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app, client=(ip, 1234)), base_url="http://test")


def credentials(secret: str = "test-secret") -> dict[str, str]:
    return {"grant_type": "client_credentials", "client_id": "client", "client_secret": secret}


def assert_limited(response: Response, delay: int | None = None) -> None:
    assert response.status_code == 429, response.text
    assert response.json() == {"detail": "Too many requests. Please try again later."}
    retry_after = response.headers["Retry-After"]
    assert retry_after.isdigit()
    assert int(retry_after) >= 1
    if delay is not None:
        assert int(retry_after) == delay


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["198.51.100.1", "127.0.0.1"])
@pytest.mark.parametrize("first_prefix,last_prefix", PREFIX_PAIRS)
async def test_token_attempt_budget_and_client_isolation(
    app: FastAPI, now: list[float], ip: str, first_prefix: str, last_prefix: str
) -> None:
    async with client_for(app, ip) as client, client_for(app, "198.51.100.2") as other:
        for _ in range(10):
            response = await client.post(first_prefix + TOKEN_PATH, data=credentials("wrong"))
            assert response.status_code == 401
            now[0] += 1
            # Clear credential backoff through a valid RPC call; retain token attempts.
            cleared = await client.post(RPC_PATH, headers={"X-API-Key": "test-key"}, json=RPC_BODY)
            assert cleared.status_code == 200
        assert_limited(await client.post(last_prefix + TOKEN_PATH, data=credentials()))
        assert (await other.post(last_prefix + TOKEN_PATH, data=credentials())).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
async def test_token_success_and_failure_backoff(app: FastAPI, now: list[float], prefix: str) -> None:
    async with client_for(app) as client:
        response = await client.post(prefix + TOKEN_PATH, data=credentials())
        assert response.status_code == 200
        token = response.json()["access_token"]
        assert token
        bearer = await client.post(RPC_PATH, headers={"Authorization": f"Bearer {token}"}, json=RPC_BODY)
        assert bearer.status_code == 200
        assert (await client.post(prefix + TOKEN_PATH, data=credentials("wrong"))).status_code == 401
        assert_limited(await client.post(prefix + TOKEN_PATH, data=credentials()), 1)
        now[0] += 1
        assert (await client.post(prefix + TOKEN_PATH, data=credentials())).status_code == 200
        assert (await client.post(prefix + TOKEN_PATH, data=credentials("wrong"))).status_code == 401
        assert_limited(await client.post(prefix + TOKEN_PATH, data=credentials()), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_prefix,last_prefix", PREFIX_PAIRS)
@pytest.mark.parametrize("bad_headers", [{"X-API-Key": "wrong"}, {"Authorization": "Bearer wrong"}, {}])
async def test_rpc_failure_backoff_and_recovery(
    app: FastAPI, now: list[float], first_prefix: str, last_prefix: str, bad_headers: dict[str, str]
) -> None:
    good_headers = {"X-API-Key": "test-key"}
    async with client_for(app) as client, client_for(app, "198.51.100.2") as other:
        for delay in (1, 2, 4):
            bad = await client.post(first_prefix + RPC_PATH, headers=bad_headers, json=RPC_BODY)
            assert bad.status_code == 401
            assert "error" in bad.json()
            assert "WWW-Authenticate" in bad.headers
            assert_limited(await client.post(last_prefix + RPC_PATH, headers=good_headers, json=RPC_BODY), delay)
            now[0] += 0.25
            assert_limited(await client.post(last_prefix + RPC_PATH, headers=good_headers, json=RPC_BODY), delay)
            assert (await other.post(RPC_PATH, headers=good_headers, json=RPC_BODY)).status_code == 200
            now[0] += delay
        recovered = await client.post(last_prefix + RPC_PATH, headers=good_headers, json=RPC_BODY)
        assert recovered.status_code == 200
        assert recovered.json()["jsonrpc"] == "2.0"
        assert "error" in recovered.json()
        assert (await client.post(RPC_PATH, headers=bad_headers, json=RPC_BODY)).status_code == 401
        assert_limited(await client.post(RPC_PATH, headers=good_headers, json=RPC_BODY), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
async def test_successful_rpc_calls_do_not_consume_attempts(app: FastAPI, now: list[float], prefix: str) -> None:
    # Loopback excludes the separate generic write limit from this 36-request check.
    async with client_for(app, "127.0.0.1") as client:
        for _ in range(25):
            response = await client.post(prefix + RPC_PATH, headers={"X-API-Key": "test-key"}, json=RPC_BODY)
            assert response.status_code == 200
            assert response.json()["jsonrpc"] == "2.0"
        # The shared authentication budget is still available to the token endpoint.
        for _ in range(10):
            assert (await client.post(TOKEN_PATH, data=credentials())).status_code == 200
        assert_limited(await client.post(TOKEN_PATH, data=credentials()))


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [TOKEN_PATH, RPC_PATH])
@pytest.mark.parametrize("success", [False, True])
async def test_each_credential_verification_records_one_result(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str, success: bool
) -> None:
    record = Mock(wraps=auth_rate_limiter.record_auth_result)
    monkeypatch.setattr(a2a_jsonrpc, "record_auth_result", record, raising=False)
    async with client_for(app) as client:
        if path == TOKEN_PATH:
            response = await client.post(path, data=credentials("test-secret" if success else "wrong"))
        else:
            response = await client.post(path, headers={"X-API-Key": "test-key" if success else "wrong"}, json=RPC_BODY)
    assert response.status_code == (200 if success else 401)
    record.assert_called_once()
    assert record.call_args.kwargs == {"success": success}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "wrong_grant", "missing_fields"])
async def test_token_preverification_rejections_record_no_result(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    record = Mock(wraps=auth_rate_limiter.record_auth_result)
    monkeypatch.setattr(a2a_jsonrpc, "record_auth_result", record, raising=False)
    data = credentials()
    if case == "disabled":
        monkeypatch.setenv("BERNSTEIN_A2A_SERVER_ENABLED", "0")
    elif case == "wrong_grant":
        data["grant_type"] = "password"
    else:
        del data["client_secret"]
    async with client_for(app) as client:
        response = await client.post(TOKEN_PATH, data=data)
    assert response.status_code == (404 if case == "disabled" else 400)
    record.assert_not_called()


@pytest.mark.parametrize("bucket", ["auth", "seed:custom"])
def test_retry_after_observes_backoff_without_recording_attempts(now: list[float], bucket: str) -> None:
    limiter = auth_rate_limiter.AuthRateLimiter()
    assert limiter.retry_after("unknown", bucket=bucket) is None
    assert not limiter._hits
    assert limiter.check("client", bucket=bucket) is None
    limiter.record_failure("client", bucket=bucket, base_delay=4)
    hits = {key: list(values) for key, values in limiter._hits.items()}
    calls = limiter._call_count
    assert limiter.retry_after("client", bucket=bucket) == 4
    now[0] += 1.5
    assert limiter.retry_after("client", bucket=bucket) == 2.5
    now[0] += 2
    assert limiter.retry_after("client", bucket=bucket) == 1.0
    now[0] += 0.5
    assert limiter.retry_after("client", bucket=bucket) is None
    assert limiter._hits == hits
    assert limiter._call_count == calls
