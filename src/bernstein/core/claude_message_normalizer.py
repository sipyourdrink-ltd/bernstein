"""CLAUDE-007: Message normalization for consistent log parsing across Claude versions.

Normalizes Claude Code CLI output messages into a consistent internal
format, regardless of which Claude version produced them.  Handles
differences in JSON structure, field naming, and output format across
Claude Code releases.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """A Claude message normalized to a consistent format.

    Attributes:
        role: Message role ("assistant", "user", "system", "tool").
        content: Text content of the message.
        tool_use: Tool invocation details, if any.
        tool_result: Tool result details, if any.
        timestamp: ISO 8601 timestamp (if available from source).
        raw_type: Original message type from the source format.
        tokens: Token count if reported by the source.
    """

    role: Literal["assistant", "user", "system", "tool"]
    content: str
    tool_use: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    timestamp: str = ""
    raw_type: str = ""
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        result: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
        }
        if self.tool_use:
            result["tool_use"] = self.tool_use
        if self.tool_result:
            result["tool_result"] = self.tool_result
        if self.timestamp:
            result["timestamp"] = self.timestamp
        if self.raw_type:
            result["raw_type"] = self.raw_type
        if self.tokens:
            result["tokens"] = self.tokens
        return result


# ---------------------------------------------------------------------------
# Version-specific parsers
# ---------------------------------------------------------------------------

# Pattern for extracting cost/token info from Claude output.
_COST_PATTERN = re.compile(
    r"(?:cost|usage).*?(\d+)\s*(?:input|prompt).*?(\d+)\s*(?:output|completion)",
    re.IGNORECASE,
)

# Pattern for session summary lines.
_SESSION_SUMMARY_PATTERN = re.compile(
    r"Total\s+(?:cost|tokens):\s*\$?([\d.]+)",
    re.IGNORECASE,
)


def _str(val: object) -> str:
    """Safely convert Any value to str."""
    return str(val) if val is not None else ""


def _normalize_stream_json(data: dict[str, Any]) -> NormalizedMessage | None:
    """Normalize a stream-json format message.

    Stream-json is the default output format for Claude Code when
    using --output-format stream-json.

    Args:
        data: Parsed JSON dict from stream output.

    Returns:
        NormalizedMessage, or None if the event is not a message.
    """
    msg_type: str = _str(data.get("type"))

    if msg_type == "assistant":
        content_raw: object = data.get("message", data.get("content", ""))
        if isinstance(content_raw, dict):
            content_dict = cast("dict[str, object]", content_raw)
            content_text: object = content_dict.get("text", str(content_dict))
            content_raw = content_text
        return NormalizedMessage(role="assistant", content=str(content_raw), raw_type=msg_type)

    if msg_type == "tool_use":
        return NormalizedMessage(
            role="assistant",
            content="",
            tool_use={
                "name": _str(data.get("name", data.get("tool", ""))),
                "input": data.get("input", data.get("arguments", {})),
                "id": _str(data.get("id", "")),
            },
            raw_type=msg_type,
        )

    if msg_type == "tool_result":
        tr_content: str = _str(data.get("content", data.get("output", "")))
        return NormalizedMessage(
            role="tool",
            content=tr_content,
            tool_result={
                "tool_use_id": _str(data.get("tool_use_id", data.get("id", ""))),
                "is_error": bool(data.get("is_error", False)),
            },
            raw_type=msg_type,
        )

    if msg_type == "result":
        result_content: str = _str(data.get("result", data.get("output", "")))
        tokens = 0
        usage_raw: object = data.get("usage", {})
        if isinstance(usage_raw, dict):
            usage = cast("dict[str, object]", usage_raw)
            inp: int = int(str(usage.get("input_tokens", 0)))
            outp: int = int(str(usage.get("output_tokens", 0)))
            tokens = inp + outp
        return NormalizedMessage(role="assistant", content=result_content, raw_type=msg_type, tokens=tokens)

    return None


def _normalize_legacy_json(data: dict[str, Any]) -> NormalizedMessage | None:
    """Normalize a legacy JSON format message (older Claude Code versions).

    Args:
        data: Parsed JSON dict.

    Returns:
        NormalizedMessage, or None if not parseable.
    """
    role: str = _str(data.get("role"))
    content_raw: object = data.get("content", "")

    if isinstance(content_raw, list):
        content_list = cast("list[object]", content_raw)
        text_parts: list[str] = []
        tool_use: dict[str, Any] | None = None
        for block_item in content_list:
            if isinstance(block_item, dict):
                block = cast("dict[str, object]", block_item)
                if block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    tool_use = {
                        "name": _str(block.get("name", "")),
                        "input": block.get("input", {}),
                        "id": _str(block.get("id", "")),
                    }
        return NormalizedMessage(
            role=role if role in ("assistant", "user", "system", "tool") else "assistant",
            content="\n".join(text_parts),
            tool_use=tool_use,
            raw_type="legacy",
        )

    if isinstance(content_raw, str):
        normalized_role: Literal["assistant", "user", "system", "tool"] = "assistant"
        _role_set = {"assistant", "user", "system", "tool"}
        if role in _role_set:
            normalized_role = role  # type: ignore[assignment]
        return NormalizedMessage(
            role=normalized_role,
            content=content_raw,
            raw_type="legacy",
        )

    return None


@dataclass
class MessageNormalizer:
    """Normalizes Claude Code output into consistent message format.

    Handles multiple output formats across Claude Code versions.

    Attributes:
        messages: Accumulated normalized messages.
        parse_errors: Count of lines that failed to parse.
    """

    messages: list[NormalizedMessage] = field(default_factory=list[NormalizedMessage])
    parse_errors: int = 0

    def normalize_line(self, line: str) -> NormalizedMessage | None:
        """Normalize a single output line.

        Tries stream-json format first, then legacy JSON.

        Args:
            line: Raw output line from Claude Code.

        Returns:
            NormalizedMessage, or None if the line is not a message.
        """
        stripped = line.strip()
        if not stripped:
            return None

        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            # Not JSON -- treat as plain text assistant message.
            if stripped.startswith("{") or stripped.startswith("["):
                self.parse_errors += 1
                return None
            return NormalizedMessage(role="assistant", content=stripped, raw_type="text")

        if not isinstance(data, dict):
            return None

        typed_data = cast("dict[str, Any]", data)

        # Try stream-json format first.
        msg = _normalize_stream_json(typed_data)
        if msg is not None:
            self.messages.append(msg)
            return msg

        # Fall back to legacy format.
        msg = _normalize_legacy_json(typed_data)
        if msg is not None:
            self.messages.append(msg)
            return msg

        return None

    def normalize_output(self, output: str) -> list[NormalizedMessage]:
        """Normalize multi-line output from Claude Code.

        Args:
            output: Full output text (may contain multiple lines/events).

        Returns:
            List of normalized messages.
        """
        results: list[NormalizedMessage] = []
        for line in output.splitlines():
            msg = self.normalize_line(line)
            if msg is not None:
                results.append(msg)
        return results

    def extract_cost_info(self, output: str) -> dict[str, Any]:
        """Extract cost and token information from Claude output.

        Args:
            output: Full output text from Claude Code.

        Returns:
            Dict with extracted cost/token metrics.
        """
        info: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "total_cost_usd": 0.0}

        for line in output.splitlines():
            stripped = line.strip()
            try:
                data = json.loads(stripped)
                if isinstance(data, dict):
                    typed_data = cast("dict[str, Any]", data)
                    usage_raw: object = typed_data.get("usage", {})
                    if isinstance(usage_raw, dict):
                        usage_dict: dict[str, Any] = cast("dict[str, Any]", usage_raw)
                        info["input_tokens"] = max(
                            int(info["input_tokens"]),
                            int(usage_dict.get("input_tokens", 0)),
                        )
                        info["output_tokens"] = max(
                            int(info["output_tokens"]),
                            int(usage_dict.get("output_tokens", 0)),
                        )
                    cost_val: object = typed_data.get("cost_usd")
                    if cost_val is None:
                        cost_val = typed_data.get("cost", 0.0)
                    if cost_val:
                        cost_num = float(cast("str | int | float", cost_val))
                        info["total_cost_usd"] = max(float(info["total_cost_usd"]), cost_num)
            except (json.JSONDecodeError, ValueError, TypeError):
                # Try regex extraction for text-format output.
                match = _SESSION_SUMMARY_PATTERN.search(stripped)
                if match:
                    info["total_cost_usd"] = max(info["total_cost_usd"], float(match.group(1)))

        return info

    def reset(self) -> None:
        """Clear all accumulated messages and error counts."""
        self.messages.clear()
        self.parse_errors = 0
