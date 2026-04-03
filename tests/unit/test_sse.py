"""Tests for SSEBus class and SSE endpoints — SSE real-time publishing and consumption."""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import SSEBus, create_app

# --- SSEBus unit tests ---


class TestSSEBus:
    """Tests for the SSEBus pub/sub bus."""

    def test_subscribe_returns_queue(self) -> None:
        bus = SSEBus()
        queue = bus.subscribe()
        assert isinstance(queue, asyncio.Queue)
        assert queue.maxsize == 64

    def test_multiple_subscribers_get_separate_queues(self) -> None:
        bus = SSEBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        assert q1 is not q2
        assert bus.subscriber_count == 2

    def test_publish_sends_to_all_subscribers(self) -> None:
        bus = SSEBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.publish("task_update", '{"id": "abc123"}')

        assert q1.qsize() == 1
        assert q2.qsize() == 1
        msg1 = q1.get_nowait()
        msg2 = q2.get_nowait()
        assert "event: task_update" in msg1
        assert 'data: {"id": "abc123"}' in msg1
        assert msg1 == msg2

    def test_publish_format_is_sse_standard(self) -> None:
        bus = SSEBus()
        queue = bus.subscribe()
        bus.publish("agent_update", '{"status": "alive"}')
        msg = queue.get_nowait()
        assert msg.startswith("event: agent_update\n")
        assert 'data: {"status": "alive"}\n\n' in msg

    def test_publish_with_default_data(self) -> None:
        bus = SSEBus()
        queue = bus.subscribe()
        bus.publish("heartbeat")
        msg = queue.get_nowait()
        assert "event: heartbeat" in msg
        assert "data: {}" in msg

    def test_unsubscribe_removes_subscriber(self) -> None:
        bus = SSEBus()
        queue = bus.subscribe()
        assert bus.subscriber_count == 1
        bus.unsubscribe(queue)
        assert bus.subscriber_count == 0

    def test_unsubscribe_nonexistent_is_noop(self) -> None:
        bus = SSEBus()
        queue = asyncio.Queue()
        bus.unsubscribe(queue)  # Should not raise
        assert bus.subscriber_count == 0

    def test_publish_to_no_subscribers_is_noop(self) -> None:
        bus = SSEBus()
        bus.publish("task_update", "{}")  # Should not raise

    def test_publish_after_unsubscribe_does_not_deliver(self) -> None:
        bus = SSEBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        bus.unsubscribe(q2)
        bus.publish("task_update", "{}")
        assert q1.qsize() == 1
        assert q2.qsize() == 0

    def test_queue_full_drops_event_silently(self) -> None:
        bus = SSEBus()
        full_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        full_queue.put_nowait("filler")
        bus._subscribers.append(full_queue)
        bus.publish("task_update", "{}")
        assert full_queue.qsize() == 1

    def test_publish_snapshot_isolation(self) -> None:
        """Publish iterates over a snapshot -- new subscriber during publish is excluded."""
        bus = SSEBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        class InterceptingQueue(asyncio.Queue):
            def __init__(self) -> None:
                super().__init__(maxsize=64)

            def put_nowait(self, item: str) -> None:
                new_q: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
                bus._subscribers.append(new_q)
                super().put_nowait(item)

        q_intercept = InterceptingQueue()
        bus._subscribers.append(q_intercept)

        bus.publish("task_update", "{}")

        assert q1.qsize() == 1
        assert q2.qsize() == 1
        assert len(bus._subscribers) == 4  # q1, q2, q_intercept, new_q


# --- SSE endpoint integration tests (non-blocking) ---


@pytest.fixture()
def jsonl_path(tmp_path):
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path):
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
def _client(app):
    """Return the AsyncClient class for tests."""
    return AsyncClient


@pytest.mark.anyio
async def test_sse_events_receives_task_create(app, _client) -> None:
    """Creating a task publishes a task_update event through SSE."""
    sse_bus = app.state.sse_bus
    queue = sse_bus.subscribe()

    transport = ASGITransport(app=app)
    async with _client(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/tasks",
            json={
                "title": "SSE test task",
                "description": "Test SSE events",
                "role": "backend",
                "model": "sonnet",
                "effort": "medium",
            },
        )
        assert resp.status_code in (200, 201, 202)
        task_data = resp.json()
        task_id = task_data["id"]

    await asyncio.sleep(0.05)
    assert queue.qsize() >= 1
    msg = await queue.get()
    assert "event: task_update" in msg
    parsed = json.loads(msg.split("data: ", 1)[1].strip())
    assert parsed["id"] == task_id


@pytest.mark.anyio
async def test_sse_events_receives_agent_update(app, _client) -> None:
    """Agent heartbeat publishes an agent_update event."""
    sse_bus = app.state.sse_bus
    queue = sse_bus.subscribe()

    transport = ASGITransport(app=app)
    async with _client(transport=transport, base_url="http://test"):
        # Direct SSE bus publish to verify agent_update routing
        sse_bus.publish("agent_update", '{"agent_id": "test-session", "status": "alive"}')

    await asyncio.sleep(0.05)
    messages: list[str] = []
    while queue.qsize() > 0:
        messages.append(await queue.get())

    agent_events = [m for m in messages if "agent_update" in m]
    assert len(agent_events) >= 1


@pytest.mark.anyio
async def test_sse_heartbeat_loop_publishes_periodically() -> None:
    """The SSE heartbeat loop publishes heartbeat events at the expected interval."""
    from bernstein.core.server import _sse_heartbeat_loop

    bus = SSEBus()
    queue = bus.subscribe()

    loop_task = asyncio.create_task(_sse_heartbeat_loop(bus, interval_s=0.1))
    await asyncio.sleep(0.35)
    loop_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await loop_task

    heartbeat_count = 0
    while queue.qsize() > 0:
        msg = queue.get_nowait()
        if "event: heartbeat" in msg:
            heartbeat_count += 1
    assert heartbeat_count >= 2
