"""Tick duration budget: prioritize critical ops, skip non-critical when over budget.

Critical ops (heartbeat checks, kill signals, reaping dead agents) always run.
Non-critical work (metrics persistence, config drift, nudge processing) is
skipped when the tick has already consumed its time budget.

Usage::

    budget = TickBudget(budget_ms=2000.0)
    budget.start()

    # Critical ops always run
    with budget.phase("heartbeat", critical=True):
        check_heartbeats()

    # Non-critical ops are skipped when over budget
    if budget.has_remaining():
        with budget.phase("metrics"):
            persist_metrics()
    else:
        budget.record_skip("metrics")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class PhaseRecord:
    """Timing record for a single tick phase.

    Attributes:
        name: Phase name (e.g. ``"heartbeat"``, ``"metrics"``).
        critical: Whether this phase is critical (always runs).
        duration_ms: Wall-clock duration in milliseconds.
        skipped: Whether this phase was skipped due to budget exhaustion.
    """

    name: str
    critical: bool = False
    duration_ms: float = 0.0
    skipped: bool = False


@dataclass
class TickBudget:
    """Time budget for a single orchestrator tick.

    Tracks elapsed time and allows non-critical phases to be skipped
    when the budget is exceeded, ensuring critical control-plane ops
    (heartbeat, kill, reap) are never starved.

    Args:
        budget_ms: Maximum tick duration in milliseconds before
            non-critical work is skipped.
    """

    budget_ms: float = 2000.0
    _start_ns: int = 0
    _phases: list[PhaseRecord] = field(default_factory=list[PhaseRecord])
    _skipped_phases: list[str] = field(default_factory=list[str])

    def start(self) -> None:
        """Mark the start of a tick. Must be called before phase tracking."""
        self._start_ns = time.monotonic_ns()
        self._phases = []
        self._skipped_phases = []

    def elapsed_ms(self) -> float:
        """Return milliseconds elapsed since ``start()`` was called.

        Returns:
            Elapsed time in milliseconds. Returns 0.0 if ``start()`` was
            not called.
        """
        if self._start_ns == 0:
            return 0.0
        return (time.monotonic_ns() - self._start_ns) / 1_000_000

    def has_remaining(self) -> bool:
        """Return True if the tick is still within its time budget.

        Returns:
            True when elapsed time is less than ``budget_ms``.
        """
        return self.elapsed_ms() < self.budget_ms

    def record_skip(self, phase_name: str) -> None:
        """Record that a non-critical phase was skipped.

        Args:
            phase_name: Name of the skipped phase.
        """
        self._skipped_phases.append(phase_name)
        self._phases.append(PhaseRecord(name=phase_name, skipped=True))

    def phase(self, name: str, *, critical: bool = False) -> _PhaseContext:
        """Context manager that times a named phase.

        Args:
            name: Human-readable phase name.
            critical: If True, this phase is always executed regardless
                of budget state.

        Returns:
            Context manager that records the phase duration.
        """
        return _PhaseContext(budget=self, name=name, critical=critical)

    def add_phase_record(self, record: PhaseRecord) -> None:
        """Append a phase record (used by _PhaseContext).

        Args:
            record: The phase record to append.
        """
        self._phases.append(record)

    @property
    def phases(self) -> list[PhaseRecord]:
        """Return all recorded phases (executed and skipped)."""
        return list(self._phases)

    @property
    def skipped_phases(self) -> list[str]:
        """Return names of phases that were skipped."""
        return list(self._skipped_phases)

    def summary(self) -> TickBudgetSummary:
        """Build a summary of the tick budget execution.

        Returns:
            Summary with total duration, phase breakdown, and skipped phases.
        """
        total_ms = self.elapsed_ms()
        executed = [p for p in self._phases if not p.skipped]
        return TickBudgetSummary(
            budget_ms=self.budget_ms,
            total_elapsed_ms=total_ms,
            over_budget=total_ms > self.budget_ms,
            phases_executed=len(executed),
            phases_skipped=len(self._skipped_phases),
            skipped_phase_names=list(self._skipped_phases),
            phase_durations={p.name: p.duration_ms for p in executed},
        )


@dataclass(frozen=True)
class TickBudgetSummary:
    """Summary of tick budget execution.

    Attributes:
        budget_ms: Configured budget in milliseconds.
        total_elapsed_ms: Total tick duration in milliseconds.
        over_budget: Whether the tick exceeded its budget.
        phases_executed: Number of phases that ran.
        phases_skipped: Number of phases that were skipped.
        skipped_phase_names: Names of skipped phases.
        phase_durations: Mapping of phase name to duration in ms.
    """

    budget_ms: float
    total_elapsed_ms: float
    over_budget: bool
    phases_executed: int
    phases_skipped: int
    skipped_phase_names: list[str]
    phase_durations: dict[str, float]


class _PhaseContext:
    """Context manager for timing a single tick phase."""

    def __init__(self, budget: TickBudget, name: str, critical: bool) -> None:
        self._budget = budget
        self._name = name
        self._critical = critical
        self._start_ns: int = 0

    def __enter__(self) -> _PhaseContext:
        self._start_ns = time.monotonic_ns()
        return self

    def __exit__(self, *_: object) -> None:
        duration_ns = time.monotonic_ns() - self._start_ns
        duration_ms = duration_ns / 1_000_000
        record = PhaseRecord(
            name=self._name,
            critical=self._critical,
            duration_ms=duration_ms,
        )
        self._budget.add_phase_record(record)
