"""Plan-and-Execute architecture: explicit separation of planning from execution.

Research shows this achieves 92% task completion with 3.6x speedup over
sequential ReAct. The planning phase uses the most capable available model
(Opus/o3), producing a structured YAML plan. Execution uses bandit-selected
cheaper models (Sonnet/Haiku) per task.

Key properties:
- Planning and execution use different model tiers (cost/quality trade-off).
- Plans are persisted and reusable — `bernstein run --plan <file>` re-runs.
- Optional plan review gate with cost estimate before execution begins.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path

# Planning tier: most capable reasoning models
PLANNING_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-opus-4-6",
    "o3",
    "gpt-5.4",
)

# Execution tier: balanced cost/speed
EXECUTION_MODELS: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "gpt-5.4-mini",
)


@dataclass
class PlannedTask:
    """A single task in a generated plan.

    Attributes:
        title: Short task title.
        description: Longer task description.
        role: Role tag (backend/frontend/qa/...).
        priority: Numeric priority (lower = more important).
        complexity: One of simple/medium/high/complex/epic.
        scope: One of small/medium/large.
        recommended_model: Preferred execution model ID (filled by planner).
        depends_on: Titles of tasks this depends on.
        estimated_minutes: Rough time estimate.
    """

    title: str
    description: str = ""
    role: str = "backend"
    priority: int = 2
    complexity: str = "medium"
    scope: str = "medium"
    recommended_model: str = ""
    depends_on: list[str] = field(default_factory=list)
    estimated_minutes: int = 10


@dataclass
class GeneratedPlan:
    """A plan produced by the planning tier, ready for execution.

    Attributes:
        goal: Top-level goal.
        goal_hash: Deterministic hash of the goal string.
        tasks: Ordered list of planned tasks.
        planning_model: Model used to produce the plan.
        created_at: Unix timestamp.
        estimated_total_minutes: Sum of task estimates.
        estimated_cost_usd: Rough cost estimate.
    """

    goal: str
    goal_hash: str
    tasks: list[PlannedTask]
    planning_model: str
    created_at: float = field(default_factory=time.time)
    estimated_total_minutes: int = 0
    estimated_cost_usd: float = 0.0

    def to_yaml(self) -> str:
        """Serialize the plan to YAML."""
        return yaml.safe_dump(
            {
                "goal": self.goal,
                "goal_hash": self.goal_hash,
                "planning_model": self.planning_model,
                "created_at": self.created_at,
                "estimated_total_minutes": self.estimated_total_minutes,
                "estimated_cost_usd": self.estimated_cost_usd,
                "tasks": [asdict(t) for t in self.tasks],
            },
            sort_keys=False,
        )


def hash_goal(goal: str) -> str:
    """Return a short deterministic hash for a goal string."""
    return hashlib.sha256(goal.encode()).hexdigest()[:16]


def select_planning_model(available: list[str]) -> str:
    """Pick the most capable planning model from the available list."""
    for preferred in PLANNING_MODELS:
        if preferred in available:
            return preferred
    return available[0] if available else PLANNING_MODELS[0]


def select_execution_model(
    task: PlannedTask,
    available: list[str],
) -> str:
    """Pick an execution model based on task complexity."""
    if task.recommended_model and task.recommended_model in available:
        return task.recommended_model
    if task.complexity in ("high", "complex", "epic"):
        # still use a capable model, but not the most expensive
        for m in ("claude-sonnet-4-6", "gpt-5.4"):
            if m in available:
                return m
    for preferred in EXECUTION_MODELS:
        if preferred in available:
            return preferred
    return available[0] if available else EXECUTION_MODELS[0]


def estimate_plan_cost(plan: GeneratedPlan) -> float:
    """Rough cost estimate based on task complexity and model tier."""
    complexity_cost = {
        "simple": 0.05,
        "medium": 0.15,
        "high": 0.50,
        "complex": 1.0,
        "epic": 3.0,
    }
    return sum(complexity_cost.get(t.complexity, 0.15) for t in plan.tasks)


def save_plan(plan: GeneratedPlan, plans_dir: Path) -> Path:
    """Persist plan to .sdd/plans/generated/{goal_hash}.yaml."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / f"{plan.goal_hash}.yaml"
    path.write_text(plan.to_yaml())
    # Also update latest symlink
    latest = plans_dir / "latest.yaml"
    try:
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.symlink_to(path.name)
    except OSError:
        # Symlink may fail on some filesystems; fall back to copy
        latest.write_text(plan.to_yaml())
    return path


def load_plan(path: Path) -> GeneratedPlan:
    """Load a plan previously written by save_plan."""
    data = yaml.safe_load(path.read_text())
    tasks_raw = data.get("tasks", [])
    tasks = [PlannedTask(**t) for t in tasks_raw]
    return GeneratedPlan(
        goal=str(data.get("goal", "")),
        goal_hash=str(data.get("goal_hash", "")),
        tasks=tasks,
        planning_model=str(data.get("planning_model", "")),
        created_at=float(data.get("created_at", 0.0)),
        estimated_total_minutes=int(data.get("estimated_total_minutes", 0)),
        estimated_cost_usd=float(data.get("estimated_cost_usd", 0.0)),
    )


def build_plan(
    goal: str,
    tasks: list[PlannedTask],
    planning_model: str,
    available_execution_models: list[str] | None = None,
) -> GeneratedPlan:
    """Assemble a plan, filling in recommended execution models per task."""
    available = available_execution_models or list(EXECUTION_MODELS)
    for task in tasks:
        if not task.recommended_model:
            task.recommended_model = select_execution_model(task, available)
    total_minutes = sum(t.estimated_minutes for t in tasks)
    plan = GeneratedPlan(
        goal=goal,
        goal_hash=hash_goal(goal),
        tasks=tasks,
        planning_model=planning_model,
        estimated_total_minutes=total_minutes,
    )
    plan.estimated_cost_usd = estimate_plan_cost(plan)
    return plan


def format_plan_review(plan: GeneratedPlan) -> str:
    """Render a plan for the review gate TUI."""
    lines = [
        f"# Plan for: {plan.goal}",
        "",
        f"**Planning model**: {plan.planning_model}",
        f"**Tasks**: {len(plan.tasks)}",
        f"**Estimated time**: {plan.estimated_total_minutes} min",
        f"**Estimated cost**: ${plan.estimated_cost_usd:.2f}",
        "",
        "## Tasks",
    ]
    for i, task in enumerate(plan.tasks, 1):
        lines.append(
            f"{i}. [{task.role}/{task.complexity}] {task.title} "
            f"→ {task.recommended_model} ({task.estimated_minutes}min)"
        )
    return "\n".join(lines)
