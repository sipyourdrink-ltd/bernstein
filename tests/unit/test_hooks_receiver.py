"""Tests for the hooks receiver module (parsing, persistence, side-effects)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.hooks_receiver import (
    HookEvent,
    HookEventType,
    parse_hook_event,
    process_hook_event,
    touch_heartbeat,
    write_hook_event,
    write_stop_marker,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# HookEventType.from_str
# ---------------------------------------------------------------------------


class TestHookEventTypeFromStr:
    """HookEventType.from_str() maps strings to enum members."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("PostToolUse", HookEventType.POST_TOOL_USE),
            ("Stop", HookEventType.STOP),
            ("PreCompact", HookEventType.PRE_COMPACT),
            ("SubagentStart", HookEventType.SUBAGENT_START),
            ("SubagentStop", HookEventType.SUBAGENT_STOP),
        ],
    )
    def test_known_events(self, raw: str, expected: HookEventType) -> None:
        assert HookEventType.from_str(raw) == expected

    def test_unknown_event_returns_unknown(self) -> None:
        assert HookEventType.from_str("SomeFutureEvent") == HookEventType.UNKNOWN

    def test_empty_string_returns_unknown(self) -> None:
        assert HookEventType.from_str("") == HookEventType.UNKNOWN


# ---------------------------------------------------------------------------
# parse_hook_event
# ---------------------------------------------------------------------------


class TestParseHookEvent:
    """parse_hook_event() creates a typed HookEvent from raw JSON body."""

    def test_post_tool_use_extracts_tool_name(self) -> None:
        body: dict[str, Any] = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": "ls -la",
        }
        event = parse_hook_event("sess-123", body)
        assert event.event_type == HookEventType.POST_TOOL_USE
        assert event.tool_name == "Bash"
        assert event.tool_input == "ls -la"
        assert event.session_id == "sess-123"

    def test_stop_event_has_no_tool_info(self) -> None:
        body: dict[str, Any] = {"hook_event_name": "Stop"}
        event = parse_hook_event("sess-456", body)
        assert event.event_type == HookEventType.STOP
        assert event.tool_name == ""
        assert event.tool_input == ""

    def test_tool_input_truncated_to_200_chars(self) -> None:
        body: dict[str, Any] = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": "x" * 500,
        }
        event = parse_hook_event("sess-789", body)
        assert len(event.tool_input) == 200

    def test_fallback_event_key(self) -> None:
        """Falls back to 'event' key when 'hook_event_name' is missing."""
        body: dict[str, Any] = {"event": "PreCompact"}
        event = parse_hook_event("s", body)
        assert event.event_type == HookEventType.PRE_COMPACT

    def test_timestamp_is_recent(self) -> None:
        before = time.time()
        event = parse_hook_event("s", {"hook_event_name": "Stop"})
        after = time.time()
        assert before <= event.timestamp <= after

    def test_payload_preserved(self) -> None:
        body: dict[str, Any] = {"hook_event_name": "Stop", "extra": "data"}
        event = parse_hook_event("s", body)
        assert event.payload == body


# ---------------------------------------------------------------------------
# write_hook_event
# ---------------------------------------------------------------------------


class TestWriteHookEvent:
    """write_hook_event() persists events to JSONL sidecar."""

    def test_creates_sidecar_file(self, tmp_path: Path) -> None:
        event = HookEvent(
            session_id="sess-a",
            event_type=HookEventType.STOP,
            raw_event_name="Stop",
        )
        write_hook_event(event, tmp_path)

        sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-a.jsonl"
        assert sidecar.exists()
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "Stop"
        assert "ts" in record

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        for i in range(3):
            event = HookEvent(
                session_id="sess-b",
                event_type=HookEventType.POST_TOOL_USE,
                raw_event_name="PostToolUse",
                tool_name=f"Tool{i}",
            )
            write_hook_event(event, tmp_path)

        sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-b.jsonl"
        lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_tool_name_included_for_post_tool_use(self, tmp_path: Path) -> None:
        event = HookEvent(
            session_id="sess-c",
            event_type=HookEventType.POST_TOOL_USE,
            raw_event_name="PostToolUse",
            tool_name="Read",
            tool_input="/tmp/file.py",
        )
        write_hook_event(event, tmp_path)

        sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-c.jsonl"
        record = json.loads(sidecar.read_text(encoding="utf-8").strip())
        assert record["tool_name"] == "Read"
        assert record["tool_input"] == "/tmp/file.py"


# ---------------------------------------------------------------------------
# write_stop_marker
# ---------------------------------------------------------------------------


class TestWriteStopMarker:
    """write_stop_marker() creates the completion marker file."""

    def test_creates_marker_file(self, tmp_path: Path) -> None:
        write_stop_marker("sess-stop", tmp_path)
        marker = tmp_path / ".sdd" / "runtime" / "completed" / "sess-stop"
        assert marker.exists()
        assert marker.read_text(encoding="utf-8") == "hook:Stop"

    def test_overwrites_existing_marker(self, tmp_path: Path) -> None:
        completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
        completed_dir.mkdir(parents=True)
        marker = completed_dir / "sess-overwrite"
        marker.write_text("old", encoding="utf-8")

        write_stop_marker("sess-overwrite", tmp_path)
        assert marker.read_text(encoding="utf-8") == "hook:Stop"


# ---------------------------------------------------------------------------
# touch_heartbeat
# ---------------------------------------------------------------------------


class TestTouchHeartbeat:
    """touch_heartbeat() updates the heartbeat file for a session."""

    def test_creates_heartbeat_file(self, tmp_path: Path) -> None:
        before = int(time.time())
        touch_heartbeat("sess-hb", tmp_path)
        after = int(time.time())

        hb_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess-hb.json"
        assert hb_path.exists()
        ts = int(hb_path.read_text(encoding="utf-8"))
        assert before <= ts <= after

    def test_updates_existing_heartbeat(self, tmp_path: Path) -> None:
        hb_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
        hb_dir.mkdir(parents=True)
        hb_path = hb_dir / "sess-hb2.json"
        hb_path.write_text("0", encoding="utf-8")

        touch_heartbeat("sess-hb2", tmp_path)
        ts = int(hb_path.read_text(encoding="utf-8"))
        assert ts > 0


# ---------------------------------------------------------------------------
# process_hook_event — integration of all side-effects
# ---------------------------------------------------------------------------


class TestProcessHookEvent:
    """process_hook_event() orchestrates persistence and side-effects."""

    def test_stop_event_writes_marker_and_sidecar(self, tmp_path: Path) -> None:
        event = parse_hook_event("sess-int", {"hook_event_name": "Stop"})
        result = process_hook_event(event, tmp_path)

        assert result["status"] == "ok"
        assert result["action"] == "stop_marker_written"

        # Completion marker written
        marker = tmp_path / ".sdd" / "runtime" / "completed" / "sess-int"
        assert marker.exists()

        # Sidecar written
        sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-int.jsonl"
        assert sidecar.exists()

        # Heartbeat touched
        hb = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess-int.json"
        assert hb.exists()

    def test_post_tool_use_event_logs_tool(self, tmp_path: Path) -> None:
        event = parse_hook_event(
            "sess-tool",
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": "echo hi"},
        )
        result = process_hook_event(event, tmp_path)

        assert result["status"] == "ok"
        assert result["action"] == "tool_use_logged"

        # Heartbeat touched
        hb = tmp_path / ".sdd" / "runtime" / "heartbeats" / "sess-tool.json"
        assert hb.exists()

    def test_pre_compact_event(self, tmp_path: Path) -> None:
        event = parse_hook_event("sess-compact", {"hook_event_name": "PreCompact"})
        result = process_hook_event(event, tmp_path)

        assert result["action"] == "compaction_logged"

    def test_subagent_start_event(self, tmp_path: Path) -> None:
        event = parse_hook_event("sess-sub", {"hook_event_name": "SubagentStart"})
        result = process_hook_event(event, tmp_path)

        assert result["action"] == "subagent_start_logged"

    def test_subagent_stop_event(self, tmp_path: Path) -> None:
        event = parse_hook_event("sess-sub", {"hook_event_name": "SubagentStop"})
        result = process_hook_event(event, tmp_path)

        assert result["action"] == "subagent_stop_logged"

    def test_unknown_event_still_persisted(self, tmp_path: Path) -> None:
        event = parse_hook_event("sess-unk", {"hook_event_name": "FutureEvent"})
        result = process_hook_event(event, tmp_path)

        assert result["status"] == "ok"
        assert result["action"] == "event_logged"

        # Still persisted to sidecar
        sidecar = tmp_path / ".sdd" / "runtime" / "hooks" / "sess-unk.jsonl"
        assert sidecar.exists()
