"""Composable trigger semantics as deterministic folds (#2548).

Covers acceptance criteria:
  * Determinism - replaying a slice re-derives every threshold, absence, and
    sequence fire decision identically.
  * Sequence rules fire only on provable lineage descent, never on wall-clock
    order between unrelated events.
"""

from __future__ import annotations

from bernstein.core.events.grammar import CanonicalEvent
from bernstein.core.events.triggers import (
    AbsenceExpectation,
    CompoundRule,
    SequenceRule,
    ThresholdRule,
    evaluate_absence,
    evaluate_compound,
    evaluate_sequence,
    evaluate_threshold,
    resource_descends,
)


def _event(
    position: int,
    label: str,
    resource_id: str,
    *,
    related: tuple[str, ...] = (),
    actor: str = "actor",
) -> CanonicalEvent:
    return CanonicalEvent(
        position=position,
        hmac=f"hmac{position:04d}",
        prev_hmac=f"hmac{position - 1:04d}" if position else "0" * 64,
        resource_id=resource_id,
        label=label,
        related_resource_ids=related,
        payload_digest="sha256:" + "0" * 64,
        timestamp=f"2026-07-17T00:00:{position:02d}.000000Z",
        actor=actor,
    )


def test_threshold_fires_once_per_crossing() -> None:
    events = [
        _event(0, "gate.result", "gate_a"),
        _event(1, "gate.result", "gate_a"),
        _event(2, "gate.result", "gate_a"),
        _event(3, "gate.result", "gate_a"),
    ]
    rule = ThresholdRule(label="gate.result", count=3, window=10)
    fires = evaluate_threshold(rule, events)
    assert len(fires) == 1
    assert fires[0].matched_hmacs == ("hmac0000", "hmac0001", "hmac0002")
    assert fires[0].trigger_position == 2


def test_threshold_window_evicts_old_events() -> None:
    events = [
        _event(0, "gate.result", "g"),
        _event(1, "gate.result", "g"),
        _event(9, "gate.result", "g"),  # positions 0,1 fall outside a window of 3
    ]
    rule = ThresholdRule(label="gate.result", count=3, window=3)
    assert evaluate_threshold(rule, events) == []


def test_threshold_for_each_buckets_independently() -> None:
    events = [
        _event(0, "gate.result", "adapter_v1", actor="v1"),
        _event(1, "gate.result", "adapter_v1", actor="v1"),
        _event(2, "gate.result", "adapter_v2", actor="v2"),
        _event(3, "gate.result", "adapter_v1", actor="v1"),
    ]
    rule = ThresholdRule(label="gate.result", count=3, window=10, for_each=("actor",))
    fires = evaluate_threshold(rule, events)
    assert len(fires) == 1
    assert fires[0].bucket == ("v1",)


def test_threshold_is_deterministic_across_replays() -> None:
    events = [_event(i, "gate.result", "g") for i in range(6)]
    rule = ThresholdRule(label="gate.result", count=2, window=100)
    first = evaluate_threshold(rule, events)
    second = evaluate_threshold(rule, events)
    assert first == second


def test_resource_descends_follows_lineage_edges() -> None:
    events = [
        _event(0, "run.started", "run_1"),
        _event(1, "task.created", "task_a", related=("run_1",)),
        _event(2, "gate.result", "gate_1", related=("task_a",)),
    ]
    index = {"run_1": set(), "task_a": {"run_1"}, "gate_1": {"task_a"}}
    assert resource_descends("gate_1", "run_1", index)
    assert not resource_descends("run_1", "gate_1", index)
    assert not resource_descends("gate_1", "unrelated", index)
    del events


def test_sequence_fires_only_on_lineage_descent() -> None:
    # task_a spawns gate_1 (descent). unrelated_gate has no lineage path to task_a
    # even though it arrives later in wall-clock/chain order.
    events = [
        _event(0, "task.created", "task_a"),
        _event(1, "gate.result", "gate_1", related=("task_a",)),
        _event(2, "gate.result", "unrelated_gate", related=("other_task",)),
    ]
    rule = SequenceRule(earlier="task.created", later="gate.result")
    fires = evaluate_sequence(rule, events)
    assert len(fires) == 1
    assert fires[0].earlier_hmac == "hmac0000"
    assert fires[0].later_hmac == "hmac0001"


def test_sequence_ignores_pure_wall_clock_order() -> None:
    events = [
        _event(0, "task.created", "task_a"),
        _event(1, "gate.result", "gate_z", related=("some_other_root",)),
    ]
    rule = SequenceRule(earlier="task.created", later="gate.result")
    assert evaluate_sequence(rule, events) == []


def test_sequence_ignores_future_lineage_edges() -> None:
    # The B->A edge only appears at position 2, after the "b" event at position 1.
    # A causal fold must not fire the (A@0, b@1) pair on an edge from the future.
    events = [
        _event(0, "task.created", "task_a"),
        _event(1, "gate.result", "gate_b", related=()),
        _event(2, "cost.update", "gate_b", related=("task_a",)),
    ]
    rule = SequenceRule(earlier="task.created", later="gate.result")
    # Fixed (causal index): no fire. Bug (merged future edges): one spurious fire.
    assert evaluate_sequence(rule, events) == []


def test_absence_emits_violation_when_expected_never_arrives() -> None:
    events = [
        _event(0, "run.started", "run_1"),
        _event(1, "cost.update", "run_1"),
        _event(2, "cost.update", "run_1"),
    ]
    # within=2 so the deadline (position 2) is actually observed by the slice.
    expectation = AbsenceExpectation(after="run.started", expect="run.completed", within=2)
    violations = evaluate_absence(expectation, events)
    assert len(violations) == 1
    assert violations[0].after_hmac == "hmac0000"
    assert violations[0].to_hmac == "hmac0002"


def test_absence_defers_when_deadline_not_observed() -> None:
    # The slice ends (position 2) before the absence deadline (position 5) is
    # reached, so no negative proof can be asserted yet -> defer (#2653).
    events = [
        _event(0, "run.started", "run_1"),
        _event(1, "cost.update", "run_1"),
        _event(2, "cost.update", "run_1"),
    ]
    expectation = AbsenceExpectation(after="run.started", expect="run.completed", within=5)
    assert evaluate_absence(expectation, events) == []


def test_absence_uses_only_causal_lineage_edges() -> None:
    # B (position 1) has no lineage to anchor A at its own position; a later
    # event (position 2) introduces the B->A edge. A causal descent check must
    # NOT let that future edge satisfy the expectation, so the violation stands.
    events = [
        _event(0, "a", "A"),
        _event(1, "b", "B"),
        _event(2, "b", "B", related=("A",)),
    ]
    expectation = AbsenceExpectation(after="a", expect="b", within=1, require_descent=True)
    violations = evaluate_absence(expectation, events)
    # Fixed (causal index): B@1 does not descend from A yet -> 1 violation.
    # Bug (future edges merged): B@1 would descend from A -> 0 violations.
    assert len(violations) == 1
    assert violations[0].after_hmac == "hmac0000"
    assert violations[0].to_hmac == "hmac0001"


def test_absence_satisfied_when_expected_arrives() -> None:
    events = [
        _event(0, "run.started", "run_1"),
        _event(1, "run.completed", "run_1", related=("run_1",)),
    ]
    expectation = AbsenceExpectation(after="run.started", expect="run.completed", within=5)
    assert evaluate_absence(expectation, events) == []


def test_absence_is_deterministic_across_replays() -> None:
    events = [_event(0, "run.started", "r"), _event(1, "cost.update", "r")]
    expectation = AbsenceExpectation(after="run.started", expect="run.completed", within=3)
    assert evaluate_absence(expectation, events) == evaluate_absence(expectation, events)


def test_compound_all_requires_every_condition() -> None:
    events = [_event(0, "gate.result", "g"), _event(1, "cost.update", "c")]
    rule = CompoundRule(mode="all", conditions=("gate.result", "cost.update"))
    fire = evaluate_compound(rule, events)
    assert fire is not None
    assert fire.satisfied_conditions == ("gate.result", "cost.update")

    rule_missing = CompoundRule(mode="all", conditions=("gate.result", "merge.completed"))
    assert evaluate_compound(rule_missing, events) is None


def test_compound_any_and_count_modes() -> None:
    events = [_event(0, "gate.result", "g")]
    assert evaluate_compound(CompoundRule(mode="any", conditions=("gate.result", "x.y")), events) is not None
    assert evaluate_compound(CompoundRule(mode="count", conditions=("gate.result", "x.y"), threshold=2), events) is None
