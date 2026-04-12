"""CLAUDE-019: Conversation export for post-mortem analysis.

Parses Claude Code NDJSON log files into structured conversation
messages and exports them as JSON for debugging and post-mortem
review of agent sessions.

Supports Claude Code event types: system, assistant (text + tool_use),
tool_result, and human (user) messages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationMessage:
    """A single message in an agent conversation.

    Attributes:
        role: Message role (system, user, assistant, tool_result).
        content: Text content of the message.
        timestamp: Unix timestamp of the message, if available.
        tool_name: Name of the tool invoked or returning a result.
        turn_number: Sequential turn index within the conversation.
    """

    role: str
    content: str
    timestamp: float | None = None
    tool_name: str | None = None
    turn_number: int = 0


@dataclass(frozen=True)
class ConversationExport:
    """Full conversation export for a single agent session.

    Attributes:
        session_id: Unique session identifier.
        task_id: Task identifier the agent was working on.
        agent_role: Role assigned to the agent (e.g. backend, qa).
        model: Model used for the session.
        messages: Ordered list of conversation messages.
        total_tokens: Total tokens consumed during the session.
        cost_usd: Total cost in USD.
        outcome: Session outcome (e.g. success, failure, timeout).
        exported_at: ISO 8601 timestamp when the export was created.
    """

    session_id: str
    task_id: str
    agent_role: str
    model: str
    messages: list[ConversationMessage] = field(default_factory=lambda: list[ConversationMessage]())
    total_tokens: int = 0
    cost_usd: float = 0.0
    outcome: str = ""
    exported_at: str = ""


def _extract_text_from_content(content: Any) -> str:
    """Extract text from assistant message content blocks.

    Handles both plain string content and list-of-blocks format
    used by Claude Code's NDJSON output.
    """
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in cast("list[Any]", content):
        if isinstance(block, dict):
            block_dict = cast("dict[str, Any]", block)
            block_type = str(block_dict.get("type", ""))
            if block_type == "text":
                parts.append(str(block_dict.get("text", "")))
    return "\n".join(parts)


def _extract_tool_uses(content: Any) -> list[tuple[str, str]]:
    """Extract tool_use blocks from assistant message content.

    Returns:
        List of (tool_name, input_preview) tuples.
    """
    if not isinstance(content, list):
        return []

    results: list[tuple[str, str]] = []
    for block in cast("list[Any]", content):
        if isinstance(block, dict):
            block_dict = cast("dict[str, Any]", block)
            if str(block_dict.get("type", "")) == "tool_use":
                name = str(block_dict.get("name", ""))
                tool_input = block_dict.get("input", {})
                preview = str(tool_input)[:200] if tool_input else ""
                results.append((name, preview))
    return results


def parse_ndjson_log(log_path: Path) -> list[ConversationMessage]:
    """Parse a Claude Code NDJSON log into conversation messages.

    Handles event types: system, human, assistant, tool_use, tool_result.

    Args:
        log_path: Path to the NDJSON log file.

    Returns:
        Ordered list of conversation messages extracted from the log.
        Returns an empty list if the file does not exist or is empty.
    """
    if not log_path.exists():
        logger.warning("Log file not found: %s", log_path)
        return []

    messages: list[ConversationMessage] = []
    turn = 0

    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("Failed to read log file: %s", log_path)
        return []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(raw, dict):
            continue

        msg = cast("dict[str, Any]", raw)
        event_type = str(msg.get("type", ""))
        ts_raw = msg.get("timestamp")
        ts: float | None = float(ts_raw) if ts_raw is not None else None

        turn = _process_ndjson_event(event_type, msg, ts, turn, messages)

    return messages


def _process_ndjson_event(
    event_type: str,
    msg: dict[str, Any],
    ts: float | None,
    turn: int,
    messages: list[ConversationMessage],
) -> int:
    """Process a single NDJSON event and append to messages. Returns updated turn."""
    if event_type in ("system", "human"):
        return _process_simple_event(event_type, msg, ts, turn, messages)
    if event_type == "assistant":
        return _process_assistant_event(msg, ts, turn, messages)
    if event_type == "tool_result":
        return _process_tool_result_event(msg, ts, turn, messages)
    return turn


def _process_simple_event(
    event_type: str,
    msg: dict[str, Any],
    ts: float | None,
    turn: int,
    messages: list[ConversationMessage],
) -> int:
    """Process system or human event types."""
    role = "system" if event_type == "system" else "user"
    content = str(msg.get("message", msg.get("content", "")))
    if content:
        messages.append(ConversationMessage(role=role, content=content, timestamp=ts, turn_number=turn))
        return turn + 1
    return turn


def _process_assistant_event(
    msg: dict[str, Any],
    ts: float | None,
    turn: int,
    messages: list[ConversationMessage],
) -> int:
    """Process assistant event type including tool_use blocks."""
    message_data = msg.get("message", {})
    if isinstance(message_data, dict):
        content_raw = cast("dict[str, Any]", message_data).get("content", "")
    else:
        content_raw = msg.get("content", "")

    text_content = _extract_text_from_content(content_raw)
    if text_content:
        messages.append(ConversationMessage(role="assistant", content=text_content, timestamp=ts, turn_number=turn))
        turn += 1

    for tool_name, tool_input in _extract_tool_uses(content_raw):
        messages.append(
            ConversationMessage(
                role="assistant",
                content=tool_input,
                timestamp=ts,
                tool_name=tool_name,
                turn_number=turn,
            )
        )
        turn += 1
    return turn


def _process_tool_result_event(
    msg: dict[str, Any],
    ts: float | None,
    turn: int,
    messages: list[ConversationMessage],
) -> int:
    """Process tool_result event type."""
    tool_name = msg.get("tool", msg.get("name"))
    content = str(msg.get("content", msg.get("output", "")))
    messages.append(
        ConversationMessage(
            role="tool_result",
            content=content,
            timestamp=ts,
            tool_name=str(tool_name) if tool_name is not None else None,
            turn_number=turn,
        )
    )
    return turn + 1


def export_conversation(
    session_id: str,
    task_id: str,
    role: str,
    model: str,
    log_path: Path,
    tokens: int,
    cost: float,
    outcome: str,
) -> ConversationExport:
    """Build a conversation export from an agent session log.

    Args:
        session_id: Unique session identifier.
        task_id: Task identifier the agent was working on.
        role: Role assigned to the agent.
        model: Model used for the session.
        log_path: Path to the NDJSON log file.
        tokens: Total tokens consumed.
        cost: Total cost in USD.
        outcome: Session outcome string.

    Returns:
        A populated ConversationExport instance.
    """
    messages = parse_ndjson_log(log_path)
    return ConversationExport(
        session_id=session_id,
        task_id=task_id,
        agent_role=role,
        model=model,
        messages=messages,
        total_tokens=tokens,
        cost_usd=cost,
        outcome=outcome,
        exported_at=datetime.now(tz=UTC).isoformat(),
    )


def serialize_export(export: ConversationExport) -> str:
    """Serialize a ConversationExport to a JSON string.

    Args:
        export: The export to serialize.

    Returns:
        JSON string with 2-space indentation.
    """
    return json.dumps(asdict(export), indent=2)


def save_export(export: ConversationExport, output_dir: Path) -> Path:
    """Save a conversation export to disk as JSON.

    Creates the output directory if it does not exist. The file is
    named ``{session_id}.json``.

    Args:
        export: The export to save.
        output_dir: Directory to write the JSON file into.

    Returns:
        Path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{export.session_id}.json"
    path.write_text(serialize_export(export), encoding="utf-8")
    return path
