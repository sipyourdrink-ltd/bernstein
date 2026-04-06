"""Tests for bernstein.core.claude_message_normalizer (CLAUDE-007)."""

from __future__ import annotations

import json

from bernstein.core.claude_message_normalizer import (
    MessageNormalizer,
    NormalizedMessage,
)


class TestNormalizedMessage:
    def test_to_dict_minimal(self) -> None:
        msg = NormalizedMessage(role="assistant", content="hello")
        d = msg.to_dict()
        assert d["role"] == "assistant"
        assert d["content"] == "hello"
        assert "tool_use" not in d

    def test_to_dict_with_tool_use(self) -> None:
        msg = NormalizedMessage(
            role="assistant",
            content="",
            tool_use={"name": "Bash", "input": {"command": "ls"}},
        )
        d = msg.to_dict()
        assert d["tool_use"]["name"] == "Bash"


class TestMessageNormalizer:
    def test_normalize_stream_json_assistant(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps({"type": "assistant", "message": "Hello world"})
        msg = norm.normalize_line(line)
        assert msg is not None
        assert msg.role == "assistant"
        assert msg.content == "Hello world"

    def test_normalize_stream_json_tool_use(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "ls"},
                "id": "tool_1",
            }
        )
        msg = norm.normalize_line(line)
        assert msg is not None
        assert msg.tool_use is not None
        assert msg.tool_use["name"] == "Bash"

    def test_normalize_stream_json_tool_result(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps(
            {
                "type": "tool_result",
                "content": "file1.py\nfile2.py",
                "tool_use_id": "tool_1",
            }
        )
        msg = norm.normalize_line(line)
        assert msg is not None
        assert msg.role == "tool"

    def test_normalize_stream_json_result(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps(
            {
                "type": "result",
                "result": "Done",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
        )
        msg = norm.normalize_line(line)
        assert msg is not None
        assert msg.tokens == 150

    def test_normalize_legacy_json(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps({"role": "assistant", "content": "Hello"})
        msg = norm.normalize_line(line)
        assert msg is not None
        assert msg.content == "Hello"
        assert msg.raw_type == "legacy"

    def test_normalize_legacy_multi_block(self) -> None:
        norm = MessageNormalizer()
        line = json.dumps(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "First"},
                    {"type": "text", "text": "Second"},
                ],
            }
        )
        msg = norm.normalize_line(line)
        assert msg is not None
        assert "First" in msg.content
        assert "Second" in msg.content

    def test_normalize_plain_text(self) -> None:
        norm = MessageNormalizer()
        msg = norm.normalize_line("Just plain text output")
        assert msg is not None
        assert msg.role == "assistant"
        assert msg.content == "Just plain text output"

    def test_empty_line_returns_none(self) -> None:
        norm = MessageNormalizer()
        assert norm.normalize_line("") is None
        assert norm.normalize_line("   ") is None

    def test_normalize_output_multi_line(self) -> None:
        norm = MessageNormalizer()
        output = (
            json.dumps({"type": "assistant", "message": "Line 1"})
            + "\n"
            + json.dumps({"type": "assistant", "message": "Line 2"})
        )
        messages = norm.normalize_output(output)
        assert len(messages) == 2

    def test_extract_cost_info_from_json(self) -> None:
        norm = MessageNormalizer()
        output = json.dumps(
            {
                "type": "result",
                "usage": {"input_tokens": 1000, "output_tokens": 500},
                "cost_usd": 0.05,
            }
        )
        info = norm.extract_cost_info(output)
        assert info["input_tokens"] == 1000
        assert info["output_tokens"] == 500
        assert info["total_cost_usd"] == 0.05

    def test_extract_cost_info_from_text(self) -> None:
        norm = MessageNormalizer()
        output = "Some output\nTotal cost: $1.23\nDone."
        info = norm.extract_cost_info(output)
        assert info["total_cost_usd"] == 1.23

    def test_reset_clears_state(self) -> None:
        norm = MessageNormalizer()
        norm.normalize_line(json.dumps({"type": "assistant", "message": "test"}))
        assert len(norm.messages) == 1
        norm.reset()
        assert len(norm.messages) == 0
