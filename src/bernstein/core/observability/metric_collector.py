"""Metrics collection and recording.

Collects time-series metrics for agent success rates, task completion times,
API usage patterns, error rates, and cost efficiency.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from bernstein.core.tenanting import normalize_tenant_id, tenant_metrics_dir

logger = logging.getLogger(__name__)


class MetricType(Enum):
    """Types of metrics collected."""

    AGENT_SUCCESS = "agent_success"
    TASK_COMPLETION_TIME = "task_completion_time"
    API_USAGE = "api_usage"
    ERROR_RATE = "error_rate"
    COST_EFFICIENCY = "cost_efficiency"
    PROVIDER_HEALTH = "provider_health"
    FREE_TIER_USAGE = "free_tier_usage"
    FAST_PATH = "fast_path"
    PARALLELISM_LEVEL = "parallelism_level"
    QUEUE_DEPTH = "queue_depth"
    MERGE_RESULT = "merge_result"
    COMPACTION = "compaction"


class ProviderStatus(Enum):
    """Health status for API providers."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    RATE_LIMITED = "rate_limited"


class PrivacyLevel(Enum):
    """Analytics privacy presets controlling what metric data is collected and exported.

    Attributes:
        FULL: All data collected — task IDs, agent IDs, costs, tokens, error detail.
        STANDARD: Aggregate-safe — strip individual task/agent/session identifiers
            from labels, but retain cost and performance signals.
        MINIMAL: Counts only — suppress identifiers, cost, and token data; only
            aggregate success/failure counts are retained.
    """

    FULL = "full"
    STANDARD = "standard"
    MINIMAL = "minimal"


class EventSink(Enum):
    """Named event sinks that can be individually disabled via kill-switch config.

    Attributes:
        FILE: JSONL metric files written to ``.sdd/metrics/``.
        PLUGIN: Plugin hook ``on_metric_record`` called on every metric point.
    """

    FILE = "file"
    PLUGIN = "plugin"


_STANDARD_STRIP: frozenset[str] = frozenset({"task_id", "agent_id", "session_id"})
_MINIMAL_STRIP: frozenset[str] = frozenset({"task_id", "agent_id", "session_id", "cost_usd", "tokens_used"})


@dataclass
class MetricPoint:
    """A single metric data point."""

    timestamp: float
    value: float
    labels: dict[str, str] = field(default_factory=dict[str, str])  # e.g., {role: "backend", model: "sonnet"}


@dataclass
class TaskMetrics:
    """Metrics for a single task execution."""

    task_id: str
    role: str
    model: str
    provider: str
    start_time: float
    tenant_id: str = "default"
    end_time: float | None = None
    success: bool = False
    error: str | None = None
    tokens_used: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cost_usd: float = 0.0
    retry_count: int = 0
    janitor_passed: bool = False
    files_modified: int = 0
    lines_added: int = 0
    lines_deleted: int = 0


@dataclass
class AgentMetrics:
    """Metrics for a single agent session."""

    agent_id: str
    role: str
    model: str
    provider: str
    start_time: float
    tenant_id: str = "default"
    end_time: float | None = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    agent_source: str = "built-in"  # "catalog", "agency", or "built-in"


@dataclass
class ProviderHealth:
    """Health tracking for an API provider."""

    provider: str
    status: ProviderStatus = ProviderStatus.HEALTHY
    last_check: float = 0.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    avg_latency_ms: float = 0.0
    rate_limit_remaining: int | None = None
    rate_limit_reset: float | None = None


@dataclass
class UsageQuota:
    """Free tier usage quota tracking."""

    provider: str
    model: str
    limit_type: str  # "requests_per_day", "tokens_per_month", "requests_per_minute"
    limit: int
    used: int = 0
    reset_time: float | None = None
    percentage_used: float = 0.0


class PercentileTracker:
    """Track and compute percentiles (p50/p95/p99) over a sliding window.

    Uses a sorted deque for efficient insertion and percentile computation.

    Args:
        window: Maximum number of values to retain (default 1000).
    """

    def __init__(self, window: int = 1000) -> None:
        self._window = window
        self._values: list[float] = []

    def add(self, value: float) -> None:
        """Add a value to the tracker.

        Args:
            value: Numeric value to track.
        """
        self._values.append(value)
        # Maintain window size
        if len(self._values) > self._window:
            self._values.pop(0)

    def p50(self) -> float:
        """Compute 50th percentile (median).

        Returns:
            50th percentile value, or 0.0 if no data.
        """
        return self._percentile(0.50)

    def p95(self) -> float:
        """Compute 95th percentile.

        Returns:
            95th percentile value, or 0.0 if no data.
        """
        return self._percentile(0.95)

    def p99(self) -> float:
        """Compute 99th percentile.

        Returns:
            99th percentile value, or 0.0 if no data.
        """
        return self._percentile(0.99)

    def _percentile(self, p: float) -> float:
        """Compute arbitrary percentile.

        Args:
            p: Percentile as fraction (0.0-1.0).

        Returns:
            Percentile value, or 0.0 if no data.
        """
        if not self._values:
            return 0.0
        sorted_vals = sorted(self._values)
        idx = int(p * (len(sorted_vals) - 1))
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def count(self) -> int:
        """Get number of values in window.

        Returns:
            Number of tracked values.
        """
        return len(self._values)

    def clear(self) -> None:
        """Clear all tracked values."""
        self._values.clear()


class MetricsCollector:
    """Collects and stores performance metrics.

    Stores time-series data in .sdd/metrics/ directory as JSONL files,
    one file per metric type per day for efficient querying.

    Args:
        metrics_dir: Directory to store metrics files.
    """

    def __init__(
        self,
        metrics_dir: Path | None = None,
        *,
        privacy_level: PrivacyLevel = PrivacyLevel.FULL,
        disabled_sinks: frozenset[EventSink] | None = None,
    ) -> None:
        self._metrics_dir = metrics_dir or Path.cwd() / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)
        self._privacy_level = privacy_level
        self._disabled_sinks: frozenset[EventSink] = disabled_sinks or frozenset()

        # In-memory tracking
        self._task_metrics: dict[str, TaskMetrics] = {}
        self._agent_metrics: dict[str, AgentMetrics] = {}
        self._provider_health: dict[str, ProviderHealth] = {}
        self._usage_quotas: dict[str, UsageQuota] = {}

        # Write buffer for batched file I/O
        self._buffer: list[tuple[Path, str]] = []
        self._buffer_limit: int = 50
        self._flush_interval: float = 5.0  # seconds between time-based flushes
        self._last_flush: float = time.time()
        self._lock: threading.Lock = threading.Lock()

    def reset_task_metrics(self) -> None:
        """Clear all task metrics. Called on orchestrator restart so stale
        failure data from prior runs doesn't poison the error budget."""
        with self._lock:
            self._task_metrics.clear()

    @property
    def privacy_level(self) -> PrivacyLevel:
        """Return the active analytics privacy preset."""
        return self._privacy_level

    @property
    def disabled_sinks(self) -> frozenset[EventSink]:
        """Return the set of event sinks currently disabled via kill-switch."""
        return self._disabled_sinks

    def is_sink_enabled(self, sink: EventSink) -> bool:
        """Return whether a specific event sink is currently enabled.

        Args:
            sink: The sink to check.

        Returns:
            True if the sink is active, False if it has been killed.
        """
        return sink not in self._disabled_sinks

    def apply_privacy_filter(self, labels: dict[str, str]) -> dict[str, str]:
        """Strip label keys that are disallowed under the active privacy preset.

        Args:
            labels: Raw label dict to filter.

        Returns:
            Filtered copy; the original is never mutated.
        """
        if self._privacy_level is PrivacyLevel.FULL:
            return labels
        if self._privacy_level is PrivacyLevel.STANDARD:
            return {k: v for k, v in labels.items() if k not in _STANDARD_STRIP}
        # MINIMAL
        return {k: v for k, v in labels.items() if k not in _MINIMAL_STRIP}

    @property
    def task_metrics(self) -> dict[str, TaskMetrics]:
        """Access per-task metrics."""
        return self._task_metrics

    @property
    def agent_metrics(self) -> dict[str, AgentMetrics]:
        """Access per-agent metrics."""
        return self._agent_metrics

    # -- Task Metrics --------------------------------------------------------

    def start_task(
        self,
        task_id: str,
        role: str,
        model: str,
        provider: str,
        *,
        tenant_id: str = "default",
    ) -> TaskMetrics:
        """Record the start of a task execution.

        Args:
            task_id: Unique task identifier.
            role: Agent role executing the task.
            model: Model being used.
            provider: API provider.

        Returns:
            TaskMetrics object for this task.
        """
        metrics = TaskMetrics(
            task_id=task_id,
            role=role,
            model=model,
            provider=provider,
            tenant_id=normalize_tenant_id(tenant_id),
            start_time=time.time(),
        )
        self._task_metrics[task_id] = metrics
        return metrics

    def complete_task(
        self,
        task_id: str,
        success: bool,
        tokens_used: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        janitor_passed: bool = False,
        files_modified: int = 0,
        lines_added: int = 0,
        lines_deleted: int = 0,
    ) -> TaskMetrics | None:
        """Record task completion.

        Args:
            task_id: Task identifier.
            success: Whether the task succeeded.
            tokens_used: Number of tokens consumed.
            cost_usd: Cost in USD.
            error: Error message if failed.
            janitor_passed: Whether janitor verification passed.
            files_modified: Number of files modified.
            lines_added: Lines of code added.
            lines_deleted: Lines of code deleted.

        Returns:
            TaskMetrics if found, None otherwise.
        """
        metrics = self._task_metrics.get(task_id)
        if not metrics:
            logger.warning("Task %s not found in metrics", task_id)
            return None

        metrics.end_time = time.time()
        metrics.success = success
        metrics.tokens_used = tokens_used
        metrics.cost_usd = cost_usd
        metrics.error = error
        metrics.janitor_passed = janitor_passed
        metrics.files_modified = files_modified
        metrics.lines_added = lines_added
        metrics.lines_deleted = lines_deleted

        # Write completion time metric point
        duration = metrics.end_time - metrics.start_time
        labels = {
            "task_id": task_id,
            "role": metrics.role,
            "model": metrics.model,
            "success": str(success),
            "tenant_id": metrics.tenant_id,
        }
        self._write_metric_point(
            MetricType.TASK_COMPLETION_TIME,
            duration,
            labels,
        )

        # Write cost efficiency metric if cost > 0 and privacy allows it
        if cost_usd > 0 and self._privacy_level is not PrivacyLevel.MINIMAL:
            self._write_metric_point(
                MetricType.COST_EFFICIENCY,
                cost_usd,
                {
                    "task_id": task_id,
                    "role": metrics.role,
                    "model": metrics.model,
                    "tenant_id": metrics.tenant_id,
                },
            )

        # Write token usage — enables avg-tokens-per-task queries in /quality
        if tokens_used > 0 and self._privacy_level is not PrivacyLevel.MINIMAL:
            self._write_metric_point(
                MetricType.API_USAGE,
                float(tokens_used),
                labels,
            )

        # Update provider health
        self._update_provider_health(metrics.provider, success)

        # Update usage quota
        self._update_usage_quota(metrics.provider, metrics.model, tokens_used)

        # Flush buffered points — task completion is a natural checkpoint
        self._flush_buffer()

        return metrics

    def increment_task_retry(self, task_id: str) -> None:
        """Increment retry count for a task.

        Args:
            task_id: Task identifier.
        """
        metrics = self._task_metrics.get(task_id)
        if metrics:
            metrics.retry_count += 1

    # -- Agent Metrics -------------------------------------------------------

    def start_agent(
        self,
        agent_id: str,
        role: str,
        model: str,
        provider: str,
        agent_source: str = "built-in",
        *,
        tenant_id: str = "default",
    ) -> AgentMetrics:
        """Record the start of an agent session.

        Args:
            agent_id: Unique agent identifier.
            role: Agent role.
            model: Model being used.
            provider: API provider.
            agent_source: Where the agent prompt came from — ``"catalog"``,
                ``"agency"``, or ``"built-in"`` (default).

        Returns:
            AgentMetrics object for this agent.
        """
        metrics = AgentMetrics(
            agent_id=agent_id,
            role=role,
            model=model,
            provider=provider,
            tenant_id=normalize_tenant_id(tenant_id),
            start_time=time.time(),
            agent_source=agent_source,
        )
        self._agent_metrics[agent_id] = metrics
        return metrics

    def complete_agent_task(
        self,
        agent_id: str,
        success: bool,
        tokens_used: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record completion of a task by an agent.

        Args:
            agent_id: Agent identifier.
            success: Whether the task succeeded.
            tokens_used: Tokens consumed for this task.
            cost_usd: Cost for this task.
        """
        metrics = self._agent_metrics.get(agent_id)
        if not metrics:
            logger.warning("Agent %s not found in metrics", agent_id)
            return

        if success:
            metrics.tasks_completed += 1
        else:
            metrics.tasks_failed += 1

        metrics.total_tokens += tokens_used
        metrics.total_cost_usd += cost_usd

    def end_agent(self, agent_id: str) -> AgentMetrics | None:
        """Record the end of an agent session.

        Args:
            agent_id: Agent identifier.

        Returns:
            AgentMetrics if found, None otherwise.
        """
        metrics = self._agent_metrics.get(agent_id)
        if not metrics:
            return None

        metrics.end_time = time.time()

        # Write agent success rate metric if agent completed any tasks
        total_tasks = metrics.tasks_completed + metrics.tasks_failed
        if total_tasks > 0:
            success_rate = metrics.tasks_completed / total_tasks
            self._write_metric_point(
                MetricType.AGENT_SUCCESS,
                success_rate,
                {
                    "agent_id": agent_id,
                    "role": metrics.role,
                    "model": metrics.model,
                    "tenant_id": metrics.tenant_id,
                },
            )

        self._flush_buffer()
        return metrics

    # -- Provider Health -----------------------------------------------------

    def get_provider_health(self, provider: str) -> ProviderHealth:
        """Get or create health status for a provider.

        Args:
            provider: Provider name.

        Returns:
            ProviderHealth object.
        """
        if provider not in self._provider_health:
            self._provider_health[provider] = ProviderHealth(provider=provider)
        return self._provider_health[provider]

    def _update_provider_health(self, provider: str, success: bool) -> None:
        """Update provider health based on request outcome.

        Args:
            provider: Provider name.
            success: Whether the request succeeded.
        """
        health = self.get_provider_health(provider)
        health.last_check = time.time()

        if success:
            health.consecutive_successes += 1
            health.consecutive_failures = 0
            if health.consecutive_successes >= 3:
                health.status = ProviderStatus.HEALTHY
        else:
            health.consecutive_failures += 1
            health.consecutive_successes = 0
            if health.consecutive_failures >= 3:
                health.status = ProviderStatus.UNHEALTHY
            elif health.consecutive_failures >= 1:
                health.status = ProviderStatus.DEGRADED

    def mark_provider_rate_limited(
        self,
        provider: str,
        remaining: int | None = None,
        reset_time: float | None = None,
    ) -> None:
        """Mark a provider as rate limited.

        Args:
            provider: Provider name.
            remaining: Remaining requests in quota.
            reset_time: Unix timestamp when quota resets.
        """
        health = self.get_provider_health(provider)
        health.status = ProviderStatus.RATE_LIMITED
        health.rate_limit_remaining = remaining
        health.rate_limit_reset = reset_time

    def mark_provider_healthy(self, provider: str) -> None:
        """Mark a provider as healthy.

        Args:
            provider: Provider name.
        """
        health = self.get_provider_health(provider)
        health.status = ProviderStatus.HEALTHY
        health.consecutive_failures = 0

    # -- Usage Quotas --------------------------------------------------------

    def set_usage_quota(
        self,
        provider: str,
        model: str,
        limit_type: str,
        limit: int,
        used: int = 0,
        reset_time: float | None = None,
    ) -> None:
        """Set or update a usage quota.

        Args:
            provider: API provider.
            model: Model name.
            limit_type: Type of limit.
            limit: Maximum allowed.
            used: Current usage.
            reset_time: When the quota resets.
        """
        key = f"{provider}:{model}:{limit_type}"
        percentage = (used / limit * 100) if limit > 0 else 0.0
        self._usage_quotas[key] = UsageQuota(
            provider=provider,
            model=model,
            limit_type=limit_type,
            limit=limit,
            used=used,
            reset_time=reset_time,
            percentage_used=percentage,
        )

    def _update_usage_quota(self, provider: str, model: str, tokens_used: int) -> None:
        """Update usage quota after a request.

        Args:
            provider: API provider.
            model: Model name.
            tokens_used: Tokens consumed.
        """
        # Update tokens_per_month quota
        key = f"{provider}:{model}:tokens_per_month"
        if key in self._usage_quotas:
            quota = self._usage_quotas[key]
            quota.used += tokens_used
            quota.percentage_used = (quota.used / quota.limit * 100) if quota.limit > 0 else 0.0

    def get_quota_status(self, provider: str, model: str) -> dict[str, UsageQuota]:
        """Get all quota statuses for a provider/model.

        Args:
            provider: API provider.
            model: Model name.

        Returns:
            Dict of quota type to UsageQuota.
        """
        result: dict[str, UsageQuota] = {}
        prefix = f"{provider}:{model}:"
        for key, quota in self._usage_quotas.items():
            if key.startswith(prefix):
                result[quota.limit_type] = quota
        return result

    def is_quota_available(self, provider: str, model: str) -> bool:
        """Check if any quota is exhausted for a provider/model.

        Args:
            provider: API provider.
            model: Model name.

        Returns:
            True if quotas are available, False if any is exhausted.
        """
        quotas = self.get_quota_status(provider, model)
        for quota in quotas.values():
            if quota.percentage_used >= 100:
                # Check if reset time has passed
                if quota.reset_time and time.time() >= quota.reset_time:
                    continue
                return False
        return True

    # -- Quality Metrics -----------------------------------------------------

    def record_janitor_result(
        self,
        task_id: str,
        passed: bool,
        role: str,
        model: str,
        provider: str,
        *,
        tenant_id: str = "default",
    ) -> None:
        """Record janitor verification result.

        Args:
            task_id: Task identifier.
            passed: Whether verification passed.
            role: Agent role.
            model: Model used.
            provider: API provider.
        """
        # Update task metrics if exists
        if task_id in self._task_metrics:
            self._task_metrics[task_id].janitor_passed = passed

        # Write janitor verification metric
        normalized_tenant = normalize_tenant_id(tenant_id)
        self._write_metric_point(
            MetricType.AGENT_SUCCESS,
            1.0 if passed else 0.0,
            {
                "task_id": task_id,
                "role": role,
                "model": model,
                "verification": "janitor",
                "tenant_id": normalized_tenant,
            },
        )

        self._flush_buffer()

    def record_free_tier_usage(
        self,
        provider: str,
        model: str,
        requests_used: int,
        requests_limit: int,
        tokens_used: int = 0,
        tokens_limit: int = 0,
    ) -> None:
        """Record free tier usage for cost tracking.

        Args:
            provider: API provider.
            model: Model name.
            requests_used: Requests consumed.
            requests_limit: Total request limit.
            tokens_used: Tokens consumed.
            tokens_limit: Total token limit.
        """
        if requests_limit > 0:
            pct = requests_used / requests_limit * 100
            self._write_metric_point(
                MetricType.FREE_TIER_USAGE,
                pct,
                {
                    "provider": provider,
                    "model": model,
                    "metric": "requests",
                    "used": str(requests_used),
                    "limit": str(requests_limit),
                },
            )
        if tokens_limit > 0:
            token_pct = tokens_used / tokens_limit * 100
            self._write_metric_point(
                MetricType.FREE_TIER_USAGE,
                token_pct,
                {
                    "provider": provider,
                    "model": model,
                    "metric": "tokens",
                    "used": str(tokens_used),
                    "limit": str(tokens_limit),
                },
            )
        self._flush_buffer()

    def record_merge_result(
        self,
        task_id: str,
        success: bool,
        *,
        tenant_id: str = "default",
    ) -> None:
        """Record the outcome of a task merge.

        Args:
            task_id: Task identifier.
            success: Whether the merge succeeded.
        """
        normalized_tenant = normalize_tenant_id(tenant_id)
        self._write_metric_point(
            MetricType.MERGE_RESULT,
            1.0 if success else 0.0,
            {
                "task_id": task_id,
                "success": str(success),
                "tenant_id": normalized_tenant,
            },
        )
        self._flush_buffer()

    # -- Error Tracking ------------------------------------------------------

    def record_error(
        self,
        error_type: str,
        provider: str,
        model: str | None = None,
        role: str | None = None,
        *,
        tenant_id: str = "default",
    ) -> None:
        """Record an error for metrics tracking.

        Args:
            error_type: Type/category of error.
            provider: API provider.
            model: Model name.
            role: Agent role.
        """
        # Write error metric
        normalized_tenant = normalize_tenant_id(tenant_id)
        self._write_metric_point(
            MetricType.ERROR_RATE,
            1.0,
            {
                "error_type": error_type,
                "provider": provider,
                "model": model or "",
                "role": role or "",
                "tenant_id": normalized_tenant,
            },
        )
        # Update provider health
        self._update_provider_health(provider, success=False)
        self._flush_buffer()

    # -- Persistence ---------------------------------------------------------

    def _write_metric_point(
        self,
        metric_type: MetricType,
        value: float,
        labels: dict[str, str],
    ) -> None:
        """Write a metric point to the appropriate file.

        Args:
            metric_type: Type of metric.
            value: Numeric value.
            labels: Additional labels for filtering.
        """
        import json
        from datetime import datetime

        today = datetime.now().strftime("%Y-%m-%d")
        filename = f"{metric_type.value}_{today}.jsonl"
        filepath = self._metrics_dir / filename

        point = {
            "timestamp": time.time(),
            "metric_type": metric_type.value,
            "value": value,
            "labels": self.apply_privacy_filter(labels),
        }

        file_disabled = EventSink.FILE in self._disabled_sinks
        if not file_disabled:
            with self._lock:
                self._buffer.append((filepath, json.dumps(point)))
                tenant_id = normalize_tenant_id(labels.get("tenant_id"))
                if tenant_id != "default":
                    tenant_dir = tenant_metrics_dir(self._metrics_dir, tenant_id)
                    tenant_dir.mkdir(parents=True, exist_ok=True)
                    self._buffer.append((tenant_dir / filename, json.dumps(point)))
                should_flush = (
                    len(self._buffer) >= self._buffer_limit or (time.time() - self._last_flush) >= self._flush_interval
                )
            if should_flush:
                self._flush_buffer()
        # Fire plugin hook so plugins can observe/forward metrics in real time
        if EventSink.PLUGIN not in self._disabled_sinks:
            self._emit_metric_hook(metric_type.value, value, labels)

    def _flush_buffer(self) -> None:
        """Batch-write all buffered metric points, grouped by file path."""
        with self._lock:
            if not self._buffer:
                return
            # Drain the buffer under the lock, then write without holding it
            batch = self._buffer
            self._buffer = []
            self._last_flush = time.time()

        # Group lines by file path
        by_file: dict[Path, list[str]] = {}
        for filepath, line in batch:
            by_file.setdefault(filepath, []).append(line)

        for filepath, lines in by_file.items():
            try:
                with filepath.open("a") as f:
                    f.write("\n".join(lines) + "\n")
            except OSError:
                logger.exception("Failed to flush metrics to %s", filepath)

    def flush(self) -> None:
        """Flush the write buffer to disk. Call this each orchestrator tick."""
        self._flush_buffer()

    # ---------------------------------------------------------------------------
    # Plugin integration
    # ---------------------------------------------------------------------------

    @staticmethod
    def _emit_metric_hook(metric_type: str, value: float, labels: dict[str, str]) -> None:
        """Fire the ``on_metric_record`` plugin hook.

        Args:
            metric_type: Metric type name.
            value: Numeric metric value.
            labels: Key-value labels attached to the point.
        """
        try:
            from bernstein.plugins.manager import get_plugin_manager

            pm = get_plugin_manager()
            pm.hook.on_metric_record(metric_type=metric_type, value=value, labels=labels)  # type: ignore[union-attr]
        except Exception:
            logger.debug("Plugin hook on_metric_record failed (swallowed)", exc_info=True)

    def __del__(self) -> None:
        """Flush remaining buffered metrics on garbage collection."""
        with contextlib.suppress(Exception):
            self._flush_buffer()

    def record_api_call(
        self,
        provider: str,
        model: str,
        latency_ms: float,
        tokens: int,
        cost_usd: float,
        success: bool,
        *,
        tenant_id: str = "default",
    ) -> None:
        """Record an API call for usage tracking.

        Args:
            provider: API provider.
            model: Model used.
            latency_ms: Request latency.
            tokens: Tokens consumed.
            cost_usd: Cost in USD.
            success: Whether the call succeeded.
        """
        # Write API usage metric
        normalized_tenant = normalize_tenant_id(tenant_id)
        self._write_metric_point(
            MetricType.API_USAGE,
            float(tokens),
            {
                "provider": provider,
                "model": model,
                "success": str(success),
                "latency_ms": str(round(latency_ms, 1)),
                "tenant_id": normalized_tenant,
            },
        )

        # Update provider health with latency
        health = self.get_provider_health(provider)
        # Exponential moving average for latency
        alpha = 0.3
        health.avg_latency_ms = alpha * latency_ms + (1 - alpha) * health.avg_latency_ms

        if success:
            self._update_provider_health(provider, True)

        self._flush_buffer()

    def record_queue_depth(
        self,
        queue_depth_open: int,
        queue_depth_claimed: int,
        queue_depth_failed: int,
    ) -> None:
        """Record task queue depth snapshot.

        Called every orchestrator tick to track queue depth over time.

        Args:
            queue_depth_open: Number of open tasks.
            queue_depth_claimed: Number of claimed tasks.
            queue_depth_failed: Number of failed tasks.
        """
        self._write_metric_point(
            MetricType.QUEUE_DEPTH,
            float(queue_depth_open + queue_depth_claimed + queue_depth_failed),
            {
                "open": str(queue_depth_open),
                "claimed": str(queue_depth_claimed),
                "failed": str(queue_depth_failed),
            },
        )

    def record_compaction(
        self,
        session_id: str,
        tokens_before: int,
        tokens_after: int,
        reason: str = "token_budget",
    ) -> None:
        """Record a context compaction event.

        Increments a compaction counter and stores the tokens-saved delta
        for post-run analysis.

        Args:
            session_id: Agent session ID.
            tokens_before: Token count before compaction.
            tokens_after: Token count after compaction.
            reason: Why compaction was triggered.
        """
        saved = max(0, tokens_before - tokens_after)
        self._write_metric_point(
            MetricType.COMPACTION,
            float(saved),
            {
                "session_id": session_id,
                "tokens_before": str(tokens_before),
                "tokens_after": str(tokens_after),
                "reason": reason,
            },
        )

    # -- Query Methods -------------------------------------------------------

    def get_agent_success_rate(self, agent_id: str | None = None, role: str | None = None) -> float:
        """Calculate success rate for agents.

        Args:
            agent_id: Optional specific agent.
            role: Optional role filter.

        Returns:
            Success rate as a float 0-1.
        """
        agents = self._agent_metrics.values()
        if agent_id:
            agents = [a for a in agents if a.agent_id == agent_id]
        if role:
            agents = [a for a in agents if a.role == role]

        total_completed = sum(a.tasks_completed for a in agents)
        total_failed = sum(a.tasks_failed for a in agents)
        total = total_completed + total_failed

        if total == 0:
            return 1.0
        return total_completed / total

    def get_avg_completion_time(self, role: str | None = None) -> float:
        """Calculate average task completion time.

        Args:
            role: Optional role filter.

        Returns:
            Average time in seconds.
        """
        tasks = [t for t in self._task_metrics.values() if t.end_time is not None and (role is None or t.role == role)]

        if not tasks:
            return 0.0

        total_time: float = sum((t.end_time - t.start_time) for t in tasks if t.end_time is not None)
        return total_time / len(tasks)

    def get_total_cost(self, agent_id: str | None = None) -> float:
        """Get total cost across agents.

        Args:
            agent_id: Optional specific agent.

        Returns:
            Total cost in USD.
        """
        agents = self._agent_metrics.values()
        if agent_id:
            agents = [a for a in agents if a.agent_id == agent_id]
        return sum(a.total_cost_usd for a in agents)

    def get_metrics_summary(self) -> dict[str, Any]:
        """Get a summary of all collected metrics.

        Returns:
            Dict with aggregated metrics including success rates, costs,
            provider health, and quota status.
        """
        total_tasks = len(self._task_metrics)
        successful_tasks = sum(1 for t in self._task_metrics.values() if t.success)
        janitor_passed = sum(1 for t in self._task_metrics.values() if t.janitor_passed)
        total_agents = len(self._agent_metrics)

        # Calculate provider-specific stats
        provider_stats = {}
        for provider, health in self._provider_health.items():
            provider_tasks = [
                t for t in self._task_metrics.values() if t.provider == provider and t.end_time is not None
            ]
            provider_cost = sum(t.cost_usd for t in provider_tasks)
            provider_tokens = sum(t.tokens_used for t in provider_tasks)
            success_count = sum(1 for t in provider_tasks if t.success)
            provider_stats[provider] = {
                "status": health.status.value,
                "tasks": len(provider_tasks),
                "success_rate": success_count / len(provider_tasks) if provider_tasks else 1.0,
                "total_cost_usd": provider_cost,
                "total_tokens": provider_tokens,
                "avg_latency_ms": health.avg_latency_ms,
            }

        return {
            "total_tasks": total_tasks,
            "successful_tasks": successful_tasks,
            "failed_tasks": total_tasks - successful_tasks,
            "success_rate": successful_tasks / total_tasks if total_tasks > 0 else 1.0,
            "janitor_pass_rate": janitor_passed / total_tasks if total_tasks > 0 else 1.0,
            "total_agents": total_agents,
            "total_cost_usd": 0.0 if self._privacy_level is PrivacyLevel.MINIMAL else self.get_total_cost(),
            "avg_completion_time_seconds": self.get_avg_completion_time(),
            "provider_stats": provider_stats,
            "provider_health": {p: h.status.value for p, h in self._provider_health.items()},
            "quota_status": {
                k: {"used": q.used, "limit": q.limit, "percentage": q.percentage_used}
                for k, q in self._usage_quotas.items()
            },
        }

    def get_quality_metrics(self) -> dict[str, Any]:
        """Compute quality metrics from in-memory task data.

        Groups completed tasks by model to compute per-model success rates,
        average token counts, and completion-time percentiles (p50/p90/p99).
        Also reports overall guardrail pass rate (via ``janitor_passed``) and
        review rejection rate (fraction of failed tasks).

        Returns:
            Dict with keys ``per_model``, ``overall``, ``guardrail_pass_rate``,
            ``review_rejection_rate``, and ``gate_stats`` (empty dict — gate
            data is read from JSONL by the API layer).
        """
        import statistics

        completed: list[TaskMetrics] = [t for t in self._task_metrics.values() if t.end_time is not None]

        empty: dict[str, Any] = {
            "per_model": {},
            "overall": {
                "total_tasks": 0,
                "success_rate": 1.0,
                "janitor_pass_rate": 1.0,
                "avg_tokens": 0.0,
                "p50_completion_seconds": 0.0,
                "p90_completion_seconds": 0.0,
                "p99_completion_seconds": 0.0,
            },
            "guardrail_pass_rate": 1.0,
            "review_rejection_rate": 0.0,
            "gate_stats": {},
        }
        if not completed:
            return empty

        def _pct(vals: list[float], p: float) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            idx = int(p * (len(s) - 1))
            return s[min(idx, len(s) - 1)]

        # Group by model
        by_model: dict[str, list[TaskMetrics]] = {}
        for t in completed:
            by_model.setdefault(t.model, []).append(t)

        per_model: dict[str, Any] = {}
        for model, tasks in by_model.items():
            durations: list[float] = [
                t.end_time - t.start_time  # type: ignore[operator]
                for t in tasks
                if t.end_time is not None
            ]
            tokens = [t.tokens_used for t in tasks]
            success_count = sum(1 for t in tasks if t.success)

            per_model[model] = {
                "total_tasks": len(tasks),
                "success_rate": success_count / len(tasks),
                "avg_tokens": statistics.mean(tokens) if tokens else 0.0,
                "avg_completion_seconds": statistics.mean(durations) if durations else 0.0,
                "p50_completion_seconds": _pct(durations, 0.50),
                "p90_completion_seconds": _pct(durations, 0.90),
                "p99_completion_seconds": _pct(durations, 0.99),
            }

        all_durations: list[float] = [
            t.end_time - t.start_time  # type: ignore[operator]
            for t in completed
            if t.end_time is not None
        ]
        all_tokens = [t.tokens_used for t in completed]
        total = len(completed)
        successes_total = sum(1 for t in completed if t.success)
        janitor_total = sum(1 for t in completed if t.janitor_passed)

        return {
            "per_model": per_model,
            "overall": {
                "total_tasks": total,
                "success_rate": successes_total / total,
                "janitor_pass_rate": janitor_total / total,
                "avg_tokens": statistics.mean(all_tokens) if all_tokens else 0.0,
                "p50_completion_seconds": _pct(all_durations, 0.50),
                "p90_completion_seconds": _pct(all_durations, 0.90),
                "p99_completion_seconds": _pct(all_durations, 0.99),
            },
            "guardrail_pass_rate": janitor_total / total,
            "review_rejection_rate": (total - successes_total) / total,
            "gate_stats": {},
        }


# Global instance for easy access
_default_collector: MetricsCollector | None = None


def get_collector(metrics_dir: Path | None = None) -> MetricsCollector:
    """Get or create the default metrics collector.

    Args:
        metrics_dir: Optional custom metrics directory.

    Returns:
        MetricsCollector instance.
    """
    global _default_collector
    if _default_collector is None:
        _default_collector = MetricsCollector(metrics_dir)
    return _default_collector


# ---------------------------------------------------------------------------
# Expected drop notifications for cache baselines (T564)
# ---------------------------------------------------------------------------


@dataclass
class CacheBaselineDrop:
    """Record of a cache baseline drop event."""

    baseline_name: str
    previous_value: float
    current_value: float
    drop_percentage: float
    threshold: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=lambda: {})


class CacheBaselineCollector:
    """Collects and records cache baseline drop events."""

    def __init__(self, metrics_dir: Path):
        self.metrics_dir = metrics_dir
        self.cache_drops_file = metrics_dir / "cache_baseline_drops.jsonl"
        self.cache_drops_file.parent.mkdir(parents=True, exist_ok=True)

    def record_drop(self, drop: CacheBaselineDrop) -> None:
        """Record a cache baseline drop event."""
        record = {
            "baseline_name": drop.baseline_name,
            "previous_value": drop.previous_value,
            "current_value": drop.current_value,
            "drop_percentage": drop.drop_percentage,
            "threshold": drop.threshold,
            "timestamp": drop.timestamp.isoformat(),
            "metadata": drop.metadata,
        }

        with open(self.cache_drops_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

        logger.warning(
            f"Cache baseline drop recorded: {drop.baseline_name} "
            f"dropped by {drop.drop_percentage:.1%} "
            f"({drop.previous_value:.2f} → {drop.current_value:.2f})"
        )
