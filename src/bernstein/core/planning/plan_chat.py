"""Conversational plan refinement via natural language intent parsing.

Provides a chat-based interface for refining Bernstein plan YAML files.
Users type natural language messages (e.g. "add a stage for testing") and
the system classifies intent using keyword matching, applies the change to
the in-memory plan dict, and generates a human-readable response describing
what changed.

Pure Python -- no LLM calls.  All intent classification is deterministic
keyword matching.
"""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# Type alias for plan dicts (YAML-loaded mappings).
PlanDict = dict[str, Any]

# Type alias for a single stage dict inside a plan.
StageDict = dict[str, Any]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DeltaAction(Enum):
    """Actions that can be applied to a plan."""

    ADD_STAGE = "add_stage"
    REMOVE_STAGE = "remove_stage"
    MODIFY_STAGE = "modify_stage"
    REORDER = "reorder"
    ADD_DEPENDENCY = "add_dependency"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChatMessage:
    """A single message in the chat session.

    Attributes:
        role: Who sent the message -- "user", "assistant", or "system".
        content: The message text.
        timestamp: Unix epoch timestamp when the message was created.
    """

    role: str
    content: str
    timestamp: float


@dataclass(frozen=True)
class PlanDelta:
    """A single atomic change to apply to a plan.

    Attributes:
        action: The kind of change (add, remove, modify, reorder, add_dependency).
        target_stage: The stage name this change targets.
        details: Extra information about the change (e.g. new stage description,
            dependency source, position hint).
    """

    action: DeltaAction
    target_stage: str
    details: str


def _empty_messages() -> list[ChatMessage]:
    return []


def _empty_plan() -> PlanDict:
    return {}


def _empty_deltas() -> list[PlanDelta]:
    return []


@dataclass
class ChatSession:
    """Holds conversational state for an interactive plan refinement session.

    Attributes:
        messages: Ordered list of chat messages exchanged so far.
        current_plan: The live plan dict being modified.
        deltas: All deltas that have been applied during this session.
    """

    messages: list[ChatMessage] = field(default_factory=_empty_messages)
    current_plan: PlanDict = field(default_factory=_empty_plan)
    deltas: list[PlanDelta] = field(default_factory=_empty_deltas)


# ---------------------------------------------------------------------------
# Intent parsing (keyword matching -- no LLM)
# ---------------------------------------------------------------------------

# Compiled patterns for intent classification.  Order matters: more specific
# patterns are tried first.

_ADD_STAGE_RE = re.compile(
    r"add\s+(?:a\s+)?(?:new\s+)?stage\s+(?:for\s+|named\s+|called\s+)?(.+)",
    re.IGNORECASE,
)
_REMOVE_STAGE_RE = re.compile(
    r"(?:remove|delete|drop)\s+(?:the\s+)?stage\s+(?:named\s+|called\s+)?(.+)",
    re.IGNORECASE,
)
_ADD_DEPENDENCY_RE = re.compile(
    r"make\s+(.+?)\s+depend\s+on\s+(.+)",
    re.IGNORECASE,
)
_REORDER_BEFORE_RE = re.compile(
    r"move\s+(.+?)\s+before\s+(.+)",
    re.IGNORECASE,
)
_REORDER_AFTER_RE = re.compile(
    r"move\s+(.+?)\s+after\s+(.+)",
    re.IGNORECASE,
)
_MODIFY_STAGE_RE = re.compile(
    r"change\s+(.+?)\s+to\s+(.+)",
    re.IGNORECASE,
)
_RENAME_STAGE_RE = re.compile(
    r"rename\s+(?:stage\s+)?(.+?)\s+to\s+(.+)",
    re.IGNORECASE,
)


def _strip_quotes(text: str) -> str:
    """Remove surrounding quotes and extra whitespace from captured text."""
    text = text.strip().strip("'\"").strip()
    return text


def parse_user_intent(message: str) -> PlanDelta | None:
    """Classify a user message into a PlanDelta using keyword matching.

    Supports the following natural language patterns:

    - "add a stage for X" / "add stage named X" -> add_stage
    - "remove stage X" / "delete stage X" -> remove_stage
    - "make X depend on Y" -> add_dependency
    - "move X before Y" / "move X after Y" -> reorder
    - "change X to Y" / "rename X to Y" -> modify_stage

    Args:
        message: Raw user input string.

    Returns:
        A PlanDelta describing the parsed intent, or ``None`` if the message
        does not match any known pattern.
    """
    text = message.strip()

    # --- add dependency (must come before add_stage to avoid "add" prefix match) ---
    m = _ADD_DEPENDENCY_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.ADD_DEPENDENCY,
            target_stage=_strip_quotes(m.group(1)),
            details=_strip_quotes(m.group(2)),
        )

    # --- add stage ---
    m = _ADD_STAGE_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.ADD_STAGE,
            target_stage=_strip_quotes(m.group(1)),
            details="",
        )

    # --- remove stage ---
    m = _REMOVE_STAGE_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.REMOVE_STAGE,
            target_stage=_strip_quotes(m.group(1)),
            details="",
        )

    # --- reorder before ---
    m = _REORDER_BEFORE_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.REORDER,
            target_stage=_strip_quotes(m.group(1)),
            details=f"before:{_strip_quotes(m.group(2))}",
        )

    # --- reorder after ---
    m = _REORDER_AFTER_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.REORDER,
            target_stage=_strip_quotes(m.group(1)),
            details=f"after:{_strip_quotes(m.group(2))}",
        )

    # --- rename stage (more specific than modify, try first) ---
    m = _RENAME_STAGE_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.MODIFY_STAGE,
            target_stage=_strip_quotes(m.group(1)),
            details=_strip_quotes(m.group(2)),
        )

    # --- modify stage ---
    m = _MODIFY_STAGE_RE.match(text)
    if m:
        return PlanDelta(
            action=DeltaAction.MODIFY_STAGE,
            target_stage=_strip_quotes(m.group(1)),
            details=_strip_quotes(m.group(2)),
        )

    return None


# ---------------------------------------------------------------------------
# Plan manipulation
# ---------------------------------------------------------------------------


def _get_stages(plan: PlanDict) -> list[StageDict]:
    """Return the stages list from a plan, or an empty list if absent."""
    raw: Any = plan.get("stages")
    if isinstance(raw, list):
        return list(raw)  # type: ignore[no-any-return]
    return []


def _find_stage_index(plan: PlanDict, name: str) -> int | None:
    """Return the index of the stage with the given name, or None."""
    stages: list[StageDict] = _get_stages(plan)
    for i, stage in enumerate(stages):
        stage_name: Any = stage.get("name")
        if stage_name == name:
            return i
    return None


def _ensure_stages(plan: PlanDict) -> list[StageDict]:
    """Return the stages list from a plan, creating it if absent."""
    raw: Any = plan.get("stages")
    if isinstance(raw, list):
        result: list[StageDict] = raw  # type: ignore[assignment]
        return result
    new_stages: list[StageDict] = []
    plan["stages"] = new_stages
    return new_stages


class PlanDeltaError(Exception):
    """Raised when a delta cannot be applied to a plan."""


def apply_delta(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Apply a PlanDelta to a plan dict, returning the modified plan.

    The input plan dict is modified in place *and* returned for convenience.

    Args:
        plan: Mutable plan dict (typically loaded from YAML).
        delta: The change to apply.

    Returns:
        The same plan dict, modified.

    Raises:
        PlanDeltaError: If the delta cannot be applied (e.g. target stage not
            found, duplicate stage name on add).
    """
    if delta.action == DeltaAction.ADD_STAGE:
        return _apply_add_stage(plan, delta)
    if delta.action == DeltaAction.REMOVE_STAGE:
        return _apply_remove_stage(plan, delta)
    if delta.action == DeltaAction.MODIFY_STAGE:
        return _apply_modify_stage(plan, delta)
    if delta.action == DeltaAction.REORDER:
        return _apply_reorder(plan, delta)
    if delta.action == DeltaAction.ADD_DEPENDENCY:
        return _apply_add_dependency(plan, delta)
    msg = f"Unknown delta action: {delta.action}"
    raise PlanDeltaError(msg)


def _apply_add_stage(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Add a new stage to the end of the stages list."""
    stages = _ensure_stages(plan)
    if _find_stage_index(plan, delta.target_stage) is not None:
        msg = f"Stage {delta.target_stage!r} already exists"
        raise PlanDeltaError(msg)
    new_stage: PlanDict = {
        "name": delta.target_stage,
        "steps": [{"title": f"Implement {delta.target_stage}", "role": "backend"}],
    }
    stages.append(new_stage)
    return plan


def _apply_remove_stage(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Remove a stage by name."""
    idx = _find_stage_index(plan, delta.target_stage)
    if idx is None:
        msg = f"Stage {delta.target_stage!r} not found"
        raise PlanDeltaError(msg)
    stages = _ensure_stages(plan)
    stages.pop(idx)
    return plan


def _apply_modify_stage(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Rename a stage (modify its name)."""
    idx = _find_stage_index(plan, delta.target_stage)
    if idx is None:
        msg = f"Stage {delta.target_stage!r} not found"
        raise PlanDeltaError(msg)
    stages = _ensure_stages(plan)
    stage: StageDict = stages[idx]
    stage["name"] = delta.details
    return plan


def _apply_reorder(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Move a stage before or after another stage."""
    stages = _ensure_stages(plan)
    src_idx = _find_stage_index(plan, delta.target_stage)
    if src_idx is None:
        msg = f"Stage {delta.target_stage!r} not found"
        raise PlanDeltaError(msg)

    # Parse "before:X" or "after:X" from details
    if ":" not in delta.details:
        msg = f"Invalid reorder details: {delta.details!r}"
        raise PlanDeltaError(msg)
    direction, ref_name = delta.details.split(":", 1)

    ref_idx = _find_stage_index(plan, ref_name)
    if ref_idx is None:
        msg = f"Reference stage {ref_name!r} not found"
        raise PlanDeltaError(msg)

    # Remove source stage first
    stage = stages.pop(src_idx)

    # Recalculate ref_idx after removal
    ref_idx_after = _find_stage_index(plan, ref_name)
    if ref_idx_after is None:
        # Should not happen, but be safe
        stages.append(stage)
        return plan

    if direction == "before":
        stages.insert(ref_idx_after, stage)
    else:  # "after"
        stages.insert(ref_idx_after + 1, stage)

    return plan


def _apply_add_dependency(plan: PlanDict, delta: PlanDelta) -> PlanDict:
    """Add a depends_on entry to the target stage."""
    idx = _find_stage_index(plan, delta.target_stage)
    if idx is None:
        msg = f"Stage {delta.target_stage!r} not found"
        raise PlanDeltaError(msg)

    # Verify the dependency stage exists
    dep_idx = _find_stage_index(plan, delta.details)
    if dep_idx is None:
        msg = f"Dependency stage {delta.details!r} not found"
        raise PlanDeltaError(msg)

    stages = _ensure_stages(plan)
    stage: StageDict = stages[idx]
    deps_raw: Any = stage.get("depends_on")
    if not isinstance(deps_raw, list):
        deps_raw = []
        stage["depends_on"] = deps_raw
    deps: list[str] = deps_raw  # type: ignore[assignment]
    if delta.details not in deps:
        deps.append(delta.details)

    return plan


# ---------------------------------------------------------------------------
# Response generation
# ---------------------------------------------------------------------------


def generate_assistant_response(delta: PlanDelta, plan: PlanDict) -> str:
    """Generate a human-readable description of what was changed.

    Args:
        delta: The delta that was just applied.
        plan: The plan after the delta was applied.

    Returns:
        A sentence describing the change.
    """
    stage_count: int = len(_get_stages(plan))

    if delta.action == DeltaAction.ADD_STAGE:
        return f"Added stage {delta.target_stage!r}. The plan now has {stage_count} stage(s)."
    if delta.action == DeltaAction.REMOVE_STAGE:
        return f"Removed stage {delta.target_stage!r}. The plan now has {stage_count} stage(s)."
    if delta.action == DeltaAction.MODIFY_STAGE:
        return f"Renamed stage {delta.target_stage!r} to {delta.details!r}."
    if delta.action == DeltaAction.REORDER:
        direction, ref = delta.details.split(":", 1) if ":" in delta.details else ("", delta.details)
        return f"Moved stage {delta.target_stage!r} {direction} {ref!r}."
    if delta.action == DeltaAction.ADD_DEPENDENCY:
        return f"Stage {delta.target_stage!r} now depends on {delta.details!r}."
    return f"Applied {delta.action.value} to {delta.target_stage!r}."


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def start_session(plan_path: Path) -> ChatSession:
    """Load a plan YAML file and start a new chat session.

    Args:
        plan_path: Path to the YAML plan file.

    Returns:
        A new ChatSession with the plan loaded and a system greeting.

    Raises:
        FileNotFoundError: If the plan file does not exist.
        yaml.YAMLError: If the file is not valid YAML.
    """
    if not plan_path.exists():
        msg = f"Plan file not found: {plan_path}"
        raise FileNotFoundError(msg)

    raw = plan_path.read_text()
    parsed: Any = yaml.safe_load(raw)
    if not isinstance(parsed, dict):
        msg = "Plan file must be a YAML mapping"
        raise ValueError(msg)

    data: PlanDict = cast("PlanDict", parsed)
    plan_name: str = str(data.get("name", "unnamed plan"))
    stage_count: int = len(_get_stages(data))

    greeting = ChatMessage(
        role="system",
        content=(
            f"Loaded plan {plan_name!r} with {stage_count} stage(s). "
            "You can add, remove, rename, reorder stages, or add dependencies. "
            "Type your changes in natural language."
        ),
        timestamp=time.time(),
    )

    session = ChatSession(
        messages=[greeting],
        current_plan=data,
        deltas=[],
    )
    return session


def process_message(session: ChatSession, user_message: str) -> str:
    """Parse a user message, apply the delta, and return the assistant response.

    Modifies the session in place: appends messages, updates the plan, and
    records the delta.

    Args:
        session: The active chat session.
        user_message: The raw text from the user.

    Returns:
        The assistant's response string.
    """
    now = time.time()

    user_msg = ChatMessage(role="user", content=user_message, timestamp=now)
    session.messages.append(user_msg)

    delta = parse_user_intent(user_message)
    if delta is None:
        response_text = (
            "I didn't understand that. Try phrases like:\n"
            '  - "add a stage for testing"\n'
            '  - "remove stage deployment"\n'
            '  - "make testing depend on build"\n'
            '  - "move testing before deployment"\n'
            '  - "rename build to compilation"'
        )
        assistant_msg = ChatMessage(role="assistant", content=response_text, timestamp=time.time())
        session.messages.append(assistant_msg)
        return response_text

    try:
        apply_delta(session.current_plan, delta)
    except PlanDeltaError as exc:
        error_text = f"Could not apply change: {exc}"
        assistant_msg = ChatMessage(role="assistant", content=error_text, timestamp=time.time())
        session.messages.append(assistant_msg)
        return error_text

    session.deltas.append(delta)
    response_text = generate_assistant_response(delta, session.current_plan)
    assistant_msg = ChatMessage(role="assistant", content=response_text, timestamp=time.time())
    session.messages.append(assistant_msg)
    return response_text


# ---------------------------------------------------------------------------
# Plan diff rendering
# ---------------------------------------------------------------------------


def render_plan_diff(before: PlanDict, after: PlanDict) -> str:
    """Show what changed between two plan versions as a human-readable diff.

    Compares the YAML serialization of both plans and produces a unified-style
    diff showing added (+) and removed (-) lines.

    Args:
        before: The plan dict before changes.
        after: The plan dict after changes.

    Returns:
        A multi-line string showing the differences.  Returns
        "No changes." if the plans are identical.
    """
    before_yaml = yaml.dump(before, default_flow_style=False, sort_keys=False)
    after_yaml = yaml.dump(after, default_flow_style=False, sort_keys=False)

    if before_yaml == after_yaml:
        return "No changes."

    before_lines = before_yaml.splitlines()
    after_lines = after_yaml.splitlines()

    # Simple line-by-line diff
    import difflib

    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )

    if not diff_lines:
        return "No changes."

    return "\n".join(diff_lines)


def snapshot_plan(plan: PlanDict) -> PlanDict:
    """Return a deep copy of a plan dict for before/after comparison.

    Args:
        plan: The plan dict to snapshot.

    Returns:
        A deep copy of the plan.
    """
    return copy.deepcopy(plan)
