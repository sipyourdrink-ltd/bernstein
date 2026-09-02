"""Guard evaluation registry.

A recovery path that never fires and a recovery path that cannot fire look
identical from the outside: neither leaves a trace. This module makes the
*evaluation* of a guard - not just its firing - a recorded fact, so absence
of firing becomes a claim with evidence behind it instead of a silence.

Usage from an instrumented guard::

    from bernstein.core.observability.guard_registry import default_registry

    GUARD_ID = "circuit_breaker.scope_violation"
    default_registry.register(GUARD_ID)  # visible with 0 evaluations even if never called

    ...
    default_registry.record_evaluation(GUARD_ID, "clean")
    # or
    default_registry.record_evaluation(GUARD_ID, "violation")

This is step 1 of the guard-reachability work (issue #3454): one registry,
one instrumented guard, one read-only report. Anchoring the report in a run
record, aggregating across runs, and gating CI on it are follow-up slices -
this module only has to make "evaluated, never fired" distinguishable from
"never evaluated" for a single process's lifetime.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GuardRecord:
    """Per-guard evaluation count and outcome distribution."""

    guard_id: str
    evaluations: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class GuardRegistry:
    """Tracks how many times each guard's predicate was evaluated, and to what
    outcome, within this process.

    A guard that is registered but never evaluated reports zero evaluations.
    A guard that is evaluated and never fires reports N evaluations with no
    "fired" outcome in its distribution. The two are not the same report.
    """

    def __init__(self) -> None:
        self._records: dict[str, GuardRecord] = {}

    def register(self, guard_id: str) -> None:
        """Make *guard_id* visible in reports even if it is never evaluated.

        Idempotent: registering an already-known guard is a no-op.
        """
        self._records.setdefault(guard_id, GuardRecord(guard_id=guard_id))

    def record_evaluation(self, guard_id: str, outcome: str) -> None:
        """Record one evaluation of *guard_id* that resolved to *outcome*.

        Implicitly registers the guard if this is its first evaluation.
        """
        record = self._records.setdefault(guard_id, GuardRecord(guard_id=guard_id))
        record.evaluations += 1
        record.outcomes[outcome] = record.outcomes.get(outcome, 0) + 1

    def records(self) -> list[GuardRecord]:
        """Return all known guard records, registered or evaluated."""
        return list(self._records.values())

    def record_for(self, guard_id: str) -> GuardRecord | None:
        """Return the record for *guard_id*, or None if it is unknown."""
        return self._records.get(guard_id)

    def evaluations_for(self, guard_id: str) -> int:
        """Return the evaluation count for *guard_id* (0 if unknown)."""
        record = self._records.get(guard_id)
        return record.evaluations if record is not None else 0

    def outcomes_for(self, guard_id: str) -> dict[str, int]:
        """Return a copy of the outcome distribution for *guard_id*."""
        record = self._records.get(guard_id)
        return dict(record.outcomes) if record is not None else {}


def reachability_report(registry: GuardRegistry) -> list[tuple[str, int]]:
    """Pure projection of *registry* into ``(guard_id, evaluation_count)``
    pairs, ordered by guard id.

    Includes every registered guard, evaluated or not - that is the whole
    point: a guard with zero evaluations is present in the report, not
    absent from it.
    """
    return sorted(
        ((record.guard_id, record.evaluations) for record in registry.records()),
        key=lambda pair: pair[0],
    )


# Process-wide registry that production guards record into. A dedicated
# instance per orchestrator run (and anchoring the report in the run record)
# is a follow-up slice; for now this is the single surface every instrumented
# guard writes to and every reader reads from.
default_registry = GuardRegistry()


def default_report() -> list[tuple[str, int]]:
    """Read-only view of :data:`default_registry` via :func:`reachability_report`."""
    return reachability_report(default_registry)
