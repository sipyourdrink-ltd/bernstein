"""WEB-006: Tests for WebSocket live dashboard endpoint."""

from __future__ import annotations

import json

from bernstein.core.routes.websocket import _parse_sse_message
from bernstein.core.server import SSEBus


class TestParseSSEMessage:
    """Tests for SSE message parsing helper."""

    def test_parse_valid_message(self) -> None:
        raw = 'event: task_update\ndata: {"id": "abc"}\n\n'
        result = _parse_sse_message(raw)
        assert result is not None
        assert result["event"] == "task_update"
        assert result["data"] == {"id": "abc"}

    def test_parse_heartbeat(self) -> None:
        raw = "event: heartbeat\ndata: {}\n\n"
        result = _parse_sse_message(raw)
        assert result is not None
        assert result["event"] == "heartbeat"
        assert result["data"] == {}

    def test_parse_no_event_returns_none(self) -> None:
        raw = "data: {}\n\n"
        result = _parse_sse_message(raw)
        assert result is None

    def test_parse_invalid_json_data(self) -> None:
        raw = "event: update\ndata: not-json\n\n"
        result = _parse_sse_message(raw)
        assert result is not None
        assert result["event"] == "update"
        assert result["data"] == {"raw": "not-json"}

    def test_parse_empty_data(self) -> None:
        raw = "event: ping\n\n"
        result = _parse_sse_message(raw)
        assert result is not None
        assert result["event"] == "ping"
        assert result["data"] == {}


class TestWebSocketIntegration:
    """Integration tests verifying WebSocket message flow through SSEBus."""

    def test_sse_bus_message_format_for_ws(self) -> None:
        """Verify that SSEBus messages parse correctly for WebSocket relay."""
        bus = SSEBus()
        queue = bus.subscribe()
        bus.publish("task_update", json.dumps({"id": "t1", "status": "done"}))

        raw = queue.get_nowait()
        parsed = _parse_sse_message(raw)
        assert parsed is not None
        assert parsed["event"] == "task_update"
        assert parsed["data"]["id"] == "t1"
        assert parsed["data"]["status"] == "done"

    def test_multiple_events_parse_correctly(self) -> None:
        """Multiple different events should all parse correctly."""
        bus = SSEBus()
        queue = bus.subscribe()

        bus.publish("task_update", '{"id": "t1"}')
        bus.publish("agent_update", '{"agent": "a1"}')
        bus.publish("heartbeat")

        events = []
        while not queue.empty():
            raw = queue.get_nowait()
            parsed = _parse_sse_message(raw)
            if parsed:
                events.append(parsed["event"])

        assert events == ["task_update", "agent_update", "heartbeat"]
