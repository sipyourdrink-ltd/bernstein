"""Meta-message nudge manager for orchestrator behavior adjustments.

Extracted from orchestrator.py (ORCH-009) to reduce file size while
preserving the public API via backward-compatible re-exports.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OrchestratorNudge:
    """Meta-message for nudging orchestrator behavior.

    Attributes:
        nudge_type: Category of the nudge (e.g. ``"reprioritize"``, ``"scale_up"``).
        message: Human-readable description of the requested adjustment.
        priority: Urgency level (1=low, 2=medium, 3=high).
        timestamp: Unix timestamp when the nudge was created.
        metadata: Arbitrary key-value pairs for structured context.
        acknowledged: Whether the orchestrator has processed this nudge.
    """

    nudge_type: str
    message: str
    priority: int = 1
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    acknowledged: bool = False


class OrchestratorNudgeManager:
    """Thread-safe manager for orchestrator nudge messages.

    Agents or subsystems post nudges to influence orchestrator behavior
    (e.g. request scale-up, reprioritize work, trigger a review pass).
    The orchestrator drains pending nudges each tick.
    """

    def __init__(self) -> None:
        self.nudges: list[OrchestratorNudge] = []
        self._lock = threading.Lock()

    def add_nudge(
        self,
        nudge_type: str,
        message: str,
        priority: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> OrchestratorNudge:
        """Add a nudge to the orchestrator.

        Args:
            nudge_type: Category of the nudge.
            message: Description of the requested adjustment.
            priority: Urgency level (1=low, 2=medium, 3=high).
            metadata: Optional structured context.

        Returns:
            The created nudge instance.
        """
        nudge = OrchestratorNudge(
            nudge_type=nudge_type,
            message=message,
            priority=priority,
            metadata=metadata or {},
        )
        with self._lock:
            self.nudges.append(nudge)
        logger.info("Orchestrator nudge added: %s - %s", nudge_type, message)
        return nudge

    def get_pending_nudges(self, priority_threshold: int = 0) -> list[OrchestratorNudge]:
        """Get pending nudges above priority threshold.

        Args:
            priority_threshold: Minimum priority to include (0=all).

        Returns:
            List of unacknowledged nudges at or above the threshold.
        """
        with self._lock:
            return [nudge for nudge in self.nudges if not nudge.acknowledged and nudge.priority >= priority_threshold]

    def acknowledge_nudge(self, nudge: OrchestratorNudge) -> None:
        """Mark a nudge as acknowledged.

        Args:
            nudge: The nudge to acknowledge.
        """
        with self._lock:
            nudge.acknowledged = True

    def clear_acknowledged(self) -> None:
        """Remove all acknowledged nudges from the queue."""
        with self._lock:
            self.nudges = [nudge for nudge in self.nudges if not nudge.acknowledged]


# Module-level singleton for cross-module access
nudge_manager = OrchestratorNudgeManager()
_nudge_manager = nudge_manager  # backward-compat alias


def nudge_orchestrator(
    nudge_type: str,
    message: str,
    priority: int = 1,
    metadata: dict[str, Any] | None = None,
) -> OrchestratorNudge:
    """Send a meta-message nudge to the orchestrator.

    Args:
        nudge_type: Category of the nudge.
        message: Description of the requested adjustment.
        priority: Urgency level (1=low, 2=medium, 3=high).
        metadata: Optional structured context.

    Returns:
        The created nudge instance.
    """
    return _nudge_manager.add_nudge(nudge_type, message, priority, metadata)


def get_orchestrator_nudges(priority_threshold: int = 0) -> list[OrchestratorNudge]:
    """Get pending orchestrator nudges.

    Args:
        priority_threshold: Minimum priority to include (0=all).

    Returns:
        List of unacknowledged nudges at or above the threshold.
    """
    return _nudge_manager.get_pending_nudges(priority_threshold)
