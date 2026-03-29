"""Agent SLOs (Service Level Objectives) and Error Budget tracking.

Defines measurable targets for agent orchestration and automatically
adjusts orchestrator behavior when error budgets are depleted.

SLO targets:
- Task success rate: >90%
- P95 task completion time: <300s (5 minutes)
- Cost per task: <$0.50 average
- Zero secret leaks
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.metric_collector import MetricsCollector

logger = logging.getLogger(__name__)


class SLOStatus(StrEnum):
    """Traffic-light status for an SLO."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class ErrorBudgetAction(StrEnum):
    """Actions triggered when error budget is depleted."""

    REDUCE_AGENTS = "reduce_agents"
    UPGRADE_MODEL = "upgrade_model"
    INCREASE_REVIEW = "increase_review"


@dataclass
class SLOTarget:
    """A single SLO target with threshold and current value."""

    name: str
    description: str
    target: float
    warning_threshold: float  # Below this = yellow
    current: float = 0.0
    window_seconds: int = 3600  # 1-hour rolling window

    @property
    def status(self) -> SLOStatus:
        """Compute traffic-light status."""
        if self.current >= self.target:
            return SLOStatus.GREEN
        if self.current >= self.warning_threshold:
            return SLOStatus.YELLOW
        return SLOStatus.RED

    @property
    def met(self) -> bool:
        return self.current >= self.target


@dataclass
class ErrorBudget:
    """Error budget computed from SLO targets.

    Error budget = allowed failures before SLO is breached.
    When budget is depleted, automatic remediation kicks in.
    """

    total_tasks: int = 0
    failed_tasks: int = 0
    slo_target: float = 0.90  # 90% success rate
    _depleted_since: float | None = None

    @property
    def budget_total(self) -> int:
        """Total allowed failures given current task count."""
        if self.total_tasks == 0:
            return 0
        return max(0, round(self.total_tasks * (1.0 - self.slo_target)))

    @property
    def budget_remaining(self) -> int:
        """How many more failures we can tolerate."""
        return max(0, self.budget_total - self.failed_tasks)

    @property
    def budget_fraction(self) -> float:
        """Fraction of error budget remaining (0.0 to 1.0)."""
        if self.budget_total == 0:
            return 1.0 if self.failed_tasks == 0 else 0.0
        return self.budget_remaining / self.budget_total

    @property
    def is_depleted(self) -> bool:
        return self.budget_remaining <= 0 and self.total_tasks > 0

    @property
    def status(self) -> SLOStatus:
        if self.budget_fraction > 0.5:
            return SLOStatus.GREEN
        if self.budget_fraction > 0.0:
            return SLOStatus.YELLOW
        return SLOStatus.RED

    def record_task(self, *, success: bool) -> None:
        """Record a task outcome."""
        self.total_tasks += 1
        if not success:
            self.failed_tasks += 1
        if self.is_depleted and self._depleted_since is None:
            self._depleted_since = time.time()
        elif not self.is_depleted:
            self._depleted_since = None


@dataclass
class ErrorBudgetPolicy:
    """Policy for what happens when error budget is depleted."""

    reduce_max_agents_to: int = 2
    upgrade_model: str = "opus"
    increase_review: bool = True
    cooldown_seconds: int = 300  # Wait before restoring normal ops

    def get_actions(self, budget: ErrorBudget) -> list[ErrorBudgetAction]:
        """Determine which actions to take based on budget state."""
        if not budget.is_depleted:
            return []
        actions: list[ErrorBudgetAction] = []
        actions.append(ErrorBudgetAction.REDUCE_AGENTS)
        actions.append(ErrorBudgetAction.UPGRADE_MODEL)
        if self.increase_review:
            actions.append(ErrorBudgetAction.INCREASE_REVIEW)
        return actions


@dataclass
class SLOTracker:
    """Tracks all SLOs and error budget for a run.

    Reads from MetricsCollector and computes SLO status. Persists
    state to .sdd/metrics/slos.json.
    """

    targets: dict[str, SLOTarget] = field(default_factory=dict)
    error_budget: ErrorBudget = field(default_factory=ErrorBudget)
    error_budget_policy: ErrorBudgetPolicy = field(default_factory=ErrorBudgetPolicy)
    _last_save: float = 0.0

    def __post_init__(self) -> None:
        if not self.targets:
            self.targets = _default_slo_targets()

    def update_from_collector(self, collector: MetricsCollector) -> None:
        """Refresh SLO values from the metrics collector."""
        task_metrics = collector._task_metrics  # pyright: ignore[reportPrivateUsage]
        if not task_metrics:
            return

        total = len(task_metrics)
        successes = sum(1 for tm in task_metrics.values() if tm.success)
        durations = [
            tm.end_time - tm.start_time
            for tm in task_metrics.values()
            if tm.end_time is not None and tm.end_time > tm.start_time
        ]
        costs = [tm.cost_usd for tm in task_metrics.values() if tm.cost_usd > 0]

        # Update success rate SLO
        if total > 0:
            self.targets["success_rate"].current = successes / total

        # Update P95 completion time SLO (lower is better, so we invert)
        if durations:
            durations_sorted = sorted(durations)
            p95_idx = int(len(durations_sorted) * 0.95)
            p95 = durations_sorted[min(p95_idx, len(durations_sorted) - 1)]
            # Store as fraction of target met (e.g., if p95=250s and target=300s, current=1.0)
            target_seconds = 300.0
            self.targets["p95_completion"].current = min(1.0, target_seconds / max(p95, 1.0))

        # Update cost per task SLO (lower is better)
        if costs:
            avg_cost = sum(costs) / len(costs)
            target_cost = 0.50
            self.targets["cost_per_task"].current = min(1.0, target_cost / max(avg_cost, 0.001))

        # Update error budget
        self.error_budget.total_tasks = total
        self.error_budget.failed_tasks = total - successes

    def get_dashboard(self) -> dict[str, object]:
        """Return SLO dashboard data for TUI/web rendering."""
        slos: list[dict[str, object]] = []
        for name, target in self.targets.items():
            slos.append(
                {
                    "name": name,
                    "description": target.description,
                    "target": target.target,
                    "current": round(target.current, 4),
                    "status": target.status.value,
                    "met": target.met,
                }
            )
        return {
            "slos": slos,
            "error_budget": {
                "total_tasks": self.error_budget.total_tasks,
                "failed_tasks": self.error_budget.failed_tasks,
                "budget_total": self.error_budget.budget_total,
                "budget_remaining": self.error_budget.budget_remaining,
                "budget_fraction": round(self.error_budget.budget_fraction, 4),
                "is_depleted": self.error_budget.is_depleted,
                "status": self.error_budget.status.value,
            },
            "actions": [a.value for a in self.error_budget_policy.get_actions(self.error_budget)],
        }

    def save(self, metrics_dir: Path) -> None:
        """Persist SLO state to disk."""
        now = time.time()
        if now - self._last_save < 10:  # Throttle writes to every 10s
            return
        self._last_save = now
        metrics_dir.mkdir(parents=True, exist_ok=True)
        path = metrics_dir / "slos.json"
        try:
            path.write_text(json.dumps(self.get_dashboard(), indent=2))
        except OSError as exc:
            logger.warning("Failed to save SLO state: %s", exc)

    @staticmethod
    def load(metrics_dir: Path) -> SLOTracker:
        """Load SLO state from disk (best-effort)."""
        path = metrics_dir / "slos.json"
        tracker = SLOTracker()
        if not path.exists():
            return tracker
        try:
            data = json.loads(path.read_text())
            for slo in data.get("slos", []):
                name = slo.get("name", "")
                if name in tracker.targets:
                    tracker.targets[name].current = slo.get("current", 0.0)
            eb = data.get("error_budget", {})
            tracker.error_budget.total_tasks = eb.get("total_tasks", 0)
            tracker.error_budget.failed_tasks = eb.get("failed_tasks", 0)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load SLO state: %s", exc)
        return tracker


def _default_slo_targets() -> dict[str, SLOTarget]:
    """Create default SLO targets per spec."""
    return {
        "success_rate": SLOTarget(
            name="success_rate",
            description="Task success rate >90%",
            target=0.90,
            warning_threshold=0.85,
        ),
        "p95_completion": SLOTarget(
            name="p95_completion",
            description="P95 completion time <5 minutes",
            target=0.90,  # Normalized: 1.0 = well under target
            warning_threshold=0.70,
        ),
        "cost_per_task": SLOTarget(
            name="cost_per_task",
            description="Average cost per task <$0.50",
            target=0.90,  # Normalized: 1.0 = well under target
            warning_threshold=0.70,
        ),
        "secret_leaks": SLOTarget(
            name="secret_leaks",
            description="Zero secret leaks",
            target=1.0,
            warning_threshold=1.0,  # Any leak is red
            current=1.0,  # Starts green (no leaks)
        ),
    }


def apply_error_budget_adjustments(
    config_max_agents: int,
    tracker: SLOTracker,
) -> tuple[int, str | None]:
    """Compute adjusted max_agents and model override based on error budget.

    Returns:
        (adjusted_max_agents, model_override_or_none)
    """
    actions = tracker.error_budget_policy.get_actions(tracker.error_budget)
    if not actions:
        return config_max_agents, None

    max_agents = config_max_agents
    model_override: str | None = None

    if ErrorBudgetAction.REDUCE_AGENTS in actions:
        max_agents = min(max_agents, tracker.error_budget_policy.reduce_max_agents_to)
        logger.warning(
            "Error budget depleted: reducing max_agents from %d to %d",
            config_max_agents,
            max_agents,
        )

    if ErrorBudgetAction.UPGRADE_MODEL in actions:
        model_override = tracker.error_budget_policy.upgrade_model
        logger.warning(
            "Error budget depleted: upgrading model to %s",
            model_override,
        )

    return max_agents, model_override
