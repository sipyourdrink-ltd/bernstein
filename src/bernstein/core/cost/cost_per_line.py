from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


class CostLineTask(TypedDict, total=False):
    """Minimal task dict for cost-per-line calculation."""

    lines_changed: int
    cost_usd: float


@dataclass(frozen=True)
class CostEfficiency:
    current_cost_per_line: float
    run_avg_cost_per_line: float
    historical_avg_cost_per_line: float
    total_lines_changed: int
    total_cost_usd: float


def compute_efficiency(
    tasks: list[CostLineTask],
    total_cost_usd: float,
    historical_avg: float | None = None,
) -> CostEfficiency:
    """Compute cost-per-line-of-code efficiency metrics."""
    total_lines = sum(t.get("lines_changed", 0) for t in tasks)

    current: CostLineTask = tasks[-1] if tasks else {}
    current_lines: int = current.get("lines_changed", 0)
    current_cost: float = current.get("cost_usd", 0.0)
    current_cpl: float = current_cost / max(current_lines, 1)

    run_avg: float = total_cost_usd / max(total_lines, 1)
    hist_avg: float = historical_avg if historical_avg is not None else run_avg

    return CostEfficiency(
        current_cost_per_line=round(current_cpl, 6),
        run_avg_cost_per_line=round(run_avg, 6),
        historical_avg_cost_per_line=round(hist_avg, 6),
        total_lines_changed=total_lines,
        total_cost_usd=round(total_cost_usd, 4),
    )
