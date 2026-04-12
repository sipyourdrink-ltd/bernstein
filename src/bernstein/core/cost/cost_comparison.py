"""Historical cost comparison across runs (COST-009).

Compare cost vs previous runs for the same plan or goal.  Uses persisted
run cost reports in ``.sdd/metrics/costs_*.json`` to find comparable
runs and compute deltas.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunCostSnapshot:
    """Minimal cost snapshot for a single historical run.

    Attributes:
        run_id: Orchestrator run identifier.
        total_spent_usd: Total cost of the run.
        budget_usd: Budget cap (0 = unlimited).
        task_count: Number of tasks in the run (if available).
        plan_path: Path to the plan file (if available).
        timestamp: When the run cost report was written.
    """

    run_id: str
    total_spent_usd: float
    budget_usd: float
    task_count: int
    plan_path: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "run_id": self.run_id,
            "total_spent_usd": round(self.total_spent_usd, 6),
            "budget_usd": round(self.budget_usd, 6),
            "task_count": self.task_count,
            "plan_path": self.plan_path,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class CostComparison:
    """Comparison of the current run against a historical baseline.

    Attributes:
        current_run_id: The current run being compared.
        baseline_run_id: The historical run used as baseline.
        current_cost_usd: Cost of the current run.
        baseline_cost_usd: Cost of the baseline run.
        delta_usd: Absolute difference (positive = more expensive).
        delta_pct: Percentage difference vs baseline.
        current_task_count: Tasks in current run.
        baseline_task_count: Tasks in baseline run.
        cost_per_task_current: Per-task cost for current run.
        cost_per_task_baseline: Per-task cost for baseline run.
    """

    current_run_id: str
    baseline_run_id: str
    current_cost_usd: float
    baseline_cost_usd: float
    delta_usd: float
    delta_pct: float
    current_task_count: int
    baseline_task_count: int
    cost_per_task_current: float
    cost_per_task_baseline: float

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "current_run_id": self.current_run_id,
            "baseline_run_id": self.baseline_run_id,
            "current_cost_usd": round(self.current_cost_usd, 6),
            "baseline_cost_usd": round(self.baseline_cost_usd, 6),
            "delta_usd": round(self.delta_usd, 6),
            "delta_pct": round(self.delta_pct, 4),
            "current_task_count": self.current_task_count,
            "baseline_task_count": self.baseline_task_count,
            "cost_per_task_current": round(self.cost_per_task_current, 6),
            "cost_per_task_baseline": round(self.cost_per_task_baseline, 6),
        }


@dataclass(frozen=True)
class CostTrendSummary:
    """Cost trend across multiple historical runs.

    Attributes:
        run_count: Number of runs in the comparison window.
        avg_cost_usd: Average cost across runs.
        min_cost_usd: Minimum cost run.
        max_cost_usd: Maximum cost run.
        trend_direction: ``"up"``, ``"down"``, or ``"stable"``.
        runs: Individual run snapshots, sorted oldest first.
    """

    run_count: int
    avg_cost_usd: float
    min_cost_usd: float
    max_cost_usd: float
    trend_direction: str
    runs: list[RunCostSnapshot]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "run_count": self.run_count,
            "avg_cost_usd": round(self.avg_cost_usd, 6),
            "min_cost_usd": round(self.min_cost_usd, 6),
            "max_cost_usd": round(self.max_cost_usd, 6),
            "trend_direction": self.trend_direction,
            "runs": [r.to_dict() for r in self.runs],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_run_snapshots(metrics_dir: Path) -> list[RunCostSnapshot]:
    """Load all historical run cost snapshots from disk.

    Reads ``costs_*.json`` files from the metrics directory.

    Args:
        metrics_dir: Path to ``.sdd/metrics``.

    Returns:
        List of :class:`RunCostSnapshot`, sorted by timestamp ascending.
    """
    if not metrics_dir.exists():
        return []

    snapshots: list[RunCostSnapshot] = []
    for cost_file in sorted(metrics_dir.glob("costs_*.json")):
        try:
            data = json.loads(cost_file.read_text())
            task_count = len(data.get("per_agent", []))
            # Sum task_count from per_agent entries
            if "per_agent" in data:
                task_count = sum(a.get("task_count", 0) for a in data["per_agent"])
            snapshots.append(
                RunCostSnapshot(
                    run_id=str(data.get("run_id", cost_file.stem)),
                    total_spent_usd=float(data.get("total_spent_usd", 0.0)),
                    budget_usd=float(data.get("budget_usd", 0.0)),
                    task_count=task_count,
                    plan_path=str(data.get("plan_path", "")),
                    timestamp=float(data.get("timestamp", cost_file.stat().st_mtime)),
                )
            )
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            logger.warning("Skipping corrupt cost file %s: %s", cost_file, exc)

    snapshots.sort(key=lambda s: s.timestamp)
    return snapshots


def compare_runs(
    current_run_id: str,
    current_cost_usd: float,
    current_task_count: int,
    metrics_dir: Path,
    *,
    baseline_run_id: str | None = None,
) -> CostComparison | None:
    """Compare the current run's cost against a historical baseline.

    If ``baseline_run_id`` is not provided, the most recent historical
    run is used.

    Args:
        current_run_id: Identifier for the current run.
        current_cost_usd: Cost of the current run.
        current_task_count: Number of tasks in the current run.
        metrics_dir: Path to ``.sdd/metrics``.
        baseline_run_id: Specific run to compare against (optional).

    Returns:
        A :class:`CostComparison`, or ``None`` if no baseline is available.
    """
    snapshots = load_run_snapshots(metrics_dir)
    # Exclude current run from baselines
    candidates = [s for s in snapshots if s.run_id != current_run_id]
    if not candidates:
        return None

    if baseline_run_id:
        baseline_list = [s for s in candidates if s.run_id == baseline_run_id]
        if not baseline_list:
            return None
        baseline = baseline_list[0]
    else:
        baseline = candidates[-1]  # most recent

    delta = current_cost_usd - baseline.total_spent_usd
    delta_pct = (delta / baseline.total_spent_usd * 100) if baseline.total_spent_usd > 0 else 0.0

    cpt_current = current_cost_usd / max(current_task_count, 1)
    cpt_baseline = baseline.total_spent_usd / max(baseline.task_count, 1)

    return CostComparison(
        current_run_id=current_run_id,
        baseline_run_id=baseline.run_id,
        current_cost_usd=current_cost_usd,
        baseline_cost_usd=baseline.total_spent_usd,
        delta_usd=delta,
        delta_pct=delta_pct,
        current_task_count=current_task_count,
        baseline_task_count=baseline.task_count,
        cost_per_task_current=cpt_current,
        cost_per_task_baseline=cpt_baseline,
    )


def compute_trend(metrics_dir: Path, *, max_runs: int = 20) -> CostTrendSummary:
    """Compute a cost trend across recent historical runs.

    Args:
        metrics_dir: Path to ``.sdd/metrics``.
        max_runs: Maximum number of recent runs to include.

    Returns:
        A :class:`CostTrendSummary` over the most recent runs.
    """
    snapshots = load_run_snapshots(metrics_dir)
    if not snapshots:
        return CostTrendSummary(
            run_count=0,
            avg_cost_usd=0.0,
            min_cost_usd=0.0,
            max_cost_usd=0.0,
            trend_direction="stable",
            runs=[],
        )

    recent = snapshots[-max_runs:]
    costs = [s.total_spent_usd for s in recent]
    avg = sum(costs) / len(costs)

    # Determine trend direction by comparing first half vs second half
    if len(recent) >= 4:
        mid = len(recent) // 2
        first_half_avg = sum(costs[:mid]) / mid
        second_half_avg = sum(costs[mid:]) / (len(costs) - mid)
        if first_half_avg > 0:
            change = (second_half_avg - first_half_avg) / first_half_avg
            if change > 0.05:
                direction = "up"
            elif change < -0.05:
                direction = "down"
            else:
                direction = "stable"
        else:
            direction = "stable"
    else:
        direction = "stable"

    return CostTrendSummary(
        run_count=len(recent),
        avg_cost_usd=avg,
        min_cost_usd=min(costs),
        max_cost_usd=max(costs),
        trend_direction=direction,
        runs=recent,
    )
