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
- ``PostToolUse`` events are the call site for post-tool enforcement
  (:mod:`bernstein.core.security.post_tool_enforcement`): the tool input and
  the tool output the hook runner reported are inspected for secrets and
  redacted *before* the sidecar record is written, an audit record is appended
  to ``.sdd/metrics/tool_audit.jsonl``, and a dangerous pattern writes a
  ``TOOL_ABORT`` signal through the existing abort chain rather than only
  setting a flag.  This is the mirror of the pre-tool ``check_secrets`` flow;
  the receiver is the one place in the live path where post-tool data reaches
  persistent storage.

Security:
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
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_path,
)
from bernstein.core.security.post_tool_enforcement import (
    redact_tool_output,
    run_post_tool_enforcement,
)
from bernstein.core.tasks.abort_chain import AbortChain

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Strict pattern for session_id values arriving from the URL path.
# Allows alphanumerics, underscore, and dash only - rejects dots, slashes,
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
    the base - whether through traversal characters, symlinks pointing
    elsewhere, or case-folding tricks on case-insensitive filesystems -
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
    try:
        return contained_path(base, f"{session_id}{suffix}", label="session id")
    except PathContainmentError as exc:
        # The barrier's message names the identifier and omits the base, which
        # is the property this error already had and must keep.
        raise InvalidSessionIdError(
            "resolved path escapes the hook base directory",
        ) from exc
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        raise InvalidSessionIdError(f"could not resolve path: {exc}") from exc


class HookEventType(Enum):
    """Known Claude Code hook event types."""

    POST_TOOL_USE = "PostToolUse"
    STOP = "Stop"
    PRE_COMPACT = "PreCompact"
    SUBAGENT_START = "SubagentStart"
    SUBAGENT_STOP = "SubagentStop"
    #: In-process verification-gate decision streamed by the gate hook (#2360).
    #: Each record links a block/allow decision to the gate receipt it sealed,
    #: so the sidecar an operator already reads carries the enforcement trail.
    GATE_DECISION = "GateDecision"
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
        tool_output: Tool output as reported by the hook runner (PostToolUse
            only).  Kept untruncated so post-tool enforcement inspects the whole
            text; ``process_hook_event`` replaces it with the redacted form
            before anything is persisted.  Empty when the payload carries no
            output field.
        timestamp: Unix epoch when the event was received.
        payload: Full raw payload for downstream consumers.
    """

    session_id: str
    event_type: HookEventType
    raw_event_name: str
    tool_name: str = ""
    tool_input: str = ""
    tool_output: str = ""
    timestamp: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict[str, Any])


#: Payload keys a hook runner may use for the text a tool produced.  The adapter
#: forwards the runner's body verbatim (``BODY=$(cat)``), so which of these is
#: present is the runner's choice, not a Bernstein protocol: the receiver reads
#: whichever it finds and enforces on an empty string when it finds none.
_TOOL_OUTPUT_KEYS: tuple[str, ...] = ("tool_response", "tool_output", "output")


def _extract_tool_output(body: dict[str, Any]) -> str:
    """Return the tool output carried by a ``PostToolUse`` payload, as text.

    A structured response is serialised rather than dropped - a secret inside a
    nested field must still be visible to the redaction patterns.

    Args:
        body: The JSON body of the hook POST request.

    Returns:
        The tool output as a string, or ``""`` when the payload carries none.
    """
    for key in _TOOL_OUTPUT_KEYS:
        if key not in body:
            continue
        raw = body[key]
        if raw is None:
            return ""
        if isinstance(raw, str):
            return raw
        try:
            return json.dumps(raw, sort_keys=True, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(raw)
    return ""


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
    tool_output = ""
    if event_type == HookEventType.POST_TOOL_USE:
        tool_name = str(body.get("tool_name", ""))
        raw_input = body.get("tool_input", body.get("input", ""))
        tool_input = str(raw_input)[:200]  # Truncate for storage
        tool_output = _extract_tool_output(body)

    return HookEvent(
        session_id=session_id,
        event_type=event_type,
        raw_event_name=raw_name,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_output=tool_output,
        timestamp=time.time(),
        payload=body,
    )


def write_hook_event(event: HookEvent, workdir: Path) -> None:
    """Append a hook event to the session's JSONL sidecar file.

    Creates ``.sdd/runtime/hooks/{session_id}.jsonl`` if it does not exist.

    Writes ``event`` as given.  For ``PostToolUse`` the caller is expected to
    have run :func:`_enforce_post_tool_use` first; the redaction lives there and
    not here so the audit record and the persisted record are produced from one
    decision.

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
    if event.tool_output:
        # Truncated for storage on the same budget as the input.  Redaction has
        # already run over the *whole* text in ``process_hook_event``, so a
        # secret straddling the cut is replaced before it can be halved.
        record["tool_output"] = event.tool_output[:200]

    try:
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.debug("Failed to write hook event for session %s", event.session_id)


def write_gate_decision_event(
    session_id: str,
    workdir: Path,
    *,
    gate_event: str,
    blocked: bool,
    reason: str,
    receipt_task_id: str,
) -> None:
    """Append an in-process gate decision to the session's JSONL sidecar (#2360).

    The gate hook streams every block/allow decision into the same sidecar the
    orchestrator and monitors already consume, linked to the ``receipt_task_id``
    of the evidence bundle the decision sealed. This is the event-to-receipt
    mapping in ingestion: the enforcement trail lives beside the tool-use trail,
    and a verifier can pull the named receipt from the audit chain.

    Args:
        session_id: Agent session identifier (validated before any fs access).
        workdir: Project working directory.
        gate_event: ``"pretooluse"`` or ``"completion"``.
        blocked: Whether the action was refused.
        reason: Human-readable decision reason.
        receipt_task_id: The task id of the sealed gate receipt.

    Raises:
        InvalidSessionIdError: If ``session_id`` fails validation or resolves
            outside the hooks directory.
    """
    hooks_dir = workdir / ".sdd" / "runtime" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    sidecar = _safe_child(hooks_dir, session_id, suffix=".jsonl")

    record: dict[str, Any] = {
        "ts": time.time(),
        "event": HookEventType.GATE_DECISION.value,
        "event_type": HookEventType.GATE_DECISION.value,
        "gate_event": gate_event,
        "blocked": blocked,
        "reason": reason,
        "receipt_task_id": receipt_task_id,
    }
    try:
        with sidecar.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        logger.debug("Failed to write gate decision for session %s", session_id)


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


def _enforce_post_tool_use(event: HookEvent, workdir: Path) -> tuple[HookEvent, bool]:
    """Run post-tool enforcement over a ``PostToolUse`` event.

    The mirror of the pre-tool ``check_secrets`` flow, at the seam where
    post-tool data first reaches persistent storage.  Redaction happens over the
    full text and *before* the caller persists anything, the audit record lands
    under ``.sdd/metrics/``, and a dangerous pattern writes a ``TOOL_ABORT``
    record through the abort chain that already owns per-tool refusals - so a
    ``should_block`` verdict reaches the signals directory an agent reads rather
    than dying in a return value.

    Args:
        event: The parsed ``PostToolUse`` event, still carrying raw text.
        workdir: Project working directory.

    Returns:
        ``(redacted_event, blocked)`` - the event whose input, output and raw
        payload no longer carry detected secrets, and whether continuation was
        refused.
    """
    # ``parse_hook_event`` truncates the input for storage, so redacting
    # ``event.tool_input`` would work on a copy already cut at 200 characters and
    # leave the first half of a straddling secret on disk.  Redact the whole
    # payload value, then cut.
    raw_input = str(event.payload.get("tool_input", event.payload.get("input", "")))
    redacted_input = redact_tool_output(raw_input)

    result = run_post_tool_enforcement(
        session_id=event.session_id,
        tool=event.tool_name,
        tool_input={"tool_input": redacted_input},
        raw_output=event.tool_output,
        workdir=workdir,
    )

    redacted_payload = dict(event.payload)
    for key in _TOOL_OUTPUT_KEYS:
        if key in redacted_payload:
            redacted_payload[key] = result.redacted_output
            break
    for key in ("tool_input", "input"):
        if key in redacted_payload:
            redacted_payload[key] = redacted_input
            break

    redacted_event = replace(
        event,
        tool_input=redacted_input[:200],
        tool_output=result.redacted_output,
        payload=redacted_payload,
    )

    if not result.should_block:
        return redacted_event, False

    AbortChain(signals_dir=workdir / ".sdd" / "runtime" / "signals").abort_tool(
        event.session_id,
        event.tool_name,
        "post-tool enforcement: dangerous pattern in tool output",
    )
    logger.warning(
        "Post-tool enforcement blocked continuation for session %s: tool=%s",
        event.session_id,
        event.tool_name,
    )
    return redacted_event, True


def process_hook_event(event: HookEvent, workdir: Path) -> dict[str, str]:
    """Process a hook event: enforce, persist, update heartbeat, write markers.

    This is the main entry point called by the route handler.  A
    ``PostToolUse`` event goes through :func:`_enforce_post_tool_use` first, so
    the record that is persisted and the payload handed to downstream consumers
    carry redacted text rather than whatever the tool printed.

    Args:
        event: The parsed hook event.
        workdir: Project working directory.

    Returns:
        A status dict suitable for the JSON response body.  A ``PostToolUse``
        whose output tripped a dangerous pattern reports
        ``"action": "tool_use_blocked"``.
    """
    # Post-tool enforcement runs before anything is written: the sidecar, the
    # heartbeat and every downstream consumer see redacted text only.
    blocked = False
    if event.event_type == HookEventType.POST_TOOL_USE:
        event, blocked = _enforce_post_tool_use(event, workdir)

    # Always persist the event
    write_hook_event(event, workdir)

    # Always touch heartbeat for liveness
    touch_heartbeat(event.session_id, workdir)

    # Event-specific handling
    if event.event_type == HookEventType.STOP:
        write_stop_marker(event.session_id, workdir)
        logger.info("Hook Stop received for session %s - completion marker written", event.session_id)
        return {"status": "ok", "action": "stop_marker_written"}

    if event.event_type == HookEventType.GATE_DECISION:
        logger.info(
            "Hook GateDecision received for session %s: blocked=%s",
            event.session_id,
            event.payload.get("blocked"),
        )
        return {"status": "ok", "action": "gate_decision_logged"}

    if event.event_type == HookEventType.PRE_COMPACT:
        logger.info("Hook PreCompact received for session %s - context pressure detected", event.session_id)
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
        if blocked:
            return {"status": "ok", "action": "tool_use_blocked"}
        return {"status": "ok", "action": "tool_use_logged"}

    return {"status": "ok", "action": "event_logged"}
