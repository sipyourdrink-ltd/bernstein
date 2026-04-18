"""Tests for the generic webhook task-creation route."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.webhook_signatures import sign_hmac_sha256
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


_WEBHOOK_SECRET = "top-secret"


@pytest.fixture()
def app(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):
    # audit-042: /webhook requires BERNSTEIN_WEBHOOK_SECRET to be
    # configured — the endpoint 503s without it.  Set a stable secret
    # so the "happy path" tests in this file can exercise the route.
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def app_with_auth(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    return create_app(jsonl_path=jsonl_path, auth_token="server-token")


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
async def auth_client(app_with_auth) -> AsyncClient:
    transport = ASGITransport(app=app_with_auth)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _signed_headers(
    body: bytes,
    secret: str = _WEBHOOK_SECRET,
    *,
    ts: int | None = None,
) -> dict[str, str]:
    """Build HMAC + timestamp headers for a generic-webhook request (audit-121)."""

    timestamp = int(time.time()) if ts is None else ts
    signed = f"{timestamp}.".encode() + body
    return {
        "content-type": "application/json",
        "x-bernstein-timestamp": str(timestamp),
        "x-bernstein-webhook-signature-256": sign_hmac_sha256(secret, signed, prefix="sha256="),
    }


@pytest.mark.anyio
async def test_generic_webhook_creates_task_with_defaults(client: AsyncClient) -> None:
    body = json.dumps({"title": "Fix flaky login", "description": "Investigate the login test flake."}).encode()
    response = await client.post("/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 201
    task = response.json()["task"]
    assert task["title"] == "Fix flaky login"
    assert task["role"] == "backend"
    assert task["priority"] == 2
    assert task["scope"] == "medium"
    assert task["complexity"] == "medium"


@pytest.mark.anyio
async def test_generic_webhook_is_public_when_server_auth_is_enabled(
    auth_client: AsyncClient,
) -> None:
    body = json.dumps({"title": "Create task", "description": "This should bypass bearer auth."}).encode()
    response = await auth_client.post("/webhook", content=body, headers=_signed_headers(body))

    assert response.status_code == 201
    assert response.json()["task"]["title"] == "Create task"


@pytest.mark.anyio
async def test_generic_webhook_enforces_hmac_only(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """audit-121: plaintext fallback is gone — HMAC + timestamp required."""

    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)
    body = json.dumps({"title": "Allowed", "description": "Correct HMAC signature."}).encode()

    missing = await client.post("/webhook", content=body, headers={"content-type": "application/json"})
    assert missing.status_code == 401

    plaintext = await client.post(
        "/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-bernstein-webhook-secret": _WEBHOOK_SECRET,
        },
    )
    assert plaintext.status_code == 401

    wrong_sig = dict(_signed_headers(body))
    wrong_sig["x-bernstein-webhook-signature-256"] = "sha256=deadbeef"
    rejected = await client.post("/webhook", content=body, headers=wrong_sig)
    assert rejected.status_code == 401

    allowed = await client.post("/webhook", content=body, headers=_signed_headers(body))
    assert allowed.status_code == 201
    assert allowed.json()["task"]["title"] == "Allowed"
