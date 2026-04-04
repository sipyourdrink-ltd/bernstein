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
        respx.post(f"{BASE}/tasks").mock(
            return_value=httpx.Response(201, json=TASK_PAYLOAD)
        )

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
                json={
                    "total": 5,
                    "open": 2,
                    "claimed": 1,
                    "done": 1,
                    "failed": 1,
                    "agents": 1,
                    "cost_usd": 0.12,
                },
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
        respx.post(f"{BASE}/tasks").mock(
            return_value=httpx.Response(422, json={"detail": "bad"})
        )
        with BernsteinClient(BASE) as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.create_task(title="Bad task")


class TestETagSync:
    """ETag-based conditional request tests for BernsteinClient."""

    @respx.mock
    def test_etag_stored_on_first_request(self) -> None:
        respx.get(f"{BASE}/tasks/abc123456789").mock(
            return_value=httpx.Response(
                200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}
            )
        )
        with BernsteinClient(BASE) as client:
            client.get_task("abc123456789")
            assert client._etag_cache.get("/tasks/abc123456789") is not None
            etag, _ = client._etag_cache["/tasks/abc123456789"]
            assert etag == '"v1"'

    @respx.mock
    def test_if_none_match_sent_on_second_request(self) -> None:
        route = respx.get(f"{BASE}/tasks/abc123456789")
        route.side_effect = [
            httpx.Response(200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
        with BernsteinClient(BASE) as client:
            client.get_task("abc123456789")
            client.get_task("abc123456789")

        # Second request must have carried the If-None-Match header
        second_req = route.calls[1].request
        assert second_req.headers.get("if-none-match") == '"v1"'

    @respx.mock
    def test_304_returns_cached_data_without_transfer(self) -> None:
        route = respx.get(f"{BASE}/tasks/abc123456789")
        route.side_effect = [
            httpx.Response(200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
        with BernsteinClient(BASE) as client:
            task_first = client.get_task("abc123456789")
            task_second = client.get_task("abc123456789")

        assert task_first.id == task_second.id == "abc123456789"
        assert route.call_count == 2  # two HTTP calls, one transfer

    @respx.mock
    def test_etag_updated_on_new_response(self) -> None:
        route = respx.get(f"{BASE}/tasks/abc123456789")
        updated_payload = {**TASK_PAYLOAD, "title": "Updated title"}
        route.side_effect = [
            httpx.Response(200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}),
            httpx.Response(200, json=updated_payload, headers={"ETag": '"v2"'}),
        ]
        with BernsteinClient(BASE) as client:
            client.get_task("abc123456789")
            task = client.get_task("abc123456789")
            etag, _ = client._etag_cache["/tasks/abc123456789"]

        assert etag == '"v2"'
        assert task.title == "Updated title"

    @respx.mock
    def test_list_tasks_304_returns_cached(self) -> None:
        route = respx.get(f"{BASE}/tasks")
        route.side_effect = [
            httpx.Response(200, json=[TASK_PAYLOAD], headers={"ETag": '"list-v1"'}),
            httpx.Response(304),
        ]
        with BernsteinClient(BASE) as client:
            tasks_first = client.list_tasks()
            tasks_second = client.list_tasks()

        assert len(tasks_first) == len(tasks_second) == 1
        assert route.call_count == 2

    @respx.mock
    def test_clear_etag_cache(self) -> None:
        respx.get(f"{BASE}/tasks/abc123456789").mock(
            return_value=httpx.Response(
                200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}
            )
        )
        with BernsteinClient(BASE) as client:
            client.get_task("abc123456789")
            assert client._etag_cache
            client.clear_etag_cache()
            assert not client._etag_cache


class TestBernsteinClientAsync:
    @pytest.mark.asyncio
    @respx.mock
    async def test_create_task_async(self) -> None:
        respx.post(f"{BASE}/tasks").mock(
            return_value=httpx.Response(201, json=TASK_PAYLOAD)
        )

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

    @pytest.mark.asyncio
    @respx.mock
    async def test_etag_304_async(self) -> None:
        route = respx.get(f"{BASE}/tasks/abc123456789")
        route.side_effect = [
            httpx.Response(200, json=TASK_PAYLOAD, headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
        async with AsyncBernsteinClient(BASE) as client:
            task_first = await client.get_task("abc123456789")
            task_second = await client.get_task("abc123456789")

        assert task_first.id == task_second.id == "abc123456789"
        second_req = route.calls[1].request
        assert second_req.headers.get("if-none-match") == '"v1"'
        assert route.call_count == 2
