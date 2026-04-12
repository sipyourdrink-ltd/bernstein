"""Explain a plan file -- extract stats and produce a plain-English summary.

Parses a plan YAML dict (already loaded) and computes summary statistics
without invoking any LLM.  The output is a human-readable explanation of
what the plan will do, how many agents it needs, and estimated cost range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bernstein.cli.cost_estimate import _SCOPE_MULTIPLIER, COST_PER_COMPLEXITY

# ---------------------------------------------------------------------------
# Cost confidence margin applied to the heuristic estimate.
# The low/high range is (1 - margin) .. (1 + margin) of the point estimate.
# ---------------------------------------------------------------------------
_COST_MARGIN: float = 0.3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanSummary:
    """Immutable summary statistics extracted from a plan YAML.

    Attributes:
        total_stages: Number of stages in the plan.
        total_steps: Total number of steps across all stages.
        roles_used: Sorted list of unique roles referenced by steps.
        estimated_agents: Estimated peak number of concurrent agents
            (widest stage).
        estimated_cost_range: ``(low, high)`` cost estimate in USD.
        critical_path_length: Length of the longest dependency chain
            measured in stages.
        description: Plan-level description, if present.
    """

    total_stages: int
    total_steps: int
    roles_used: list[str] = field(default_factory=list)
    estimated_agents: int = 1
    estimated_cost_range: tuple[float, float] = (0.0, 0.0)
    critical_path_length: int = 0
    description: str = ""


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _estimate_step_cost(step: dict[str, Any]) -> float:
    """Return a heuristic point-estimate cost (USD) for a single step."""
    complexity = str(step.get("complexity", "medium")).lower()
    scope = str(step.get("scope", "medium")).lower()

    base = COST_PER_COMPLEXITY.get(complexity, COST_PER_COMPLEXITY["medium"])
    multiplier = _SCOPE_MULTIPLIER.get(scope, 1.0)
    return base * multiplier


def _compute_critical_path(stages: list[dict[str, Any]]) -> int:
    """Return the length of the longest dependency chain in stages.

    Each stage may declare ``depends_on: [stage_name, ...]``.  The critical
    path is the longest path through the DAG measured in number of stages.
    """
    if not stages:
        return 0

    name_to_deps: dict[str, list[str]] = {}
    for stage in stages:
        name = str(stage.get("name", ""))
        deps = stage.get("depends_on", [])
        if not isinstance(deps, list):
            deps = [deps]
        name_to_deps[name] = [str(d) for d in deps]

    # Memoised DFS for longest path ending at each node.
    cache: dict[str, int] = {}

    def _depth(name: str) -> int:
        if name in cache:
            return cache[name]
        deps = name_to_deps.get(name, [])
        if not deps:
            cache[name] = 1
            return 1
        longest = 1 + max(_depth(d) for d in deps if d in name_to_deps)
        cache[name] = longest
        return longest

    return max(_depth(n) for n in name_to_deps)


def analyze_plan(plan_data: dict[str, Any]) -> PlanSummary:
    """Extract summary statistics from a parsed plan YAML dict.

    This is a pure-computation function -- no IO, no LLM calls.

    Args:
        plan_data: The plan dict as returned by ``yaml.safe_load()``.

    Returns:
        A frozen :class:`PlanSummary` with computed statistics.
    """
    stages: list[dict[str, Any]] = plan_data.get("stages", [])
    if not isinstance(stages, list):
        stages = []

    total_stages = len(stages)

    # Collect steps and roles
    all_steps: list[dict[str, Any]] = []
    roles: set[str] = set()
    max_parallel = 0

    for stage in stages:
        steps = stage.get("steps", [])
        if not isinstance(steps, list):
            steps = []
        all_steps.extend(steps)
        stage_width = len(steps)
        if stage_width > max_parallel:
            max_parallel = stage_width
        for step in steps:
            role = step.get("role")
            if role:
                roles.add(str(role))

    total_steps = len(all_steps)

    # Cost estimation
    total_cost = sum(_estimate_step_cost(s) for s in all_steps)
    cost_low = round(total_cost * (1.0 - _COST_MARGIN), 2)
    cost_high = round(total_cost * (1.0 + _COST_MARGIN), 2)

    # Critical path
    critical_path = _compute_critical_path(stages)

    # Description
    description = str(plan_data.get("description", "")).strip()

    return PlanSummary(
        total_stages=total_stages,
        total_steps=total_steps,
        roles_used=sorted(roles),
        estimated_agents=max(max_parallel, 1) if total_steps > 0 else 0,
        estimated_cost_range=(cost_low, cost_high),
        critical_path_length=critical_path,
        description=description,
    )


# ---------------------------------------------------------------------------
# Explanation generation
# ---------------------------------------------------------------------------


def generate_explanation(summary: PlanSummary) -> str:
    """Generate a plain-English paragraph from plan summary statistics.

    Args:
        summary: The :class:`PlanSummary` to describe.

    Returns:
        A human-readable paragraph summarising the plan.
    """
    parts: list[str] = []

    parts.append(
        f"This plan has {summary.total_stages} stage{'s' if summary.total_stages != 1 else ''} "
        f"with {summary.total_steps} step{'s' if summary.total_steps != 1 else ''}."
    )

    if summary.roles_used:
        roles_str = ", ".join(summary.roles_used)
        parts.append(
            f"It uses {len(summary.roles_used)} role{'s' if len(summary.roles_used) != 1 else ''} ({roles_str})."
        )

    if summary.critical_path_length > 0:
        parts.append(
            f"The critical path is {summary.critical_path_length} "
            f"stage{'s' if summary.critical_path_length != 1 else ''} long."
        )

    if summary.estimated_agents > 0:
        parts.append(
            f"Up to {summary.estimated_agents} agent{'s' if summary.estimated_agents != 1 else ''} "
            f"may run concurrently."
        )

    low, high = summary.estimated_cost_range
    parts.append(f"Estimated cost: ${low:.2f}-${high:.2f}.")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Full formatted output
# ---------------------------------------------------------------------------


def format_plan_explanation(plan_data: dict[str, Any]) -> str:
    """Produce full formatted output combining summary stats and explanation.

    Args:
        plan_data: The plan dict as returned by ``yaml.safe_load()``.

    Returns:
        Multi-line string suitable for terminal display.
    """
    summary = analyze_plan(plan_data)
    explanation = generate_explanation(summary)

    lines: list[str] = []

    # Title
    name = plan_data.get("name", "Untitled Plan")
    lines.append(f"Plan: {name}")
    lines.append("")

    # Description
    if summary.description:
        lines.append(summary.description)
        lines.append("")

    # Stats table
    lines.append("Summary")
    lines.append("-" * 40)
    lines.append(f"  Stages:          {summary.total_stages}")
    lines.append(f"  Steps:           {summary.total_steps}")
    lines.append(f"  Roles:           {', '.join(summary.roles_used) if summary.roles_used else 'none'}")
    lines.append(f"  Peak agents:     {summary.estimated_agents}")
    cp_suffix = "s" if summary.critical_path_length != 1 else ""
    lines.append(f"  Critical path:   {summary.critical_path_length} stage{cp_suffix}")
    low, high = summary.estimated_cost_range
    lines.append(f"  Estimated cost:  ${low:.2f}-${high:.2f}")
    lines.append("")

    # Explanation paragraph
    lines.append(explanation)

    return "\n".join(lines)
