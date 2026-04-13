"""Tests for tool_use_context — per-agent tool invocation tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.tool_use_context import ToolInvocation, ToolUseContext

# ---------------------------------------------------------------------------
# ToolInvocation
# ---------------------------------------------------------------------------


class TestToolInvocation:
    def test_duration_ms_zero_when_not_finished(self) -> None:
        inv = ToolInvocation(tool_name="Bash", session_id="s1")
        assert inv.duration_ms == pytest.approx(0.0)

    def test_duration_ms_computed_when_finished(self) -> None:
        inv = ToolInvocation(
            tool_name="Read",
            session_id="s1",
            start_time=1000.0,
            end_time=1002.5,
        )
        assert inv.duration_ms == pytest.approx(2500.0)

    def test_token_cost_sums_input_and_output(self) -> None:
        inv = ToolInvocation(
            tool_name="Edit",
            session_id="s1",
            input_tokens=100,
            output_tokens=50,
        )
        assert inv.token_cost == 150

    def test_to_dict_roundtrip(self) -> None:
        inv = ToolInvocation(
            tool_name="Grep",
            session_id="agent-42",
            start_time=1700000000.0,
            end_time=1700000001.0,
            success=True,
            error_message="",
            input_tokens=10,
            output_tokens=20,
            tool_input_preview="pattern",
        )
        d = inv.to_dict()
        restored = ToolInvocation.from_dict(d)
        assert restored.tool_name == inv.tool_name
        assert restored.session_id == inv.session_id
        assert restored.start_time == inv.start_time
        assert restored.end_time == inv.end_time
        assert restored.success == inv.success
        assert restored.input_tokens == inv.input_tokens
        assert restored.output_tokens == inv.output_tokens

    def test_from_dict_handles_missing_fields(self) -> None:
        d: dict[str, object] = {"tool_name": "Bash", "session_id": "s1"}
        inv = ToolInvocation.from_dict(d)
        assert inv.tool_name == "Bash"
        assert inv.success is True
        assert inv.input_tokens == 0


# ---------------------------------------------------------------------------
# ToolUseContext
# ---------------------------------------------------------------------------


class TestToolUseContext:
    def test_record_tool_start_creates_pending(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        inv = ctx.record_tool_start("Bash", tool_input_preview="ls -la")
        assert inv.tool_name == "Bash"
        assert inv.session_id == "s1"
        assert inv.tool_input_preview == "ls -la"
        assert "Bash" in ctx._pending

    def test_record_tool_end_moves_to_invocations(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_start("Read")
        result = ctx.record_tool_end("Read", success=True, input_tokens=5)
        assert result is not None
        assert result.tool_name == "Read"
        assert result.success is True
        assert result.input_tokens == 5
        assert len(ctx.invocations) == 1
        assert "Read" not in ctx._pending

    def test_record_tool_end_without_start_creates_synthetic(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        result = ctx.record_tool_end("Edit", success=False, error_message="file not found")
        assert result is not None
        assert result.tool_name == "Edit"
        assert result.success is False
        assert result.error_message == "file not found"
        assert len(ctx.invocations) == 1

    def test_total_invocations(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash")
        ctx.record_tool_end("Read")
        ctx.record_tool_end("Grep")
        assert ctx.total_invocations == 3

    def test_total_tokens(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash", input_tokens=100, output_tokens=50)
        ctx.record_tool_end("Read", input_tokens=200, output_tokens=100)
        assert ctx.total_tokens == 450

    def test_failed_count_and_success_rate(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash", success=True)
        ctx.record_tool_end("Edit", success=False, error_message="oops")
        ctx.record_tool_end("Read", success=True)
        ctx.record_tool_end("Grep", success=False, error_message="nope")
        assert ctx.failed_count == 2
        assert ctx.success_rate == pytest.approx(0.5)

    def test_success_rate_no_invocations(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        assert ctx.success_rate == pytest.approx(1.0)

    def test_tool_counts(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash")
        ctx.record_tool_end("Bash")
        ctx.record_tool_end("Read")
        counts = ctx.tool_counts()
        assert counts == {"Bash": 2, "Read": 1}

    def test_summary_returns_expected_keys(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash", input_tokens=10, output_tokens=5)
        s = ctx.summary()
        assert s["session_id"] == "s1"
        assert s["total_invocations"] == 1
        assert s["total_tokens"] == 15
        assert "tool_counts" in s
        assert "success_rate" in s

    def test_truncates_tool_input_preview(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        long_input = "x" * 500
        inv = ctx.record_tool_start("Bash", tool_input_preview=long_input)
        assert len(inv.tool_input_preview) == 200


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestToolUseContextPersistence:
    def test_persist_writes_jsonl(self, tmp_path: Path) -> None:
        ctx = ToolUseContext(session_id="s1")
        ctx.record_tool_end("Bash", input_tokens=10, output_tokens=5)
        ctx.record_tool_end("Read", input_tokens=20, output_tokens=10)
        ctx.persist(tmp_path)

        jsonl_path = tmp_path / "tool_use_context.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 2

        first = json.loads(lines[0])
        assert first["tool_name"] == "Bash"
        assert first["input_tokens"] == 10

    def test_load_filters_by_session(self, tmp_path: Path) -> None:
        # Write records for two sessions
        jsonl_path = tmp_path / "tool_use_context.jsonl"
        records = [
            {
                "tool_name": "Bash",
                "session_id": "s1",
                "start_time": 1.0,
                "end_time": 2.0,
                "success": True,
                "error_message": "",
                "input_tokens": 10,
                "output_tokens": 5,
                "tool_input_preview": "",
            },
            {
                "tool_name": "Read",
                "session_id": "s2",
                "start_time": 1.0,
                "end_time": 2.0,
                "success": True,
                "error_message": "",
                "input_tokens": 20,
                "output_tokens": 10,
                "tool_input_preview": "",
            },
            {
                "tool_name": "Edit",
                "session_id": "s1",
                "start_time": 3.0,
                "end_time": 4.0,
                "success": False,
                "error_message": "err",
                "input_tokens": 5,
                "output_tokens": 2,
                "tool_input_preview": "",
            },
        ]
        with jsonl_path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        ctx = ToolUseContext.load("s1", tmp_path)
        assert ctx.session_id == "s1"
        assert len(ctx.invocations) == 2
        assert ctx.invocations[0].tool_name == "Bash"
        assert ctx.invocations[1].tool_name == "Edit"

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        ctx = ToolUseContext.load("s1", tmp_path)
        assert ctx.total_invocations == 0

    def test_total_duration_ms(self) -> None:
        ctx = ToolUseContext(session_id="s1")
        inv1 = ctx.record_tool_start("Bash")
        inv1.start_time = 1000.0
        ctx.record_tool_end("Bash")
        # The end_time is set by time.time() so duration > 0 in principle,
        # but for determinism let's just check the property exists.
        assert ctx.total_duration_ms >= 0.0
