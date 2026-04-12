"""Real-time cost-per-line-of-code efficiency metric."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostEfficiency:
    """Cost efficiency snapshot for a run."""

    current_cost_per_line: float  # $/line for the most recent task
    run_avg_cost_per_line: float  # $/line average across this run
    historical_avg_cost_per_line: float  # $/line from past runs
    total_lines_changed: int
    total_cost_usd: float


def compute_efficiency(
    tasks: list[dict],
    total_cost_usd: float,
    historical_avg: float | None = None,
) -> CostEfficiency:
    """Compute cost-per-line metrics from completed tasks.

    Args:
        tasks: List of completed task dicts with 'lines_changed' and 'cost_usd' fields.
        total_cost_usd: Total run cost so far.
        historical_avg: Historical $/line from previous runs (from .sdd/metrics/).

    Returns:
        CostEfficiency snapshot.
    """
    total_lines = sum(t.get("lines_changed", 0) for t in tasks)

    # Current task (most recent)
    current = tasks[-1] if tasks else {}
    current_lines = current.get("lines_changed", 0)
    current_cost = current.get("cost_usd", 0.0)
    current_cpl = current_cost / max(current_lines, 1)

    # Run average
    run_avg = total_cost_usd / max(total_lines, 1)

    # Historical
    hist_avg = historical_avg if historical_avg is not None else run_avg

    return CostEfficiency(
        current_cost_per_line=round(current_cpl, 6),
        run_avg_cost_per_line=round(run_avg, 6),
        historical_avg_cost_per_line=round(hist_avg, 6),
        total_lines_changed=total_lines,
        total_cost_usd=round(total_cost_usd, 4),
    )
