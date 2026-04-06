"""Cost anomaly root cause analysis (COST-011).

When cost exceeds the estimate, identify which agent or task caused the
overshoot and produce a structured explanation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.cost_tracker import CostTracker, TokenUsage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostContributor:
    """A single contributor to a cost overshoot.

    Attributes:
        entity_type: ``"agent"`` or ``"task"``.
        entity_id: Agent or task identifier.
        cost_usd: Cost attributed to this entity.
        share_pct: Percentage of total overshoot attributable to this entity.
        reason: Short explanation of why this entity is expensive.
    """

    entity_type: str
    entity_id: str
    cost_usd: float
    share_pct: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "cost_usd": round(self.cost_usd, 6),
            "share_pct": round(self.share_pct, 2),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RootCauseReport:
    """Root cause analysis for a cost overshoot.

    Attributes:
        run_id: Orchestrator run identifier.
        estimated_cost_usd: The pre-run cost estimate.
        actual_cost_usd: The actual cost.
        overshoot_usd: How much over the estimate (positive = over).
        overshoot_pct: Percentage over the estimate.
        top_contributors: Entities most responsible for the overshoot.
        summary: Human-readable summary.
    """

    run_id: str
    estimated_cost_usd: float
    actual_cost_usd: float
    overshoot_usd: float
    overshoot_pct: float
    top_contributors: list[CostContributor]
    summary: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "run_id": self.run_id,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "actual_cost_usd": round(self.actual_cost_usd, 6),
            "overshoot_usd": round(self.overshoot_usd, 6),
            "overshoot_pct": round(self.overshoot_pct, 2),
            "top_contributors": [c.to_dict() for c in self.top_contributors],
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyse_cost_overshoot(
    tracker: CostTracker,
    estimated_cost_usd: float,
    *,
    top_n: int = 5,
) -> RootCauseReport | None:
    """Analyse a cost overshoot and identify root causes.

    Compares actual spend to the estimate and attributes the overshoot
    to the most expensive agents and tasks.

    Args:
        tracker: The :class:`CostTracker` with recorded usage data.
        estimated_cost_usd: The pre-run cost estimate.
        top_n: Maximum number of contributors to include.

    Returns:
        A :class:`RootCauseReport`, or ``None`` if there is no overshoot.
    """
    actual = tracker.spent_usd
    overshoot = actual - estimated_cost_usd
    if overshoot <= 0:
        return None

    overshoot_pct = (overshoot / estimated_cost_usd * 100) if estimated_cost_usd > 0 else 0.0

    # Attribute by agent
    agent_costs = _aggregate_by_agent(tracker.usages)
    # Attribute by task
    task_costs = _aggregate_by_task(tracker.usages)

    # Merge and rank all contributors
    contributors: list[CostContributor] = []
    for agent_id, cost in agent_costs:
        share = (cost / overshoot * 100) if overshoot > 0 else 0.0
        reason = _reason_for_agent(agent_id, tracker.usages)
        contributors.append(
            CostContributor(
                entity_type="agent",
                entity_id=agent_id,
                cost_usd=cost,
                share_pct=share,
                reason=reason,
            )
        )

    for task_id, cost in task_costs:
        share = (cost / overshoot * 100) if overshoot > 0 else 0.0
        reason = _reason_for_task(task_id, tracker.usages)
        contributors.append(
            CostContributor(
                entity_type="task",
                entity_id=task_id,
                cost_usd=cost,
                share_pct=share,
                reason=reason,
            )
        )

    contributors.sort(key=lambda c: c.cost_usd, reverse=True)
    top = contributors[:top_n]

    summary = _build_summary(tracker.run_id, estimated_cost_usd, actual, overshoot, top)

    return RootCauseReport(
        run_id=tracker.run_id,
        estimated_cost_usd=estimated_cost_usd,
        actual_cost_usd=actual,
        overshoot_usd=overshoot,
        overshoot_pct=overshoot_pct,
        top_contributors=top,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _aggregate_by_agent(usages: list[TokenUsage]) -> list[tuple[str, float]]:
    """Aggregate cost by agent, sorted descending."""
    totals: dict[str, float] = {}
    for u in usages:
        totals[u.agent_id] = totals.get(u.agent_id, 0.0) + u.cost_usd
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _aggregate_by_task(usages: list[TokenUsage]) -> list[tuple[str, float]]:
    """Aggregate cost by task, sorted descending."""
    totals: dict[str, float] = {}
    for u in usages:
        totals[u.task_id] = totals.get(u.task_id, 0.0) + u.cost_usd
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _reason_for_agent(agent_id: str, usages: list[TokenUsage]) -> str:
    """Generate a reason string for an agent's cost."""
    agent_usages = [u for u in usages if u.agent_id == agent_id]
    if not agent_usages:
        return "No usage data"
    total_tokens = sum(u.input_tokens + u.output_tokens for u in agent_usages)
    models = {u.model for u in agent_usages}
    invocations = len(agent_usages)
    return f"{invocations} invocation(s), {total_tokens:,} tokens, model(s): {', '.join(sorted(models))}"


def _reason_for_task(task_id: str, usages: list[TokenUsage]) -> str:
    """Generate a reason string for a task's cost."""
    task_usages = [u for u in usages if u.task_id == task_id]
    if not task_usages:
        return "No usage data"
    total_tokens = sum(u.input_tokens + u.output_tokens for u in task_usages)
    agents = {u.agent_id for u in task_usages}
    return f"{len(agents)} agent(s), {total_tokens:,} tokens"


def _build_summary(
    run_id: str,
    estimated: float,
    actual: float,
    overshoot: float,
    contributors: list[CostContributor],
) -> str:
    """Build a human-readable summary of the root cause analysis."""
    lines: list[str] = [
        f"Run {run_id} exceeded its estimate by ${overshoot:.4f} (${actual:.4f} actual vs ${estimated:.4f} estimated).",
    ]
    if contributors:
        lines.append("Top contributors:")
        for c in contributors:
            lines.append(
                f"  - {c.entity_type} {c.entity_id}: ${c.cost_usd:.4f} ({c.share_pct:.1f}% of overshoot) -- {c.reason}"
            )
    return "\n".join(lines)
