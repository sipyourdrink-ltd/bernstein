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

Security (audit-114):
- ``session_id`` arrives from an untrusted URL path parameter and is used
  verbatim as a filename for marker/sidecar/heartbeat files.  An attacker
  who can reach the endpoint (which is explicitly public because hooks
  fire from localhost) could otherwise submit values such as
  ``..%2F..%2Fruntime%2Fsignals%2FSHUTDOWN`` to escape the intended
  directory and forge completion markers or clobber runtime state.
- Primary defense: validate ``session_id`` with a conservative
  ``^[A-Za-z0-9_-]{1,128}$`` regex.  This rejects dots, slashes,
  backslashes, null bytes, whitespace, and every URL-decoded traversal
  character before any filesystem access happens.
- Defense in depth: every file write resolves the candidate path and
  verifies ``is_relative_to`` the intended base directory, so a symlink
  pointing outside or a future code change cannot silently reintroduce
  traversal.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Strict pattern for session_id values arriving from the URL path.
# Allows alphanumerics, underscore, and dash only — rejects dots, slashes,
# backslashes, null bytes, whitespace, and any URL-decoded traversal chars.
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class InvalidSessionIdError(ValueError):
    """Raised when a session_id fails validation.

    The HTTP layer maps this to a 400 response.  Callers that touch the
    filesystem also raise this defensively before opening any files.
    """


def validate_session_id(session_id: str) -> str:
    """Validate that ``session_id`` is a safe filename component.

    Args:
        session_id: The raw session identifier (typically from the URL
            path parameter).

    Returns:
        The validated ``session_id`` unchanged.

    Raises:
        InvalidSessionIdError: If the value is empty, too long, contains
            a null byte, contains any path separator or traversal
            character, or otherwise fails the strict allowlist regex.
    """
    if not isinstance(session_id, str):
        raise InvalidSessionIdError("session_id must be a string")
    # Fast-fail on the most dangerous characters so the error message is
    # precise even if the regex would have caught them anyway.
    if "\x00" in session_id:
        raise InvalidSessionIdError("session_id contains a null byte")
    if "/" in session_id or "\\" in session_id:
        raise InvalidSessionIdError("session_id contains a path separator")
    if ".." in session_id:
        raise InvalidSessionIdError("session_id contains '..'")
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise InvalidSessionIdError(
            "session_id must match ^[A-Za-z0-9_-]{1,128}$",
        )
    return session_id


def _safe_child(base: Path, session_id: str, *, suffix: str = "") -> Path:
    """Build a path under ``base`` for ``session_id`` and verify containment.

    The candidate path is resolved (following symlinks) and compared with
    the resolved base via ``Path.is_relative_to``.  Any path that escapes
    the base — whether through traversal characters, symlinks pointing
    elsewhere, or case-folding tricks on case-insensitive filesystems —
    raises :class:`InvalidSessionIdError`.

    Args:
        base: The intended containing directory (will be created if
            necessary by the caller before this function resolves it).
        session_id: A value that must already have passed
            :func:`validate_session_id`.
        suffix: Optional filename suffix (e.g. ``".jsonl"``).

    Returns:
        The validated, contained child path.

    Raises:
        InvalidSessionIdError: If the resolved child escapes ``base``.
    """
    validate_session_id(session_id)
    candidate = base / f"{session_id}{suffix}"
    try:
        resolved_base = base.resolve()
        # ``strict=False`` so we can resolve a file that does not yet exist.
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:  # pragma: no cover — defensive
        raise InvalidSessionIdError(f"could not resolve path: {exc}") from exc
    if not resolved_candidate.is_relative_to(resolved_base):
        raise InvalidSessionIdError(
            "resolved path escapes the hook base directory",
        )
    return resolved_candidate


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

    Raises:
        InvalidSessionIdError: If ``event.session_id`` fails validation
            or resolves outside the hooks directory (defense in depth).
    """
    hooks_dir = workdir / ".sdd" / "runtime" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    sidecar = _safe_child(hooks_dir, event.session_id, suffix=".jsonl")

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

    Raises:
        InvalidSessionIdError: If ``session_id`` fails validation or
            resolves outside the completion marker directory.
    """
    completed_dir = workdir / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True, exist_ok=True)
    marker = _safe_child(completed_dir, session_id)
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

    Raises:
        InvalidSessionIdError: If ``session_id`` fails validation or
            resolves outside the heartbeats directory.
    """
    heartbeat_dir = workdir / ".sdd" / "runtime" / "heartbeats"
    heartbeat_dir.mkdir(parents=True, exist_ok=True)
    hb_path = _safe_child(heartbeat_dir, session_id, suffix=".json")
    try:
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
