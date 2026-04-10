"""Multi-model conversation routing.

Routes different conversation phases (planning, implementation, review,
cleanup) to different models with per-phase turn budgets and effort levels.
This enables cost-efficient agent sessions that use heavier models only
where they add value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

logger = logging.getLogger(__name__)


class ConversationPhase(Enum):
    """Phases of an agent conversation session."""

    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class PhaseModelConfig:
    """Model configuration for a single conversation phase.

    Attributes:
        phase: Which conversation phase this config applies to.
        model: Short model name (e.g. "opus", "sonnet", "haiku").
        max_turns: Maximum turns allocated to this phase.
        effort: Effort level hint for the model (e.g. "high", "medium", "low").
    """

    phase: ConversationPhase
    model: str
    max_turns: int
    effort: str


@dataclass(frozen=True)
class ModelRoutingStrategy:
    """Complete routing strategy for a task's agent session.

    Attributes:
        task_id: The task this strategy applies to.
        phases: Per-phase model configurations.
    """

    task_id: str
    phases: tuple[PhaseModelConfig, ...]


DEFAULT_ROUTING: list[PhaseModelConfig] = [
    PhaseModelConfig(
        phase=ConversationPhase.PLANNING,
        model="opus",
        max_turns=5,
        effort="high",
    ),
    PhaseModelConfig(
        phase=ConversationPhase.IMPLEMENTATION,
        model="sonnet",
        max_turns=30,
        effort="high",
    ),
    PhaseModelConfig(
        phase=ConversationPhase.REVIEW,
        model="sonnet",
        max_turns=5,
        effort="medium",
    ),
    PhaseModelConfig(
        phase=ConversationPhase.CLEANUP,
        model="haiku",
        max_turns=5,
        effort="low",
    ),
]

# Lookup for fast default resolution by phase.
_DEFAULT_BY_PHASE: dict[ConversationPhase, PhaseModelConfig] = {
    cfg.phase: cfg for cfg in DEFAULT_ROUTING
}


def load_routing_strategy(task_data: dict[str, Any]) -> ModelRoutingStrategy | None:
    """Parse a ``ModelRoutingStrategy`` from a task's ``model_routing`` section.

    Expected task_data shape::

        {
            "id": "task-001",
            "model_routing": {
                "phases": [
                    {"phase": "planning", "model": "opus", "max_turns": 5, "effort": "high"},
                    ...
                ]
            }
        }

    Args:
        task_data: Raw task dict, typically from the task server.

    Returns:
        A ``ModelRoutingStrategy`` if ``model_routing`` is present, else ``None``.
    """
    routing_section: object = task_data.get("model_routing")
    if not isinstance(routing_section, dict):
        return None

    section = cast("dict[str, Any]", routing_section)
    raw_phases: object = section.get("phases")
    if not isinstance(raw_phases, list) or not raw_phases:
        return None

    phase_list = cast("list[Any]", raw_phases)
    parsed: list[PhaseModelConfig] = []
    for entry_obj in phase_list:
        if not isinstance(entry_obj, dict):
            continue
        entry = cast("dict[str, Any]", entry_obj)
        phase_str: str = str(entry.get("phase", ""))
        try:
            phase = ConversationPhase(phase_str)
        except ValueError:
            logger.warning("Unknown conversation phase %r, skipping", phase_str)
            continue
        parsed.append(
            PhaseModelConfig(
                phase=phase,
                model=str(entry.get("model", "sonnet")),
                max_turns=int(entry.get("max_turns", 10)),
                effort=str(entry.get("effort", "medium")),
            )
        )

    if not parsed:
        return None

    task_id = str(task_data.get("id", ""))
    return ModelRoutingStrategy(task_id=task_id, phases=tuple(parsed))


def get_phase_config(
    strategy: ModelRoutingStrategy,
    phase: ConversationPhase,
) -> PhaseModelConfig:
    """Return the config for *phase*, falling back to defaults.

    Args:
        strategy: The routing strategy to look up.
        phase: The conversation phase to get config for.

    Returns:
        The matching ``PhaseModelConfig`` from the strategy, or the default
        config for that phase.
    """
    for cfg in strategy.phases:
        if cfg.phase == phase:
            return cfg
    return _DEFAULT_BY_PHASE[phase]


def detect_phase(
    turn_number: int,
    total_turns: int,
    strategy: ModelRoutingStrategy,
) -> ConversationPhase:
    """Heuristically detect the current conversation phase from turn position.

    Allocation (percentage of *total_turns*):
        - First 20%: PLANNING
        - Next 60%: IMPLEMENTATION
        - Next 15%: REVIEW
        - Last 5%: CLEANUP

    ``turn_number`` is 0-indexed.  When *total_turns* is 0 or negative the
    function returns PLANNING as a safe default.

    Args:
        turn_number: Current turn (0-indexed).
        total_turns: Expected total turns for the session.
        strategy: The routing strategy (reserved for future per-strategy
            overrides; currently unused but kept in the signature for
            forward compatibility).

    Returns:
        The detected ``ConversationPhase``.
    """
    # Unused today, but part of the public API for future per-strategy
    # phase boundaries.
    _ = strategy

    if total_turns <= 0:
        return ConversationPhase.PLANNING

    # Clamp turn_number into [0, total_turns - 1].
    clamped = max(0, min(turn_number, total_turns - 1))
    fraction = clamped / total_turns

    if fraction < 0.20:
        return ConversationPhase.PLANNING
    if fraction < 0.80:
        return ConversationPhase.IMPLEMENTATION
    if fraction < 0.95:
        return ConversationPhase.REVIEW
    return ConversationPhase.CLEANUP
