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
import os
import sys
from typing import Any, cast

logger = logging.getLogger(__name__)

# prometheus_client can hang on Windows during import due to multiprocessing issues.
# Make it optional with stub fallbacks for Windows compatibility.

# How long the Windows import gets before this module gives up on it. Raising it
# is the fix for a machine slow enough that metrics disable themselves on every
# start; the default is what CI runners have always used.
_IMPORT_TIMEOUT_SECONDS = float(os.environ.get("BERNSTEIN_PROMETHEUS_IMPORT_TIMEOUT", "3.0"))

_PROMETHEUS_AVAILABLE = False
try:
    # Set a short import timeout using threading on Windows
    if sys.platform == "win32":
        import threading

        _import_done = threading.Event()
        _import_error: Exception | None = None
        # The worker publishes here and never touches this module's public names.
        # An import that lands after the timeout finds nobody reading the box: by
        # then the stubs below are defined and ``registry`` is built from them, and
        # rebinding ``Counter`` to the real class at that point would hand a real
        # metric constructor a stub registry to register itself into.
        _import_result: dict[str, Any] = {}

        def _try_import() -> None:
            global _import_error
            try:
                from prometheus_client import (
                    CollectorRegistry as _RealCollectorRegistry,
                )
                from prometheus_client import (
                    Counter as _RealCounter,
                )
                from prometheus_client import (
                    Gauge as _RealGauge,
                )
                from prometheus_client import (
                    Histogram as _RealHistogram,
                )
                from prometheus_client import (
                    generate_latest as _real_generate_latest,
                )
            except Exception as e:
                _import_error = e
            else:
                _import_result.update(
                    CollectorRegistry=_RealCollectorRegistry,
                    Counter=_RealCounter,
                    Gauge=_RealGauge,
                    Histogram=_RealHistogram,
                    generate_latest=_real_generate_latest,
                )
            finally:
                _import_done.set()

        t = threading.Thread(target=_try_import, daemon=True)
        t.start()
        if not _import_done.wait(timeout=_IMPORT_TIMEOUT_SECONDS):
            logger.warning("prometheus_client import timed out on Windows - metrics disabled")
        elif _import_error:
            logger.warning("prometheus_client import failed: %s - metrics disabled", _import_error)
        else:
            # ``wait`` returned True, so the worker's dict writes happened before
            # the event was set and are visible here.
            CollectorRegistry = _import_result["CollectorRegistry"]
            Counter = _import_result["Counter"]
            Gauge = _import_result["Gauge"]
            Histogram = _import_result["Histogram"]
            generate_latest = _import_result["generate_latest"]
            _PROMETHEUS_AVAILABLE = True
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

# Stubs for when prometheus is unavailable. Defined unconditionally so they can
# be inspected and tested on a machine where the real client imports fine; only
# the binding below is conditional.


class _StubCollectorRegistry:
    """No-op collector registry when prometheus_client is unavailable.

    It answers every call made on ``registry`` anywhere in this module, and the
    ``register`` that a real metric constructor performs on the registry it is
    handed. A stub that raises on those turns "metrics are disabled" into an
    import-time ``AttributeError``, which is worse than having no metrics and
    reads in CI as a broken conftest rather than as a fallback.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: prometheus_client not installed

    def register(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: nothing to collect from

    def unregister(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: nothing was registered

    def collect(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        return ()


class _StubMetric:
    """No-op metric stub when prometheus_client is unavailable."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: no-op metric

    def labels(self, *args: Any, **kwargs: Any) -> _StubMetric:
        return self

    def inc(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: no-op increment

    def dec(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: no-op decrement

    def set(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: no-op set

    def observe(self, *args: Any, **kwargs: Any) -> None:
        pass  # Stub: no-op observe


def _stub_generate_latest(*args: Any, **kwargs: Any) -> bytes:
    return b""


if not _PROMETHEUS_AVAILABLE:
    CollectorRegistry = _StubCollectorRegistry  # type: ignore[misc,assignment]
    Counter = Gauge = Histogram = _StubMetric  # type: ignore[misc,assignment]
    generate_latest = _stub_generate_latest  # type: ignore[assignment]


__all__ = [
    "action_cache_hits_total",
    "action_cache_savings_usd_total",
    "agent_spawn_duration",
    "agent_spawns_by_mode_total",
    "agent_transition_reasons_total",
    "agents_active",
    "best_of_n_candidates_total",
    "best_of_n_judge_score",
    "cascade_auto_promotions_total",
    "cluster_admission_failures_total",
    "cluster_heartbeats_total",
    "cluster_nodes_total",
    "cluster_scaling_decisions_total",
    "cluster_task_steals_total",
    "cost_usd_by_model_total",
    "cost_usd_total",
    "evolution_errors_by_type",
    "evolve_proposals_total",
    "generate_latest",
    "get_transition_reason_histogram",
    "incident_evals_total",
    "incident_recurrence_rate",
    "lineage_tamper_total",
    "memo_hits_total",
    "memo_misses_total",
    "memo_size_bytes",
    "merge_duration",
    "prompt_cache_drift_total",
    "record_transition_reason",
    "registry",
    "rework_rate_per_model",
    "sandbox_exec_count_total",
    "sandbox_session_created_total",
    "sandbox_session_destroyed_total",
    "sandbox_session_duration_seconds",
    "set_prometheus_enabled",
    "skills_unicode_tags_stripped_total",
    "task_duration_seconds",
    "task_queue_depth",
    "task_transition_reasons_total",
    "tasks_active",
    "tasks_total",
    "update_metrics_from_status",
]

# ---------------------------------------------------------------------------
# Dedicated registry - avoids polluting the default global registry, which
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

agent_spawns_by_mode_total: Counter = Counter(
    "bernstein_agent_spawns_by_mode_total",
    "Total agent spawns partitioned by applied mode profile.",
    labelnames=["mode_profile"],
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

# Task-budget countdown: how often agents land softly under the
# ``graceful-finish-on-low`` mode and how much headroom they leave behind.
# See ``bernstein.core.cost.budget_countdown``.
task_budget_graceful_finish_total: Counter = Counter(
    "bernstein_task_budget_graceful_finish_total",
    "Agents that ended via graceful-finish-on-low instead of hard-stop.",
    labelnames=["role"],
    registry=registry,
)

task_budget_remaining_at_finish_pct: Histogram = Histogram(
    "bernstein_task_budget_remaining_at_finish_pct",
    "Token-budget headroom (0-100) left when an agent finished gracefully.",
    buckets=(0, 5, 10, 15, 20, 30, 40, 50, 75, 100),
    labelnames=["role"],
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

memo_hits_total: Counter = Counter(
    "bernstein_memo_hits_total",
    "Persistent fingerprint memoization cache hits, partitioned by call site.",
    labelnames=["site"],
    registry=registry,
)

memo_misses_total: Counter = Counter(
    "bernstein_memo_misses_total",
    "Persistent fingerprint memoization cache misses, partitioned by call site.",
    labelnames=["site"],
    registry=registry,
)

memo_size_bytes: Gauge = Gauge(
    "bernstein_memo_size_bytes",
    "On-disk size of the persistent fingerprint memoization store in bytes.",
    registry=registry,
)

incident_evals_total: Counter = Counter(
    "bernstein_incident_evals_total",
    "Incident-derived eval cases synthesised, by severity (P0/P1/P2).",
    labelnames=["severity"],
    registry=registry,
)

incident_recurrence_rate: Gauge = Gauge(
    "bernstein_incident_recurrence_rate",
    "Fraction of incident eval cases that re-fail in the most recent run.",
    registry=registry,
)

action_cache_hits_total: Counter = Counter(
    "bernstein_action_cache_hits_total",
    "Action-cache hits: a recorded LLM/tool action served from disk instead of a live call.",
    labelnames=["model"],
    registry=registry,
)

action_cache_savings_usd_total: Counter = Counter(
    "bernstein_action_cache_savings_usd",
    "Cumulative USD saved by serving actions from the action cache.",
    labelnames=["model"],
    registry=registry,
)

lineage_tamper_total: Counter = Counter(
    "bernstein_lineage_tamper_total",
    "Lineage chain verification failures detected (per run).",
    labelnames=["run_id"],
    registry=registry,
)

best_of_n_candidates_total: Counter = Counter(
    "bernstein_best_of_n_candidates_total",
    "Best-of-N candidate workers by outcome (winner/loser).",
    labelnames=["outcome", "role"],
    registry=registry,
)

best_of_n_judge_score: Histogram = Histogram(
    "bernstein_best_of_n_judge_score",
    "LLM-as-judge rubric score per best-of-N candidate (0.0-1.0).",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    labelnames=["role"],
    registry=registry,
)

# Rework-rate ledger surface - see ``bernstein.core.routing.rework_ledger``.
rework_rate_per_model: Gauge = Gauge(
    "bernstein_rework_rate_per_model",
    "Observed rework rate per (model, effort, phase) bucket from the rework ledger.",
    labelnames=["model", "effort", "phase"],
    registry=registry,
)

cascade_auto_promotions_total: Counter = Counter(
    "bernstein_cascade_auto_promotions_total",
    "Cascade-router auto-promotion events triggered by rework-rate signal.",
    labelnames=["from_model", "to_model", "reason"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Prompt-cache locality - see ``bernstein.core.agents.prompt_cache_locality``.
# Drift events fire when consecutive same-role spawns disagree on the
# cacheable prefix, breaking Anthropic's 90% / OpenAI's 50% cache discount.
# Reason labels are bucketed against a closed taxonomy in the locality
# module; do not pass arbitrary strings.
# ---------------------------------------------------------------------------

prompt_cache_drift_total: Counter = Counter(
    "bernstein_prompt_cache_drift_total",
    "Prompt-cache prefix drift events between consecutive same-role spawns.",
    labelnames=["role", "reason"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Sandbox session metrics (oai-002 phase 2).  Labelled by backend so the
# orchestrator and operators can see which isolation primitive is in use
# and how often agents bounce off it.  Cardinality is bounded by the
# fixed set of registered backends (worktree, docker, e2b, modal, ...).
# ---------------------------------------------------------------------------

sandbox_session_created_total: Counter = Counter(
    "bernstein_sandbox_session_created_total",
    "Sandbox sessions created via SandboxBackend.create, by backend.",
    labelnames=["backend"],
    registry=registry,
)

sandbox_session_destroyed_total: Counter = Counter(
    "bernstein_sandbox_session_destroyed_total",
    "Sandbox sessions destroyed via SandboxBackend.destroy, by backend.",
    labelnames=["backend"],
    registry=registry,
)

sandbox_session_duration_seconds: Histogram = Histogram(
    "bernstein_sandbox_session_duration_seconds",
    "End-to-end session lifetime in seconds, by backend.",
    buckets=(1, 5, 30, 60, 300, 1800, 3600, 7200),
    labelnames=["backend"],
    registry=registry,
)

sandbox_exec_count_total: Counter = Counter(
    "bernstein_sandbox_exec_count_total",
    "Commands executed via SandboxSession.exec, by backend and exit code.",
    labelnames=["backend", "exit_code"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Skill-pack sanitization - counts invisible Unicode Tag codepoints stripped
# from a skill body before it is injected into an agent worktree. Labelled by
# the owning :class:`SkillSource` name so operators can spot a poisoned source
# without re-scanning the index. See ``bernstein.core.skills.sanitizer``.
# ---------------------------------------------------------------------------

skills_unicode_tags_stripped_total: Counter = Counter(
    "bernstein_skills_unicode_tags_stripped_total",
    "Invisible Unicode codepoints stripped from skill bodies before injection.",
    labelnames=["source_name"],
    registry=registry,
)

# ---------------------------------------------------------------------------
# Cardinality guard - only allow known TransitionReason enum values as labels.
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
    return value or "unknown"


# ---------------------------------------------------------------------------
# Cluster observability - node registry, task stealing, autoscaler.
# Labels are bucketed against closed sets below to prevent cardinality
# explosion on operator-supplied values.
# ---------------------------------------------------------------------------

cluster_nodes_total: Gauge = Gauge(
    "bernstein_cluster_nodes_total",
    "Number of cluster nodes currently in each lifecycle status.",
    labelnames=["status"],
    registry=registry,
)

cluster_heartbeats_total: Counter = Counter(
    "bernstein_cluster_heartbeats_total",
    "Cluster heartbeat outcomes (accepted / rejected_token / rejected_unknown_node).",
    labelnames=["result"],
    registry=registry,
)

cluster_task_steals_total: Counter = Counter(
    "bernstein_cluster_task_steals_total",
    "Task-stealing attempt outcomes (stolen / cooldown / no_victim / rejected_version_mismatch).",
    labelnames=["result"],
    registry=registry,
)

cluster_scaling_decisions_total: Counter = Counter(
    "bernstein_cluster_scaling_decisions_total",
    "Autoscaler decisions by action and backend.",
    labelnames=["action", "backend"],
    registry=registry,
)

cluster_admission_failures_total: Counter = Counter(
    "bernstein_cluster_admission_failures_total",
    "Cluster admission failures (invalid_token / scope_denied / cert_invalid).",
    labelnames=["reason"],
    registry=registry,
)


# ---------------------------------------------------------------------------
# Cluster cardinality guards - keep the label sets bounded so an attacker
# (or a buggy worker) cannot blow up Prometheus storage by sending novel
# strings on every call.
# ---------------------------------------------------------------------------

_KNOWN_NODE_STATUSES: frozenset[str] = frozenset(
    {"online", "ready", "degraded", "cordoned", "draining", "offline"},
)

_KNOWN_HEARTBEAT_RESULTS: frozenset[str] = frozenset(
    {"accepted", "rejected_token", "rejected_unknown_node"},
)

_KNOWN_STEAL_RESULTS: frozenset[str] = frozenset(
    {"stolen", "cooldown", "no_victim", "rejected_version_mismatch"},
)

_KNOWN_SCALE_ACTIONS: frozenset[str] = frozenset({"scale_up", "scale_down", "no_op"})

_KNOWN_SCALE_BACKENDS: frozenset[str] = frozenset({"noop", "kubernetes"})

_KNOWN_ADMISSION_REASONS: frozenset[str] = frozenset(
    {"invalid_token", "scope_denied", "cert_invalid"},
)


def _bucket(value: str, allowed: frozenset[str], fallback: str = "unknown") -> str:
    """Bucket *value* under *fallback* if it isn't in the closed *allowed* set."""
    normalised = (value).strip().lower()
    return normalised if normalised in allowed else fallback


def set_node_count(status: str, count: int) -> None:
    """Set the cluster_nodes_total gauge for a given status bucket.

    Unknown statuses are bucketed under ``"unknown"`` to prevent label-
    cardinality explosion.

    Args:
        status: One of online / ready / degraded / cordoned / draining / offline.
        count: Number of nodes currently in that status.
    """
    if not _prometheus_enabled:
        return
    bucket = _bucket(status, _KNOWN_NODE_STATUSES)
    try:
        cluster_nodes_total.labels(status=bucket).set(float(count))
    except Exception:
        logger.debug("Failed to set cluster_nodes_total gauge", exc_info=True)


def record_heartbeat(result: str) -> None:
    """Increment the cluster heartbeat outcome counter.

    Args:
        result: One of accepted / rejected_token / rejected_unknown_node.
    """
    if not _prometheus_enabled:
        return
    bucket = _bucket(result, _KNOWN_HEARTBEAT_RESULTS)
    try:
        cluster_heartbeats_total.labels(result=bucket).inc()
    except Exception:
        logger.debug("Failed to record cluster heartbeat metric", exc_info=True)


def record_steal_attempt(result: str) -> None:
    """Increment the cluster task-stealing outcome counter.

    Args:
        result: One of stolen / cooldown / no_victim / rejected_version_mismatch.
    """
    if not _prometheus_enabled:
        return
    bucket = _bucket(result, _KNOWN_STEAL_RESULTS)
    try:
        cluster_task_steals_total.labels(result=bucket).inc()
    except Exception:
        logger.debug("Failed to record cluster task-steal metric", exc_info=True)


def record_scaling_decision(action: str, backend: str) -> None:
    """Increment the autoscaler decision counter.

    Args:
        action: One of scale_up / scale_down / no_op.
        backend: One of noop / kubernetes (or any other registered backend).
    """
    if not _prometheus_enabled:
        return
    action_bucket = _bucket(action, _KNOWN_SCALE_ACTIONS)
    backend_bucket = _bucket(backend, _KNOWN_SCALE_BACKENDS)
    try:
        cluster_scaling_decisions_total.labels(action=action_bucket, backend=backend_bucket).inc()
    except Exception:
        logger.debug("Failed to record cluster scaling decision metric", exc_info=True)


def record_admission_failure(reason: str) -> None:
    """Increment the cluster admission-failure counter.

    Args:
        reason: One of invalid_token / scope_denied / cert_invalid.
    """
    if not _prometheus_enabled:
        return
    bucket = _bucket(reason, _KNOWN_ADMISSION_REASONS)
    try:
        cluster_admission_failures_total.labels(reason=bucket).inc()
    except Exception:
        logger.debug("Failed to record cluster admission failure metric", exc_info=True)


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

    Safe to call from hot paths - respects the kill-switch and silently
    drops bad input rather than raising.

    Args:
        reason: The ``TransitionReason`` value (or raw string).
        role: Agent/task role label (e.g. ``"backend"``, ``"qa"``).
        entity_type: ``"agent"`` or ``"task"`` - selects which counter family.
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
# Kill-switch - lets operators disable the Prometheus sink without restarting
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
        model_name = model.strip() or "unknown"
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
