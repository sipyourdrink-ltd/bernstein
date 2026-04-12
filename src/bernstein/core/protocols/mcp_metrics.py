"""MCP-011: MCP server metrics collection.

Collects per-server tool call latency, error rate, and availability
metrics.  Designed to feed dashboards and alerting without depending
on external metric systems (Prometheus, Datadog).

Each server has an ``MCPServerMetrics`` dataclass accumulating:
- total_calls, error_count, latency samples (for percentiles)
- availability windows (up/down transitions)

Usage::

    from bernstein.core.mcp_metrics import MCPMetricsCollector

    collector = MCPMetricsCollector()
    collector.record_call("github", "create_issue", latency_ms=142.3)
    collector.record_call("github", "list_repos", latency_ms=87.1, error=True)
    collector.record_availability("github", alive=True)

    summary = collector.summary("github")
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_LATENCY_SAMPLES: int = 1000


@dataclass
class ToolCallRecord:
    """A single tool call record.

    Attributes:
        tool_name: Name of the MCP tool called.
        latency_ms: Latency in milliseconds.
        error: Whether the call errored.
        ts: Unix timestamp of the call.
    """

    tool_name: str
    latency_ms: float
    error: bool = False
    ts: float = field(default_factory=time.time)


@dataclass
class AvailabilityRecord:
    """A single availability check record.

    Attributes:
        alive: Whether the server was alive.
        ts: Unix timestamp of the check.
    """

    alive: bool
    ts: float = field(default_factory=time.time)


@dataclass
class MCPServerMetrics:
    """Accumulated metrics for a single MCP server.

    Attributes:
        server_name: Name of the MCP server.
        total_calls: Total tool calls recorded.
        error_count: Number of errored calls.
        latency_samples: Recent latency values in ms.
        availability_checks: Recent availability check results.
    """

    server_name: str
    total_calls: int = 0
    error_count: int = 0
    latency_samples: list[float] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    availability_checks: list[AvailabilityRecord] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    _tool_calls: list[ToolCallRecord] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]

    @property
    def error_rate(self) -> float:
        """Error rate as a fraction [0.0, 1.0]."""
        if self.total_calls == 0:
            return 0.0
        return self.error_count / self.total_calls

    @property
    def availability_rate(self) -> float:
        """Availability rate based on recent checks."""
        if not self.availability_checks:
            return 1.0
        alive_count = sum(1 for r in self.availability_checks if r.alive)
        return alive_count / len(self.availability_checks)

    def latency_percentile(self, p: float) -> float:
        """Compute a latency percentile.

        Args:
            p: Percentile as a fraction (e.g. 0.95 for p95).

        Returns:
            Latency at the given percentile, or 0.0 if no samples.
        """
        if not self.latency_samples:
            return 0.0
        sorted_samples = sorted(self.latency_samples)
        idx = min(math.ceil(len(sorted_samples) * p) - 1, len(sorted_samples) - 1)
        return sorted_samples[max(0, idx)]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible summary dict."""
        return {
            "server_name": self.server_name,
            "total_calls": self.total_calls,
            "error_count": self.error_count,
            "error_rate": round(self.error_rate, 4),
            "availability_rate": round(self.availability_rate, 4),
            "latency_p50": round(self.latency_percentile(0.50), 2),
            "latency_p95": round(self.latency_percentile(0.95), 2),
            "latency_p99": round(self.latency_percentile(0.99), 2),
            "sample_count": len(self.latency_samples),
        }


class MCPMetricsCollector:
    """Collects and queries metrics for all MCP servers.

    Args:
        max_latency_samples: Maximum latency samples per server.
        max_availability_records: Maximum availability records per server.
    """

    def __init__(
        self,
        max_latency_samples: int = MAX_LATENCY_SAMPLES,
        max_availability_records: int = 200,
    ) -> None:
        self._metrics: dict[str, MCPServerMetrics] = {}
        self._max_latency_samples = max_latency_samples
        self._max_availability_records = max_availability_records

    def _ensure_server(self, server_name: str) -> MCPServerMetrics:
        """Get or create metrics for a server."""
        if server_name not in self._metrics:
            self._metrics[server_name] = MCPServerMetrics(server_name=server_name)
        return self._metrics[server_name]

    def record_call(
        self,
        server_name: str,
        tool_name: str,
        latency_ms: float,
        *,
        error: bool = False,
    ) -> None:
        """Record a tool call.

        Args:
            server_name: MCP server name.
            tool_name: Tool that was called.
            latency_ms: Call latency in milliseconds.
            error: Whether the call produced an error.
        """
        metrics = self._ensure_server(server_name)
        metrics.total_calls += 1
        if error:
            metrics.error_count += 1

        metrics.latency_samples.append(latency_ms)
        if len(metrics.latency_samples) > self._max_latency_samples:
            trim_at = len(metrics.latency_samples) - self._max_latency_samples
            del metrics.latency_samples[:trim_at]

        record = ToolCallRecord(tool_name=tool_name, latency_ms=latency_ms, error=error)
        metrics._tool_calls.append(record)  # pyright: ignore[reportPrivateUsage]

    def record_availability(self, server_name: str, *, alive: bool) -> None:
        """Record an availability check.

        Args:
            server_name: MCP server name.
            alive: Whether the server was alive.
        """
        metrics = self._ensure_server(server_name)
        metrics.availability_checks.append(AvailabilityRecord(alive=alive))
        if len(metrics.availability_checks) > self._max_availability_records:
            trim_at = len(metrics.availability_checks) - self._max_availability_records
            del metrics.availability_checks[:trim_at]

    def get_metrics(self, server_name: str) -> MCPServerMetrics | None:
        """Return metrics for a server, or None if not tracked."""
        return self._metrics.get(server_name)

    def summary(self, server_name: str) -> dict[str, Any] | None:
        """Return a summary dict for a server, or None if not tracked."""
        metrics = self._metrics.get(server_name)
        if metrics is None:
            return None
        return metrics.to_dict()

    def all_summaries(self) -> dict[str, dict[str, Any]]:
        """Return summaries for all tracked servers."""
        return {name: m.to_dict() for name, m in self._metrics.items()}

    def servers_above_error_rate(self, threshold: float) -> list[str]:
        """Return server names whose error rate exceeds the threshold.

        Args:
            threshold: Error rate threshold (e.g. 0.10 for 10%).

        Returns:
            List of server names above the threshold.
        """
        return [name for name, m in self._metrics.items() if m.total_calls > 0 and m.error_rate > threshold]

    def servers_below_availability(self, threshold: float) -> list[str]:
        """Return servers whose availability is below the threshold.

        Args:
            threshold: Availability threshold (e.g. 0.95 for 95%).

        Returns:
            List of server names below the threshold.
        """
        return [name for name, m in self._metrics.items() if m.availability_checks and m.availability_rate < threshold]

    def reset(self, server_name: str | None = None) -> None:
        """Reset metrics for a server or all servers.

        Args:
            server_name: Server to reset, or None for all.
        """
        if server_name is not None:
            self._metrics.pop(server_name, None)
        else:
            self._metrics.clear()

    def to_dict(self) -> dict[str, Any]:
        """Serialize all metrics to a JSON-compatible dict."""
        return {
            "servers": self.all_summaries(),
            "total_servers": len(self._metrics),
        }
