"""Structured telemetry spans for every tick phase using OTel-compatible API.

Wraps each tick phase in an OTel span with standardized attributes,
enabling flame-graph debugging of slow ticks and bottleneck detection
across orchestrator runs.

Usage::

    tracker = TickTelemetryTracker()
    with tracker.tick_span(tick_number=42):
        with tracker.phase_span("fetch_tasks", critical=True):
            tasks = fetch_all()
        with tracker.phase_span("spawn_agents"):
            spawn(tasks)
    # Spans are automatically closed and exported.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpanRecord:
    """Completed span record for a single phase.

    Attributes:
        name: Span name (e.g. ``"orchestrator.tick.fetch_tasks"``).
        start_ns: Start time in nanoseconds (monotonic clock).
        end_ns: End time in nanoseconds (monotonic clock).
        duration_ms: Duration in milliseconds.
        attributes: Key-value attributes attached to the span.
        error: Error message if the phase failed, else empty string.
        children: Nested child spans.
    """

    name: str
    start_ns: int
    end_ns: int
    duration_ms: float
    attributes: dict[str, Any]
    error: str = ""
    children: list[SpanRecord] = field(default_factory=lambda: list[SpanRecord]())


@dataclass
class TickTelemetryTracker:
    """Tracks structured telemetry spans for orchestrator tick phases.

    Integrates with OpenTelemetry when available; otherwise records
    spans locally for diagnostic queries and the TUI.

    Attributes:
        _spans: Completed spans from the current or most recent tick.
        _active_stack: Stack of currently open spans for nesting.
    """

    _spans: list[SpanRecord] = field(default_factory=lambda: list[SpanRecord]())
    _active_stack: list[_MutableSpan] = field(default_factory=lambda: list[_MutableSpan]())

    @contextlib.contextmanager
    def tick_span(
        self,
        tick_number: int,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[None, None, None]:
        """Context manager for the top-level tick span.

        Args:
            tick_number: Current tick number.
            attributes: Optional extra attributes for the span.

        Yields:
            None. On exit the span is closed and recorded.
        """
        self._spans = []
        self._active_stack = []
        attrs = {"tick.number": tick_number}
        if attributes:
            attrs.update(attributes)

        span = _MutableSpan(
            name="orchestrator.tick",
            start_ns=time.monotonic_ns(),
            attributes=attrs,
        )
        self._active_stack.append(span)

        otel_ctx = _start_otel_span("orchestrator.tick", attrs)

        try:
            yield
        except Exception as exc:
            span.error = str(exc)
            raise
        finally:
            span.end_ns = time.monotonic_ns()
            span.duration_ms = (span.end_ns - span.start_ns) / 1_000_000
            record = span.to_record()
            self._spans.append(record)
            self._active_stack.pop()
            _end_otel_span(otel_ctx)

    @contextlib.contextmanager
    def phase_span(
        self,
        phase_name: str,
        *,
        critical: bool = False,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[None, None, None]:
        """Context manager for a tick phase span.

        Args:
            phase_name: Name of the phase (e.g. ``"fetch_tasks"``).
            critical: Whether this is a critical control-plane phase.
            attributes: Optional extra attributes.

        Yields:
            None. On exit the span is closed and attached to its parent.
        """
        full_name = f"orchestrator.tick.{phase_name}"
        attrs: dict[str, Any] = {"phase.name": phase_name, "phase.critical": critical}
        if attributes:
            attrs.update(attributes)

        span = _MutableSpan(
            name=full_name,
            start_ns=time.monotonic_ns(),
            attributes=attrs,
        )

        otel_ctx = _start_otel_span(full_name, attrs)

        try:
            yield
        except Exception as exc:
            span.error = str(exc)
            raise
        finally:
            span.end_ns = time.monotonic_ns()
            span.duration_ms = (span.end_ns - span.start_ns) / 1_000_000
            record = span.to_record()
            # Attach to parent if one exists
            if self._active_stack:
                self._active_stack[-1].children.append(record)
            else:
                self._spans.append(record)
            _end_otel_span(otel_ctx)

    @property
    def completed_spans(self) -> list[SpanRecord]:
        """Return all completed spans from the most recent tick."""
        return list(self._spans)

    def slowest_phases(self, top_n: int = 5) -> list[SpanRecord]:
        """Return the slowest phase spans from the most recent tick.

        Args:
            top_n: Maximum number of phases to return.

        Returns:
            List of SpanRecord sorted by duration (descending).
        """
        all_phases: list[SpanRecord] = []
        for span in self._spans:
            all_phases.extend(span.children)
        all_phases.sort(key=lambda s: s.duration_ms, reverse=True)
        return all_phases[:top_n]


@dataclass
class _MutableSpan:
    """Internal mutable span builder."""

    name: str
    start_ns: int
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])
    end_ns: int = 0
    duration_ms: float = 0.0
    error: str = ""
    children: list[SpanRecord] = field(default_factory=lambda: list[SpanRecord]())

    def to_record(self) -> SpanRecord:
        """Convert to an immutable SpanRecord.

        Returns:
            Frozen span record.
        """
        return SpanRecord(
            name=self.name,
            start_ns=self.start_ns,
            end_ns=self.end_ns,
            duration_ms=self.duration_ms,
            attributes=dict(self.attributes),
            error=self.error,
            children=list(self.children),
        )


# ---------------------------------------------------------------------------
# OTel integration (graceful no-op when not installed)
# ---------------------------------------------------------------------------


def _start_otel_span(name: str, attributes: dict[str, Any]) -> Any:
    """Start an OTel span if the SDK is available.

    Args:
        name: Span name.
        attributes: Span attributes.

    Returns:
        Opaque context token, or None.
    """
    try:
        from bernstein.core.telemetry import get_tracer

        tracer = get_tracer()
        if tracer is not None:
            span = tracer.start_span(name, attributes=attributes)
            return span
    except Exception:
        pass
    return None


def _end_otel_span(ctx: Any) -> None:
    """End an OTel span if one was started.

    Args:
        ctx: Context token from ``_start_otel_span``.
    """
    if ctx is not None:
        with contextlib.suppress(Exception):
            ctx.end()
