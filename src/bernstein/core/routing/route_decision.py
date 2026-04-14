"""Route decision tracking — explains why agent/model was chosen for a task."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    """Records why a specific agent/model was chosen for a task.

    Attributes:
        task_id: Task identifier.
        adapter: Adapter name chosen.
        model: Model name chosen.
        effort: Effort level chosen.
        reasons: List of human-readable reason strings.
        timestamp: Unix timestamp of decision.
    """

    task_id: str
    adapter: str
    model: str
    effort: str
    reasons: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "model": self.model,
            "effort": self.effort,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RouteDecision:
        """Deserialize from dictionary."""
        return cls(
            task_id=data.get("task_id", ""),
            adapter=data.get("adapter", ""),
            model=data.get("model", ""),
            effort=data.get("effort", ""),
            reasons=data.get("reasons", []),
            timestamp=data.get("timestamp", time.time()),
        )


class RouteDecisionTracker:
    """Track and store routing decisions.

    Stores decisions to .sdd/metrics/routing_decisions.jsonl for later analysis.

    Args:
        workdir: Project working directory.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._metrics_dir = workdir / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        self._filepath = self._metrics_dir / "routing_decisions.jsonl"
        self._decisions: list[RouteDecision] = []

    def record(self, decision: RouteDecision) -> None:
        """Record a routing decision.

        Args:
            decision: RouteDecision instance.
        """
        self._decisions.append(decision)
        self._write_decision(decision)

        logger.info(
            "Task %s routed to %s/%s (%s) — %s",
            decision.task_id,
            decision.adapter,
            decision.model,
            decision.effort,
            "; ".join(decision.reasons[:2]),
        )

    def _write_decision(self, decision: RouteDecision) -> None:
        """Write decision to JSONL file."""
        with self._filepath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(decision.to_dict()) + "\n")

    def get_decision(self, task_id: str) -> RouteDecision | None:
        """Get routing decision for a specific task.

        Args:
            task_id: Task identifier.

        Returns:
            RouteDecision or None if not found.
        """
        for decision in self._decisions:
            if decision.task_id == task_id:
                return decision
        return None

    def get_all_decisions(self, limit: int = 100) -> list[RouteDecision]:
        """Get all routing decisions.

        Args:
            limit: Maximum number of decisions to return.

        Returns:
            List of RouteDecision instances.
        """
        return self._decisions[-limit:]

    def load_from_file(self) -> int:
        """Load decisions from file.

        Returns:
            Number of decisions loaded.
        """
        if not self._filepath.exists():
            return 0

        count = 0
        with self._filepath.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    self._decisions.append(RouteDecision.from_dict(data))
                    count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

        return count


def format_routing_reasons(
    task_id: str,
    adapter: str,
    model: str,
    effort: str,
    complexity: str,
    role: str,
    priority: int,
    skill_profile_success_rate: float | None = None,
) -> list[str]:
    """Format human-readable routing reasons.

    Args:
        _task_id: Task identifier (part of interface).
        adapter: Adapter chosen.
        model: Model chosen.
        effort: Effort level.
        complexity: Task complexity.
        role: Task role.
        priority: Task priority.
        skill_profile_success_rate: Optional success rate from skill profile.

    Returns:
        List of reason strings.
    """
    _ = task_id  # Part of interface; not included in reason strings
    reasons: list[str] = []

    # Complexity-based reasoning
    if complexity == "high":
        reasons.append(f"complexity=high → {model}")
    elif complexity == "low":
        reasons.append("complexity=low → cheaper model")

    # Role-based reasoning
    if role in ("security", "architect"):
        reasons.append(f"role={role} → requires audit model")
    elif role == "manager":
        reasons.append("role=manager → premium model for planning")

    # Priority-based reasoning
    if priority == 1:
        reasons.append("priority=critical → best model")

    # Skill profile reasoning
    if skill_profile_success_rate is not None:
        reasons.append(f"skill_profile: {adapter} has {skill_profile_success_rate:.0f}% success rate for {role} tasks")

    # Effort reasoning
    if effort == "max":
        reasons.append("effort=max → thorough analysis")
    elif effort == "low":
        reasons.append("effort=low → quick task")

    return reasons
