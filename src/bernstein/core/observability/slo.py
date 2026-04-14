"""Agent SLOs (Service Level Objectives) and Error Budget tracking.

Defines measurable targets for agent orchestration and automatically
adjusts orchestrator behavior when error budgets are depleted.

SLO targets:
- Task success rate: >= 90%
- Merge success rate: >= 95%
- P95 task duration: < 30 minutes

OBS-150: Burn-down rate visualization — tracks burn rate over time to project
SLO breach date using linear extrapolation on the error budget.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.observability.metric_collector import MetricsCollector

logger = logging.getLogger(__name__)

# Maximum burn rate snapshots to keep in memory (1 per update cycle ≈ every 30s)
_MAX_BURN_HISTORY = 120  # ~1 hour of history at 30s intervals


@dataclass(frozen=True)
class BurnRateSnapshot:
    """A point-in-time sample of the error budget burn rate.

    Attributes:
        timestamp: Unix timestamp when the sample was taken.
        burn_rate: Error budget consumption rate relative to ideal (1.0 = on-target).
        budget_fraction: Fraction of error budget remaining (0.0 to 1.0).
        slo_current: Current task success rate (0.0 to 1.0).
        total_tasks: Total tasks processed at this point.
    """

    timestamp: float
    burn_rate: float
    budget_fraction: float
    slo_current: float
    total_tasks: int

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict."""
        return {
            "timestamp": self.timestamp,
            "burn_rate": round(self.burn_rate, 4),
            "budget_fraction": round(self.budget_fraction, 4),
            "slo_current": round(self.slo_current, 4),
            "total_tasks": self.total_tasks,
        }


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
        """Total allowed failures given current task count.

        Always allows at least 3 failures to avoid false depletion when
        only a few tasks have completed (e.g. 2 * 0.10 rounds to 0).
        """
        if self.total_tasks == 0:
            return 0
        return max(3, round(self.total_tasks * (1.0 - self.slo_target)))

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
    def burn_rate(self) -> float:
        """Rate of error budget consumption relative to ideal."""
        if self.total_tasks == 0:
            return 0.0
        actual_failure_rate = self.failed_tasks / self.total_tasks
        allowed_failure_rate = 1.0 - self.slo_target
        if allowed_failure_rate <= 0:
            return 10.0 if actual_failure_rate > 0 else 0.0
        return actual_failure_rate / allowed_failure_rate

    @property
    def time_to_exhaustion_tasks(self) -> int | None:
        """Estimated number of tasks until budget is gone at current burn rate."""
        if self.burn_rate <= 1.0:
            return None
        if self.budget_remaining <= 0:
            return 0

        actual_rate = self.failed_tasks / self.total_tasks
        allowed_rate = 1.0 - self.slo_target
        if actual_rate <= allowed_rate:
            return None

        return int(self.budget_remaining / (actual_rate - allowed_rate))

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

    def reset(self) -> None:
        """Reset error budget state."""
        self.total_tasks = 0
        self.failed_tasks = 0
        self._depleted_since = None


@dataclass
class ErrorBudgetPolicy:
    """Policy for what happens when error budget is depleted."""

    reduce_max_agents_to: int = 4
    upgrade_model: str = "opus"
    increase_review: bool = True
    cooldown_seconds: int = 120  # Wait before restoring normal ops

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


def _breach_projection_text(eb: Any, days_to_breach: float | None) -> str:
    """Generate a human-readable breach projection string."""
    if eb.is_depleted:
        return "Error budget exhausted — SLO breached now"
    if days_to_breach is None:
        return "On track — error budget not at risk"
    if days_to_breach < 1:
        hours = days_to_breach * 24
        return f"SLO will breach in {hours:.1f} hours at current rate"
    return f"SLO will breach in {days_to_breach:.1f} days at current rate"


def _burndown_status(eb: Any) -> str:
    """Determine burndown dashboard status from error budget."""
    if eb.is_depleted or eb.burn_rate > 3.0:
        return SLOStatus.RED.value
    if eb.burn_rate > 1.5 or eb.budget_fraction < 0.3:
        return SLOStatus.YELLOW.value
    return SLOStatus.GREEN.value


@dataclass
class SLOTracker:
    """Tracks all SLOs and error budget for a run.

    Reads from MetricsCollector and computes SLO status. Persists
    state to .sdd/metrics/slos.json.

    OBS-150: Maintains a rolling window of burn rate snapshots for
    burn-down rate visualization and SLO breach projection.
    """

    targets: dict[str, SLOTarget] = field(default_factory=dict)
    error_budget: ErrorBudget = field(default_factory=ErrorBudget)
    error_budget_policy: ErrorBudgetPolicy = field(default_factory=ErrorBudgetPolicy)
    _last_save: float = 0.0
    _burn_history: list[BurnRateSnapshot] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.targets:
            self.targets = _default_slo_targets()

    def _record_burn_snapshot(self) -> None:
        """Append the current burn rate as a time-series data point."""
        slo_current = self.targets.get("task_success")
        current_slo_val = slo_current.current if slo_current is not None else 0.0

        snapshot = BurnRateSnapshot(
            timestamp=time.time(),
            burn_rate=self.error_budget.burn_rate,
            budget_fraction=self.error_budget.budget_fraction,
            slo_current=current_slo_val,
            total_tasks=self.error_budget.total_tasks,
        )
        self._burn_history.append(snapshot)
        if len(self._burn_history) > _MAX_BURN_HISTORY:
            self._burn_history = self._burn_history[-_MAX_BURN_HISTORY:]

    def get_burndown_dashboard(self) -> dict[str, object]:
        """Return burn-down rate data for SLO visualization (OBS-150).

        Computes:
        - Current burn rate and SLO compliance
        - Linear projection of time-to-breach in days
        - Sparkline data points (budget_fraction over recent history)
        - Status: green / yellow / red

        Returns:
            JSON-serializable dict with burndown visualization data.
        """
        eb = self.error_budget
        task_slo = self.targets.get("task_success")
        slo_target = task_slo.target if task_slo is not None else 0.90
        slo_current = task_slo.current if task_slo is not None else 0.0

        # Compute rate of budget consumption per day using recent history
        days_to_breach: float | None = None
        burn_rate_per_day: float | None = None

        if len(self._burn_history) >= 2:
            oldest = self._burn_history[0]
            newest = self._burn_history[-1]
            elapsed_seconds = newest.timestamp - oldest.timestamp
            if elapsed_seconds > 0:
                budget_consumed = oldest.budget_fraction - newest.budget_fraction
                # Rate of budget consumption per second
                consumption_rate_per_sec = budget_consumed / elapsed_seconds
                burn_rate_per_day = consumption_rate_per_sec * 86400

                if consumption_rate_per_sec > 0 and eb.budget_fraction > 0:
                    remaining_fraction = eb.budget_fraction
                    seconds_to_exhaustion = remaining_fraction / consumption_rate_per_sec
                    days_to_breach = seconds_to_exhaustion / 86400
                elif consumption_rate_per_sec <= 0:
                    # Budget is not being consumed (or is recovering)
                    days_to_breach = None

        # Build sparkline data from recent history (last 20 points)
        sparkline_points = [
            {
                "timestamp": s.timestamp,
                "budget_fraction": round(s.budget_fraction, 4),
                "burn_rate": round(s.burn_rate, 4),
                "slo_current": round(s.slo_current, 4),
            }
            for s in self._burn_history[-20:]
        ]

        breach_projection = _breach_projection_text(eb, days_to_breach)
        status = _burndown_status(eb)

        return {
            "slo_name": "task_success",
            "slo_target": slo_target,
            "slo_current": round(slo_current, 4),
            "slo_met": slo_current >= slo_target,
            "burn_rate": round(eb.burn_rate, 4),
            "burn_rate_per_day": round(burn_rate_per_day, 6) if burn_rate_per_day is not None else None,
            "budget_fraction": round(eb.budget_fraction, 4),
            "budget_consumed_pct": round((1.0 - eb.budget_fraction) * 100, 1),
            "days_to_breach": round(days_to_breach, 2) if days_to_breach is not None else None,
            "breach_projection": breach_projection,
            "total_tasks": eb.total_tasks,
            "failed_tasks": eb.failed_tasks,
            "status": status,
            "sparkline": sparkline_points,
            "history_size": len(self._burn_history),
        }

    def update_from_collector(self, collector: MetricsCollector) -> None:
        """Refresh SLO values from the metrics collector."""
        task_metrics: dict[str, Any] = getattr(collector, "_task_metrics", {})
        if not task_metrics:
            return

        total = len(task_metrics)
        successes = sum(1 for tm in task_metrics.values() if getattr(tm, "success", False))
        durations = [
            tm.end_time - tm.start_time
            for tm in task_metrics.values()
            if getattr(tm, "end_time", None) is not None and tm.end_time > tm.start_time
        ]

        # Update task success SLO
        if total > 0:
            self.targets["task_success"].current = successes / total

        # Update P95 task duration SLO (target = 30 min = 1800s)
        if durations:
            durations_sorted = sorted(durations)
            p95_idx = int(len(durations_sorted) * 0.95)
            p95 = durations_sorted[min(p95_idx, len(durations_sorted) - 1)]
            target_seconds = 1800.0
            self.targets["p95_duration"].current = min(1.0, target_seconds / max(p95, 1.0))

        # Update merge success SLO (if we have merge metrics)
        # We need to find merge metrics in collector.
        # For now, let's assume we can query them or they are tracked separately.
        # To keep it simple for this ticket, we'll proxy it from janitor_passed if available.
        janitor_passed = sum(1 for tm in task_metrics.values() if getattr(tm, "janitor_passed", False))
        if total > 0:
            self.targets["merge_success"].current = janitor_passed / total

        # Update error budget
        self.error_budget.total_tasks = total
        self.error_budget.failed_tasks = total - successes

        # OBS-150: Record burn rate snapshot for burn-down visualization
        self._record_burn_snapshot()

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
                "burn_rate": round(self.error_budget.burn_rate, 4),
                "time_to_exhaustion_tasks": self.error_budget.time_to_exhaustion_tasks,
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
                    tracker.targets[name].current = float(slo.get("current", 0.0))
            eb = data.get("error_budget", {})
            tracker.error_budget.total_tasks = int(eb.get("total_tasks", 0))
            tracker.error_budget.failed_tasks = int(eb.get("failed_tasks", 0))
        except (OSError, ValueError) as exc:
            logger.warning("Failed to load SLO state: %s", exc)
        return tracker


def _default_slo_targets() -> dict[str, SLOTarget]:
    """Create default SLO targets per spec."""
    return {
        "task_success": SLOTarget(
            name="task_success",
            description="Task success rate >= 90%",
            target=0.90,
            warning_threshold=0.92,
        ),
        "merge_success": SLOTarget(
            name="merge_success",
            description="Merge success rate >= 95%",
            target=0.95,
            warning_threshold=0.97,
        ),
        "p95_duration": SLOTarget(
            name="p95_duration",
            description="P95 task duration < 30 minutes",
            target=0.90,  # Normalized
            warning_threshold=0.70,
        ),
    }


def apply_error_budget_adjustments(
    config_max_agents: int,
    _tracker: SLOTracker,
) -> tuple[int, str | None]:
    """Compute adjusted max_agents and model override based on error budget.

    Returns:
        (adjusted_max_agents, model_override_or_none)
    """
    # SLO throttling is disabled: the error budget calculation counts stale
    # tasks from previous runs and "already resolved" agent deaths as real
    # failures, creating a death spiral that reduces agents to 0.  Until
    # the SLO data source is fixed, return config values unchanged.
    return config_max_agents, None
