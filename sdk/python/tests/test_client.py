"""Tests for bernstein_sdk.client using respx to mock HTTP."""

from __future__ import annotations

import httpx
import pytest
import respx

from bernstein_sdk.client import AsyncBernsteinClient, BernsteinClient
from bernstein_sdk.models import TaskResponse, TaskStatus


BASE = "http://127.0.0.1:8052"

TASK_PAYLOAD = {
    "id": "abc123456789",
    "title": "Fix login bug",
    "role": "backend",
    "status": "open",
    "priority": 1,
    "scope": "small",
    "complexity": "medium",
    "description": "Auth service crashes on null email",
    "external_ref": "",
    "metadata": {},
    "created_at": 1700000000.0,
}


class TestBernsteinClientSync:
    @respx.mock
    def test_create_task(self) -> None:
        respx.post(f"{BASE}/tasks").mock(return_value=httpx.Response(201, json=TASK_PAYLOAD))

        with BernsteinClient(BASE) as client:
            task = client.create_task(title="Fix login bug", priority=1)

        assert task.id == "abc123456789"
        assert task.status == TaskStatus.OPEN
        assert task.priority == 1

    @respx.mock
    def test_get_task(self) -> None:
        respx.get(f"{BASE}/tasks/abc123456789").mock(
            return_value=httpx.Response(200, json=TASK_PAYLOAD)
        )
        with BernsteinClient(BASE) as client:
            task = client.get_task("abc123456789")
        assert task.title == "Fix login bug"

    @respx.mock
    def test_list_tasks_returns_list(self) -> None:
        respx.get(f"{BASE}/tasks").mock(
            return_value=httpx.Response(200, json=[TASK_PAYLOAD])
        )
        with BernsteinClient(BASE) as client:
            tasks = client.list_tasks()
        assert len(tasks) == 1
        assert isinstance(tasks[0], TaskResponse)

    @respx.mock
    def test_list_tasks_returns_wrapped_object(self) -> None:
        respx.get(f"{BASE}/tasks").mock(
            return_value=httpx.Response(200, json={"tasks": [TASK_PAYLOAD]})
        )
        with BernsteinClient(BASE) as client:
            tasks = client.list_tasks()
        assert len(tasks) == 1

    @respx.mock
    def test_complete_task(self) -> None:
        respx.post(f"{BASE}/tasks/abc123456789/complete").mock(
            return_value=httpx.Response(204)
        )
        with BernsteinClient(BASE) as client:
            client.complete_task("abc123456789", result_summary="Fixed")

    @respx.mock
    def test_fail_task(self) -> None:
        respx.post(f"{BASE}/tasks/abc123456789/fail").mock(
            return_value=httpx.Response(204)
        )
        with BernsteinClient(BASE) as client:
            client.fail_task("abc123456789", error="boom")

    @respx.mock
    def test_get_status(self) -> None:
        respx.get(f"{BASE}/status").mock(
            return_value=httpx.Response(
                200,
                json={"total": 5, "open": 2, "claimed": 1, "done": 1, "failed": 1, "agents": 1, "cost_usd": 0.12},
            )
        )
        with BernsteinClient(BASE) as client:
            summary = client.get_status()
        assert summary.total == 5
        assert summary.cost_usd == 0.12

    @respx.mock
    def test_health_true(self) -> None:
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200))
        with BernsteinClient(BASE) as client:
            assert client.health() is True

    @respx.mock
    def test_health_false_on_transport_error(self) -> None:
        respx.get(f"{BASE}/health").mock(side_effect=httpx.ConnectError("refused"))
        with BernsteinClient(BASE) as client:
            assert client.health() is False

    @respx.mock
    def test_raises_on_4xx(self) -> None:
        respx.post(f"{BASE}/tasks").mock(return_value=httpx.Response(422, json={"detail": "bad"}))
        with BernsteinClient(BASE) as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.create_task(title="Bad task")


class TestBernsteinClientAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_task_async(self) -> None:
        respx.post(f"{BASE}/tasks").mock(return_value=httpx.Response(201, json=TASK_PAYLOAD))

        async with AsyncBernsteinClient(BASE) as client:
            task = await client.create_task(title="Fix login bug", priority=1)

        assert task.id == "abc123456789"
        assert task.status == TaskStatus.OPEN

    @pytest.mark.asyncio
    @respx.mock
    async def test_health_async(self) -> None:
        respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200))
        async with AsyncBernsteinClient(BASE) as client:
            assert await client.health() is True
