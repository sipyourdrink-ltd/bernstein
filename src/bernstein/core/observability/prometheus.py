"""Prometheus metrics for Bernstein.

Exposes task lifecycle, agent activity, cost, and evolution proposal
counters/gauges so that a Prometheus scraper can pull them from the
``/metrics`` endpoint on the task server.

Usage::

    from bernstein.core.observability.prometheus import update_metrics_from_status, registry
    from prometheus_client import generate_latest

    update_metrics_from_status(status_dict)
    payload = generate_latest(registry)
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

logger = logging.getLogger(__name__)

# prometheus_client can hang on Windows during import due to multiprocessing issues.
# Make it optional with stub fallbacks for Windows compatibility.
_PROMETHEUS_AVAILABLE = False
try:
    # Set a short import timeout using threading on Windows
    if sys.platform == "win32":
        import threading
        _import_done = threading.Event()
        _import_error: Exception | None = None
        def _try_import() -> None:
            global _PROMETHEUS_AVAILABLE, _import_error
            try:
                global CollectorRegistry, Counter, Gauge, Histogram, generate_latest
                from prometheus_client import (
                    CollectorRegistry,
                    Counter,
                    Gauge,
                    Histogram,
                    generate_latest,
                )
                _PROMETHEUS_AVAILABLE = True
            except Exception as e:
                _import_error = e
            finally:
                _import_done.set()
        t = threading.Thread(target=_try_import, daemon=True)
        t.start()
        if not _import_done.wait(timeout=3.0):
            logger.warning("prometheus_client import timed out on Windows - metrics disabled")
        elif _import_error:
            logger.warning("prometheus_client import failed: %s - metrics disabled", _import_error)
    else:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
        )
        _PROMETHEUS_AVAILABLE = True
except ImportError as e:
    logger.warning("prometheus_client not available: %s - metrics disabled", e)

# Stub classes for when prometheus is unavailable
if not _PROMETHEUS_AVAILABLE:
    class CollectorRegistry:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    class _StubMetric:
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
        def labels(self, *args: Any, **kwargs: Any) -> "_StubMetric": return self
        def inc(self, *args: Any, **kwargs: Any) -> None: pass
        def dec(self, *args: Any, **kwargs: Any) -> None: pass
        def set(self, *args: Any, **kwargs: Any) -> None: pass
        def observe(self, *args: Any, **kwargs: Any) -> None: pass
    Counter = Gauge = Histogram = _StubMetric  # type: ignore[misc,assignment]
    def generate_latest(*args: Any, **kwargs: Any) -> bytes: return b""

__all__ = [
    "agent_spawn_duration",
    "agent_transition_reasons_total",
    "agents_active",
    "cost_usd_by_model_total",
    "cost_usd_total",
    "evolution_errors_by_type",
    "evolve_proposals_total",
    "generate_latest",
    "get_transition_reason_histogram",
    "merge_duration",
    "record_transition_reason",
    "registry",
    "set_prometheus_enabled",
    "task_duration_seconds",
    "task_queue_depth",
    "task_transition_reasons_total",
    "tasks_active",
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
    labelnames=["status", "role"],
    registry=registry,
)

tasks_active: Gauge = Gauge(
    "bernstein_tasks_active",
    "Number of currently active (claimed/in_progress) tasks.",
    labelnames=["role"],
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
    labelnames=["status", "role"],
    registry=registry,
)

agent_spawn_duration: Histogram = Histogram(
    "bernstein_agent_spawn_duration_seconds",
    "Time taken to spawn an agent subprocess.",
    buckets=(1, 2, 5, 10, 20, 30),
    labelnames=["adapter"],
    registry=registry,
)

merge_duration: Histogram = Histogram(
    "bernstein_merge_duration_seconds",
    "Time taken to merge task work into main.",
    buckets=(1, 2, 5, 10, 20, 30, 60),
    registry=registry,
)

cost_usd_total: Counter = Counter(
    "bernstein_cost_usd_total",
    "Total API cost in USD.",
    labelnames=["adapter"],
    registry=registry,
)

cost_usd_by_model_total: Counter = Counter(
    "bernstein_cost_usd_by_model_total",
    "Total API cost in USD, partitioned by model.",
    labelnames=["model", "adapter"],
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

agent_transition_reasons_total: Counter = Counter(
    "bernstein_agent_transition_reasons_total",
    "Agent lifecycle transitions by reason (why agents die or change state).",
    labelnames=["reason", "role"],
    registry=registry,
)

task_transition_reasons_total: Counter = Counter(
    "bernstein_task_transition_reasons_total",
    "Task lifecycle transitions by reason.",
    labelnames=["reason", "role"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Cardinality guard — only allow known TransitionReason enum values as labels.
# Unknown values are bucketed under "unknown" to prevent cardinality explosion.
# ---------------------------------------------------------------------------

_KNOWN_REASONS: frozenset[str] = frozenset(
    {
        "completed",
        "aborted",
        "retry",
        "prompt_too_long",
        "max_output_tokens",
        "max_turns",
        "provider_413",
        "provider_529",
        "compaction_failed",
        "stop_hook_blocked",
        "permission_denied",
        "sibling_aborted",
        "orphan_recovered",
    }
)

_CARDINALITY_LIMIT: int = 64
_seen_reasons: set[str] = set()


def _sanitize_reason(raw: str) -> str:
    """Normalise a transition reason label and enforce cardinality limits.

    Returns a known reason string unchanged, or ``"unknown"`` if the value
    is not in the closed set or the cardinality limit has been reached.
    """
    value = raw.strip().lower()
    if value in _KNOWN_REASONS:
        return value
    # Dynamic overflow bucket
    if len(_seen_reasons) >= _CARDINALITY_LIMIT:
        return "unknown"
    _seen_reasons.add(value)
    return value if value else "unknown"


def get_transition_reason_histogram() -> dict[str, dict[str, float]]:
    """Return in-process transition reason counts from the Prometheus counters.

    Reads the current sample values directly from the registry so the TUI
    and status endpoint can display a histogram without scraping ``/metrics``.

    Returns:
        Dict with ``"agent"`` and ``"task"`` keys.  Each maps a reason label
        (e.g. ``"completed"``, ``"aborted"``) to its cumulative count.
        Labels with a count of zero are omitted.

    Example::

        {
            "agent": {"completed": 12.0, "aborted": 3.0},
            "task":  {"completed": 12.0, "retry": 1.0},
        }
    """
    result: dict[str, dict[str, float]] = {"agent": {}, "task": {}}
    try:
        for metric_family in registry.collect():
            if metric_family.name == "bernstein_agent_transition_reasons_total":
                target = result["agent"]
            elif metric_family.name == "bernstein_task_transition_reasons_total":
                target = result["task"]
            else:
                continue
            for sample in metric_family.samples:
                # Skip _created timestamps; only aggregate _total samples
                if not sample.name.endswith("_total"):
                    continue
                if sample.value <= 0:
                    continue
                reason = sample.labels.get("reason", "unknown")
                target[reason] = target.get(reason, 0.0) + sample.value
    except Exception:
        logger.debug("get_transition_reason_histogram failed", exc_info=True)
    return result


def record_transition_reason(
    reason: str,
    role: str = "unknown",
    *,
    entity_type: str = "agent",
) -> None:
    """Increment the transition-reason counter for a lifecycle event.

    Safe to call from hot paths — respects the kill-switch and silently
    drops bad input rather than raising.

    Args:
        reason: The ``TransitionReason`` value (or raw string).
        role: Agent/task role label (e.g. ``"backend"``, ``"qa"``).
        entity_type: ``"agent"`` or ``"task"`` — selects which counter family.
    """
    if not _prometheus_enabled:
        return
    sanitized = _sanitize_reason(reason)
    role = (role.strip() or "unknown").lower()
    try:
        if entity_type == "task":
            task_transition_reasons_total.labels(reason=sanitized, role=role).inc()
        else:
            agent_transition_reasons_total.labels(reason=sanitized, role=role).inc()
    except Exception:
        logger.debug("Failed to record transition reason metric", exc_info=True)


# ---------------------------------------------------------------------------
# Kill-switch — lets operators disable the Prometheus sink without restarting
# ---------------------------------------------------------------------------

_prometheus_enabled: bool = True


def set_prometheus_enabled(enabled: bool) -> None:
    """Enable or disable the Prometheus event sink (kill-switch).

    When disabled, :func:`update_metrics_from_status` is a no-op.  This lets
    operators silence Prometheus metric emission without restarting the server
    (e.g. when scraping is not configured and metric churn is unwanted).

    Args:
        enabled: ``True`` to enable (default); ``False`` to kill the sink.
    """
    global _prometheus_enabled
    _prometheus_enabled = enabled


# ---------------------------------------------------------------------------
# Internal state for delta-tracking on counters
# ---------------------------------------------------------------------------

_prev_tasks: dict[str, float] = {}
_prev_cost: float = 0.0
_prev_cost_by_model: dict[str, float] = {}


def _inc_counter_delta(prev_store: dict[str, float], key: str, current: float, counter: Any, **labels: str) -> None:
    """Increment *counter* by the positive delta since the last observation."""
    prev = prev_store.get(key, 0.0)
    delta = current - prev
    if delta > 0:
        counter.labels(**labels).inc(delta)
    prev_store[key] = current


def _sync_per_role_metrics(per_role: list[dict[str, Any]]) -> None:
    """Update per-role task counters and active gauges."""
    for role_entry in per_role:
        role = str(role_entry.get("role", "unknown"))
        for status_key in ("done", "failed"):
            current = float(role_entry.get(status_key, 0))
            _inc_counter_delta(_prev_tasks, f"{role}:{status_key}", current, tasks_total, status=status_key, role=role)
        claimed = float(role_entry.get("claimed", 0))
        tasks_active.labels(role=role).set(claimed)
        agents_active.labels(role=role).set(claimed)


def _sync_global_task_counters(status_data: dict[str, Any]) -> None:
    """Update global (role=all) task counters."""
    for status_key in ("done", "failed"):
        current = float(status_data.get(status_key, 0))
        _inc_counter_delta(_prev_tasks, f"total:{status_key}", current, tasks_total, status=status_key, role="all")


def _sync_cost_by_model(status_data: dict[str, Any]) -> None:
    """Update per-model cost counters."""
    global _prev_cost_by_model
    per_model_raw: Any = status_data.get("cost_by_model_usd", {})
    if not isinstance(per_model_raw, dict):
        return
    raw_map = cast("dict[str, Any]", per_model_raw)
    for model, raw_cost in raw_map.items():
        model_name = str(model).strip() or "unknown"
        current_model_cost = float(raw_cost or 0.0)
        _inc_counter_delta(
            _prev_cost_by_model,
            model_name,
            current_model_cost,
            cost_usd_by_model_total,
            model=model_name,
            adapter="unknown",
        )


def update_metrics_from_status(status_data: dict[str, Any]) -> None:
    """Sync Prometheus gauges/counters from a ``/status`` response dict.

    Counters are monotonically increasing; this function computes the delta
    between the last observed value and the current one so that repeated
    calls never decrement a counter.

    If the Prometheus sink has been disabled via :func:`set_prometheus_enabled`,
    this function is a no-op.

    Args:
        status_data: The parsed JSON body returned by ``GET /status``.
            Expected keys: ``open``, ``claimed``, ``done``, ``failed``,
            ``total_cost_usd``, and optionally ``per_role`` (list of dicts
            with ``role``, ``open``, ``claimed``, ``done``, ``failed`` keys).
    """
    if not _prometheus_enabled:
        return

    global _prev_cost

    _sync_per_role_metrics(status_data.get("per_role", []))
    _sync_global_task_counters(status_data)

    task_queue_depth.set(float(status_data.get("open", 0)))

    current_cost = float(status_data.get("total_cost_usd", 0.0))
    cost_delta = current_cost - _prev_cost
    if cost_delta > 0:
        cost_usd_total.labels(adapter="total").inc(cost_delta)
    _prev_cost = current_cost

    _sync_cost_by_model(status_data)
