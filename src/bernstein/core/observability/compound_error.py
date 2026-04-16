"""Compound error rate tracking for multi-step agent tasks.

Tracks per-step success probability and calculates compound success
rates. The compound error problem (85% per step x 10 steps = 20%
end-to-end) is the key constraint in multi-agent systems.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class StepOutcome:
    """Outcome of a single agent step/task."""

    task_id: str
    model: str
    role: str
    complexity: str
    step_count: int
    success: bool
    timestamp: float


@dataclass
class CompoundErrorTracker:
    """Tracks compound error rates across orchestration runs."""

    outcomes: list[StepOutcome] = field(default_factory=list)
    _alert_threshold: float = 0.5  # alert when compound rate drops below this

    def record(self, outcome: StepOutcome) -> None:
        """Record a task outcome."""
        self.outcomes.append(outcome)
        compound = self.compound_success_rate()
        if compound < self._alert_threshold and len(self.outcomes) >= 5:
            logger.warning(
                "Compound success rate %.1f%% below threshold %.1f%%",
                compound * 100,
                self._alert_threshold * 100,
            )

    def per_step_success_rate(self) -> float:
        """Calculate per-step (per-task) success rate."""
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.success) / len(self.outcomes)

    def avg_step_count(self) -> float:
        """Average number of steps per task."""
        counts = [o.step_count for o in self.outcomes if o.step_count > 0]
        return sum(counts) / len(counts) if counts else 1.0

    def compound_success_rate(self, steps: int | None = None) -> float:
        """Calculate compound success rate: p_step ^ avg_steps.

        With 85% per-step accuracy and 10 steps, compound rate is ~20%.
        """
        p = self.per_step_success_rate()
        if p <= 0:
            return 0.0
        n = steps if steps is not None else self.avg_step_count()
        if n <= 0:
            return p
        return p**n

    def success_rate_by_model(self) -> dict[str, float]:
        """Per-model success rates."""
        by_model: dict[str, list[bool]] = {}
        for o in self.outcomes:
            by_model.setdefault(o.model, []).append(o.success)
        return {m: sum(v) / len(v) for m, v in by_model.items() if v}

    def avg_steps_by_model(self) -> dict[str, float]:
        """Average step counts per model."""
        by_model: dict[str, list[int]] = {}
        for o in self.outcomes:
            if o.step_count > 0:
                by_model.setdefault(o.model, []).append(o.step_count)
        return {m: sum(v) / len(v) for m, v in by_model.items() if v}

    def should_escalate_model(self, model: str, threshold: float = 0.6) -> bool:
        """Check if a model's compound rate is low enough to warrant escalation."""
        model_outcomes = [o for o in self.outcomes if o.model == model]
        if len(model_outcomes) < 3:
            return False
        p = sum(1 for o in model_outcomes if o.success) / len(model_outcomes)
        step_outcomes = [o for o in model_outcomes if o.step_count > 0]
        avg_steps = sum(o.step_count for o in step_outcomes) / len(step_outcomes) if step_outcomes else 1.0
        compound = p ** max(avg_steps, 1)
        return compound < threshold

    def to_summary(self) -> dict[str, Any]:
        """Summary for /status endpoint."""
        return {
            "total_outcomes": len(self.outcomes),
            "per_step_success_rate": round(self.per_step_success_rate(), 3),
            "avg_step_count": round(self.avg_step_count(), 1),
            "compound_success_rate": round(self.compound_success_rate(), 3),
            "by_model": {
                m: {
                    "success_rate": round(r, 3),
                    "avg_steps": round(self.avg_steps_by_model().get(m, 0), 1),
                }
                for m, r in self.success_rate_by_model().items()
            },
        }

    def save(self, path: Path) -> None:
        """Persist outcomes to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [
            {
                "task_id": o.task_id,
                "model": o.model,
                "role": o.role,
                "complexity": o.complexity,
                "step_count": o.step_count,
                "success": o.success,
                "timestamp": o.timestamp,
            }
            for o in self.outcomes
        ]
        path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls, path: Path, alert_threshold: float = 0.5) -> CompoundErrorTracker:
        """Load outcomes from file."""
        tracker = cls(_alert_threshold=alert_threshold)
        if path.exists():
            data = json.loads(path.read_text())
            for d in data:
                tracker.outcomes.append(StepOutcome(**d))
        return tracker
