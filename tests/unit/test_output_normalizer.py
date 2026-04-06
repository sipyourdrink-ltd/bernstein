"""Tests for agent output normalization (AGENT-010)."""

from __future__ import annotations

from bernstein.core.output_normalizer import EventType, NormalizedEvent, OutputNormalizer


class TestNormalizedEvent:
    def test_to_log_line_with_adapter(self) -> None:
        event = NormalizedEvent(
            event_type=EventType.LOG,
            message="hello world",
            adapter="claude",
        )
        assert event.to_log_line() == "[claude] LOG: hello world"

    def test_to_log_line_without_adapter(self) -> None:
        event = NormalizedEvent(
            event_type=EventType.ERROR,
            message="something broke",
        )
        assert event.to_log_line() == "[unknown] ERROR: something broke"

    def test_to_log_line_with_progress(self) -> None:
        event = NormalizedEvent(
            event_type=EventType.PROGRESS,
            message="working",
            adapter="codex",
            progress_pct=42,
        )
        assert event.to_log_line() == "[codex] PROGRESS (42%): working"


class TestOutputNormalizer:
    def setup_method(self) -> None:
        self.normalizer = OutputNormalizer()

    def test_empty_line(self) -> None:
        event = self.normalizer.parse_line("")
        assert event.event_type == EventType.UNKNOWN

    def test_completion_detected(self) -> None:
        event = self.normalizer.parse_line(
            "Agent completed task successfully",
            adapter="claude",
        )
        assert event.event_type == EventType.COMPLETION
        assert event.adapter == "claude"

    def test_completion_json_status(self) -> None:
        event = self.normalizer.parse_line('"status": "done"')
        assert event.event_type == EventType.COMPLETION

    def test_error_detected(self) -> None:
        event = self.normalizer.parse_line("Fatal error occurred in module")
        assert event.event_type == EventType.ERROR

    def test_error_failure_pattern(self) -> None:
        event = self.normalizer.parse_line("Failed to spawn agent process")
        assert event.event_type == EventType.ERROR

    def test_progress_percentage(self) -> None:
        event = self.normalizer.parse_line("Processing... 75% complete")
        assert event.event_type == EventType.PROGRESS
        assert event.progress_pct == 75

    def test_progress_fraction(self) -> None:
        event = self.normalizer.parse_line("Completed 3/4 steps")
        assert event.event_type == EventType.PROGRESS
        assert event.progress_pct == 75

    def test_tool_use_detected(self) -> None:
        event = self.normalizer.parse_line("using tool: Read file config.py")
        assert event.event_type == EventType.TOOL_USE

    def test_file_change_detected(self) -> None:
        event = self.normalizer.parse_line("Modified file main.py")
        assert event.event_type == EventType.FILE_CHANGE

    def test_warning_detected(self) -> None:
        event = self.normalizer.parse_line("Warning: disk space low")
        assert event.event_type == EventType.WARNING

    def test_plain_log_line(self) -> None:
        event = self.normalizer.parse_line("Starting agent initialization")
        assert event.event_type == EventType.LOG

    def test_raw_line_preserved(self) -> None:
        original = "  some output with spaces  "
        event = self.normalizer.parse_line(original)
        assert event.raw_line == original

    def test_session_id_preserved(self) -> None:
        event = self.normalizer.parse_line(
            "hello",
            adapter="claude",
            session_id="sess-123",
        )
        assert event.session_id == "sess-123"

    def test_parse_lines(self) -> None:
        lines = [
            "Agent started",
            "50% progress",
            "Task complete successfully",
        ]
        events = self.normalizer.parse_lines(lines, adapter="mock")
        assert len(events) == 3
        assert events[0].event_type == EventType.LOG
        assert events[1].event_type == EventType.PROGRESS
        assert events[2].event_type == EventType.COMPLETION

    def test_mock_agent_completed(self) -> None:
        event = self.normalizer.parse_line("Mock agent completed successfully")
        assert event.event_type == EventType.COMPLETION

    def test_progress_capped_at_100(self) -> None:
        event = self.normalizer.parse_line("progress: 150")
        assert event.event_type == EventType.PROGRESS
        assert event.progress_pct == 100
