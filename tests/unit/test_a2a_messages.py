"""Tests for extended A2A message exchange support."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, MockTransport, Request, Response

from bernstein.core.a2a import A2AHandler
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@dataclass(frozen=True)
class _ClientHarness:
    client: AsyncClient
    app: object


@pytest_asyncio.fixture()
async def harness(tmp_path: Path) -> AsyncGenerator[_ClientHarness, None]:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    app = create_app(jsonl_path=runtime_dir / "tasks.jsonl")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield _ClientHarness(client=client, app=app)


@pytest.mark.anyio
async def test_get_a2a_agents_returns_card(harness: _ClientHarness) -> None:
    response = await harness.client.get("/a2a/agents")

    assert response.status_code == 200
    assert response.json()["name"] == "bernstein-orchestrator"
    assert "a2a_message" in response.json()["capabilities"]


@pytest.mark.anyio
async def test_post_a2a_message_injects_progress_context(harness: _ClientHarness) -> None:
    create_response = await harness.client.post(
        "/tasks",
        json={
            "title": "Review patch",
            "description": "Review the pending patch set.",
            "role": "backend",
        },
    )
    task_id = create_response.json()["id"]

    response = await harness.client.post(
        "/a2a/message",
        json={
            "sender": "external-agent",
            "recipient": "bernstein-orchestrator",
            "content": "Focus on database migrations.",
            "task_id": task_id,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["direction"] == "inbound"
    assert payload["delivered"] is True

    task_response = await harness.client.get(f"/tasks/{task_id}")
    progress_log = task_response.json()["progress_log"]
    assert progress_log
    assert "Focus on database migrations." in progress_log[-1]["message"]


@pytest.mark.anyio
async def test_handler_sends_external_a2a_message() -> None:
    requests: list[Request] = []

    def handler(request: Request) -> Response:
        requests.append(request)
        return Response(200, json={"status": "ok"})

    client = AsyncClient(transport=MockTransport(handler), base_url="http://external.test")
    a2a_handler = A2AHandler(server_url="http://localhost:8052")
    try:
        message = await a2a_handler.send_message(
            sender="bernstein-orchestrator",
            recipient="external-reviewer",
            content="Please inspect task T-001.",
            task_id="T-001",
            external_endpoint="http://external.test",
            client=client,
        )
    finally:
        await client.aclose()

    assert message.direction == "outbound"
    assert message.delivered is True
    assert message.external_endpoint == "http://external.test"
    assert requests
    assert requests[0].url.path == "/a2a/message"
