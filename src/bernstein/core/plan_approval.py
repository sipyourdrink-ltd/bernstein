"""Plan mode: pre-execution cost estimation and human approval.

When plan_mode is enabled, the planner creates tasks with status=PLANNED
and builds a TaskPlan with per-task cost/risk estimates. The plan is
persisted to .sdd/runtime/plans/ and must be approved before tasks are
promoted to OPEN for execution.

High-risk tasks (schema changes, auth, security, infrastructure) require
explicit approval even when other tasks might be auto-approved.

This implements the "bounded autonomy" enterprise pattern: the system
proposes work, estimates cost and risk, and waits for human sign-off.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from bernstein.core.cost import _MODEL_COST_USD_PER_1K  # pyright: ignore[reportPrivateUsage]
from bernstein.core.models import (
    Complexity,
    PlanStatus,
    Scope,
    Task,
    TaskCostEstimate,
    TaskPlan,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------

# Keywords in task title/description that signal high-risk work.
_HIGH_RISK_KEYWORDS: frozenset[str] = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "schema",
        "migration",
        "database",
        "security",
        "secrets",
        "credentials",
        "permissions",
        "rbac",
        "encryption",
        "deploy",
        "infrastructure",
        "production",
        "billing",
        "payment",
        "delete",
        "drop",
        "destructive",
    }
)

# Roles that inherently carry higher risk.
_HIGH_RISK_ROLES: frozenset[str] = frozenset(
    {
        "security",
        "devops",
        "infrastructure",
        "dba",
    }
)


def _classify_risk(task: Task) -> tuple[str, list[str]]:
    """Classify a task's risk level based on its attributes.

    Returns:
        Tuple of (risk_level, list_of_reasons).
    """
    reasons: list[str] = []
    text = f"{task.title} {task.description}".lower()

    # Check keywords
    matched_keywords = [kw for kw in _HIGH_RISK_KEYWORDS if kw in text]
    if matched_keywords:
        reasons.append(f"Contains high-risk keywords: {', '.join(matched_keywords)}")

    # Check role
    if task.role.lower() in _HIGH_RISK_ROLES:
        reasons.append(f"High-risk role: {task.role}")

    # Check complexity
    if task.complexity == Complexity.HIGH:
        reasons.append("High complexity task")

    # Check scope
    if task.scope == Scope.LARGE:
        reasons.append("Large scope task")

    # Determine level
    if len(reasons) >= 2 or any("security" in r.lower() or "auth" in r.lower() for r in reasons):
        return "critical" if len(reasons) >= 3 else "high", reasons
    if reasons:
        return "medium", reasons
    return "low", reasons


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

# Average tokens per task by scope (empirical estimates).
_TOKENS_BY_SCOPE: dict[str, int] = {
    "small": 30_000,  # ~30k tokens for small tasks
    "medium": 80_000,  # ~80k tokens for medium tasks
    "large": 200_000,  # ~200k tokens for large tasks
}

# Model selection heuristic by complexity (when no explicit model set).
_DEFAULT_MODEL_BY_COMPLEXITY: dict[str, str] = {
    "low": "haiku",
    "medium": "sonnet",
    "high": "opus",
}


def _estimate_task_cost(task: Task) -> TaskCostEstimate:
    """Estimate the cost of executing a single task.

    Uses scope for token estimation and complexity for model selection,
    then applies the per-1k-token pricing from the cost module.
    """
    # Determine model
    model = task.model or _DEFAULT_MODEL_BY_COMPLEXITY.get(task.complexity.value, "sonnet")

    # Estimate tokens
    estimated_tokens = _TOKENS_BY_SCOPE.get(task.scope.value, 80_000)

    # Look up cost rate
    rate: float = 0.005  # fallback
    model_lower = model.lower()
    for key, cost in _MODEL_COST_USD_PER_1K.items():
        if key in model_lower:
            rate = cost
            break

    estimated_cost = (estimated_tokens / 1000.0) * rate

    # Classify risk
    risk_level, risk_reasons = _classify_risk(task)

    return TaskCostEstimate(
        task_id=task.id,
        title=task.title,
        role=task.role,
        model=model,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=estimated_cost,
        risk_level=risk_level,
        risk_reasons=risk_reasons,
    )


# ---------------------------------------------------------------------------
# Plan creation and management
# ---------------------------------------------------------------------------


def create_plan(goal: str, tasks: list[Task]) -> TaskPlan:
    """Create a TaskPlan from a list of planned tasks with cost/risk estimates.

    Args:
        goal: The original goal that produced these tasks.
        tasks: Tasks to include in the plan (should have status=PLANNED).

    Returns:
        A populated TaskPlan ready for human review.
    """
    estimates = [_estimate_task_cost(t) for t in tasks]
    total_cost = sum(e.estimated_cost_usd for e in estimates)
    total_minutes = sum(t.estimated_minutes for t in tasks)
    high_risk = [e.task_id for e in estimates if e.risk_level in ("high", "critical")]

    return TaskPlan(
        id=uuid.uuid4().hex[:12],
        goal=goal,
        task_estimates=estimates,
        total_estimated_cost_usd=total_cost,
        total_estimated_minutes=total_minutes,
        high_risk_tasks=high_risk,
    )


class PlanStore:
    """Persists plans to .sdd/runtime/plans/ as JSON files.

    Plans are lightweight — typically 1-20 per run — so individual
    JSON files are fine (no JSONL needed).
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._plans_dir = sdd_dir / "runtime" / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        self._plans: dict[str, TaskPlan] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """Load all existing plans from disk."""
        for f in self._plans_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                plan = TaskPlan.from_dict(data)
                self._plans[plan.id] = plan
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Failed to load plan %s: %s", f.name, exc)

    def _save(self, plan: TaskPlan) -> None:
        """Write a plan to disk."""
        path = self._plans_dir / f"{plan.id}.json"
        path.write_text(json.dumps(plan.to_dict(), indent=2))

    def save_plan(self, plan: TaskPlan) -> None:
        """Store a new plan."""
        self._plans[plan.id] = plan
        self._save(plan)
        logger.info(
            "Plan %s saved: %d tasks, $%.4f estimated, %d high-risk",
            plan.id,
            len(plan.task_estimates),
            plan.total_estimated_cost_usd,
            len(plan.high_risk_tasks),
        )

    def get_plan(self, plan_id: str) -> TaskPlan | None:
        """Retrieve a plan by ID."""
        return self._plans.get(plan_id)

    def list_plans(self, *, status: PlanStatus | None = None) -> list[TaskPlan]:
        """List all plans, optionally filtered by status."""
        plans = list(self._plans.values())
        if status is not None:
            plans = [p for p in plans if p.status == status]
        return sorted(plans, key=lambda p: p.created_at, reverse=True)

    def approve_plan(self, plan_id: str, reason: str = "") -> TaskPlan | None:
        """Mark a plan as approved.

        Returns the updated plan, or None if not found.
        """
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        plan.status = PlanStatus.APPROVED
        plan.decided_at = time.time()
        plan.decision_reason = reason
        self._save(plan)
        logger.info("Plan %s approved: %s", plan_id, reason or "(no reason)")
        return plan

    def reject_plan(self, plan_id: str, reason: str = "") -> TaskPlan | None:
        """Mark a plan as rejected.

        Returns the updated plan, or None if not found.
        """
        plan = self._plans.get(plan_id)
        if plan is None:
            return None
        plan.status = PlanStatus.REJECTED
        plan.decided_at = time.time()
        plan.decision_reason = reason
        self._save(plan)
        logger.info("Plan %s rejected: %s", plan_id, reason or "(no reason)")
        return plan
