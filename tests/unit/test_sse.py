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
        assert queue.maxsize == 256

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

    def test_mark_read_updates_timestamp(self) -> None:
        """mark_read refreshes the subscriber's last-read timestamp."""
        import time

        bus = SSEBus(stale_timeout_s=0.01)
        queue = bus.subscribe()
        # Make it look stale
        bus._subscriber_last_read[id(queue)] = time.time() - 1.0
        # After mark_read the timestamp is fresh and cleanup_stale should keep it
        bus.mark_read(queue)
        removed = bus.cleanup_stale()
        assert removed == 0
        assert bus.subscriber_count == 1

    def test_cleanup_stale_removes_inactive_subscribers(self) -> None:
        """cleanup_stale evicts subscribers that haven't consumed messages."""
        bus = SSEBus(stale_timeout_s=0.0)
        queue = bus.subscribe()
        # Force last_read into the past so it appears stale
        bus._subscriber_last_read[id(queue)] = 0.0
        removed = bus.cleanup_stale()
        assert removed == 1
        assert bus.subscriber_count == 0

    def test_cleanup_stale_preserves_fresh_subscribers(self) -> None:
        """cleanup_stale does not remove recently-active subscribers."""
        bus = SSEBus(stale_timeout_s=60.0)
        queue = bus.subscribe()
        bus.mark_read(queue)
        removed = bus.cleanup_stale()
        assert removed == 0
        assert bus.subscriber_count == 1

    def test_cleanup_stale_mixed_subscribers(self) -> None:
        """cleanup_stale selectively removes only stale subscribers."""
        bus = SSEBus(stale_timeout_s=60.0)
        fresh_queue = bus.subscribe()
        stale_queue = bus.subscribe()

        bus.mark_read(fresh_queue)
        # Force stale queue's timestamp into the past
        bus._subscriber_last_read[id(stale_queue)] = 0.0

        removed = bus.cleanup_stale()
        assert removed == 1
        assert bus.subscriber_count == 1
        # Verify the fresh one survived
        bus.publish("task_update", "{}")
        assert fresh_queue.qsize() == 1

    # -- audit-122: SSE slow-client DoS mitigations -----------------------

    def test_default_stale_timeout_is_30_seconds(self) -> None:
        """audit-122: default stale timeout dropped from 120s -> 30s."""
        bus = SSEBus()
        assert bus._stale_timeout_s == 30.0
        assert SSEBus.STALE_TIMEOUT_S == 30.0

    def test_reconnect_limiter_blocks_after_three_in_window(self) -> None:
        """audit-122: 4th reconnect inside the window is rejected with PermissionError."""
        bus = SSEBus(reconnect_window_s=60.0, reconnect_cooldown_s=300.0)
        # Three reconnects are allowed...
        q1 = bus.subscribe(client_ip="10.0.0.1")
        q2 = bus.subscribe(client_ip="10.0.0.1")
        q3 = bus.subscribe(client_ip="10.0.0.1")
        assert bus.subscriber_count == 3
        # ...the fourth inside the same window is blocked.
        with pytest.raises(PermissionError):
            bus.subscribe(client_ip="10.0.0.1")
        # Cleanup: verify the known queues are tracked and unsubscribable.
        bus.unsubscribe(q1)
        bus.unsubscribe(q2)
        bus.unsubscribe(q3)

    def test_reconnect_limiter_tolerates_wifi_blip(self) -> None:
        """audit-122: 3 reconnects in 60s must NOT trigger the limiter.

        A real-world wifi drop causes one reconnect every ~30-60s. Three
        reconnects inside the window is within tolerance.
        """
        bus = SSEBus(reconnect_window_s=60.0, reconnect_cooldown_s=300.0)
        bus.subscribe(client_ip="192.168.1.42")
        bus.subscribe(client_ip="192.168.1.42")
        bus.subscribe(client_ip="192.168.1.42")
        # IP is at capacity but not yet blocked — is_blocked should be False.
        assert not bus.is_blocked("192.168.1.42")

    def test_reconnect_limiter_isolates_ips(self) -> None:
        """audit-122: one IP hitting the limit does not affect another IP."""
        bus = SSEBus()
        # Exhaust 10.0.0.1
        for _ in range(3):
            bus.subscribe(client_ip="10.0.0.1")
        with pytest.raises(PermissionError):
            bus.subscribe(client_ip="10.0.0.1")
        # 10.0.0.2 is unaffected.
        other = bus.subscribe(client_ip="10.0.0.2")
        assert other is not None
        assert bus.subscriber_count == 4  # 3 x .1 + 1 x .2

    def test_reconnect_cooldown_lifts_after_timeout(self) -> None:
        """audit-122: cooldown expiry clears the block."""
        bus = SSEBus(reconnect_window_s=60.0, reconnect_cooldown_s=1.0)
        # Force the cooldown via the internal API, then jump past expiry.
        for _ in range(3):
            bus.subscribe(client_ip="10.0.0.1")
        with pytest.raises(PermissionError):
            bus.subscribe(client_ip="10.0.0.1")
        assert bus.is_blocked("10.0.0.1")
        # Simulate cooldown expiry by calling is_blocked with a future ts.
        future = bus._reconnect_cooldown_until["10.0.0.1"] + 1.0
        assert not bus.is_blocked("10.0.0.1", now=future)

    def test_per_ip_buffer_budget_caps_total_events(self) -> None:
        """audit-122: per-IP buffer budget drops events once summed qsize
        exceeds max_buffer_per_ip, regardless of individual queue capacity.
        """
        bus = SSEBus(max_buffer=256, max_buffer_per_ip=4)
        q1 = bus.subscribe(client_ip="10.0.0.1")
        q2 = bus.subscribe(client_ip="10.0.0.1")
        # Publish 6 events — the per-IP cap (4) should limit total across both.
        for _ in range(6):
            bus.publish("task_update", "{}")
        total = q1.qsize() + q2.qsize()
        assert total <= 4  # budget enforced
        # And drops were recorded.
        assert bus.dropped_events_total > 0

    def test_per_ip_buffer_budget_isolated_across_ips(self) -> None:
        """audit-122: one IP's buffer pressure does not starve another IP."""
        bus = SSEBus(max_buffer=256, max_buffer_per_ip=2)
        q_a = bus.subscribe(client_ip="10.0.0.1")
        q_b = bus.subscribe(client_ip="10.0.0.2")
        for _ in range(5):
            bus.publish("task_update", "{}")
        # Each IP capped at 2 regardless of other IP's budget.
        assert q_a.qsize() == 2
        assert q_b.qsize() == 2

    def test_dropped_events_counter_tracks_queue_full(self) -> None:
        """audit-122: the dropped-events counter increments on QueueFull."""
        bus = SSEBus(max_buffer=1)
        queue = bus.subscribe(client_ip="10.0.0.1")
        bus.publish("task_update", "{}")
        assert queue.qsize() == 1
        # Second publish should fill-and-drop for the single-slot queue.
        bus.publish("task_update", "{}")
        assert bus.dropped_events_total >= 1

    def test_subscribe_without_ip_skips_per_ip_tracking(self) -> None:
        """audit-122: heartbeat/internal subscribers bypass the limiter."""
        bus = SSEBus()
        for _ in range(10):  # Would exceed the 3-per-window limit with an IP
            bus.subscribe()
        assert bus.subscriber_count == 10

    def test_buffered_for_ip_sums_across_queues(self) -> None:
        """audit-122: buffered_for_ip() reports the combined queue depth."""
        bus = SSEBus(max_buffer=256, max_buffer_per_ip=1024)
        q1 = bus.subscribe(client_ip="10.0.0.1")
        q2 = bus.subscribe(client_ip="10.0.0.1")
        bus.publish("task_update", "{}")
        bus.publish("task_update", "{}")
        assert bus.buffered_for_ip("10.0.0.1") == q1.qsize() + q2.qsize()


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


# --- audit-122: middleware integration ------------------------------------


@pytest.mark.anyio
async def test_sse_middleware_blocks_reconnect_flood(app, _client) -> None:
    """audit-122: the SSEReconnectLimiterMiddleware returns 429 on flood."""
    # Pre-seed the bus so the next /events GET is the 4th attempt from the
    # test client's IP and therefore over the limit.
    sse_bus = app.state.sse_bus
    # httpx ASGITransport ships with client=("127.0.0.1", 123) by default.
    client_ip = "127.0.0.1"
    for _ in range(3):
        sse_bus._record_connect_attempt(client_ip)

    transport = ASGITransport(app=app)
    async with _client(transport=transport, base_url="http://test") as c:
        resp = await c.get("/events")
        assert resp.status_code == 429
        payload = resp.json()
        assert payload["bucket"] == "sse_reconnect"
        assert resp.headers.get("retry-after") == "300"
