"""Hook dry-run mode for testing configurations (HOOK-015).

Simulates hook event dispatch without executing any handlers, allowing
users to verify which hooks *would* fire for a given event type and
payload.  Useful for debugging hook configurations and testing new
event wiring before enabling it in production.

Usage::

    from bernstein.core.hook_dry_run import simulate_hook_event, format_dry_run_report

    report = simulate_hook_event("task.failed", {"task_id": "t-42", "error": "OOM"})
    print(format_dry_run_report(report))
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DryRunResult:
    """Outcome of evaluating a single handler against a simulated event.

    Attributes:
        hook_name: Registered handler name (e.g. ``"slack_notify"``).
        event_type: The event type that was simulated.
        would_execute: Whether this handler would have been invoked.
        payload: The payload that would have been passed to the handler.
        matched_handlers: Names of all handlers that matched the event.
        execution_time_ms: Estimated execution time, if available.
    """

    hook_name: str
    event_type: str
    would_execute: bool
    payload: dict[str, Any]
    matched_handlers: list[str]
    execution_time_ms: float | None = None


@dataclass(frozen=True)
class DryRunReport:
    """Aggregated report for a simulated hook event dispatch.

    Attributes:
        event_type: The event type that was simulated.
        payload: The payload used for simulation.
        results: Per-handler dry-run results.
        total_handlers: Total number of handlers evaluated.
        would_trigger: Number of handlers that would have fired.
    """

    event_type: str
    payload: dict[str, Any]
    results: list[DryRunResult] = field(default_factory=lambda: list[DryRunResult]())
    total_handlers: int = 0
    would_trigger: int = 0


# ---------------------------------------------------------------------------
# Default hook registry
# ---------------------------------------------------------------------------

DEFAULT_HOOK_REGISTRY: dict[str, list[str]] = {
    "task.created": ["log_handler"],
    "task.claimed": ["log_handler"],
    "task.completed": ["slack_notify"],
    "task.failed": ["slack_notify", "pagerduty_alert"],
    "task.retried": ["log_handler"],
    "agent.spawned": ["log_handler"],
    "agent.heartbeat": ["log_handler"],
    "agent.completed": ["log_handler", "slack_notify"],
    "agent.killed": ["slack_notify", "pagerduty_alert"],
    "agent.stalled": ["pagerduty_alert"],
    "merge.started": ["log_handler"],
    "merge.completed": ["slack_notify"],
    "merge.conflict": ["slack_notify", "pagerduty_alert"],
    "quality_gate.passed": ["log_handler"],
    "quality_gate.failed": ["slack_notify"],
    "budget.threshold": ["slack_notify"],
    "budget.exceeded": ["slack_notify", "pagerduty_alert"],
    "config.drift": ["slack_notify"],
    "orchestrator.startup": ["log_handler"],
    "orchestrator.shutdown": ["log_handler"],
    "orchestrator.tick": ["log_handler"],
    "plan.loaded": ["log_handler"],
    "plan.stage_completed": ["slack_notify"],
    "permission.denied": ["log_handler", "slack_notify"],
    "permission.escalated": ["slack_notify", "pagerduty_alert"],
    "secret.detected": ["slack_notify", "pagerduty_alert"],
    "circuit_breaker.tripped": ["slack_notify", "pagerduty_alert"],
    "cluster.node_joined": ["log_handler"],
    "pre_merge": ["log_handler"],
    "pre_spawn": ["log_handler"],
    "pre_approve": ["log_handler"],
}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


def simulate_hook_event(
    event_type: str,
    payload: dict[str, Any],
    hook_registry: dict[str, list[str]] | None = None,
) -> DryRunReport:
    """Evaluate which hooks would fire for *event_type* without executing them.

    Args:
        event_type: Dot-separated event name (e.g. ``"task.failed"``).
        payload: Arbitrary payload dict that would accompany the event.
        hook_registry: Optional override for the handler mapping.  When
            ``None``, :data:`DEFAULT_HOOK_REGISTRY` is used.

    Returns:
        A :class:`DryRunReport` describing what would happen.
    """
    registry = hook_registry if hook_registry is not None else DEFAULT_HOOK_REGISTRY
    handlers = registry.get(event_type, [])

    results: list[DryRunResult] = []
    for handler_name in handlers:
        start = time.monotonic()
        # Simulate matching — all registered handlers for the event would fire.
        elapsed_ms = (time.monotonic() - start) * 1000.0
        results.append(
            DryRunResult(
                hook_name=handler_name,
                event_type=event_type,
                would_execute=True,
                payload=payload,
                matched_handlers=list(handlers),
                execution_time_ms=elapsed_ms,
            )
        )

    return DryRunReport(
        event_type=event_type,
        payload=payload,
        results=results,
        total_handlers=len(handlers),
        would_trigger=len(results),
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_dry_run_report(report: DryRunReport) -> str:
    """Render a :class:`DryRunReport` as a human-readable string.

    Args:
        report: The dry-run report to format.

    Returns:
        Multi-line summary suitable for terminal output.
    """
    lines: list[str] = [
        f"Hook dry-run report for event '{report.event_type}':",
        f"  Payload: {report.payload}",
        f"  Total handlers: {report.total_handlers}",
        f"  Would trigger: {report.would_trigger}",
    ]

    if not report.results:
        lines.append("  No handlers registered for this event.")
    else:
        lines.append("  Handlers:")
        for result in report.results:
            status = "WOULD FIRE" if result.would_execute else "SKIP"
            time_str = (
                f" ({result.execution_time_ms:.2f}ms)"
                if result.execution_time_ms is not None
                else ""
            )
            lines.append(f"    - {result.hook_name}: {status}{time_str}")

    return "\n".join(lines)
