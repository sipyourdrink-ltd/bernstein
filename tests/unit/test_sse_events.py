"""Tests for structured SSE event types."""

from __future__ import annotations

import json

from pytest import approx

from bernstein.core.server.sse_events import SSEEvent, SSEEventType


class TestSSEEventType:
    def test_has_18_members(self) -> None:
        assert len(SSEEventType) == 18

    def test_event_type_values_are_dotted(self) -> None:
        for member in SSEEventType:
            if member == SSEEventType.HEARTBEAT:
                continue
            assert "." in member.value, f"{member.name} should have dotted value"

    def test_all_expected_types_exist(self) -> None:
        expected = {
            "TASK_CREATED",
            "TASK_CLAIMED",
            "TASK_COMPLETED",
            "TASK_FAILED",
            "TASK_RETRIED",
            "TASK_ARTIFACT",
            "TASK_PROGRESS",
            "ARTIFACT_PRODUCED",
            "AGENT_SPAWNED",
            "AGENT_EXITED",
            "GATE_RESULT",
            "COST_UPDATE",
            "MERGE_STARTED",
            "MERGE_COMPLETED",
            "RUN_STARTED",
            "RUN_COMPLETED",
            "MISSION_UPDATED",
            "HEARTBEAT",
        }
        actual = {m.name for m in SSEEventType}
        assert expected == actual

    def test_artifact_produced_factory(self) -> None:
        evt = SSEEvent.artifact_produced(
            uri="pkg://pypi/bernstein/3.9.0",
            entry_hash="sha256:abc",
            content_hash="sha256:def",
            actor="agent-release",
            verified=True,
        )
        assert evt.event == SSEEventType.ARTIFACT_PRODUCED
        assert evt.data["uri"] == "pkg://pypi/bernstein/3.9.0"
        assert evt.data["entry_hash"] == "sha256:abc"
        assert evt.data["verified"] is True
        assert json.loads(evt.to_sse().split("data: ", 1)[1].split("\n", 1)[0])["actor"] == "agent-release"

    def test_artifact_produced_carries_no_artifact_bytes(self) -> None:
        """The payload is identity and provenance; clients re-verify the content."""
        evt = SSEEvent.artifact_produced(
            uri="pkg://pypi/bernstein/3.9.0",
            entry_hash="sha256:abc",
            content_hash="sha256:def",
        )
        assert "content" not in evt.data
        assert set(evt.data) == {"uri", "entry_hash", "content_hash"}

    def test_mission_updated_factory(self) -> None:
        evt = SSEEvent.mission_updated("m-1", status_hash="abc", overall="active")
        assert evt.event == SSEEventType.MISSION_UPDATED
        assert evt.data["mission_id"] == "m-1"
        assert evt.data["status_hash"] == "abc"
        assert evt.data["overall"] == "active"
        assert json.loads(evt.to_sse().split("data: ", 1)[1].split("\n", 1)[0])["mission_id"] == "m-1"


class TestSSEEvent:
    def test_to_sse_wire_format(self) -> None:
        evt = SSEEvent(event=SSEEventType.TASK_CREATED, data={"task_id": "t1"}, timestamp=1000.0)
        wire = evt.to_sse()
        assert wire.startswith("event: task.created\n")
        assert "data: " in wire
        assert wire.endswith("\n\n")

    def test_to_sse_json_payload_valid(self) -> None:
        evt = SSEEvent(event=SSEEventType.TASK_COMPLETED, data={"task_id": "t2"}, timestamp=2000.0)
        wire = evt.to_sse()
        data_line = next(line for line in wire.split("\n") if line.startswith("data: "))
        payload = json.loads(data_line[6:])
        assert payload["task_id"] == "t2"
        assert payload["timestamp"] == approx(2000.0)

    def test_timestamp_auto_generated(self) -> None:
        evt = SSEEvent(event=SSEEventType.HEARTBEAT, data={})
        assert evt.timestamp > 0

    def test_id_field_in_wire_format(self) -> None:
        evt = SSEEvent(event=SSEEventType.HEARTBEAT, data={}, id="evt-42")
        wire = evt.to_sse()
        assert "id: evt-42\n" in wire

    def test_no_id_by_default(self) -> None:
        evt = SSEEvent(event=SSEEventType.HEARTBEAT, data={})
        wire = evt.to_sse()
        assert "id: " not in wire


class TestSSEEventFactories:
    def test_task_created(self) -> None:
        evt = SSEEvent.task_created(task_id="abc", title="Fix bug")
        assert evt.event == SSEEventType.TASK_CREATED
        assert evt.data["task_id"] == "abc"
        assert evt.data["title"] == "Fix bug"

    def test_task_completed(self) -> None:
        evt = SSEEvent.task_completed(task_id="abc", cost_usd=0.12)
        assert evt.event == SSEEventType.TASK_COMPLETED
        assert evt.data["task_id"] == "abc"
        assert evt.data["cost_usd"] == approx(0.12)

    def test_task_failed(self) -> None:
        evt = SSEEvent.task_failed(task_id="abc", reason="timeout")
        assert evt.event == SSEEventType.TASK_FAILED
        assert evt.data["reason"] == "timeout"

    def test_agent_spawned(self) -> None:
        evt = SSEEvent.agent_spawned(agent_id="a1", role="backend")
        assert evt.event == SSEEventType.AGENT_SPAWNED
        assert evt.data["agent_id"] == "a1"
        assert evt.data["role"] == "backend"

    def test_gate_result_passed(self) -> None:
        evt = SSEEvent.gate_result(gate_name="lint", passed=True)
        assert evt.event == SSEEventType.GATE_RESULT
        assert evt.data["passed"] is True

    def test_gate_result_failed(self) -> None:
        evt = SSEEvent.gate_result(gate_name="test", passed=False)
        assert evt.event == SSEEventType.GATE_RESULT
        assert evt.data["passed"] is False

    def test_cost_update(self) -> None:
        evt = SSEEvent.cost_update(total_usd=1.23)
        assert evt.event == SSEEventType.COST_UPDATE
        assert evt.data["total_usd"] == approx(1.23)

    def test_merge_completed(self) -> None:
        evt = SSEEvent.merge_completed(branch="feat/x", result="success")
        assert evt.event == SSEEventType.MERGE_COMPLETED
        assert evt.data["branch"] == "feat/x"

    def test_run_completed(self) -> None:
        evt = SSEEvent.run_completed(run_id="run-1")
        assert evt.event == SSEEventType.RUN_COMPLETED
        assert evt.data["run_id"] == "run-1"

    def test_extra_kwargs(self) -> None:
        evt = SSEEvent.task_created(task_id="t1", title="X", custom_field="val")
        assert evt.data["custom_field"] == "val"
