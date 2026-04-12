"""Tool execution timing telemetry.

Records per-tool-call timing splits (queue wait, execute, total wall time)
and aggregates p50/p90/p99 histograms per tool type from a sliding window
of the last 1000 records per tool.

Data is persisted to ``.sdd/metrics/tool_timing.jsonl`` for observability
and post-hoc analysis.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Sliding window cap per tool name.
_WINDOW_SIZE = 1000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolTimingRecord:
    """Timing record for a single tool invocation.

    Attributes:
        tool_name: Name of the tool being executed.
        queue_wait_ms: Milliseconds spent waiting in queue.
        execute_ms: Milliseconds spent executing the tool.
        total_ms: Total wall time (queue_wait_ms + execute_ms).
        session_id: The agent/session that triggered the tool.
        timestamp: Unix epoch when execution finished.
    """

    tool_name: str
    queue_wait_ms: float
    execute_ms: float
    total_ms: float
    session_id: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "tool_name": self.tool_name,
            "queue_wait_ms": self.queue_wait_ms,
            "execute_ms": self.execute_ms,
            "total_ms": self.total_ms,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> ToolTimingRecord:
        """Deserialise from a JSON-safe dict."""
        ts_val = d.get("timestamp", 0.0)
        return cls(
            tool_name=str(d["tool_name"]),
            queue_wait_ms=cast("float", d["queue_wait_ms"]),
            execute_ms=cast("float", d["execute_ms"]),
            total_ms=cast("float", d["total_ms"]),
            session_id=str(d["session_id"]),
            timestamp=cast("float", ts_val if isinstance(ts_val, (int, float)) else 0.0),
        )


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], p: float) -> float:
    """Compute arbitrary percentile from a sorted list.

    Args:
        sorted_values: Already-sorted list of numeric values.
        p: Percentile as a fraction (0.0 to 1.0).

    Returns:
        The nearest-rank percentile value, or 0.0 when empty.
    """
    if not sorted_values:
        return 0.0
    idx = int(p * (len(sorted_values) - 1))
    return sorted_values[min(idx, len(sorted_values) - 1)]


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


class ToolTimingRecorder:
    """Record tool execution timings and compute per-tool histograms.

    Maintains a sliding window of the last *1000* records per tool name.
    Each record is appended to ``.sdd/metrics/tool_timing.jsonl`` for
    persistence and external analysis.

    Args:
        metrics_dir: Directory to store the JSONL file (default
            ``.sdd/metrics`` under CWD).
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        self._metrics_dir = metrics_dir or Path.cwd() / ".sdd" / "metrics"
        self._metrics_dir.mkdir(parents=True, exist_ok=True)

        # Sliding window per tool: tool_name -> list[float] of total_ms
        self._windows: dict[str, list[float]] = {}
        # Raw records per tool for queue_wait_ms and execute_ms percentiles.
        self._queue_windows: dict[str, list[float]] = {}
        self._execute_windows: dict[str, list[float]] = {}
        # Total record count for JSONL line numbering (append-only safety).
        self._record_count: int = 0

    # -- context manager API -------------------------------------------------

    @contextmanager
    def record(
        self,
        tool: str,
        session_id: str,
        queue_start: float | None = None,
    ) -> Iterator[None]:
        """Context manager to time a tool execution.

        Use this when the caller wants full control over queue-wait timing::

            # queue_start is set when we enqueue the tool
            recorder.record("search", "agent-1", queue_start=queue_start)
                # ... tool runs ...
            # on exit, execute timing is captured

        Or, with no queue context (queue_wait_ms = 0)::

            with recorder.record("search", "agent-1"):
                # ... tool runs ...

        Args:
            tool: Tool name.
            session_id: Agent session identifier.
            queue_start: Unix epoch when the tool was enqueued.  When
                ``None``, queue wait is recorded as 0.0 ms.
        """
        exec_start = time.monotonic()
        # Record wall time for total_ms and execute_ms calculation
        wall_start = time.time()

        try:
            yield
        finally:
            exec_end = time.monotonic()
            exec_ms = (exec_end - exec_start) * 1000.0
            wall_end = time.time()
            total_ms = (wall_end - wall_start) * 1000.0

            queue_wait_ms = (wall_start - queue_start) * 1000.0 if queue_start is not None else 0.0

            self._commit(tool, session_id, queue_wait_ms, exec_ms, total_ms)

    # -- direct record API ---------------------------------------------------

    def record_direct(
        self,
        tool: str,
        session_id: str,
        queue_wait_ms: float,
        execute_ms: float,
        total_ms: float,
    ) -> ToolTimingRecord:
        """Record a tool execution with pre-computed timings.

        Use this when timing is measured externally (e.g. by the adapter)
        and the caller wants to pass in the values directly.

        Args:
            tool: Tool name.
            session_id: Agent session identifier.
            queue_wait_ms: Milliseconds spent in queue.
            execute_ms: Milliseconds spent executing.
            total_ms: Total wall time.

        Returns:
            The persisted :class:`ToolTimingRecord`.
        """
        record = ToolTimingRecord(
            tool_name=tool,
            queue_wait_ms=queue_wait_ms,
            execute_ms=execute_ms,
            total_ms=total_ms,
            session_id=session_id,
        )
        self._store_record(record)
        return record

    # -- histogram API -------------------------------------------------------

    def get_histogram(self, tool_name: str) -> dict[str, float]:
        """Get p50/p90/p99 histogram for a tool's total_ms.

        Args:
            tool_name: Tool name to look up.

        Returns:
            Dict with keys ``"p50"``, ``"p90"``, ``"p99"`` and float values
            in milliseconds.  Returns zeros when no data exists.
        """
        values = self._windows.get(tool_name, [])
        if not values:
            return {"p50": 0.0, "p90": 0.0, "p99": 0.0}
        sorted_vals = sorted(values)
        return {
            "p50": _percentile(sorted_vals, 0.50),
            "p90": _percentile(sorted_vals, 0.90),
            "p99": _percentile(sorted_vals, 0.99),
        }

    def get_full_histogram(self, tool_name: str) -> dict[str, dict[str, float]]:
        """Get p50/p90/p99 for all timing splits of a tool.

        Args:
            tool_name: Tool name to look up.

        Returns:
            Dict with keys for each timing split: ``"total_ms"``,
            ``"queue_wait_ms"``, ``"execute_ms"``, each mapping to
            ``{"p50": ..., "p90": ..., "p99": ...}``.  Returns empty
            nested dicts when no data exists.
        """
        if tool_name not in self._windows:
            return {}
        return {
            "total_ms": self._compute_percentiles(self._windows[tool_name]),
            "queue_wait_ms": self._compute_percentiles(self._queue_windows.get(tool_name, [])),
            "execute_ms": self._compute_percentiles(self._execute_windows.get(tool_name, [])),
        }

    def get_tool_names(self) -> list[str]:
        """Return all tool names for which timing data exists.

        Returns:
            Sorted list of unique tool names.
        """
        return sorted(self._windows.keys())

    def get_record_count(self) -> int:
        """Return the total number of records written to the JSONL file.

        Returns:
            Total count since the recorder was created.
        """
        return self._record_count

    def get_tool_window(self, tool_name: str) -> list[float]:
        """Return a copy of the sliding window for tool total_ms.

        Args:
            tool_name: Tool name to look up.

        Returns:
            List of total_ms values in the current sliding window.
        """
        return list(self._windows.get(tool_name, []))

    def get_queue_window(self, tool_name: str) -> list[float]:
        """Return a copy of the sliding window for tool queue_wait_ms.

        Args:
            tool_name: Tool name to look up.

        Returns:
            List of queue_wait_ms values in the current sliding window.
        """
        return list(self._queue_windows.get(tool_name, []))

    def get_execute_window(self, tool_name: str) -> list[float]:
        """Return a copy of the sliding window for tool execute_ms.

        Args:
            tool_name: Tool name to look up.

        Returns:
            List of execute_ms values in the current sliding window.
        """
        return list(self._execute_windows.get(tool_name, []))

    # -- load from disk ------------------------------------------------------

    def load_from_jsonl(self, limit: int | None = None) -> int:
        """Load existing records from ``tool_timing.jsonl`` into windows.

        Args:
            limit: Maximum number of records to load per tool (default:
                window size).

        Returns:
            Number of records loaded.
        """
        jsonl_path = self._jsonl_path()
        if not jsonl_path.exists():
            return 0

        loaded = 0
        per_tool: dict[str, list[ToolTimingRecord]] = {}
        try:
            with jsonl_path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = ToolTimingRecord.from_dict(json.loads(line))
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue

                    tool = record.tool_name
                    per_tool.setdefault(tool, []).append(record)
                    loaded += 1

        except OSError as exc:
            logger.warning("Failed to load tool timing JSONL: %s", exc)
            return 0

        # Rebuild sliding windows from loaded data (keep last _WINDOW_SIZE).
        for tool_name, records in per_tool.items():
            effective_limit = limit if limit is not None else _WINDOW_SIZE
            recent = records[-effective_limit:]

            self._windows[tool_name] = [r.total_ms for r in recent]
            self._queue_windows[tool_name] = [r.queue_wait_ms for r in recent]
            self._execute_windows[tool_name] = [r.execute_ms for r in recent]

        self._record_count = loaded
        return loaded

    # -- internal ------------------------------------------------------------

    def _commit(
        self,
        tool: str,
        session_id: str,
        queue_wait_ms: float,
        execute_ms: float,
        total_ms: float,
    ) -> ToolTimingRecord:
        """Create and store a timing record from computed values.

        Args:
            tool: Tool name.
            session_id: Agent session identifier.
            queue_wait_ms: Queue wait in milliseconds.
            execute_ms: Execution time in milliseconds.
            total_ms: Total wall time in milliseconds.

        Returns:
            The persisted :class:`ToolTimingRecord`.
        """
        record = ToolTimingRecord(
            tool_name=tool,
            queue_wait_ms=queue_wait_ms,
            execute_ms=execute_ms,
            total_ms=total_ms,
            session_id=session_id,
        )
        self._store_record(record)
        return record

    def _store_record(self, record: ToolTimingRecord) -> None:
        """Persist a timing record to the JSONL file and update sliding windows.

        Args:
            record: Timing record to store.
        """
        # Append to JSONL.
        jsonl_path = self._jsonl_path()
        try:
            with jsonl_path.open("a") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("Failed to write tool timing record: %s", exc)

        # Update sliding windows.
        tool = record.tool_name
        self._windows.setdefault(tool, [])
        self._queue_windows.setdefault(tool, [])
        self._execute_windows.setdefault(tool, [])

        self._windows[tool].append(record.total_ms)
        self._queue_windows[tool].append(record.queue_wait_ms)
        self._execute_windows[tool].append(record.execute_ms)

        # Enforce window size.
        if len(self._windows[tool]) > _WINDOW_SIZE:
            self._windows[tool] = self._windows[tool][-_WINDOW_SIZE:]
            self._queue_windows[tool] = self._queue_windows[tool][-_WINDOW_SIZE:]
            self._execute_windows[tool] = self._execute_windows[tool][-_WINDOW_SIZE:]

        self._record_count += 1

    def _jsonl_path(self) -> Path:
        """Return the path to the tool timing JSONL file.

        Returns:
            Absolute path to ``tool_timing.jsonl``.
        """
        return self._metrics_dir / "tool_timing.jsonl"

    @staticmethod
    def _compute_percentiles(values: list[float]) -> dict[str, float]:
        """Compute p50/p90/p99 for a list of values.

        Args:
            values: List of numeric values.

        Returns:
            Dict with p50, p90, p99 keys.
        """
        if not values:
            return {}
        sorted_vals = sorted(values)
        return {
            "p50": _percentile(sorted_vals, 0.50),
            "p90": _percentile(sorted_vals, 0.90),
            "p99": _percentile(sorted_vals, 0.99),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_recorder: ToolTimingRecorder | None = None


def get_recorder(metrics_dir: Path | None = None) -> ToolTimingRecorder:
    """Return the default module-level :class:`ToolTimingRecorder`.

    Creates the recorder on first call (lazy singleton).

    Args:
        metrics_dir: Optional directory override for recorder creation.

    Returns:
        Shared :class:`ToolTimingRecorder` instance.
    """
    global _default_recorder
    if _default_recorder is None:
        _default_recorder = ToolTimingRecorder(metrics_dir=metrics_dir)
    return _default_recorder


def reset_recorder() -> None:
    """Reset the module-level singleton (mainly for testing).

    After calling, :func:`get_recorder` will create a fresh recorder.
    """
    global _default_recorder
    _default_recorder = None
