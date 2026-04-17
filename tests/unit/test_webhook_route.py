"""Tests for the generic webhook task-creation route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
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


_SECRET_HEADERS = {"x-bernstein-webhook-secret": _WEBHOOK_SECRET}


@pytest.mark.anyio
async def test_generic_webhook_creates_task_with_defaults(client: AsyncClient) -> None:
    response = await client.post(
        "/webhook",
        json={"title": "Fix flaky login", "description": "Investigate the login test flake."},
        headers=_SECRET_HEADERS,
    )

    assert response.status_code == 201
    task = response.json()["task"]
    assert task["title"] == "Fix flaky login"
    assert task["role"] == "backend"
    assert task["priority"] == 2
    assert task["scope"] == "medium"
    assert task["complexity"] == "medium"


@pytest.mark.anyio
async def test_generic_webhook_is_public_when_server_auth_is_enabled(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/webhook",
        json={"title": "Create task", "description": "This should bypass bearer auth."},
        headers=_SECRET_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["task"]["title"] == "Create task"


@pytest.mark.anyio
async def test_generic_webhook_enforces_shared_secret(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", _WEBHOOK_SECRET)

    missing = await client.post(
        "/webhook",
        json={"title": "Denied", "description": "Missing the shared secret header."},
    )
    assert missing.status_code == 401

    wrong = await client.post(
        "/webhook",
        json={"title": "Denied", "description": "Wrong shared secret header."},
        headers={"x-bernstein-webhook-secret": "wrong"},
    )
    assert wrong.status_code == 401

    allowed = await client.post(
        "/webhook",
        json={"title": "Allowed", "description": "Correct shared secret header."},
        headers=_SECRET_HEADERS,
    )
    assert allowed.status_code == 201
    assert allowed.json()["task"]["title"] == "Allowed"
