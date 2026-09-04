"""Focused pytest coverage for the run-journal scorecard projection (#5402).

The scorecard is the operator-numbers view of a sealed journal: every count
is paired with the event-index range it was computed from so a reader can
walk back to the rows behind the figure. These tests pin the projection's
contract: counts, ranges, deterministic serialisation, separation of
encountered / honoured / overridden approval gates, and the safety rails
that refuse to scorecard a torn tail.

The :class:`EventJournal` fixture mirrors the one in
:mod:`tests.unit.test_replay`; this file is intentionally standalone so the
scorecard tests can be discovered and run on their own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.scorecard import (
    APPROVAL_GATE_EVENT,
    APPROVAL_HONOURED_EVENT,
    APPROVAL_OVERRIDDEN_EVENT,
    SCORECARD_SCHEMA_VERSION,
    TASK_RETRIED_EVENT,
    TASK_VERIFICATION_FAILED_EVENT,
    TOOL_CALL_EVENT,
    Scorecard,
    ScorecardError,
    derive_scorecard,
    derive_scorecard_from_path,
)

# ---------------------------------------------------------------------------
# EventJournal fixture
# ---------------------------------------------------------------------------


def _scorecard_journal(sdd_dir: Path) -> EventJournal:
    """Build a journal whose every interesting event class is exercised at least once.

    Indices are deliberately interleaved so a fold that mis-reads positions
    (e.g. by looking at *all* of one event before the others) shows up
    immediately as a wrong range.
    """
    journal = EventJournal(run_id="run-sc", sdd_dir=sdd_dir)
    journal.record("run_started", run_id="run-sc", max_agents=2)
    journal.record("plan.graph.full", goal="ship it", nodes=[], task_count=0)
    journal.record("task_claimed", task_id="T-A", agent_id="a-1")
    journal.record(TOOL_CALL_EVENT, task_id="T-A", tool_name="Read")
    journal.record(TASK_VERIFICATION_FAILED_EVENT, task_id="T-A", failed_signals=["lint"])
    journal.record(TASK_RETRIED_EVENT, task_id="T-A")
    journal.record("task_claimed", task_id="T-A", agent_id="a-1")
    journal.record(APPROVAL_GATE_EVENT, task_id="T-A", gate="deploy")
    journal.record(APPROVAL_OVERRIDDEN_EVENT, task_id="T-A", gate="deploy")
    journal.record("task_completed", task_id="T-A", agent_id="a-1", cost_usd=0.1)
    journal.record("task_claimed", task_id="T-B", agent_id="a-2")
    journal.record(TOOL_CALL_EVENT, task_id="T-B", tool_name="Edit")
    journal.record(APPROVAL_GATE_EVENT, task_id="T-B", gate="merge")
    journal.record(APPROVAL_HONOURED_EVENT, task_id="T-B", gate="merge")
    journal.record("task_completed", task_id="T-B", agent_id="a-2", cost_usd=0.2)
    journal.record("run_completed", run_id="run-sc", ticks=2, outcome="completed")
    return journal


# ---------------------------------------------------------------------------
# Count + event-index range contract
# ---------------------------------------------------------------------------


def test_derive_scorecard_folds_tool_call_count_with_event_index_range() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.tool_calls.count == 3
    assert card.tool_calls.first_index == 1
    assert card.tool_calls.last_index == 3


def test_derive_scorecard_empty_count_carries_null_range() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_started", "run_id": "r"},
        {"event": "run_completed", "run_id": "r"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.tool_calls.count == 0
    assert card.tool_calls.first_index is None
    assert card.tool_calls.last_index is None
    # Same null-pair shape for every other zero count.
    assert card.retries.first_index is None
    assert card.recoveries.last_index is None
    assert card.verifier_failures.first_index is None
    assert card.approval_gates_encountered.first_index is None
    assert card.approval_gates_honoured.last_index is None
    assert card.approval_gates_overridden.first_index is None


def test_derive_scorecard_retry_count_and_range() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_RETRIED_EVENT, "task_id": "T-A"},
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_RETRIED_EVENT, "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.retries.count == 2
    assert card.retries.first_index == 1
    assert card.retries.last_index == 3


def test_derive_scorecard_recoveries_only_count_retries_after_verification_failure() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-A", "failed_signals": ["lint"]},
        # Recovery 1: retry immediately follows a verification failure for the same task.
        {"event": TASK_RETRIED_EVENT, "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
        {"event": "task_claimed", "task_id": "T-B"},
        # No preceding verification failure for T-B; not a recovery.
        {"event": TASK_RETRIED_EVENT, "task_id": "T-B"},
        {"event": "task_completed", "task_id": "T-B"},
        {"event": "task_claimed", "task_id": "T-C"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-C", "failed_signals": ["type"]},
        {"event": "task_completed", "task_id": "T-C"},
        {"event": "task_claimed", "task_id": "T-C"},
        # Recovery 2: another retry on T-C after the failed attempt.
        {"event": TASK_RETRIED_EVENT, "task_id": "T-C"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.verifier_failures.count == 2
    assert card.retries.count == 3
    assert card.recoveries.count == 2
    # Recovery range should be the indices of the two retry rows that
    # closed a verification failure, in append order. T-A's retry at
    # index 2 follows a failure on T-A (index 1); T-C's retry at
    # index 11 follows a failure on T-C (index 8). T-B's retry at
    # index 5 has no preceding failure on the same task, so it is
    # excluded.
    assert card.recoveries.first_index == 2
    assert card.recoveries.last_index == 11


def test_derive_scorecard_recoveries_do_not_cross_task_boundaries() -> None:
    """A retry only counts as a recovery if the failure is the *same* task's prior event."""
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-A", "failed_signals": ["lint"]},
        # Last event on T-A is a failure, but this retry is for T-B; not a recovery.
        {"event": TASK_RETRIED_EVENT, "task_id": "T-B"},
        {"event": "task_completed", "task_id": "T-B"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.retries.count == 1
    assert card.recoveries.count == 0


def test_derive_scorecard_verifier_failures_count_and_range() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-A", "failed_signals": ["lint"]},
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-A", "failed_signals": ["type"]},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.verifier_failures.count == 2
    assert card.verifier_failures.first_index == 1
    assert card.verifier_failures.last_index == 3


# ---------------------------------------------------------------------------
# Verifier coverage
# ---------------------------------------------------------------------------


def test_derive_scorecard_verifier_coverage_is_fraction_of_claimed_tasks_with_verdicts() -> None:
    events: list[dict[str, Any]] = [
        # T-A: claimed, then fails verification, then completes. Both rows count as verdicts.
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TASK_VERIFICATION_FAILED_EVENT, "task_id": "T-A", "failed_signals": ["lint"]},
        {"event": "task_completed", "task_id": "T-A"},
        # T-B: claimed and completed. One verdict.
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": "task_completed", "task_id": "T-B"},
        # T-C: claimed but never reaches a verdict.
        {"event": "task_claimed", "task_id": "T-C"},
    ]
    card = derive_scorecard(events, run_id="r")

    # Three distinct tasks claimed; two reached a verdict (T-A via failure+complete, T-B via complete).
    # T-C never reached task_completed or task_verification_failed, so it stays out of coverage.
    assert card.verifier_coverage.count == 2
    # The range covers every claim row across the three tasks.
    assert card.verifier_coverage.first_index == 0
    assert card.verifier_coverage.last_index == 5


def test_derive_scorecard_verifier_coverage_zero_when_nothing_claimed() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_started", "run_id": "r"},
        {"event": "run_completed", "run_id": "r"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.verifier_coverage.count == 0
    assert card.verifier_coverage.first_index is None
    assert card.verifier_coverage.last_index is None


# ---------------------------------------------------------------------------
# Approval gates: encountered / honoured / overridden stay separate
# ---------------------------------------------------------------------------


def test_derive_scorecard_approval_gates_are_distinct_counts_not_folded() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {APPROVAL_GATE_EVENT: APPROVAL_GATE_EVENT, "event": APPROVAL_GATE_EVENT, "task_id": "T-A", "gate": "deploy"},
        {
            APPROVAL_OVERRIDDEN_EVENT: APPROVAL_OVERRIDDEN_EVENT,
            "event": APPROVAL_OVERRIDDEN_EVENT,
            "task_id": "T-A",
            "gate": "deploy",
        },
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-B", "gate": "merge"},
        {"event": APPROVAL_HONOURED_EVENT, "task_id": "T-B", "gate": "merge"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.approval_gates_encountered.count == 2
    assert card.approval_gates_honoured.count == 1
    assert card.approval_gates_overridden.count == 1
    # Honoured + overridden are both subsets of encountered; the document
    # must show them separately rather than folding them into one number.
    assert (
        card.approval_gates_honoured.count + card.approval_gates_overridden.count
        <= card.approval_gates_encountered.count
    )


def test_derive_scorecard_approval_gates_honoured_range_spans_only_honoured_rows() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-A", "gate": "deploy"},
        {"event": APPROVAL_OVERRIDDEN_EVENT, "task_id": "T-A", "gate": "deploy"},
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-B", "gate": "merge"},
        {"event": APPROVAL_HONOURED_EVENT, "task_id": "T-B", "gate": "merge"},
        {"event": "task_claimed", "task_id": "T-C"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-C", "gate": "promote"},
        {"event": APPROVAL_HONOURED_EVENT, "task_id": "T-C", "gate": "promote"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.approval_gates_honoured.count == 2
    assert card.approval_gates_honoured.first_index == 5
    assert card.approval_gates_honoured.last_index == 8


def test_derive_scorecard_approval_gates_overridden_range_spans_only_overridden_rows() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-A", "gate": "deploy"},
        {"event": APPROVAL_OVERRIDDEN_EVENT, "task_id": "T-A", "gate": "deploy"},
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-B", "gate": "merge"},
        {"event": APPROVAL_HONOURED_EVENT, "task_id": "T-B", "gate": "merge"},
        {"event": "task_claimed", "task_id": "T-C"},
        {"event": APPROVAL_GATE_EVENT, "task_id": "T-C", "gate": "promote"},
        {"event": APPROVAL_OVERRIDDEN_EVENT, "task_id": "T-C", "gate": "promote"},
    ]
    card = derive_scorecard(events, run_id="r")

    assert card.approval_gates_overridden.count == 2
    assert card.approval_gates_overridden.first_index == 2
    assert card.approval_gates_overridden.last_index == 8


# ---------------------------------------------------------------------------
# Ignored event types
# ---------------------------------------------------------------------------


def test_derive_scorecard_ignored_event_types_is_sorted_list() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_started", "run_id": "r"},
        {"event": "plan.graph.full", "goal": "ship", "task_count": 0},
        {"event": "run_completed", "run_id": "r"},
        {"event": "z_unknown_type", "task_id": "T-A"},
        {"event": "a_unknown_type", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    # Names are sorted for a deterministic document.
    assert card.ignored_event_types == (
        "a_unknown_type",
        "plan.graph.full",
        "run_completed",
        "run_started",
        "z_unknown_type",
    )


def test_derive_scorecard_ignored_event_types_excludes_folded_types() -> None:
    events: list[dict[str, Any]] = [
        {"event": "run_started", "run_id": "r"},
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-A"},
    ]
    card = derive_scorecard(events, run_id="r")

    # Folded types must not appear in the ignored list. The scorecard
    # only folds the six tool/verifier/approval event types; every
    # other event is recorded-but-not-folded into ignored_event_types.
    assert TOOL_CALL_EVENT not in card.ignored_event_types
    assert TASK_RETRIED_EVENT not in card.ignored_event_types
    assert TASK_VERIFICATION_FAILED_EVENT not in card.ignored_event_types
    assert APPROVAL_GATE_EVENT not in card.ignored_event_types
    assert APPROVAL_HONOURED_EVENT not in card.ignored_event_types
    assert APPROVAL_OVERRIDDEN_EVENT not in card.ignored_event_types
    # Non-folded events (run_started, task_claimed, task_completed) are listed.
    assert "run_started" in card.ignored_event_types
    assert "task_claimed" in card.ignored_event_types
    assert "task_completed" in card.ignored_event_types


# ---------------------------------------------------------------------------
# Empty journal and torn-tail guards
# ---------------------------------------------------------------------------


def test_derive_scorecard_rejects_an_empty_event_list() -> None:
    with pytest.raises(ScorecardError):
        derive_scorecard([], run_id="r")


def test_derive_scorecard_from_path_raises_when_path_missing(tmp_path: Path) -> None:
    with pytest.raises(ScorecardError):
        derive_scorecard_from_path(tmp_path / "no-such-journal.jsonl")


def test_derive_scorecard_from_path_raises_for_torn_journal_tail(tmp_path: Path) -> None:
    journal = _scorecard_journal(tmp_path / ".sdd")
    raw = journal.path.read_bytes()
    cut = raw.rfind(b"\n")
    # Truncate the last physical line mid-record so the tolerant reader
    # has to discard it; the scorecard must refuse to count from a torn
    # tail rather than silently undercount.
    journal.path.write_bytes(raw[: cut + 1] + b'{"event": "tool_call", "task_id":')

    with pytest.raises(ScorecardError) as excinfo:
        derive_scorecard_from_path(journal.path)
    message = str(excinfo.value).lower()
    assert "torn" in message or "truncated" in message


# ---------------------------------------------------------------------------
# Determinism, purity, schema
# ---------------------------------------------------------------------------


def test_derive_scorecard_is_deterministic_byte_identical() -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
        {"event": "task_claimed", "task_id": "T-B"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-B"},
        {"event": "task_completed", "task_id": "T-A"},
        {"event": "task_completed", "task_id": "T-B"},
    ]

    card_one = derive_scorecard(events, run_id="r")
    card_two = derive_scorecard(events, run_id="r")

    assert card_one.to_dict() == card_two.to_dict()
    assert json.dumps(card_one.to_dict(), sort_keys=True) == json.dumps(card_two.to_dict(), sort_keys=True)


def test_derive_scorecard_is_pure_no_filesystem_access(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = [
        {"event": "task_claimed", "task_id": "T-A"},
        {"event": TOOL_CALL_EVENT, "task_id": "T-A"},
    ]

    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("derive_scorecard must not touch the filesystem")

    monkeypatch.setattr(Path, "is_file", _explode)
    monkeypatch.setattr(Path, "read_bytes", _explode)
    monkeypatch.setattr(Path, "read_text", _explode)

    # If any of the patched paths were touched, the call below would
    # raise AssertionError; the assertion is the test.
    card = derive_scorecard(events, run_id="r")
    assert card.tool_calls.count == 1


def test_derive_scorecard_scorecard_schema_version_is_pinned() -> None:
    events: list[dict[str, Any]] = [{"event": "run_started", "run_id": "r"}]
    card = derive_scorecard(events, run_id="r")

    assert card.schema_version == SCORECARD_SCHEMA_VERSION
    assert card.schema_version >= 1


# ---------------------------------------------------------------------------
# End-to-end: write a journal, derive from the file
# ---------------------------------------------------------------------------


def test_derive_scorecard_from_path_end_to_end(tmp_path: Path) -> None:
    journal = _scorecard_journal(tmp_path / ".sdd")

    card = derive_scorecard_from_path(journal.path)

    assert isinstance(card, Scorecard)
    assert card.run_id == "run-sc"
    assert card.tool_calls.count == 2
    assert card.approval_gates_encountered.count == 2
    assert card.approval_gates_honoured.count == 1
    assert card.approval_gates_overridden.count == 1
    assert card.retries.count == 1
    assert card.recoveries.count == 1
