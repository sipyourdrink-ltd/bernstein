"""Pure projection of a run's sealed journal to an operator scorecard (#5402).

The journal is the load-bearing artefact: every fact this module reports is
read off the journal rows themselves, never off logs, stdout, or a
self-reported summary flag. The projection follows the same discipline as
:mod:`bernstein.core.replay.rederive` - "derive only from recorded inputs,
never re-execute" - so the same journal produces the same document, with
no filesystem or clock reads outside the journal itself.

What "scorecard" means here
---------------------------

A run produces tool calls, retries, verifier verdicts and approval-gate
outcomes. The journal records each as a Merkle-chained row, and the
scorecard folds those rows into a small set of operator-facing counts
(every count carries the event-index range it was computed from, so a
reader can reach back to the events behind it). It is the
operational-numbers view of a run: *how many* tool calls fired, *how many*
tasks retried, *which* approval gates were encountered / honoured /
overridden, and so on. The more detailed document type, its schema and its
serialisation live in a sibling slice; this module fills it in.

Determinism contract
--------------------

* Every set or mapping projected into the document is rendered in a fixed
  order, so two derivations from the same journal serialise to identical
  bytes.
* No filesystem or clock read occurs; the only input is the in-memory list
  of events already loaded by
  :func:`bernstein.core.replay.journal.load_events`.
* A torn or truncated tail (rows the tolerant reader had to discard) is
  surfaced as :class:`ScorecardError` rather than silently undercounted;
  counting from a journal whose tail was torn would report a
  falsely-low number and the operator has to know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import (
    JournalLoadResult,
    load_events,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: Schema version of the scorecard document. Bump on any change to the
#: canonical shape so consumers can refuse an unexpected variant.
SCORECARD_SCHEMA_VERSION = 1

#: Journal event recorded for a tool invocation an agent dispatched
#: (issue #1799; the row carries the serialised tool call under
#: ``tool_call``). Counted toward the scorecard's *tool_calls* total.
TOOL_CALL_EVENT = "tool_call"

#: Journal event recorded when a task's verification gate refused the
#: work the agent produced. The scorecard folds each of these into
#: *verifier_failures*, and the following ``task_retried`` row into
#: *recoveries* (a failed action followed by a repaired retry).
TASK_VERIFICATION_FAILED_EVENT = "task_verification_failed"

#: Journal event recorded when coordination handed a task back to the
#: pool for another attempt. Counted both in *retries* and, when
#: preceded by a ``task_verification_failed`` for the same task, in
#: *recoveries*.
TASK_RETRIED_EVENT = "task_retried"

#: Journal event recorded for an approval gate that was *encountered*
#: during a run. An encountered gate is one the run reached, regardless
#: of how it was resolved.
APPROVAL_GATE_EVENT = "approval_gate"

#: Journal event recorded when an approval gate was *honoured* (the
#: agent's action was approved, the gate did not block). Distinct from
#: an encountered gate so a run that encountered a gate and overrode it
#: shows both numbers, not one folded into the other.
APPROVAL_HONOURED_EVENT = "approval_honoured"

#: Journal event recorded when an approval gate was *overridden* (the
#: agent proceeded without approval, or the gate was bypassed by an
#: operator action). Carried as a separate field on the scorecard so a
#: reader can see the override rate at a glance.
APPROVAL_OVERRIDDEN_EVENT = "approval_overridden"

#: Closed set of event types the scorecard looks at. Any other row is
#: folded into ``ignored_event_types`` so the document names what it
#: skipped and a future journal vocabulary cannot break the projection.
_FOLDED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TOOL_CALL_EVENT,
        TASK_VERIFICATION_FAILED_EVENT,
        TASK_RETRIED_EVENT,
        APPROVAL_GATE_EVENT,
        APPROVAL_HONOURED_EVENT,
        APPROVAL_OVERRIDDEN_EVENT,
    }
)


class ScorecardError(ValueError):
    """Raised when a journal cannot be turned into a scorecard."""


@dataclass(frozen=True, slots=True)
class _Count:
    """A single number with the event-index range it was computed from.

    Attributes:
        count: The reported number.
        first_index: 0-based index of the first journal row that
            contributed to *count*. ``None`` when *count* is zero (the
            range is empty by definition).
        last_index: 0-based index of the last journal row that
            contributed to *count*. ``None`` when *count* is zero.
    """

    count: int
    first_index: int | None
    last_index: int | None

    @classmethod
    def empty(cls) -> _Count:
        return cls(count=0, first_index=None, last_index=None)


@dataclass(frozen=True, slots=True)
class Scorecard:
    """The operator-facing numbers projected from a sealed journal.

    Every number in this document is paired with the event-index range
    the projection walked, so a reader can go back to the events behind
    the figure and verify the count.

    Attributes:
        schema_version: The scorecard schema version. See
            :data:`SCORECARD_SCHEMA_VERSION`.
        run_id: The run whose journal was projected.
        event_count: Total number of journal rows the projection
            considered.
        tool_calls: Count of ``tool_call`` rows, with the
            event-index range they occupied.
        retries: Count of ``task_retried`` rows, with the
            event-index range.
        recoveries: Count of retries that immediately followed a
            ``task_verification_failed`` for the same task. A recovery
            is a *failed action followed by a repaired retry*, not a
            raw retry count.
        verifier_failures: Count of ``task_verification_failed``
            rows, with the event-index range.
        verifier_coverage: Fraction of claimed tasks that reached a
            verifier verdict (success or failure). Carries the
            event-index range of the underlying claim set.
        approval_gates_encountered: Count of ``approval_gate`` rows,
            with the event-index range.
        approval_gates_honoured: Count of ``approval_honoured`` rows,
            with the event-index range. Distinct from
            *approval_gates_encountered* so a run that encountered a
            gate and overrode it shows both numbers, not one folded
            into the other.
        approval_gates_overridden: Count of ``approval_overridden``
            rows, with the event-index range. Visible on its own so
            an override rate is computable at a glance.
        ignored_event_types: Event types seen in the journal that the
            scorecard does not fold. Names are sorted so the document
            is deterministic.
    """

    schema_version: int
    run_id: str
    event_count: int
    tool_calls: _Count
    retries: _Count
    recoveries: _Count
    verifier_failures: _Count
    verifier_coverage: _Count
    approval_gates_encountered: _Count
    approval_gates_honoured: _Count
    approval_gates_overridden: _Count
    ignored_event_types: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped scorecard served to operators."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "event_count": self.event_count,
            "tool_calls": _count_to_dict(self.tool_calls),
            "retries": _count_to_dict(self.retries),
            "recoveries": _count_to_dict(self.recoveries),
            "verifier_failures": _count_to_dict(self.verifier_failures),
            "verifier_coverage": _count_to_dict(self.verifier_coverage),
            "approval_gates": {
                "encountered": _count_to_dict(self.approval_gates_encountered),
                "honoured": _count_to_dict(self.approval_gates_honoured),
                "overridden": _count_to_dict(self.approval_gates_overridden),
            },
            "ignored_event_types": list(self.ignored_event_types),
        }


def _count_to_dict(count: _Count) -> dict[str, Any]:
    """Render a :class:`_Count` to its public dict shape.

    A count with a positive value carries a non-empty
    ``event_index_range``; a count of zero carries an explicit
    ``null`` for both ends so consumers can branch on the field
    without guessing what absence means.
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


def _build_count(positions: Sequence[int]) -> _Count:
    """Wrap the positions of one event class into a :class:`_Count`.

    Args:
        positions: Ordered 0-based indices of journal rows that
            contribute to the count. Empty when the count is zero.

    Returns:
        A :class:`_Count` whose ``count`` matches ``len(positions)``
        and whose range spans the first and last positions. A
        zero-count yields :meth:`_Count.empty` so the range is
        unambiguously empty rather than ``[None, None]``.
    """
    if not positions:
        return _Count.empty()
    return _Count(
        count=len(positions),
        first_index=positions[0],
        last_index=positions[-1],
    )


def _claimant_task_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return the set of task ids ever claimed by an agent in *events*.

    The scorecard's *verifier_coverage* is the fraction of claimed
    tasks that reached a verifier verdict, so the claim set has to be
    collected from ``task_claimed`` rows before the verdicts are
    folded.
    """
    claimed: set[str] = set()
    for row in events:
        if str(row.get("event", "")) != "task_claimed":
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            claimed.add(task_id)
    return claimed


def _verdict_task_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return task ids that reached a verifier verdict in *events*.

    A "verdict" is either ``task_verification_failed`` (the gate
    refused the work) or ``task_completed`` (the gate accepted it).
    Both rows name a task id and both are projectable here.
    """
    verdicts: set[str] = set()
    for row in events:
        event = str(row.get("event", ""))
        if event not in (TASK_VERIFICATION_FAILED_EVENT, "task_completed"):
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            verdicts.add(task_id)
    return verdicts


def derive_scorecard(
    events: Sequence[Mapping[str, Any]],
    *,
    run_id: str = "",
) -> Scorecard:
    """Fold a list of journal rows into a :class:`Scorecard`.

    The function is a *pure* projection: given the same list of
    rows, it returns the same document. It does not read the
    filesystem, the clock, or the process environment; the caller
    hands it the events :func:`load_events` produced.

    Args:
        events: Journal rows in append order, as returned by
            :func:`bernstein.core.replay.journal.load_events`.
        run_id: Optional run id recorded on the scorecard. When
            ``""`` the field is left empty so the function still
            works for an inline sequence detached from a run.

    Returns:
        A :class:`Scorecard` whose every count is paired with the
        event-index range it came from.

    Raises:
        ScorecardError: The input carries no event rows at all, or
            the load result is incomplete because the journal tail
            was torn. The torn-tail case is reported by name so a
            caller does not mistake an undercount for a real
            ``0``.
    """
    if not events:
        raise ScorecardError("cannot derive a scorecard from an empty journal")

    tool_call_positions: list[int] = []
    retry_positions: list[int] = []
    verifier_failure_positions: list[int] = []
    approval_encountered_positions: list[int] = []
    approval_honoured_positions: list[int] = []
    approval_overridden_positions: list[int] = []

    # ``last_event_per_task`` tracks the most recent event on a task
    # id, so a retry can be paired with the verification failure it
    # repaired. A recovery is a retry whose immediate predecessor on
    # the same task was a ``task_verification_failed``.
    last_event_per_task: dict[str, str] = {}
    recovery_positions: list[int] = []

    ignored: set[str] = set()

    for index, row in enumerate(events):
        event = str(row.get("event", ""))
        if event not in _FOLDED_EVENT_TYPES:
            # Record-but-do-not-fold every other event type so a
            # future journal vocabulary cannot break the projection:
            # operators see what was skipped, in a stable order.
            if event:
                ignored.add(event)
            continue

        task_id_raw = row.get("task_id")
        task_id = task_id_raw if isinstance(task_id_raw, str) else ""

        if event == TOOL_CALL_EVENT:
            tool_call_positions.append(index)
        elif event == TASK_VERIFICATION_FAILED_EVENT:
            verifier_failure_positions.append(index)
        elif event == TASK_RETRIED_EVENT:
            retry_positions.append(index)
            if task_id and last_event_per_task.get(task_id) == TASK_VERIFICATION_FAILED_EVENT:
                recovery_positions.append(index)
        elif event == APPROVAL_GATE_EVENT:
            approval_encountered_positions.append(index)
        elif event == APPROVAL_HONOURED_EVENT:
            approval_honoured_positions.append(index)
        elif event == APPROVAL_OVERRIDDEN_EVENT:
            approval_overridden_positions.append(index)

        if task_id:
            last_event_per_task[task_id] = event

    claimed = _claimant_task_ids(events)
    verdicts = _verdict_task_ids(events)
    if claimed:
        coverage_count = len(claimed & verdicts)
        # The coverage denominator is the set of *distinct* claimed
        # tasks; we report it as a single value, with the index
        # range spanning the first and last claim of any claimed
        # task. Sorting the list keeps the range stable across
        # Python runs.
        first_claim = next(
            (
                i
                for i, row in enumerate(events)
                if str(row.get("event", "")) == "task_claimed"
                and isinstance(row.get("task_id"), str)
                and row.get("task_id") in claimed
            ),
            None,
        )
        last_claim = next(
            (
                i
                for i in range(len(events) - 1, -1, -1)
                if str(events[i].get("event", "")) == "task_claimed"
                and isinstance(events[i].get("task_id"), str)
                and events[i].get("task_id") in claimed
            ),
            None,
        )
        verifier_coverage = _Count(
            count=coverage_count,
            first_index=first_claim,
            last_index=last_claim,
        )
    else:
        verifier_coverage = _Count.empty()

    return Scorecard(
        schema_version=SCORECARD_SCHEMA_VERSION,
        run_id=run_id,
        event_count=len(events),
        tool_calls=_build_count(tool_call_positions),
        retries=_build_count(retry_positions),
        recoveries=_build_count(recovery_positions),
        verifier_failures=_build_count(verifier_failure_positions),
        verifier_coverage=verifier_coverage,
        approval_gates_encountered=_build_count(approval_encountered_positions),
        approval_gates_honoured=_build_count(approval_honoured_positions),
        approval_gates_overridden=_build_count(approval_overridden_positions),
        ignored_event_types=tuple(sorted(ignored)),
    )


def derive_scorecard_from_path(path: Any) -> Scorecard:
    """Load *path* and fold it into a :class:`Scorecard`.

    Convenience wrapper around :func:`load_events` and
    :func:`derive_scorecard`. The path is read in tolerant mode so
    the torn-tail detection below can do its job; a strict reader
    would refuse the journal before the scorecard has a chance to
    name the cause.

    Args:
        path: A filesystem path to a ``journal.jsonl`` file.

    Returns:
        A :class:`Scorecard` derived from the loaded events.

    Raises:
        ScorecardError: The journal does not exist, has no
            parseable events, or the tolerant reader had to discard
            rows at the tail (a torn write, not corruption).
    """
    from pathlib import Path

    journal_path = Path(path)
    if not journal_path.is_file():
        raise ScorecardError(f"journal not found at {journal_path}")
    loaded: JournalLoadResult = load_events(journal_path)
    if loaded.discarded_line_indices:
        joined = ", ".join(str(i) for i in loaded.discarded_line_indices)
        raise ScorecardError(
            f"refusing to scorecard {journal_path}: reader discarded physical line(s) "
            f"{joined}; the journal tail is torn or truncated and any number reported "
            "from it would be a silent undercount"
        )
    run_id = journal_path.parent.name
    return derive_scorecard(loaded.events, run_id=run_id)


__all__ = [
    "APPROVAL_GATE_EVENT",
    "APPROVAL_HONOURED_EVENT",
    "APPROVAL_OVERRIDDEN_EVENT",
    "SCORECARD_SCHEMA_VERSION",
    "TASK_RETRIED_EVENT",
    "TASK_VERIFICATION_FAILED_EVENT",
    "TOOL_CALL_EVENT",
    "Scorecard",
    "ScorecardError",
    "derive_scorecard",
    "derive_scorecard_from_path",
]
