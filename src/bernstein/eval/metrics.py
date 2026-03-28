"""Custom eval metrics — each metric is a dataclass with a compute method.

Metrics feed into the multiplicative scoring formula:
    Score = (0.5*TaskSuccess + 0.3*CodeQuality + 0.2*Efficiency) * Reliability * Safety
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.eval.telemetry import AgentTelemetry

# ---------------------------------------------------------------------------
# Individual metric classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskCompletionRate:
    """Fraction of tasks passing all completion signals.

    Attributes:
        total_tasks: Number of tasks evaluated.
        passed_tasks: Number of tasks where all signals passed.
    """

    total_tasks: int = 0
    passed_tasks: int = 0

    @property
    def rate(self) -> float:
        """Completion rate in [0.0, 1.0]."""
        if self.total_tasks == 0:
            return 0.0
        return self.passed_tasks / self.total_tasks


@dataclass(frozen=True)
class RetryRate:
    """Fraction of tasks requiring retry (lower is better).

    Attributes:
        total_tasks: Number of tasks evaluated.
        retried_tasks: Number of tasks that needed at least one retry.
    """

    total_tasks: int = 0
    retried_tasks: int = 0

    @property
    def rate(self) -> float:
        """Retry rate in [0.0, 1.0]."""
        if self.total_tasks == 0:
            return 0.0
        return self.retried_tasks / self.total_tasks


@dataclass(frozen=True)
class AgentUtilization:
    """Productive turns / total turns per agent.

    Attributes:
        productive_turns: Turns that resulted in code changes.
        total_turns: Total LLM turns consumed.
    """

    productive_turns: int = 0
    total_turns: int = 0

    @property
    def rate(self) -> float:
        """Utilization rate in [0.0, 1.0]."""
        if self.total_turns == 0:
            return 0.0
        return self.productive_turns / self.total_turns


@dataclass(frozen=True)
class CostEfficiency:
    """Total cost / tasks completed, normalized to baseline.

    Attributes:
        total_cost_usd: Total cost across all tasks.
        tasks_completed: Number of successfully completed tasks.
        baseline_cost_per_task: Expected cost per task (for normalization).
    """

    total_cost_usd: float = 0.0
    tasks_completed: int = 0
    baseline_cost_per_task: float = 0.50  # $0.50 default baseline

    @property
    def cost_per_task(self) -> float:
        """Cost per completed task in USD."""
        if self.tasks_completed == 0:
            return float("inf")
        return self.total_cost_usd / self.tasks_completed

    @property
    def efficiency(self) -> float:
        """Efficiency score in [0.0, 1.0]. Higher = more cost-efficient."""
        if self.tasks_completed == 0:
            return 0.0
        ratio = self.baseline_cost_per_task / max(self.cost_per_task, 0.01)
        return min(ratio, 1.0)


@dataclass(frozen=True)
class TimeEfficiency:
    """Wall-clock seconds / tasks completed, normalized to baseline.

    Attributes:
        total_duration_s: Total wall-clock time in seconds.
        tasks_completed: Number of successfully completed tasks.
        baseline_seconds_per_task: Expected seconds per task.
    """

    total_duration_s: float = 0.0
    tasks_completed: int = 0
    baseline_seconds_per_task: float = 120.0  # 2 minutes default

    @property
    def seconds_per_task(self) -> float:
        """Seconds per completed task."""
        if self.tasks_completed == 0:
            return float("inf")
        return self.total_duration_s / self.tasks_completed

    @property
    def efficiency(self) -> float:
        """Efficiency score in [0.0, 1.0]. Higher = faster."""
        if self.tasks_completed == 0:
            return 0.0
        ratio = self.baseline_seconds_per_task / max(self.seconds_per_task, 0.1)
        return min(ratio, 1.0)


@dataclass(frozen=True)
class ContextWaste:
    """Estimated tokens spent on exploration vs actual coding.

    Attributes:
        exploration_tokens: Tokens spent reading/exploring.
        coding_tokens: Tokens spent on actual code generation.
    """

    exploration_tokens: int = 0
    coding_tokens: int = 0

    @property
    def waste_ratio(self) -> float:
        """Fraction of tokens wasted on exploration. Lower is better."""
        total = self.exploration_tokens + self.coding_tokens
        if total == 0:
            return 0.0
        return self.exploration_tokens / total


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------


@dataclass
class EvalScoreComponents:
    """All components of the multiplicative eval score.

    Attributes:
        task_success: TaskCompletionRate metric.
        code_quality: LLM judge score [0.0, 1.0].
        efficiency: Combined cost + time efficiency [0.0, 1.0].
        reliability: 1.0 if no crashes/orphans, degrades per failure.
        safety: 1.0 if no test regressions, 0.0 on any regression.
    """

    task_success: float = 0.0
    code_quality: float = 0.0
    efficiency: float = 0.0
    reliability: float = 1.0
    safety: float = 1.0

    @property
    def weighted_base(self) -> float:
        """Weighted sum of base components (before multiplicative gates)."""
        return 0.5 * self.task_success + 0.3 * self.code_quality + 0.2 * self.efficiency

    @property
    def final_score(self) -> float:
        """Final multiplicative score."""
        return self.weighted_base * self.reliability * self.safety


@dataclass
class TierScores:
    """Per-tier scores for the eval run.

    Attributes:
        smoke: Score for smoke-tier tasks.
        standard: Score for standard-tier tasks.
        stretch: Score for stretch-tier tasks.
        adversarial: Score for adversarial-tier tasks.
    """

    smoke: float = 0.0
    standard: float = 0.0
    stretch: float = 0.0
    adversarial: float = 0.0


def compute_efficiency(
    telemetry_list: list[AgentTelemetry],
    tasks_completed: int,
    baseline_cost: float = 0.50,
    baseline_seconds: float = 120.0,
) -> float:
    """Compute combined efficiency from telemetry.

    Args:
        telemetry_list: Telemetry data from all task runs.
        tasks_completed: Number of tasks that passed.
        baseline_cost: Expected cost per task in USD.
        baseline_seconds: Expected seconds per task.

    Returns:
        Efficiency score in [0.0, 1.0].
    """
    if tasks_completed == 0:
        return 0.0

    total_cost = sum(t.cost_usd for t in telemetry_list)
    total_duration = sum(t.duration_s for t in telemetry_list)

    cost_eff = CostEfficiency(
        total_cost_usd=total_cost,
        tasks_completed=tasks_completed,
        baseline_cost_per_task=baseline_cost,
    )
    time_eff = TimeEfficiency(
        total_duration_s=total_duration,
        tasks_completed=tasks_completed,
        baseline_seconds_per_task=baseline_seconds,
    )

    # Equal weight to cost and time efficiency
    return 0.5 * cost_eff.efficiency + 0.5 * time_eff.efficiency


def compute_reliability(
    crash_count: int = 0,
    orphan_count: int = 0,
    telemetry_valid: bool = True,
) -> float:
    """Compute reliability gate value.

    Args:
        crash_count: Number of agent crashes during the run.
        orphan_count: Number of orphaned agent processes.
        telemetry_valid: Whether all telemetry passed schema validation.

    Returns:
        Reliability multiplier in [0.0, 1.0].
    """
    score = 1.0
    # Each crash degrades reliability by 0.1
    score -= crash_count * 0.1
    # Each orphan degrades by 0.05
    score -= orphan_count * 0.05
    # Invalid telemetry halves the score
    if not telemetry_valid:
        score *= 0.5
    return max(score, 0.0)


def compute_safety(has_test_regressions: bool) -> float:
    """Compute safety gate value.

    One test regression = zero score. This forces defensive architecture.

    Args:
        has_test_regressions: Whether any existing tests were broken.

    Returns:
        Safety multiplier: 1.0 or 0.0.
    """
    return 0.0 if has_test_regressions else 1.0
