"""Supervision-period conduct fold: one agent's work across a sealed journal window.

Mirrors the pure event-list projection pattern in
:mod:`bernstein.core.replay.scorecard` (``derive_scorecard``, issue #5402).
The fold receives a principal id and a time window, filters the journal rows
to those authored by that principal in that window, and collapses them into
operator-facing counts --- every count carries the event-index range it was
computed from, and a period with no rows for the agent is reported as
**UNVERIFIED** (not clean), per the absence-coverage contract in
:mod:`bernstein.core.quality.absence_coverage`.

Determinism
-----------
The fold is a *pure function*: the same row list, principal id, and time
window always produce byte-identical canonical bytes. Wall clock never enters
the fold. The canonical encoding mirrors
:func:`bernstein.core.replay.scorecard.ScorecardProjection.canonical_bytes`
(sorted keys, compact separators, UTF-8).

Coverage classification
-----------------------
An absence claim with no coverage record is **UNVERIFIED**, not clean.
A period whose filtered row list is empty is therefore reported as
``UNVERIFIED`` so a verifier cannot mistake "no data" for "nothing happened".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.quality.absence_coverage import CompletionCoverageStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CONDUCT_FOLD_SCHEMA_VERSION",
    "CONDUCT_FOLD_TYPE",
    "CompletionCoverageStatus",
    "ConductProjection",
    "derive_conduct",
    "derive_conduct_from_path",
]

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

#: Schema version of the :class:`ConductProjection` document.
CONDUCT_FOLD_SCHEMA_VERSION: int = 1

#: Type URL for the conduct fold predicate.
CONDUCT_FOLD_TYPE: str = "https://bernstein.run/attestations/conduct/v1"

# ---------------------------------------------------------------------------
# Journal event wire strings the fold recognises.
# ---------------------------------------------------------------------------

#: A task was claimed by an agent.
EVENT_TASK_CLAIMED = "task_claimed"

#: A task completed successfully.
EVENT_TASK_COMPLETED = "task_completed"

#: A task's verification gate refused the work.
EVENT_TASK_VERIFICATION_FAILED = "task_verification_failed"

#: An approval gate was encountered.
EVENT_APPROVAL_GATE = "approval_gate"

#: An approval gate was honoured (agent action approved).
EVENT_APPROVAL_HONOURED = "approval_honoured"

#: An approval gate was overridden or bypassed.
EVENT_APPROVAL_OVERRIDDEN = "approval_overridden"

#: A task was delegated to another principal.
EVENT_TASK_DELEGATED = "task_delegated"

#: A task was submitted to an external system.
EVENT_TASK_SUBMITTED = "task_submitted"

#: A tool call was made.
EVENT_TOOL_CALL = "tool_call"

#: Closed set of event types the fold processes.
_FOLDED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_TASK_CLAIMED,
        EVENT_TASK_COMPLETED,
        EVENT_TASK_VERIFICATION_FAILED,
        EVENT_APPROVAL_GATE,
        EVENT_APPROVAL_HONOURED,
        EVENT_APPROVAL_OVERRIDDEN,
        EVENT_TASK_DELEGATED,
        EVENT_TASK_SUBMITTED,
        EVENT_TOOL_CALL,
    }
)

# ---------------------------------------------------------------------------
# Count helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Count:
    """A count with the event-index range it was computed from.

    Mirrors the private :class:`_Count` in scorecard.py; kept local so
    this module has no dependency on the scorecard internals.
    """

    count: int
    first_index: int | None
    last_index: int | None

    @classmethod
    def empty(cls) -> _Count:
        return cls(count=0, first_index=None, last_index=None)


# ---------------------------------------------------------------------------
# Conduct projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConductProjection:
    """One agent's work collapsed into operator-facing counts over a time window.

    Every count is paired with the event-index range the fold walked to
    produce it, so a verifier can re-derive the number from the embedded rows.

    Attributes:
        schema_version: Projection schema version.
        run_id: Run whose journal was folded.
        principal_id: Agent principal whose rows were projected.
        period_start: Window start as unix epoch seconds.
        period_end: Window end as unix epoch seconds.
        coverage: Whether the period has verified coverage for this principal.
        event_count: Total rows in *events* that survived period + principal
            filtering.
        tool_calls: Count of tool-call events with event-index range.
        completed: Count of completed tasks with range.
        failed: Count of failed/verification-rejected tasks with range.
        gated: Count of approval-gate encounters with range.
        gated_honoured: Count of gates that were honoured with range.
        gated_overridden: Count of gates that were overridden with range.
        delegated: Count of delegation/submission events with range.
        ignored_event_types: Event types seen but not folded; sorted stable.
    """

    schema_version: int = CONDUCT_FOLD_SCHEMA_VERSION
    run_id: str = ""
    principal_id: str = ""
    period_start: float = 0.0
    period_end: float = 0.0
    coverage: CompletionCoverageStatus = CompletionCoverageStatus.UNVERIFIED
    event_count: int = 0
    tool_calls: _Count = field(default_factory=_Count.empty)
    completed: _Count = field(default_factory=_Count.empty)
    failed: _Count = field(default_factory=_Count.empty)
    gated: _Count = field(default_factory=_Count.empty)
    gated_honoured: _Count = field(default_factory=_Count.empty)
    gated_overridden: _Count = field(default_factory=_Count.empty)
    delegated: _Count = field(default_factory=_Count.empty)
    ignored_event_types: tuple[str, ...] = field(default_factory=tuple)

    def canonical_bytes(self) -> bytes:
        """Return deterministic UTF-8 JSON bytes of the projection.

        Mirrors the canonical encoding discipline in
        :func:`bernstein.core.replay.scorecard.ScorecardProjection.canonical_bytes`:
        sorted keys, compact separators, UTF-8. Wall-clock fields (``period_start``,
        ``period_end``) are **included** here because they are part of the
        reproducible identity of the fold (two folds of the same rows in the
        same window produce the same window bounds regardless of when the
        fold ran).
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped projection served to operators."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "principal_id": self.principal_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "coverage": self.coverage.value,
            "event_count": self.event_count,
            "tool_calls": _count_to_dict(self.tool_calls),
            "completed": _count_to_dict(self.completed),
            "failed": _count_to_dict(self.failed),
            "gated": _count_to_dict(self.gated),
            "gated_honoured": _count_to_dict(self.gated_honoured),
            "gated_overridden": _count_to_dict(self.gated_overridden),
            "delegated": _count_to_dict(self.delegated),
            "ignored_event_types": list(self.ignored_event_types),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ConductProjection:
        """Inverse of :meth:`to_dict`."""
        coverage_raw = raw.get("coverage", "")
        coverage = CompletionCoverageStatus(coverage_raw) if coverage_raw else CompletionCoverageStatus.UNVERIFIED
        return cls(
            schema_version=int(raw.get("schema_version", CONDUCT_FOLD_SCHEMA_VERSION)),
            run_id=str(raw.get("run_id", "")),
            principal_id=str(raw.get("principal_id", "")),
            period_start=float(raw.get("period_start", 0.0)),
            period_end=float(raw.get("period_end", 0.0)),
            coverage=coverage,
            event_count=int(raw.get("event_count", 0)),
            tool_calls=_count_from_dict(raw.get("tool_calls", {})),
            completed=_count_from_dict(raw.get("completed", {})),
            failed=_count_from_dict(raw.get("failed", {})),
            gated=_count_from_dict(raw.get("gated", {})),
            gated_honoured=_count_from_dict(raw.get("gated_honoured", {})),
            gated_overridden=_count_from_dict(raw.get("gated_overridden", {})),
            delegated=_count_from_dict(raw.get("delegated", {})),
            ignored_event_types=tuple(raw.get("ignored_event_types", [])),
        )


def _count_to_dict(count: _Count) -> dict[str, Any]:
    """Render a :class:`_Count` to its public dict shape.

    Mirrors :func:`bernstein.core.replay.scorecard._count_to_dict`.
    """
    if count.count == 0:
        return {
            "count": 0,
            "event_index_range": {"first": None, "last": None},
        }
    return {
        "count": count.count,
        "event_index_range": {
            "first": count.first_index,
            "last": count.last_index,
        },
    }


def _count_from_dict(raw: dict[str, Any]) -> _Count:
    """Inverse of :func:`_count_to_dict`."""
    count = int(raw.get("count", 0)) if isinstance(raw, dict) else 0
    if count == 0:
        return _Count.empty()
    rng = raw.get("event_index_range", {}) if isinstance(raw, dict) else {}
    return _Count(
        count=count,
        first_index=int(rng.get("first")) if rng.get("first") is not None else None,
        last_index=int(rng.get("last")) if rng.get("last") is not None else None,
    )


def _build_count(positions: Sequence[int]) -> _Count:
    """Build a :class:`_Count` from ordered 0-based indices."""
    if not positions:
        return _Count.empty()
    return _Count(
        count=len(positions),
        first_index=positions[0],
        last_index=positions[-1],
    )


# ---------------------------------------------------------------------------
# Principal filtering helpers
# ---------------------------------------------------------------------------

#: Event fields that may carry the principal identifier.
#: Checked in order; the first non-empty value is the principal key.
_PRINCIPAL_FIELDS: tuple[str, ...] = (
    "principal_id",
    "agent_id",
    "principal",
    "agent",
    "completed_by",
    "performed_by",
)


def _principal_id_from_row(row: Mapping[str, Any]) -> str:
    """Return the principal identifier from *row*, or ``""`` when absent."""
    for field_name in _PRINCIPAL_FIELDS:
        val = row.get(field_name)
        if isinstance(val, str) and val:
            return val
    return ""


def _row_in_window(row: Mapping[str, Any], start: float, end: float) -> bool:
    """Return True when *row*'s ``ts`` falls within the window."""
    ts = row.get("ts")
    if ts is None:
        return False
    try:
        ts_float = float(ts)
    except (TypeError, ValueError):
        return False
    return start <= ts_float <= end


# ---------------------------------------------------------------------------
# Pure fold
# ---------------------------------------------------------------------------


def derive_conduct(
    events: Sequence[Mapping[str, Any]],
    *,
    principal_id: str,
    period_start: float,
    period_end: float,
    run_id: str = "",
) -> ConductProjection:
    """Fold journal rows into a :class:`ConductProjection` (pure function).

    The function is a *pure projection*: given the same row list and
    parameters, it returns the same document. It does not read the
    filesystem, the clock, or the process environment; the caller
    hands it the events :func:`bernstein.core.replay.journal.load_events`
    produced.

    The period is **inclusive** on both ends. Rows whose ``ts`` equals
    ``period_start`` or ``period_end`` are included.

    Args:
        events: Journal rows in append order, as returned by
            :func:`bernstein.core.replay.journal.load_events`.
        principal_id: The agent principal whose rows to project.
        period_start: Window start as unix epoch seconds.
        period_end: Window end as unix epoch seconds.
        run_id: Optional run id recorded on the projection.

    Returns:
        A :class:`ConductProjection` whose counts are paired with
        event-index ranges; a period with no rows for the agent is
        reported as ``UNVERIFIED`` coverage (not clean).
    """
    tool_call_positions: list[int] = []
    completed_positions: list[int] = []
    failed_positions: list[int] = []
    gated_positions: list[int] = []
    gated_honoured_positions: list[int] = []
    gated_overridden_positions: list[int] = []
    delegated_positions: list[int] = []
    ignored: set[str] = set()

    for index, row in enumerate(events):
        # Apply both filters: window + principal. A row that names no
        # principal is not attributable to the agent, so it is never
        # evidence of the agent's conduct - counting principal-less rows
        # would let a period of agent-less rows read as verified.
        if not _row_in_window(row, period_start, period_end):
            continue
        if _principal_id_from_row(row) != principal_id:
            continue

        event = str(row.get("event", ""))
        if event not in _FOLDED_EVENT_TYPES:
            if event:
                ignored.add(event)
            continue

        if event == EVENT_TOOL_CALL:
            tool_call_positions.append(index)
        elif event == EVENT_TASK_COMPLETED:
            completed_positions.append(index)
        elif event == EVENT_TASK_VERIFICATION_FAILED:
            failed_positions.append(index)
        elif event == EVENT_APPROVAL_GATE:
            gated_positions.append(index)
        elif event == EVENT_APPROVAL_HONOURED:
            gated_honoured_positions.append(index)
        elif event == EVENT_APPROVAL_OVERRIDDEN:
            gated_overridden_positions.append(index)
        elif event in (EVENT_TASK_DELEGATED, EVENT_TASK_SUBMITTED):
            delegated_positions.append(index)

    total_filtered = (
        len(tool_call_positions)
        + len(completed_positions)
        + len(failed_positions)
        + len(gated_positions)
        + len(gated_honoured_positions)
        + len(gated_overridden_positions)
        + len(delegated_positions)
    )

    # Coverage classification: no filtered rows -> UNVERIFIED (not clean).
    coverage = CompletionCoverageStatus.UNVERIFIED if total_filtered == 0 else CompletionCoverageStatus.VERIFIED

    return ConductProjection(
        schema_version=CONDUCT_FOLD_SCHEMA_VERSION,
        run_id=run_id,
        principal_id=principal_id,
        period_start=period_start,
        period_end=period_end,
        coverage=coverage,
        event_count=total_filtered,
        tool_calls=_build_count(tool_call_positions),
        completed=_build_count(completed_positions),
        failed=_build_count(failed_positions),
        gated=_build_count(gated_positions),
        gated_honoured=_build_count(gated_honoured_positions),
        gated_overridden=_build_count(gated_overridden_positions),
        delegated=_build_count(delegated_positions),
        ignored_event_types=tuple(sorted(ignored)),
    )


def derive_conduct_from_path(
    path: Any,
    *,
    principal_id: str,
    period_start: float,
    period_end: float,
    run_id: str | None = None,
) -> ConductProjection:
    """Load a journal and fold it into a :class:`ConductProjection`.

    Convenience wrapper around
    :func:`bernstein.core.replay.journal.load_events` and
    :func:`derive_conduct`. The path is read in tolerant mode so torn-tail
    detection can do its job; a strict reader would refuse before the
    projection surfaces the cause.

    Args:
        path: A filesystem path to a ``journal.jsonl`` file.
        principal_id: The agent principal whose rows to project.
        period_start: Window start as unix epoch seconds.
        period_end: Window end as unix epoch seconds.
        run_id: Optional run id; defaults to the journal file's parent
            directory name (the standard run-id naming).

    Returns:
        A :class:`ConductProjection`.

    Raises:
        ValueError: The journal does not exist, has no parseable events,
            or the tolerant reader had to discard rows at the tail.
    """
    from pathlib import Path

    from bernstein.core.replay.journal import JournalLoadResult, load_events

    journal_path = Path(path)
    if not journal_path.is_file():
        raise ValueError(f"journal not found at {journal_path}")

    loaded: JournalLoadResult = load_events(journal_path)
    if loaded.discarded_line_indices:
        joined = ", ".join(str(i) for i in loaded.discarded_line_indices)
        raise ValueError(
            f"refusing to conduct-fold {journal_path}: reader discarded physical line(s) "
            f"{joined}; the journal tail is torn or truncated"
        )

    resolved_run_id = run_id if run_id is not None else journal_path.parent.name
    return derive_conduct(
        loaded.events,
        principal_id=principal_id,
        period_start=period_start,
        period_end=period_end,
        run_id=resolved_run_id,
    )
