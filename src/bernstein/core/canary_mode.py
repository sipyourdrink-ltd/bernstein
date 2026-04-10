"""Orchestrator canary mode (ORCH-021).

Simulates routing decisions without spawning agents, allowing operators to
compare how two configs would route the same set of tasks.  This is useful
for validating config changes (adapter swaps, model policy tweaks, effort
overrides) before deploying them to a live orchestrator.

Usage::

    from bernstein.core.canary_mode import (
        simulate_routing,
        compare_decisions,
        build_canary_report,
        format_canary_report,
    )

    primary = [simulate_routing(t, primary_cfg) for t in tasks]
    canary  = [simulate_routing(t, canary_cfg) for t in tasks]
    report  = build_canary_report(primary, canary)
    print(format_canary_report(report))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanaryDecision:
    """Result of simulating what adapter/model would be chosen for a task.

    Attributes:
        task_id: Task identifier.
        would_spawn: Whether the task would actually trigger a spawn.
        adapter: Adapter name that would be selected.
        model: Model name that would be selected.
        effort: Effort level that would be selected.
        reason: Human-readable explanation of the routing decision.
    """

    task_id: str
    would_spawn: bool
    adapter: str
    model: str
    effort: str
    reason: str


@dataclass(frozen=True)
class CanaryDiff:
    """Comparison of a single task's routing under primary vs canary config.

    Attributes:
        task_id: Task identifier.
        primary_adapter: Adapter chosen under primary config.
        canary_adapter: Adapter chosen under canary config.
        primary_model: Model chosen under primary config.
        canary_model: Model chosen under canary config.
        matches: True when both configs produce the same adapter+model.
    """

    task_id: str
    primary_adapter: str
    canary_adapter: str
    primary_model: str
    canary_model: str
    matches: bool


@dataclass(frozen=True)
class CanaryReport:
    """Aggregated comparison report for primary vs canary routing.

    Attributes:
        total_tasks: Number of tasks that were simulated.
        decisions: Canary config decisions (for inspection).
        diffs: Per-task comparison results.
        match_rate: Fraction of tasks where primary and canary agree (0.0-1.0).
        generated_at: ISO-8601 timestamp of report generation.
    """

    total_tasks: int
    decisions: list[CanaryDecision]
    diffs: list[CanaryDiff]
    match_rate: float
    generated_at: str


# ---------------------------------------------------------------------------
# Default routing tables (mirrors router.py heuristics)
# ---------------------------------------------------------------------------

_DEFAULT_ADAPTER = "claude"

_ROLE_MODEL_MAP: dict[str, tuple[str, str]] = {
    "manager": ("opus", "max"),
    "architect": ("opus", "max"),
    "security": ("opus", "max"),
}

_COMPLEXITY_MODEL_MAP: dict[str, tuple[str, str]] = {
    "high": ("sonnet", "high"),
    "medium": ("sonnet", "high"),
    "low": ("haiku", "low"),
}

_SCOPE_MODEL_MAP: dict[str, tuple[str, str]] = {
    "large": ("opus", "max"),
}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def _resolve_adapter(config: dict[str, Any]) -> str:
    """Extract adapter name from a config dict.

    Args:
        config: Configuration dictionary, may contain 'adapter' key.

    Returns:
        Adapter name string.
    """
    return str(config.get("adapter", _DEFAULT_ADAPTER))


def _resolve_model_effort(
    task: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, str, str]:
    """Determine model, effort, and reason for a task under a config.

    Applies the same priority cascade as ``router.route_task``:
    1. Explicit overrides in config (``model``, ``effort``).
    2. Task-level overrides (``task.model``, ``task.effort``).
    3. Role-based defaults.
    4. Scope-based defaults.
    5. Complexity-based heuristic fallback.

    Args:
        task: Task dictionary with at least 'id'; optionally 'role',
              'complexity', 'scope', 'model', 'effort', 'priority'.
        config: Configuration dictionary; may contain 'model', 'effort'
                overrides.

    Returns:
        Tuple of (model, effort, reason).
    """
    # 1. Config-level overrides
    if config.get("model") or config.get("effort"):
        model = str(config.get("model", "sonnet"))
        effort = str(config.get("effort", "high"))
        return model, effort, "config override"

    # 2. Task-level overrides
    task_model = task.get("model")
    task_effort = task.get("effort")
    if task_model or task_effort:
        model = str(task_model or "sonnet")
        effort = str(task_effort or "high")
        return model, effort, "task override"

    # 3. Critical priority
    priority = task.get("priority", 3)
    if priority == 1:
        return "opus", "max", "critical priority"

    # 4. Role-based
    role = task.get("role", "")
    if role in _ROLE_MODEL_MAP:
        m, e = _ROLE_MODEL_MAP[role]
        return m, e, f"role={role}"

    # 5. Scope-based
    scope = task.get("scope", "")
    if scope in _SCOPE_MODEL_MAP:
        m, e = _SCOPE_MODEL_MAP[scope]
        return m, e, f"scope={scope}"

    # 6. Complexity-based fallback
    complexity = task.get("complexity", "medium")
    m, e = _COMPLEXITY_MODEL_MAP.get(complexity, ("sonnet", "high"))
    return m, e, f"complexity={complexity}"


def simulate_routing(task: dict[str, Any], config: dict[str, Any]) -> CanaryDecision:
    """Simulate what adapter/model would be chosen for a task under a config.

    This is a pure function with no side effects -- it never spawns agents
    or writes state.

    Args:
        task: Task dictionary with at least 'id'.  Optional keys: 'role',
              'complexity', 'scope', 'model', 'effort', 'priority'.
        config: Configuration dictionary.  Optional keys: 'adapter',
                'model', 'effort'.

    Returns:
        CanaryDecision describing the simulated routing outcome.
    """
    task_id = str(task.get("id", "unknown"))
    adapter = _resolve_adapter(config)
    model, effort, reason = _resolve_model_effort(task, config)
    would_spawn = True

    logger.debug(
        "Canary: task=%s -> adapter=%s model=%s effort=%s (%s)",
        task_id,
        adapter,
        model,
        effort,
        reason,
    )

    return CanaryDecision(
        task_id=task_id,
        would_spawn=would_spawn,
        adapter=adapter,
        model=model,
        effort=effort,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_decisions(
    primary: list[CanaryDecision],
    canary: list[CanaryDecision],
) -> list[CanaryDiff]:
    """Compare primary vs canary decisions task-by-task.

    Both lists must be the same length and ordered by the same task
    sequence.  If lengths differ, comparison stops at the shorter list.

    Args:
        primary: Decisions under the primary (current) config.
        canary: Decisions under the canary (proposed) config.

    Returns:
        List of CanaryDiff, one per compared task pair.
    """
    diffs: list[CanaryDiff] = []
    for p, c in zip(primary, canary, strict=False):
        matches = p.adapter == c.adapter and p.model == c.model
        diffs.append(
            CanaryDiff(
                task_id=p.task_id,
                primary_adapter=p.adapter,
                canary_adapter=c.adapter,
                primary_model=p.model,
                canary_model=c.model,
                matches=matches,
            )
        )
    return diffs


def build_canary_report(
    primary: list[CanaryDecision],
    canary: list[CanaryDecision],
) -> CanaryReport:
    """Build an aggregated canary comparison report.

    Args:
        primary: Decisions under the primary (current) config.
        canary: Decisions under the canary (proposed) config.

    Returns:
        CanaryReport with per-task diffs and overall match rate.
    """
    diffs = compare_decisions(primary, canary)
    total = len(diffs)
    matching = sum(1 for d in diffs if d.matches)
    match_rate = matching / total if total > 0 else 1.0
    generated_at = datetime.now(tz=UTC).isoformat()

    return CanaryReport(
        total_tasks=total,
        decisions=list(canary),
        diffs=diffs,
        match_rate=match_rate,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_canary_report(report: CanaryReport) -> str:
    """Render a CanaryReport as a human-readable string.

    Args:
        report: The canary report to format.

    Returns:
        Multi-line string suitable for terminal output.
    """
    lines: list[str] = [
        "=== Canary Report ===",
        f"Generated: {report.generated_at}",
        f"Total tasks: {report.total_tasks}",
        f"Match rate:  {report.match_rate:.1%}",
        "",
    ]

    if not report.diffs:
        lines.append("No tasks to compare.")
        return "\n".join(lines)

    # Show diffs (mismatches first, then matches)
    mismatches = [d for d in report.diffs if not d.matches]
    matches = [d for d in report.diffs if d.matches]

    if mismatches:
        lines.append(f"--- Mismatches ({len(mismatches)}) ---")
        for d in mismatches:
            lines.append(f"  {d.task_id}: {d.primary_adapter}/{d.primary_model} -> {d.canary_adapter}/{d.canary_model}")
        lines.append("")

    if matches:
        lines.append(f"--- Matches ({len(matches)}) ---")
        for d in matches:
            lines.append(f"  {d.task_id}: {d.primary_adapter}/{d.primary_model}")

    return "\n".join(lines)
