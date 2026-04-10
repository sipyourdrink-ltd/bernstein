"""Pre-run cost projections for planned task batches.

Estimates the total cost of a set of tasks *before* execution begins,
using per-complexity baselines and optional historical averages.  Produces
a Rich-formatted table with per-task breakdown, totals, budget comparison,
and confidence intervals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Cost baselines (USD) by complexity level
# ---------------------------------------------------------------------------

COST_PER_COMPLEXITY: dict[str, float] = {
    "low": 0.02,
    "medium": 0.08,
    "high": 0.25,
    "critical": 0.50,
}

# Scope multipliers applied on top of the complexity baseline.
_SCOPE_MULTIPLIER: dict[str, float] = {
    "small": 0.5,
    "medium": 1.0,
    "large": 2.5,
}

# Rough token-per-dollar ratio used to back-derive an estimated token count.
_TOKENS_PER_USD: float = 100_000.0

# Confidence assigned to purely heuristic estimates (no historical data).
_BASE_CONFIDENCE: float = 0.6


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskCostEstimate:
    """Cost projection for a single task.

    Attributes:
        task_id: Unique task identifier.
        title: Short human-readable task title.
        role: Agent role assigned to the task.
        complexity: Complexity level (low / medium / high / critical).
        scope: Scope level (small / medium / large).
        estimated_cost_usd: Point estimate of cost in USD.
        confidence: Confidence in the estimate (0.0 -- 1.0).
        estimated_tokens: Approximate token count backing the cost estimate.
    """

    task_id: str
    title: str
    role: str
    complexity: str
    scope: str
    estimated_cost_usd: float
    confidence: float
    estimated_tokens: int


@dataclass(frozen=True)
class RunCostEstimate:
    """Aggregate cost projection for a full run.

    Attributes:
        tasks: Per-task estimates.
        total_estimated_usd: Sum of all task estimates.
        budget_usd: Budget cap (``None`` if unlimited).
        over_budget: Whether the estimate exceeds the budget.
        confidence_low: Lower bound of the aggregate confidence interval.
        confidence_high: Upper bound of the aggregate confidence interval.
    """

    tasks: list[TaskCostEstimate]
    total_estimated_usd: float
    budget_usd: float | None
    over_budget: bool
    confidence_low: float
    confidence_high: float


# ---------------------------------------------------------------------------
# Estimation helpers
# ---------------------------------------------------------------------------


def estimate_task_cost(
    task_id: str,
    title: str,
    role: str,
    complexity: str,
    scope: str,
    historical_avg: float | None = None,
) -> TaskCostEstimate:
    """Estimate the cost of a single task.

    When *historical_avg* is provided (e.g. from past runs with the same
    role/complexity), it is blended 60/40 with the heuristic baseline and
    confidence is boosted.

    Args:
        task_id: Unique task identifier.
        title: Short description of the task.
        role: Agent role (e.g. ``"backend"``, ``"qa"``).
        complexity: One of ``"low"``, ``"medium"``, ``"high"``, ``"critical"``.
        scope: One of ``"small"``, ``"medium"``, ``"large"``.
        historical_avg: Optional historical average cost for similar tasks.

    Returns:
        A frozen :class:`TaskCostEstimate`.
    """
    base = COST_PER_COMPLEXITY.get(complexity, COST_PER_COMPLEXITY["medium"])
    scope_mult = _SCOPE_MULTIPLIER.get(scope, 1.0)
    heuristic_cost = base * scope_mult

    if historical_avg is not None and historical_avg > 0:
        # Blend: 60 % historical, 40 % heuristic
        blended = 0.6 * historical_avg + 0.4 * heuristic_cost
        confidence = min(_BASE_CONFIDENCE + 0.25, 1.0)
    else:
        blended = heuristic_cost
        confidence = _BASE_CONFIDENCE

    estimated_tokens = max(1, int(blended * _TOKENS_PER_USD))

    return TaskCostEstimate(
        task_id=task_id,
        title=title,
        role=role,
        complexity=complexity,
        scope=scope,
        estimated_cost_usd=round(blended, 6),
        confidence=round(confidence, 2),
        estimated_tokens=estimated_tokens,
    )


def estimate_run_cost(
    tasks: list[TaskCostEstimate],
    budget: float | None = None,
) -> RunCostEstimate:
    """Aggregate per-task estimates into a run-level projection.

    The confidence interval is derived from the individual task
    confidences: the low bound uses the *minimum* confidence as a
    pessimistic scaling factor, and the high bound uses the *maximum*.

    Args:
        tasks: List of individual task estimates.
        budget: Optional budget cap in USD.

    Returns:
        A frozen :class:`RunCostEstimate`.
    """
    if not tasks:
        return RunCostEstimate(
            tasks=[],
            total_estimated_usd=0.0,
            budget_usd=budget,
            over_budget=False,
            confidence_low=0.0,
            confidence_high=0.0,
        )

    total = sum(t.estimated_cost_usd for t in tasks)
    confidences = [t.confidence for t in tasks]
    conf_low = min(confidences)
    conf_high = max(confidences)

    over = budget is not None and total > budget

    return RunCostEstimate(
        tasks=tasks,
        total_estimated_usd=round(total, 6),
        budget_usd=budget,
        over_budget=over,
        confidence_low=round(conf_low, 2),
        confidence_high=round(conf_high, 2),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _ascii_bar(value: float, max_value: float, width: int = 20) -> str:
    """Return a block-character bar proportional to *value / max_value*."""
    if max_value <= 0 or value <= 0:
        return "\u2591" * width  # light shade
    filled = max(1, round((value / max_value) * width))
    filled = min(filled, width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def format_cost_estimate(estimate: RunCostEstimate) -> str:
    """Format a :class:`RunCostEstimate` as a Rich-renderable string.

    Produces a per-task table, total line, budget comparison (if a budget
    is set), and a confidence interval summary.

    Args:
        estimate: The run-level cost estimate to render.

    Returns:
        A multi-line string suitable for ``rich.console.Console.print``.
    """
    lines: list[str] = []
    lines.append("[bold cyan]Pre-Run Cost Estimate[/bold cyan]")
    lines.append("")

    if not estimate.tasks:
        lines.append("[dim]No tasks to estimate.[/dim]")
        return "\n".join(lines)

    # Header
    lines.append(
        f"  {'Task':<12} {'Role':<12} {'Cplx':<10} {'Scope':<8} {'Est. Cost':>10}  {'Conf':>5}  {'Tokens':>10}  Bar"
    )
    lines.append("  " + "\u2500" * 90)

    max_cost = max(t.estimated_cost_usd for t in estimate.tasks) if estimate.tasks else 1.0

    for t in estimate.tasks:
        short_id = t.task_id[:10]
        bar = _ascii_bar(t.estimated_cost_usd, max_cost)
        lines.append(
            f"  {short_id:<12} {t.role:<12} {t.complexity:<10} {t.scope:<8} "
            f"${t.estimated_cost_usd:>9.4f}  {t.confidence:>4.0%}  "
            f"{t.estimated_tokens:>10,}  {bar}"
        )

    lines.append("  " + "\u2500" * 90)
    lines.append(f"  [bold]Total: ${estimate.total_estimated_usd:.4f}[/bold]")

    # Budget comparison
    if estimate.budget_usd is not None:
        budget = estimate.budget_usd
        pct = (estimate.total_estimated_usd / budget * 100) if budget > 0 else math.inf
        if estimate.over_budget:
            lines.append(
                f"  [bold red]Over budget![/bold red]  ${estimate.total_estimated_usd:.4f} / ${budget:.4f} ({pct:.0f}%)"
            )
        else:
            lines.append(
                f"  [green]Within budget[/green]  ${estimate.total_estimated_usd:.4f} / ${budget:.4f} ({pct:.0f}%)"
            )

    # Confidence interval
    lines.append("")
    lines.append(f"  Confidence range: {estimate.confidence_low:.0%} \u2013 {estimate.confidence_high:.0%}")

    return "\n".join(lines)
