"""Prometheus metrics for Bernstein.

Exposes task lifecycle, agent activity, cost, and evolution proposal
counters/gauges so that a Prometheus scraper can pull them from the
``/metrics`` endpoint on the task server.

Usage::

    from bernstein.core.prometheus import update_metrics_from_status, registry
    from prometheus_client import generate_latest

    update_metrics_from_status(status_dict)
    payload = generate_latest(registry)
"""

from __future__ import annotations

from typing import Any, cast

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "agents_active",
    "cost_usd_by_model_total",
    "cost_usd_total",
    "evolution_errors_by_type",
    "evolve_proposals_total",
    "generate_latest",
    "registry",
    "task_duration_seconds",
    "task_queue_depth",
    "tasks_total",
    "update_metrics_from_status",
]

# ---------------------------------------------------------------------------
# Dedicated registry — avoids polluting the default global registry, which
# matters in tests where multiple apps share a process.
# ---------------------------------------------------------------------------

registry: CollectorRegistry = CollectorRegistry(auto_describe=True)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

tasks_total: Counter = Counter(
    "bernstein_tasks_total",
    "Total tasks by terminal or active status.",
    labelnames=["status"],
    registry=registry,
)

agents_active: Gauge = Gauge(
    "bernstein_agents_active",
    "Currently active agents by role.",
    labelnames=["role"],
    registry=registry,
)

task_queue_depth: Gauge = Gauge(
    "bernstein_task_queue_depth",
    "Number of open (unclaimed) tasks in the queue.",
    registry=registry,
)

task_duration_seconds: Histogram = Histogram(
    "bernstein_task_duration_seconds",
    "Task completion time in seconds.",
    buckets=(10, 30, 60, 120, 300, 600, 1800, 3600),
    registry=registry,
)

cost_usd_total: Counter = Counter(
    "bernstein_cost_usd_total",
    "Total API cost in USD.",
    registry=registry,
)

cost_usd_by_model_total: Counter = Counter(
    "bernstein_cost_usd_by_model_total",
    "Total API cost in USD, partitioned by model.",
    labelnames=["model"],
    registry=registry,
)

evolve_proposals_total: Counter = Counter(
    "bernstein_evolve_proposals_total",
    "Evolution proposals by verdict (accepted/rejected/pending).",
    labelnames=["verdict"],
    registry=registry,
)

evolution_errors_by_type: Counter = Counter(
    "bernstein_evolution_errors_by_type",
    "Evolution loop errors by error type.",
    labelnames=["error_type"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Internal state for delta-tracking on counters
# ---------------------------------------------------------------------------

_prev_tasks: dict[str, float] = {}
_prev_cost: float = 0.0
_prev_cost_by_model: dict[str, float] = {}


def update_metrics_from_status(status_data: dict[str, Any]) -> None:
    """Sync Prometheus gauges/counters from a ``/status`` response dict.

    Counters are monotonically increasing; this function computes the delta
    between the last observed value and the current one so that repeated
    calls never decrement a counter.

    Args:
        status_data: The parsed JSON body returned by ``GET /status``.
            Expected keys: ``open``, ``claimed``, ``done``, ``failed``,
            ``total_cost_usd``, and optionally ``per_role`` (list of dicts
            with ``role``, ``open``, ``claimed`` keys for agent tracking).
    """
    global _prev_cost, _prev_cost_by_model

    # -- Task counters -------------------------------------------------------
    for status_key in ("open", "claimed", "done", "failed"):
        current: float = float(status_data.get(status_key, 0))
        prev: float = _prev_tasks.get(status_key, 0.0)
        delta = current - prev
        if delta > 0:
            tasks_total.labels(status=status_key).inc(delta)
        _prev_tasks[status_key] = current

    # -- Queue depth gauge (for HPA) -----------------------------------------
    queue_depth: float = float(status_data.get("open", 0))
    task_queue_depth.set(queue_depth)

    # -- Cost counter --------------------------------------------------------
    current_cost: float = float(status_data.get("total_cost_usd", 0.0))
    cost_delta = current_cost - _prev_cost
    if cost_delta > 0:
        cost_usd_total.inc(cost_delta)
    _prev_cost = current_cost

    # -- Cost by model counter ----------------------------------------------
    per_model_raw: Any = status_data.get("cost_by_model_usd", {})
    per_model: dict[str, float] = {}
    if isinstance(per_model_raw, dict):
        raw_map = cast("dict[str, Any]", per_model_raw)
        for model in raw_map:
            raw_cost = raw_map[model]
            per_model[str(model)] = float(raw_cost or 0.0)
    for model_name, current_model_cost in per_model.items():
        model_name = model_name.strip() or "unknown"
        previous_model_cost = _prev_cost_by_model.get(model_name, 0.0)
        delta_model_cost = current_model_cost - previous_model_cost
        if delta_model_cost > 0:
            cost_usd_by_model_total.labels(model=model_name).inc(delta_model_cost)
        _prev_cost_by_model[model_name] = current_model_cost

    # -- Active-agent gauges -------------------------------------------------
    # Derive active-agent counts from per_role claimed tasks as a proxy.
    per_role: list[dict[str, Any]] = status_data.get("per_role", [])
    for role_entry in per_role:
        role: str = str(role_entry.get("role", "unknown"))
        claimed_count: float = float(role_entry.get("claimed", 0))
        agents_active.labels(role=role).set(claimed_count)
