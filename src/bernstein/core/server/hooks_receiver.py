"""Hooks receiver for Claude Code hook events.

Processes structured hook events (PostToolUse, Stop, PreCompact, SubagentStart,
SubagentStop) sent by Claude Code's built-in hook system via HTTP POST.

Each hook event is written to a JSONL sidecar file per session so the
orchestrator and token monitor can consume them without polling.

Design:
- Events arrive as JSON POSTs from Claude Code hooks configured in
  ``.claude/settings.local.json`` by the Claude adapter before spawning.
- Each event is appended to ``.sdd/runtime/hooks/{session_id}.jsonl``.
- The ``Stop`` event writes a completion marker for instant reaping
  (same file the wrapper script uses, but fires immediately from the hook
  rather than waiting for stream-json parsing).
- ``PostToolUse`` events update an activity timestamp file so the heartbeat
  monitor has a second source of liveness signals.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class HookEventType(Enum):
    """Known Claude Code hook event types."""

    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    PRE_COMPACT = "PreCompact"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    UNKNOWN = "Unknown"

    @classmethod
    def from_str(cls, value: str) -> HookEventType:
        """Parse a hook event name string into an enum value.

        Args:
            value: The raw event name from the hook payload.

        Returns:
            The matching ``HookEventType``, or ``UNKNOWN`` if unrecognised.
        """
        for member in cls:
            if member.value == value:
                return member
        return cls.UNKNOWN


@dataclass(frozen=True)
class HookEvent:
    """A single hook event received from Claude Code.

    Attributes:
        session_id: The agent session that produced this event.
        event_type: Parsed hook event type.
        raw_event_name: The original event name string from the payload.
        tool_name: Tool name (PostToolUse only).
        tool_input: Truncated tool input (PostToolUse only).
        timestamp: Unix epoch when the event was received.
        payload: Full raw payload for downstream consumers.
    """

    session_id: str
    event_type: HookEventType
    raw_event_name: str
    tool_name: str = ""
    tool_input: str = ""
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


def parse_hook_event(session_id: str, body: dict[str, Any]) -> HookEvent:
    """Parse a raw hook POST body into a typed ``HookEvent``.

    Args:
        session_id: Agent session identifier from the URL path.
        body: The JSON body of the hook POST request.

    Returns:
        A populated ``HookEvent`` instance.
    """
    raw_name = body.get("hook_event_name", "") or body.get("event", "")
    event_type = HookEventType.from_str(raw_name)

    tool_name = ""
    tool_input = ""
    if event_type == HookEventType.POST_TOOL_USE:
        tool_name = str(body.get("tool_name", ""))
        raw_input = body.get("tool_input", body.get("input", ""))
        tool_input = str(raw_input)[:200]  # Truncate for storage

    return HookEvent(
        session_id=session_id,
        event_type=event_type,
        raw_event_name=raw_name,
        tool_name=tool_name,
        tool_input=tool_input,
        timestamp=time.time(),
        payload=body,
    )


def write_hook_event(event: HookEvent, workdir: Path) -> None:
    """Append a hook event to the session's JSONL sidecar file.

    Creates ``.sdd/runtime/hooks/{session_id}.jsonl`` if it does not exist.

    Args:
        event: The parsed hook event to persist.
        workdir: Project working directory.
    """
    hooks_dir = workdir / ".sdd" / "runtime" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    sidecar = hooks_dir / f"{event.session_id}.jsonl"

    record: dict[str, Any] = {
        "ts": event.timestamp,
        "event": event.raw_event_name,
        "event_type": event.event_type.value,
    }
    if event.tool_name:
        record["tool_name"] = event.tool_name
    if event.tool_input:
        record["tool_input"] = event.tool_input

    try:
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.debug("Failed to write hook event for session %s", event.session_id)


def write_stop_marker(session_id: str, workdir: Path) -> None:
    """Write a completion marker when a Stop hook fires.

    Uses the same completion marker directory as the wrapper script so the
    orchestrator's existing reaping logic picks it up immediately.

    Args:
        session_id: Agent session identifier.
        workdir: Project working directory.
    """
    completed_dir = workdir / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    marker = completed_dir / session_id
    try:
        marker.write_text("hook:Stop", encoding="utf-8")
    except OSError:
        logger.debug("Failed to write stop marker for session %s", session_id)


def touch_heartbeat(session_id: str, workdir: Path) -> None:
    """Update the heartbeat file for a session from a hook event.

    Writes the current timestamp so the heartbeat monitor sees fresh
    activity without relying on the wrapper's heartbeat touch.

    Args:
        session_id: Agent session identifier.
        workdir: Project working directory.
    """
    heartbeat_dir = workdir / ".sdd" / "runtime" / "heartbeats"
    try:
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        hb_path = heartbeat_dir / f"{session_id}.json"
        hb_path.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        logger.debug("Failed to touch heartbeat for session %s", session_id)


def process_hook_event(event: HookEvent, workdir: Path) -> dict[str, str]:
    """Process a hook event: persist, update heartbeat, write markers.

    This is the main entry point called by the route handler.

    Args:
        event: The parsed hook event.
        workdir: Project working directory.

    Returns:
        A status dict suitable for the JSON response body.
    """
    # Always persist the event
    write_hook_event(event, workdir)

    # Always touch heartbeat for liveness
    touch_heartbeat(event.session_id, workdir)

    # Event-specific handling
    if event.event_type == HookEventType.STOP:
        write_stop_marker(event.session_id, workdir)
        logger.info("Hook Stop received for session %s — completion marker written", event.session_id)
        return {"status": "ok", "action": "stop_marker_written"}

    if event.event_type == HookEventType.PRE_COMPACT:
        logger.info("Hook PreCompact received for session %s — context pressure detected", event.session_id)
        return {"status": "ok", "action": "compaction_logged"}

    if event.event_type == HookEventType.SUBAGENT_START:
        logger.info("Hook SubagentStart received for session %s", event.session_id)
        return {"status": "ok", "action": "subagent_start_logged"}

    if event.event_type == HookEventType.SUBAGENT_STOP:
        logger.info("Hook SubagentStop received for session %s", event.session_id)
        return {"status": "ok", "action": "subagent_stop_logged"}

    if event.event_type == HookEventType.POST_TOOL_USE:
        logger.debug(
            "Hook PostToolUse received for session %s: tool=%s",
            event.session_id,
            event.tool_name,
        )
        return {"status": "ok", "action": "tool_use_logged"}

    return {"status": "ok", "action": "event_logged"}
