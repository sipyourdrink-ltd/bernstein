"""Tests for structured SSE event types."""

from __future__ import annotations

import json
import time

from bernstein.core.server.sse_events import SSEEvent, SSEEventType


class TestSSEEventType:
    """Tests for the SSEEventType enum."""

    def test_all_14_event_types_defined(self) -> None:
        assert len(SSEEventType) == 14

    def test_event_type_values_are_dotted(self) -> None:
        for member in SSEEventType:
            assert "." in member.value, f"{member.name} should have dotted value"

    def test_event_type_is_str_enum(self) -> None:
        assert isinstance(SSEEventType.TASK_CREATED, str)
        assert SSEEventType.TASK_CREATED == "task.created"


class TestSSEEventToSSE:
    """Tests for SSE wire format output."""

    def test_to_sse_starts_with_event(self) -> None:
        event = SSEEvent.task_created("t1", "do stuff", "backend", "medium")
        wire = event.to_sse()
        assert wire.startswith("event: task.created\n")

    def test_to_sse_has_data_line(self) -> None:
        event = SSEEvent.task_created("t1", "do stuff", "backend", "medium")
        wire = event.to_sse()
        lines = wire.strip().split("\n")
        assert lines[1].startswith("data: ")

    def test_to_sse_ends_with_double_newline(self) -> None:
        event = SSEEvent.task_created("t1", "do stuff", "backend", "medium")
        wire = event.to_sse()
        assert wire.endswith("\n\n")

    def test_to_sse_data_is_valid_json(self) -> None:
        event = SSEEvent.task_created("t1", "do stuff", "backend", "medium")
        wire = event.to_sse()
        data_line = wire.strip().split("\n")[1]
        payload = json.loads(data_line.removeprefix("data: "))
        assert isinstance(payload, dict)

    def test_to_sse_payload_contains_timestamp(self) -> None:
        event = SSEEvent.task_created("t1", "do stuff", "backend", "medium")
        wire = event.to_sse()
        data_line = wire.strip().split("\n")[1]
        payload = json.loads(data_line.removeprefix("data: "))
        assert "timestamp" in payload
        assert isinstance(payload["timestamp"], float)


class TestSSEEventTimestamp:
    """Tests for auto-generated timestamps."""

    def test_timestamp_auto_generated(self) -> None:
        before = time.time()
        event = SSEEvent.task_created("t1", "goal", "role", "low")
        after = time.time()
        assert before <= event.timestamp <= after

    def test_timestamp_preserved_when_provided(self) -> None:
        event = SSEEvent(SSEEventType.TASK_CREATED, {"task_id": "t1"}, timestamp=123.0)
        assert event.timestamp == 123.0


class TestSSEEventFactories:
    """Tests for each factory method."""

    def test_task_created(self) -> None:
        event = SSEEvent.task_created("t1", "build API", "backend", "high")
        assert event.event_type == SSEEventType.TASK_CREATED
        assert event.data["task_id"] == "t1"
        assert event.data["goal"] == "build API"
        assert event.data["role"] == "backend"
        assert event.data["complexity"] == "high"

    def test_task_completed(self) -> None:
        event = SSEEvent.task_completed("t1", "agent-1", "opus", 42.567, 0.12345)
        assert event.event_type == SSEEventType.TASK_COMPLETED
        assert event.data["task_id"] == "t1"
        assert event.data["agent_id"] == "agent-1"
        assert event.data["model"] == "opus"
        assert event.data["duration_s"] == 42.57
        assert event.data["cost_usd"] == 0.1235

    def test_task_failed(self) -> None:
        event = SSEEvent.task_failed("t1", "timeout", True)
        assert event.event_type == SSEEventType.TASK_FAILED
        assert event.data["task_id"] == "t1"
        assert event.data["reason"] == "timeout"
        assert event.data["will_retry"] is True

    def test_agent_spawned(self) -> None:
        event = SSEEvent.agent_spawned("a1", "t1", "sonnet", "claude")
        assert event.event_type == SSEEventType.AGENT_SPAWNED
        assert event.data["agent_id"] == "a1"
        assert event.data["task_id"] == "t1"
        assert event.data["model"] == "sonnet"
        assert event.data["adapter"] == "claude"

    def test_gate_result_passed(self) -> None:
        event = SSEEvent.gate_result("t1", "ruff", passed=True, details="clean")
        assert event.event_type == SSEEventType.GATE_PASSED
        assert event.data["passed"] is True
        assert event.data["gate"] == "ruff"

    def test_gate_result_failed(self) -> None:
        event = SSEEvent.gate_result("t1", "pytest", passed=False, details="3 failures")
        assert event.event_type == SSEEventType.GATE_FAILED
        assert event.data["passed"] is False

    def test_cost_update(self) -> None:
        event = SSEEvent.cost_update(1.23456, 10.0, 12.3456)
        assert event.event_type == SSEEventType.COST_UPDATE
        assert event.data["total_usd"] == 1.2346
        assert event.data["budget_usd"] == 10.0
        assert event.data["budget_pct"] == 12.3

    def test_merge_completed(self) -> None:
        event = SSEEvent.merge_completed("t1", "feat/x", "abc1234")
        assert event.event_type == SSEEventType.MERGE_COMPLETED
        assert event.data["branch"] == "feat/x"
        assert event.data["commit_sha"] == "abc1234"

    def test_run_completed(self) -> None:
        event = SSEEvent.run_completed(10, 8, 2, 5.6789)
        assert event.event_type == SSEEventType.RUN_COMPLETED
        assert event.data["total_tasks"] == 10
        assert event.data["passed"] == 8
        assert event.data["failed"] == 2
        assert event.data["total_cost_usd"] == 5.6789
