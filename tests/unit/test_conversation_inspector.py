"""Unit tests for road-033 conversation inspector."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.conversation_inspector import (
    InspectorView,
    build_inspector_view,
    format_inspector_output,
    format_message,
    search_messages,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_ndjson(tmp_path: Path, lines: list[dict[str, object]]) -> Path:
    log = tmp_path / "session.ndjson"
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return log


_SAMPLE_LINES: list[dict[str, object]] = [
    {"type": "system", "message": "session started", "timestamp": 1.0},
    {"type": "human", "message": "fix the bug in parser.py", "timestamp": 2.0},
    {
        "type": "assistant",
        "message": {"content": "I will look at parser.py now."},
        "timestamp": 3.0,
    },
    {
        "type": "tool_result",
        "tool": "Bash",
        "content": "parser.py:42 SyntaxError",
        "timestamp": 4.0,
    },
    {
        "type": "assistant",
        "message": {"content": "Found the issue. Applying fix."},
        "timestamp": 5.0,
    },
]


# ---------------------------------------------------------------------------
# build_inspector_view
# ---------------------------------------------------------------------------


class TestBuildInspectorView:
    """Tests for build_inspector_view."""

    def test_parses_sample_ndjson(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        assert view.total_messages == 5
        assert view.by_role["system"] == 1
        assert view.by_role["user"] == 1
        assert view.by_role["assistant"] == 2
        assert view.by_role["tool_result"] == 1
        assert view.total_tokens > 0

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.ndjson"
        assert build_inspector_view(missing) is None

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.ndjson"
        empty.write_text("", encoding="utf-8")
        assert build_inspector_view(empty) is None

    def test_messages_have_expected_keys(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        for msg in view.messages:
            assert "role" in msg
            assert "content" in msg
            assert "tokens" in msg
            assert "tool_name" in msg

    def test_tool_name_populated(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        tool_msgs = [m for m in view.messages if m["tool_name"] is not None]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0]["tool_name"] == "Bash"


# ---------------------------------------------------------------------------
# search_messages
# ---------------------------------------------------------------------------


class TestSearchMessages:
    """Tests for search_messages."""

    def test_finds_matching_messages(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        indices = search_messages(view, "parser")
        # "fix the bug in parser.py" and "parser.py:42 SyntaxError"
        assert len(indices) >= 2

    def test_case_insensitive(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        lower = search_messages(view, "syntaxerror")
        upper = search_messages(view, "SYNTAXERROR")
        assert lower == upper
        assert len(lower) >= 1

    def test_no_matches_returns_empty(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        assert search_messages(view, "zzz_no_match_zzz") == []


# ---------------------------------------------------------------------------
# format_message
# ---------------------------------------------------------------------------


class TestFormatMessage:
    """Tests for format_message."""

    def test_contains_role_marker(self) -> None:
        msg = {"role": "assistant", "content": "hello", "tokens": 2, "tool_name": None}
        out = format_message(msg, 0)
        assert "ASSISTANT" in out

    def test_contains_index(self) -> None:
        msg = {"role": "user", "content": "hi", "tokens": 1, "tool_name": None}
        out = format_message(msg, 7)
        assert "[7]" in out

    def test_shows_tool_name(self) -> None:
        msg = {"role": "tool_result", "content": "ok", "tokens": 1, "tool_name": "Bash"}
        out = format_message(msg, 0)
        assert "Bash" in out

    def test_tokens_hidden(self) -> None:
        msg = {"role": "user", "content": "hi", "tokens": 5, "tool_name": None}
        out = format_message(msg, 0, show_tokens=False)
        assert "tokens" not in out


# ---------------------------------------------------------------------------
# format_inspector_output
# ---------------------------------------------------------------------------


class TestFormatInspectorOutput:
    """Tests for format_inspector_output."""

    def test_header_present(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        out = format_inspector_output(view)
        assert "Conversation Inspector" in out

    def test_role_filter(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        out = format_inspector_output(view, role_filter="user")
        assert "USER" in out
        # assistant messages should be excluded
        assert "ASSISTANT" not in out

    def test_search_filter(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        out = format_inspector_output(view, search="SyntaxError")
        assert "TOOL_RESULT" in out

    def test_role_counts_in_header(self, tmp_path: Path) -> None:
        log = _write_ndjson(tmp_path, _SAMPLE_LINES)
        view = build_inspector_view(log)
        assert view is not None
        out = format_inspector_output(view)
        assert "assistant: 2" in out
        assert "user: 1" in out


# ---------------------------------------------------------------------------
# InspectorView frozen
# ---------------------------------------------------------------------------


class TestInspectorViewFrozen:
    """InspectorView should be immutable."""

    def test_is_frozen(self) -> None:
        view = InspectorView()
        try:
            view.total_tokens = 99  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised, "InspectorView should be frozen"
