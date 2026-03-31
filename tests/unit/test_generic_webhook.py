"""Tests for POST /webhook — generic inbound webhook task creation."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path):
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_WEBHOOK_PAYLOAD = {
    "title": "Fix login bug",
    "description": "Users cannot log in with SSO",
}


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_creates_task(client: AsyncClient) -> None:
    """POST /webhook returns 201 and a task nested in .task."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 201
    data = resp.json()
    assert "task" in data
    task = data["task"]
    assert task["title"] == "Fix login bug"
    assert task["status"] == "open"
    assert task["id"]


@pytest.mark.anyio
async def test_webhook_defaults_role_to_backend(client: AsyncClient) -> None:
    """POST /webhook uses 'backend' as the default role."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "backend"


@pytest.mark.anyio
async def test_webhook_accepts_explicit_role(client: AsyncClient) -> None:
    """POST /webhook respects an explicit role in the payload."""
    resp = await client.post("/webhook", json={**_WEBHOOK_PAYLOAD, "role": "qa"})
    assert resp.status_code == 201
    assert resp.json()["task"]["role"] == "qa"


@pytest.mark.anyio
async def test_webhook_task_is_retrievable(client: AsyncClient) -> None:
    """Task created via /webhook can be fetched from GET /tasks/{id}."""
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    task_id = resp.json()["task"]["id"]

    get_resp = await client.get(f"/tasks/{task_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == task_id


# ---------------------------------------------------------------------------
# Secret verification
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_webhook_no_secret_configured_allows_any(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """When BERNSTEIN_WEBHOOK_SECRET is unset, any request is accepted."""
    monkeypatch.delenv("BERNSTEIN_WEBHOOK_SECRET", raising=False)
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 201


@pytest.mark.anyio
async def test_webhook_correct_secret_accepted(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Correct X-Bernstein-Webhook-Secret header is accepted when secret is configured."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post(
        "/webhook",
        json=_WEBHOOK_PAYLOAD,
        headers={"X-Bernstein-Webhook-Secret": "s3cr3t"},
    )
    assert resp.status_code == 201


@pytest.mark.anyio
async def test_webhook_wrong_secret_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wrong secret returns 401."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post(
        "/webhook",
        json=_WEBHOOK_PAYLOAD,
        headers={"X-Bernstein-Webhook-Secret": "wrong"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_webhook_missing_secret_header_rejected(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing secret header returns 401 when secret is configured."""
    monkeypatch.setenv("BERNSTEIN_WEBHOOK_SECRET", "s3cr3t")
    resp = await client.post("/webhook", json=_WEBHOOK_PAYLOAD)
    assert resp.status_code == 401
