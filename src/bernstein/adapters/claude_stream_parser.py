"""Parse Claude Code --output-format stream-json events.

Reads NDJSON lines from Claude Code's streaming output and extracts
structured events for real-time TUI updates, tool-use tracking, and
orchestrator dashboards.

Event types handled:
- assistant: text blocks and tool_use blocks
- result: final result with cost/turn/duration metadata
- system: system-level messages (init, error)

See: https://docs.anthropic.com/en/docs/claude-code/cli-usage#output-formats
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

logger = logging.getLogger(__name__)

# Block type constants from Claude Code stream-json format.
_BLOCK_TEXT = "text"
_BLOCK_TOOL_USE = "tool_use"
_BLOCK_TOOL_RESULT = "tool_result"
_BLOCK_THINKING = "thinking"

# Top-level event type constants.
_EVENT_ASSISTANT = "assistant"
_EVENT_RESULT = "result"
_EVENT_SYSTEM = "system"


class StreamEventType(StrEnum):
    """Types of events extracted from Claude Code's stream-json output."""

    TEXT = "text"
    TOOL_USE_START = "tool_use_start"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    RESULT = "result"
    ERROR = "error"
    SYSTEM = "system"


@dataclass(frozen=True)
class StreamEvent:
    """A parsed event from Claude Code's NDJSON stream.

    Attributes:
        event_type: Categorised event type.
        data: Event-specific payload.
        raw: Original JSON dict for pass-through.
    """

    event_type: StreamEventType
    data: dict[str, Any] = field(default_factory=dict[str, Any])
    raw: dict[str, Any] = field(default_factory=dict[str, Any], repr=False)


@dataclass
class StreamParserState:
    """Accumulated state from parsing a Claude Code stream.

    Attributes:
        text_blocks: All assistant text blocks seen.
        tool_uses: All tool_use invocations with name and truncated input.
        tool_results: All tool results with tool_use_id and content preview.
        thinking_blocks: Extended thinking blocks.
        result: Final result event data (None until stream ends).
        total_cost_usd: Total cost from the result event.
        num_turns: Number of turns from the result event.
        duration_ms: Duration from the result event.
        subtype: Result subtype (success, error_max_turns, etc.).
        errors: Any error messages encountered.
    """

    text_blocks: list[str] = field(default_factory=list[str])
    tool_uses: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    tool_results: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    thinking_blocks: list[str] = field(default_factory=list[str])
    result: dict[str, Any] | None = None
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    subtype: str = ""
    errors: list[str] = field(default_factory=list[str])


class ClaudeStreamParser:
    """Stateful parser for Claude Code's stream-json NDJSON output.

    Processes one line at a time via :meth:`feed_line` and emits
    :class:`StreamEvent` objects.  Accumulated state is available
    via :attr:`state`.

    Example::

        parser = ClaudeStreamParser()
        for line in stream:
            events = parser.feed_line(line)
            for event in events:
                update_tui(event)
    """

    def __init__(self) -> None:
        self.state = StreamParserState()
        self._seen_text: set[str] = set()

    def feed_line(self, line: str) -> list[StreamEvent]:
        """Parse a single NDJSON line and return extracted events.

        Args:
            line: A single line from Claude Code's stream-json output.

        Returns:
            List of StreamEvent objects extracted from this line.
            May be empty if the line is not parseable or contains no events.
        """
        line = line.strip()
        if not line:
            return []
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return []

        if not isinstance(raw, dict):
            return []

        msg = cast("dict[str, Any]", raw)
        event_type = str(msg.get("type", ""))
        events: list[StreamEvent] = []

        match event_type:
            case "assistant":
                events.extend(self._parse_assistant(msg))
            case "result":
                events.extend(self._parse_result(msg))
            case "system":
                events.append(self._parse_system(msg))

        return events

    def feed_lines(self, lines: str) -> list[StreamEvent]:
        """Parse multiple NDJSON lines (newline-separated) at once.

        Args:
            lines: Newline-separated NDJSON content.

        Returns:
            All StreamEvent objects extracted from the input.
        """
        all_events: list[StreamEvent] = []
        for line in lines.splitlines():
            all_events.extend(self.feed_line(line))
        return all_events

    def _parse_assistant(self, msg: dict[str, Any]) -> list[StreamEvent]:
        """Extract text and tool_use blocks from an assistant message.

        Args:
            msg: Parsed JSON dict with type="assistant".

        Returns:
            List of events from the content blocks.
        """
        events: list[StreamEvent] = []
        message_raw = msg.get("message", {})
        if not isinstance(message_raw, dict):
            return events
        message = cast("dict[str, Any]", message_raw)

        content_raw = message.get("content", [])
        if not isinstance(content_raw, list):
            return events
        content = cast("list[Any]", content_raw)

        for block_raw in content:
            if not isinstance(block_raw, dict):
                continue
            block = cast("dict[str, Any]", block_raw)

            block_type = str(block.get("type", ""))

            match block_type:
                case "text":
                    text = str(block.get("text", ""))
                    if text and text not in self._seen_text:
                        self._seen_text.add(text)
                        self.state.text_blocks.append(text)
                        events.append(
                            StreamEvent(
                                event_type=StreamEventType.TEXT,
                                data={"text": text},
                                raw=msg,
                            )
                        )

                case "tool_use":
                    tool_name = str(block.get("name", ""))
                    tool_input = block.get("input", {})
                    tool_id = str(block.get("id", ""))
                    preview = str(tool_input)[:200] if tool_input else ""
                    tool_record: dict[str, Any] = {
                        "name": tool_name,
                        "id": tool_id,
                        "input_preview": preview,
                    }
                    self.state.tool_uses.append(tool_record)
                    events.append(
                        StreamEvent(
                            event_type=StreamEventType.TOOL_USE_START,
                            data=tool_record,
                            raw=msg,
                        )
                    )

                case "tool_result":
                    tool_use_id = str(block.get("tool_use_id", ""))
                    is_error = bool(block.get("is_error", False))
                    result_content = str(block.get("content", ""))
                    preview = result_content[:200] if result_content else ""
                    result_record: dict[str, Any] = {
                        "tool_use_id": tool_use_id,
                        "is_error": is_error,
                        "content_preview": preview,
                    }
                    self.state.tool_results.append(result_record)
                    events.append(
                        StreamEvent(
                            event_type=StreamEventType.TOOL_RESULT,
                            data=result_record,
                            raw=msg,
                        )
                    )

                case "thinking":
                    thinking_text = str(block.get("thinking", ""))
                    if thinking_text:
                        self.state.thinking_blocks.append(thinking_text)
                        events.append(
                            StreamEvent(
                                event_type=StreamEventType.THINKING,
                                data={"thinking": thinking_text},
                                raw=msg,
                            )
                        )

        return events

    def _parse_result(self, msg: dict[str, Any]) -> list[StreamEvent]:
        """Extract result metadata from a result event.

        Args:
            msg: Parsed JSON dict with type="result".

        Returns:
            Single-element list with the result event.
        """
        result_text = str(msg.get("result", ""))
        subtype = str(msg.get("subtype", "success"))
        cost = float(msg.get("total_cost_usd", 0.0))
        turns = int(msg.get("num_turns", 0))
        duration = int(msg.get("duration_ms", 0))
        is_error = bool(msg.get("is_error", False))

        self.state.result = {
            "result": result_text,
            "subtype": subtype,
            "total_cost_usd": cost,
            "num_turns": turns,
            "duration_ms": duration,
            "is_error": is_error,
        }
        self.state.total_cost_usd = cost
        self.state.num_turns = turns
        self.state.duration_ms = duration
        self.state.subtype = subtype

        if is_error:
            self.state.errors.append(result_text)

        return [
            StreamEvent(
                event_type=StreamEventType.RESULT,
                data=self.state.result,
                raw=msg,
            )
        ]

    def _parse_system(self, msg: dict[str, Any]) -> StreamEvent:
        """Extract system-level messages.

        Args:
            msg: Parsed JSON dict with type="system".

        Returns:
            A system event.
        """
        message = str(msg.get("message", ""))
        subtype = str(msg.get("subtype", ""))

        if subtype == "error" or "error" in message.lower():
            self.state.errors.append(message)
            return StreamEvent(
                event_type=StreamEventType.ERROR,
                data={"message": message, "subtype": subtype},
                raw=msg,
            )

        return StreamEvent(
            event_type=StreamEventType.SYSTEM,
            data={"message": message, "subtype": subtype},
            raw=msg,
        )
