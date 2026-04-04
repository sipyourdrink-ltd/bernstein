"""Tests for orphan tool result prevention on agent restart.

Covers:
- Detection of orphaned tool_use blocks via find_orphaned_tool_uses
- Repair of transcripts via repair_transcript
- Edge cases: empty transcript, no orphans, partial streams, duplicate IDs,
  multiple orphans per assistant turn, already-resolved turns
"""

from __future__ import annotations

from typing import Any

from bernstein.core.orphan_tool_result import (
    SYNTHETIC_RESULT_MARKER,
    OrphanRepairResult,
    find_orphaned_tool_uses,
    repair_transcript,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assistant_tool_use(tool_id: str, tool_name: str = "bash", command: str = "ls") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": tool_name, "input": {"command": command}},
        ],
    }


def _user_tool_result(tool_id: str, content: str = "ok") -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": content},
        ],
    }


def _text_msg(role: str = "assistant", text: str = "Hello") -> dict[str, Any]:
    return {"role": role, "content": [{"type": "text", "text": text}]}


# ---------------------------------------------------------------------------
# find_orphaned_tool_uses
# ---------------------------------------------------------------------------


class TestFindOrphanedToolUses:
    def test_empty_transcript_returns_empty(self) -> None:
        assert find_orphaned_tool_uses([]) == []

    def test_no_tool_use_blocks(self) -> None:
        msgs = [_text_msg("user", "hi"), _text_msg("assistant", "hello")]
        assert find_orphaned_tool_uses(msgs) == []

    def test_resolved_tool_use_not_orphaned(self) -> None:
        msgs = [
            _assistant_tool_use("tu_01"),
            _user_tool_result("tu_01"),
        ]
        assert find_orphaned_tool_uses(msgs) == []

    def test_single_orphan_detected(self) -> None:
        msgs = [_assistant_tool_use("tu_01")]
        orphans = find_orphaned_tool_uses(msgs)
        assert len(orphans) == 1
        msg_idx, tool_id, tool_name = orphans[0]
        assert msg_idx == 0
        assert tool_id == "tu_01"
        assert tool_name == "bash"

    def test_orphan_after_resolved_turn(self) -> None:
        """First tool use is resolved; second crashes before its result."""
        msgs = [
            _assistant_tool_use("tu_01"),
            _user_tool_result("tu_01"),
            _assistant_tool_use("tu_02"),
            # Crash happens here — no tool_result for tu_02
        ]
        orphans = find_orphaned_tool_uses(msgs)
        assert len(orphans) == 1
        _, tid, _ = orphans[0]
        assert tid == "tu_02"

    def test_multiple_orphans_same_assistant_message(self) -> None:
        """Two tool_use blocks in one assistant turn, no results for either."""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_a", "name": "read", "input": {}},
                {"type": "tool_use", "id": "tu_b", "name": "write", "input": {}},
            ],
        }
        orphans = find_orphaned_tool_uses([msg])
        assert len(orphans) == 2
        ids = [tid for _, tid, _ in orphans]
        assert "tu_a" in ids
        assert "tu_b" in ids

    def test_partial_stream_block_with_empty_id_skipped(self) -> None:
        """A tool_use block with an empty id (e.g. cut-off stream) is ignored."""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "", "name": "bash", "input": {}},
            ],
        }
        assert find_orphaned_tool_uses([msg]) == []

    def test_string_content_messages_skipped(self) -> None:
        """Messages with plain string content don't crash the detector."""
        msgs: list[dict[str, Any]] = [
            {"role": "assistant", "content": "Just a string"},
            {"role": "user", "content": "Another string"},
        ]
        assert find_orphaned_tool_uses(msgs) == []

    def test_resolved_by_later_user_message(self) -> None:
        """Orphan in first turn resolved by a tool_result in second user turn."""
        msgs = [
            _assistant_tool_use("tu_01"),
            {"role": "user", "content": [{"type": "text", "text": "context"}]},
            _user_tool_result("tu_01"),
        ]
        assert find_orphaned_tool_uses(msgs) == []

    def test_duplicate_tool_ids_treated_as_separate(self) -> None:
        """Duplicate IDs (shouldn't happen but must not crash)."""
        msgs: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "dup_01", "name": "bash", "input": {}},
                    {"type": "tool_use", "id": "dup_01", "name": "bash", "input": {}},
                ],
            }
        ]
        # Both should be listed as orphans (detection is positional)
        orphans = find_orphaned_tool_uses(msgs)
        assert len(orphans) == 2
        assert all(tid == "dup_01" for _, tid, _ in orphans)


# ---------------------------------------------------------------------------
# repair_transcript
# ---------------------------------------------------------------------------


class TestRepairTranscript:
    def test_empty_transcript_returns_empty_result(self) -> None:
        result = repair_transcript([])
        assert result.orphan_count == 0
        assert result.orphan_ids == []
        assert result.messages == []

    def test_no_orphans_returns_copy_unchanged(self) -> None:
        msgs = [_assistant_tool_use("tu_01"), _user_tool_result("tu_01")]
        result = repair_transcript(msgs)
        assert result.orphan_count == 0
        assert result.orphan_ids == []
        assert result.messages == msgs
        # Must be a copy, not the same list object
        assert result.messages is not msgs

    def test_single_orphan_repaired(self) -> None:
        msgs = [_assistant_tool_use("tu_01")]
        result = repair_transcript(msgs)
        assert isinstance(result, OrphanRepairResult)
        assert result.orphan_count == 1
        assert result.orphan_ids == ["tu_01"]
        assert len(result.messages) == 2  # original + synthetic user msg

        synthetic = result.messages[1]
        assert synthetic["role"] == "user"
        assert synthetic.get("_orphan_recovery") is True
        content_blocks = synthetic["content"]
        assert len(content_blocks) == 1
        block = content_blocks[0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "tu_01"
        assert SYNTHETIC_RESULT_MARKER in block["content"]
        assert block.get("_synthetic") is True

    def test_synthetic_content_override(self) -> None:
        msgs = [_assistant_tool_use("tu_01")]
        result = repair_transcript(msgs, synthetic_content="custom error message")
        block = result.messages[1]["content"][0]
        assert block["content"] == "custom error message"

    def test_multiple_orphans_same_turn_single_synthetic_message(self) -> None:
        """Two orphaned tool_use in one assistant turn → one synthetic user message."""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_a", "name": "read", "input": {}},
                {"type": "tool_use", "id": "tu_b", "name": "write", "input": {}},
            ],
        }
        result = repair_transcript([msg])
        assert result.orphan_count == 2
        assert set(result.orphan_ids) == {"tu_a", "tu_b"}
        assert len(result.messages) == 2  # 1 assistant + 1 synthetic user

        synthetic = result.messages[1]
        assert synthetic["role"] == "user"
        tool_result_ids = {b["tool_use_id"] for b in synthetic["content"]}
        assert tool_result_ids == {"tu_a", "tu_b"}

    def test_orphan_in_second_turn_only(self) -> None:
        """First turn resolved; second turn orphaned."""
        msgs = [
            _assistant_tool_use("tu_01"),
            _user_tool_result("tu_01"),
            _assistant_tool_use("tu_02"),
        ]
        result = repair_transcript(msgs)
        assert result.orphan_count == 1
        assert result.orphan_ids == ["tu_02"]
        # Synthetic message inserted at index 3 (after index 2 assistant)
        assert len(result.messages) == 4
        assert result.messages[3]["role"] == "user"
        assert result.messages[3]["content"][0]["tool_use_id"] == "tu_02"

    def test_original_messages_not_mutated(self) -> None:
        """repair_transcript must not modify the input list."""
        msgs = [_assistant_tool_use("tu_01")]
        original_len = len(msgs)
        repair_transcript(msgs)
        assert len(msgs) == original_len

    def test_multiple_orphaned_turns_all_repaired(self) -> None:
        """Two separate assistant turns each with an orphaned tool_use."""
        msgs = [
            _assistant_tool_use("tu_01"),
            # No tool_result for tu_01
            _assistant_tool_use("tu_02"),
            # No tool_result for tu_02
        ]
        result = repair_transcript(msgs)
        assert result.orphan_count == 2
        assert set(result.orphan_ids) == {"tu_01", "tu_02"}
        # One synthetic message inserted after each assistant turn
        assert len(result.messages) == 4

    def test_mixed_resolved_and_orphaned_turns(self) -> None:
        """Interleaved resolved and orphaned turns."""
        msgs = [
            _assistant_tool_use("tu_01"),
            _user_tool_result("tu_01"),
            _text_msg("assistant", "thinking..."),
            _assistant_tool_use("tu_02"),
            # Crash here — no result for tu_02
        ]
        result = repair_transcript(msgs)
        assert result.orphan_count == 1
        assert result.orphan_ids == ["tu_02"]
        assert len(result.messages) == 5  # 4 original + 1 synthetic

    def test_partial_stream_tool_use_ignored(self) -> None:
        """tool_use blocks with empty id are not treated as orphans."""
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "", "name": "bash", "input": {}},
            ],
        }
        result = repair_transcript([msg])
        assert result.orphan_count == 0
        assert len(result.messages) == 1  # no synthetic message added

    def test_result_messages_order_preserved(self) -> None:
        """Message ordering is stable after repair."""
        msgs = [
            _text_msg("user", "start"),
            _assistant_tool_use("tu_01"),
            _user_tool_result("tu_01"),
            _assistant_tool_use("tu_02"),
        ]
        result = repair_transcript(msgs)
        roles = [m["role"] for m in result.messages]
        assert roles == ["user", "assistant", "user", "assistant", "user"]

    def test_is_error_false_on_synthetic_results(self) -> None:
        """Synthetic tool_result blocks have is_error=False."""
        msgs = [_assistant_tool_use("tu_01")]
        result = repair_transcript(msgs)
        block = result.messages[1]["content"][0]
        assert block["is_error"] is False
