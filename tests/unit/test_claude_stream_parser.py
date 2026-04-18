"""Tests for claude_stream_parser — Claude Code streaming event parsing."""

from __future__ import annotations

import json

import pytest

from bernstein.adapters.claude_stream_parser import (
    ClaudeStreamParser,
    StreamEventType,
    StreamParserState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_assistant_text(text: str) -> str:
    """Build a stream-json line for an assistant text block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": text}],
            },
        }
    )


def _make_assistant_tool_use(name: str, tool_input: dict[str, object] | None = None, tool_id: str = "t1") -> str:
    """Build a stream-json line for an assistant tool_use block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": name,
                        "id": tool_id,
                        "input": tool_input or {},
                    }
                ],
            },
        }
    )


def _make_assistant_thinking(thinking: str) -> str:
    """Build a stream-json line for an assistant thinking block."""
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "thinking", "thinking": thinking}],
            },
        }
    )


def _make_result(
    result: str = "done",
    subtype: str = "success",
    cost: float = 0.05,
    turns: int = 3,
    duration: int = 15000,
    is_error: bool = False,
) -> str:
    """Build a stream-json result line."""
    return json.dumps(
        {
            "type": "result",
            "result": result,
            "subtype": subtype,
            "total_cost_usd": cost,
            "num_turns": turns,
            "duration_ms": duration,
            "is_error": is_error,
        }
    )


def _make_system(message: str = "init", subtype: str = "init") -> str:
    """Build a stream-json system line."""
    return json.dumps(
        {
            "type": "system",
            "message": message,
            "subtype": subtype,
        }
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClaudeStreamParser:
    def test_empty_line_returns_no_events(self) -> None:
        parser = ClaudeStreamParser()
        assert parser.feed_line("") == []
        assert parser.feed_line("   ") == []

    def test_invalid_json_returns_no_events(self) -> None:
        parser = ClaudeStreamParser()
        assert parser.feed_line("not json") == []
        assert parser.feed_line("{incomplete") == []

    def test_parse_text_block(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_assistant_text("Hello world"))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.TEXT
        assert events[0].data["text"] == "Hello world"
        assert parser.state.text_blocks == ["Hello world"]

    def test_deduplicates_text(self) -> None:
        parser = ClaudeStreamParser()
        parser.feed_line(_make_assistant_text("Hello"))
        events = parser.feed_line(_make_assistant_text("Hello"))
        assert len(events) == 0
        assert len(parser.state.text_blocks) == 1

    def test_parse_tool_use(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_assistant_tool_use("Bash", {"command": "ls"}, "tool-1"))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.TOOL_USE_START
        assert events[0].data["name"] == "Bash"
        assert events[0].data["id"] == "tool-1"
        assert len(parser.state.tool_uses) == 1

    def test_parse_thinking_block(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_assistant_thinking("Let me think..."))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.THINKING
        assert events[0].data["thinking"] == "Let me think..."
        assert parser.state.thinking_blocks == ["Let me think..."]

    def test_parse_result(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_result("task done", "success", 0.12, 5, 30000))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.RESULT
        assert events[0].data["result"] == "task done"
        assert events[0].data["subtype"] == "success"
        assert events[0].data["total_cost_usd"] == pytest.approx(0.12)
        assert events[0].data["num_turns"] == 5
        assert events[0].data["duration_ms"] == 30000
        assert parser.state.total_cost_usd == pytest.approx(0.12)
        assert parser.state.num_turns == 5
        assert parser.state.duration_ms == 30000
        assert parser.state.subtype == "success"

    def test_parse_error_result(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_result("context overflow", "error_context_window", is_error=True))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.RESULT
        assert events[0].data["is_error"] is True
        assert "context overflow" in parser.state.errors

    def test_parse_system_message(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_system("initialized", "init"))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.SYSTEM
        assert events[0].data["message"] == "initialized"

    def test_parse_system_error(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_system("fatal error occurred", "error"))
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.ERROR
        assert "fatal error occurred" in parser.state.errors

    def test_feed_lines_processes_multiple(self) -> None:
        parser = ClaudeStreamParser()
        lines = "\n".join(
            [
                _make_assistant_text("line 1"),
                _make_assistant_tool_use("Read", {"file": "test.py"}),
                _make_result("done"),
            ]
        )
        events = parser.feed_lines(lines)
        assert len(events) == 3
        assert events[0].event_type == StreamEventType.TEXT
        assert events[1].event_type == StreamEventType.TOOL_USE_START
        assert events[2].event_type == StreamEventType.RESULT

    def test_tool_result_block(self) -> None:
        parser = ClaudeStreamParser()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "is_error": False,
                            "content": "file contents here",
                        }
                    ],
                },
            }
        )
        events = parser.feed_line(line)
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.TOOL_RESULT
        assert events[0].data["tool_use_id"] == "t1"
        assert events[0].data["is_error"] is False
        assert len(parser.state.tool_results) == 1

    def test_non_dict_json_ignored(self) -> None:
        parser = ClaudeStreamParser()
        assert parser.feed_line(json.dumps([1, 2, 3])) == []
        assert parser.feed_line(json.dumps("just a string")) == []

    def test_unknown_event_type_ignored(self) -> None:
        parser = ClaudeStreamParser()
        assert parser.feed_line(json.dumps({"type": "unknown_type"})) == []

    def test_multiple_content_blocks_in_one_message(self) -> None:
        parser = ClaudeStreamParser()
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I'll search for it"},
                        {"type": "tool_use", "name": "Grep", "id": "t2", "input": {"pattern": "foo"}},
                    ],
                },
            }
        )
        events = parser.feed_line(line)
        assert len(events) == 2
        assert events[0].event_type == StreamEventType.TEXT
        assert events[1].event_type == StreamEventType.TOOL_USE_START

    def test_raw_field_preserves_original(self) -> None:
        parser = ClaudeStreamParser()
        events = parser.feed_line(_make_assistant_text("test"))
        assert events[0].raw["type"] == "assistant"


class TestStreamParserState:
    def test_initial_state_empty(self) -> None:
        state = StreamParserState()
        assert state.text_blocks == []
        assert state.tool_uses == []
        assert state.tool_results == []
        assert state.thinking_blocks == []
        assert state.result is None
        assert state.total_cost_usd == pytest.approx(0.0)
        assert state.num_turns == 0
        assert state.duration_ms == 0
        assert state.subtype == ""
        assert state.errors == []


class TestClaudeStreamParserBuffering:
    """audit-143 — feed_line must buffer byte-split input and bound dedup."""

    def test_feed_line_buffers_partial_then_completes_on_newline(self) -> None:
        parser = ClaudeStreamParser()
        line = _make_assistant_text("split text") + "\n"
        mid = len(line) // 2
        # First half is partial JSON — no events yet.
        events = parser.feed_line(line[:mid])
        assert events == []
        # Second half completes the record (includes trailing \n).
        events = parser.feed_line(line[mid:])
        assert len(events) == 1
        assert events[0].event_type == StreamEventType.TEXT
        assert events[0].data["text"] == "split text"

    def test_feed_line_accepts_bytes(self) -> None:
        parser = ClaudeStreamParser()
        raw = (_make_assistant_text("hello bytes") + "\n").encode("utf-8")
        events = parser.feed_line(raw)
        assert len(events) == 1
        assert events[0].data["text"] == "hello bytes"

    def test_feed_line_bytes_split_at_boundary(self) -> None:
        parser = ClaudeStreamParser()
        raw = (_make_assistant_text("hello bytes") + "\n").encode("utf-8")
        # Split right in the middle of the JSON payload.
        assert parser.feed_line(raw[:10]) == []
        events = parser.feed_line(raw[10:])
        assert len(events) == 1
        assert events[0].data["text"] == "hello bytes"

    def test_feed_line_multiple_records_in_one_chunk(self) -> None:
        parser = ClaudeStreamParser()
        combined = _make_assistant_text("one") + "\n" + _make_assistant_text("two") + "\n"
        events = parser.feed_line(combined)
        assert len(events) == 2
        assert events[0].data["text"] == "one"
        assert events[1].data["text"] == "two"

    def test_feed_line_preserves_trailing_partial_across_calls(self) -> None:
        parser = ClaudeStreamParser()
        first = _make_assistant_text("first") + "\n" + _make_assistant_text("second")[:20]
        events = parser.feed_line(first)
        assert len(events) == 1
        assert events[0].data["text"] == "first"
        # Remainder of the second record arrives on the next call.
        rest = _make_assistant_text("second")[20:] + "\n"
        events = parser.feed_line(rest)
        assert len(events) == 1
        assert events[0].data["text"] == "second"

    def test_seen_text_bounded_at_10000(self) -> None:
        parser = ClaudeStreamParser()
        for i in range(20_000):
            parser.feed_line(_make_assistant_text(f"unique-{i}"))
        # LRU cap must hold at exactly 10 000 entries.
        assert len(parser._seen_text) == 10_000
        # All surviving keys must be the 10 000 most recent inserts.
        assert "unique-19999" in parser._seen_text
        assert "unique-10000" in parser._seen_text
        assert "unique-9999" not in parser._seen_text
        assert "unique-0" not in parser._seen_text

    def test_seen_text_eviction_preserves_dedup_semantics(self) -> None:
        parser = ClaudeStreamParser()
        # Fill to capacity with distinct texts.
        for i in range(10_000):
            parser.feed_line(_make_assistant_text(f"t-{i}"))
        assert len(parser.state.text_blocks) == 10_000
        # A brand-new text still dedupes against itself within-window.
        e1 = parser.feed_line(_make_assistant_text("fresh"))
        e2 = parser.feed_line(_make_assistant_text("fresh"))
        assert len(e1) == 1
        assert e2 == []
