"""Datadog DogStatsD metrics exporter for Bernstein.

Implements a lightweight statsd-compatible emitter that sends Bernstein
metrics to a local or remote DogStatsD agent via UDP.  Handles counters,
gauges, and histograms using the simple text-based statsd protocol,
compatible with the ``datadog`` package or standalone dogstatsd agents.

No external dependency on the ``datadog`` Python package is required --
this module speaks raw statsd protocol over UDP.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DogStatsDConfig:
    """Configuration for the DogStatsD exporter.

    Args:
        host: DogStatsD agent hostname or IP.
        port: DogStatsD agent UDP port.
        prefix: Metric name prefix (e.g. ``bernstein``).
        default_tags: Static tags always appended.
        buffer_max: Flush buffer when this many entries are queued.
        flush_interval: Max seconds between flushes.
    """

    host: str = "localhost"
    port: int = 8125
    prefix: str = "bernstein"
    default_tags: list[str] = field(default_factory=lambda: ["app:bernstein"])
    buffer_max: int = 200
    flush_interval: float = 5.0


class DogStatsDExporter:
    """Buffers Bernstein metrics and flushes them to a DogStatsD agent.

    Thread-safe via a simple lock.

    Args:
        config: Exporter configuration.
    """

    def __init__(self, config: DogStatsDConfig | None = None) -> None:
        self._cfg = config or DogStatsDConfig()
        self._buf: list[str] = []
        self._last_flush = time.monotonic()
        import threading

        self._lock = threading.Lock()

    # -- Public API -----------------------------------------------------------

    def record_counter(self, name: str, value: int = 1, tags: list[str] | None = None) -> None:
        """Increment a counter metric.

        Args:
            name: Metric name (appended to prefix).
            value: Increment value.
            tags: Optional list of ``key:value`` tags.
        """
        self._enqueue(f"{self._cfg.prefix}.{name}:{value}|c", tags)

    def record_gauge(self, name: str, value: float | int, tags: list[str] | None = None) -> None:
        """Record a gauge metric.

        Args:
            name: Metric name.
            value: Current gauge value.
            tags: Optional tags.
        """
        self._enqueue(f"{self._cfg.prefix}.{name}:{value}|g", tags)

    def record_histogram(
        self,
        name: str,
        value: float | int,
        tags: list[str] | None = None,
    ) -> None:
        """Record a histogram / distribution metric.

        Args:
            name: Metric name.
            value: Observed value.
            tags: Optional tags.
        """
        self._enqueue(f"{self._cfg.prefix}.{name}:{value}|d", tags)

    def flush(self) -> None:
        """Send buffered metrics to the DogStatsD agent."""

        with self._lock:
            if not self._buf:
                return
            lines = list(self._buf)
            self._buf.clear()
            self._last_flush = time.monotonic()

        # Aggregate duplicate metric lines to reduce UDP traffic
        aggregated: dict[str, float] = {}
        for line in lines:
            aggregated[line] = aggregated.get(line, 0) + 1

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for metric_line, count in aggregated.items():
                    payload = metric_line
                    if count > 1:
                        # statsd supports @sample-rate; but for counters,
                        # it's better to just multiply the value
                        parts = payload.split("|")
                        val_part = parts[0]
                        name_val, val_str = val_part.rsplit(":", 1)
                        multiplied_val = float(val_str) * count
                        parts[0] = f"{name_val}:{multiplied_val}"
                        payload = "|".join(parts)
                    sock.sendto(payload.encode("utf-8"), (self._cfg.host, self._cfg.port))
            logger.debug("DogStatsD flushed %d unique metrics", len(aggregated))
        except OSError as exc:
            logger.warning("DogStatsD flush failed: %s", exc)
            # Restore buffer on failure so we don't lose data
            with self._lock:
                self._buf = lines + self._buf

    def record_metric_point(self, metric_type: str, value: float, labels: dict[str, Any]) -> None:
        """Record a metric point from the Bernstein MetricsCollector.

        Adapts the internal metric point to DogStatsD format using the
        metric type to determine the statsd type.

        Args:
            metric_type: Metric type string (e.g. ``task_completion_time``).
            value: Numeric value.
            labels: Key-value labels converted to tags.
        """
        tags = [f"{k}:{v}" for k, v in labels.items() if v is not None]
        tags.extend(self._cfg.default_tags)

        # Map Bernstein metric types to statsd
        type_map = {
            "task_completion_time": "h",
            "api_usage": "c",
            "agent_success": "c",
            "error_rate": "c",
            "cost_efficiency": "h",
            "provider_health": "g",
            "free_tier_usage": "c",
            "fast_path": "c",
            "parallelism_level": "g",
            "queue_depth": "g",
            "merge_result": "c",
            "compaction": "c",
        }
        statsd_type = type_map.get(metric_type, "g")

        suffix = ""
        if tags:
            suffix = "|#" + ",".join(tags)

        # Normalize metric name
        safe_name = metric_type.replace("_", ".").lower()
        line = f"{self._cfg.prefix}.{safe_name}:{value}|{statsd_type}{suffix}"
        self._enqueue(line, None)

    # -- Internal -------------------------------------------------------------

    def _enqueue(self, line: str, tags: list[str] | None) -> None:
        suffix = ""
        all_tags = list(tags or []) + self._cfg.default_tags
        if all_tags:
            suffix = "|#" + ",".join(all_tags)
        final_line = f"{line}{suffix}" if suffix else line

        with self._lock:
            self._buf.append(final_line)
            if len(self._buf) >= self._cfg.buffer_max:
                self._safe_flush_locked()

    def _safe_flush_locked(self) -> None:
        """Flush without re-acquiring the lock (called from _enqueue)."""
        lines = list(self._buf)
        self._buf.clear()
        self._last_flush = time.monotonic()

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                for line in lines:
                    sock.sendto(line.encode("utf-8"), (self._cfg.host, self._cfg.port))
        except OSError as exc:
            logger.warning("DogStatsD flush failed: %s", exc)
            with self._lock:
                self._buf = lines + self._buf

    def should_flush(self) -> bool:
        """Check if automatic flush is due (buffer full or timeout)."""
        with self._lock:
            return (
                len(self._buf) >= self._cfg.buffer_max
                or time.monotonic() - self._last_flush >= self._cfg.flush_interval
            )

    def close(self) -> None:
        """Flush any remaining metrics and close the exporter."""
        self.flush()


def export_to_datadog(
    metrics_dir: Path,
    config: DogStatsDConfig | None = None,
    exporter: DogStatsDExporter | None = None,
) -> int:
    """Read JSONL metrics from *metrics_dir* and export to Datadog.

    Scans all JSONL files in the directory and emits each metric point
    via the DogStatsD exporter.  This is a one-shot export function.

    Args:
        metrics_dir: Path to .sdd/metrics/ directory.
        config: DogStatsD configuration.
        exporter: Pre-existing exporter instance (created if None).

    Returns:
        Number of metric points exported.
    """
    import json

    exporter = exporter or DogStatsDExporter(config)
    count = 0

    if not metrics_dir.is_dir():
        logger.warning("Metrics directory not found: %s", metrics_dir)
        return 0

    for jsonl_file in metrics_dir.glob("*.jsonl"):
        try:
            for raw_line in jsonl_file.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                point = json.loads(raw_line)
                metric_type = point.get("metric_type", "unknown")
                value = point.get("value", 0)
                labels = point.get("labels", {})
                exporter.record_metric_point(metric_type, value, labels)
                count += 1
        except Exception as exc:
            logger.warning("Failed to export metrics from %s: %s", jsonl_file, exc)

    exporter.flush()
    return count
