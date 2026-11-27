"""Tests for the pure reconcile apply identity and observation decision."""

from __future__ import annotations

from dataclasses import replace

import pytest

from bernstein.core.govern.plan_models import canonical_digest
from bernstein.core.govern.reconcile import apply_reconcile_diff
from bernstein.core.govern.reconcile_models import (
    DiffAction,
    EntityKind,
    EntityStatus,
    ReconcileDiff,
    ReconcileEntry,
    SnapshotEntity,
)
from bernstein.core.security.change_receipt import ChangeReceipt


def _change() -> ReconcileEntry:
    return ReconcileEntry(
        kind=EntityKind.LANE,
        entity_id="default",
        status=EntityStatus.CHANGED,
        action=DiffAction.MUTATE,
        declared_value="1.5",
        observed_value="1.0",
        observed_at=1,
        evidence_ref="fixture",
    )


def test_idempotency_key_tracks_intent_without_rewriting_signed_diff() -> None:
    change = _change()
    same_intent = replace(change, observed_value="1.25", observed_at=2, evidence_ref="new fixture")
    assert same_intent.idempotency_key == change.idempotency_key
    assert ReconcileEntry.from_dict(change.to_dict()).idempotency_key == change.idempotency_key
    assert "idempotency_key" not in change.to_dict()
    assert replace(change, entity_id="other").idempotency_key != change.idempotency_key
    assert replace(change, kind=EntityKind.CAPABILITY).idempotency_key != change.idempotency_key
    assert replace(change, declared_value="2.0").idempotency_key != change.idempotency_key
    assert replace(change, declared_value="").idempotency_key != replace(change, declared_value=None).idempotency_key


def test_already_satisfied_uses_fresh_observation_of_the_same_entity() -> None:
    change = _change()
    current = SnapshotEntity(EntityKind.LANE, "default", "1.5", 2, "fixture")
    assert change.already_satisfied(current)
    assert not change.already_satisfied(replace(current, observed_value="1.0"))
    assert not change.already_satisfied(replace(current, entity_id="other"))
    assert not change.already_satisfied(replace(current, kind=EntityKind.CAPABILITY))
    assert not change.already_satisfied(None, absence_confirmed=True)


def test_unread_removal_is_not_a_satisfied_absence() -> None:
    change = replace(
        _change(),
        status=EntityStatus.PRESENT_BUT_UNDECLARED,
        action=DiffAction.REMOVE,
        declared_value=None,
    )
    current = SnapshotEntity(EntityKind.LANE, "default", "", 2, "fixture")
    assert not change.already_satisfied(current)
    assert not change.already_satisfied(None)
    assert change.already_satisfied(None, absence_confirmed=True)


def _diff(*entries: ReconcileEntry) -> ReconcileDiff:
    return ReconcileDiff("fixture-run", entries or (_change(),), "fixture-inputs", 1)


def _receipt(diff: ReconcileDiff) -> ChangeReceipt:
    return ChangeReceipt(
        plan_id=diff.run_id,
        plan_digest=canonical_digest(diff.to_dict()),
        playbook_digest="policy-a",
        environment_digest="fixture-environment",
        approver_identity="fixture-operator",
        timestamp="2026-11-15T00:00:00Z",
    )


def _observe(value: str) -> tuple[SnapshotEntity, bool]:
    return SnapshotEntity(EntityKind.LANE, "default", value, 2, "fresh-fixture"), False


def test_already_satisfied_entry_applies_as_skip_with_no_side_effect() -> None:
    diff = _diff()
    calls = []
    result = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, policy: policy == "policy-a",
        observe=lambda _entry: _observe("1.5"),
        apply_entry=calls.append,
    )
    assert calls == []
    assert [attempt.outcome for attempt in result.changes] == ["skipped"]
    assert result.changes[0].change_id == diff.entries[0].idempotency_key
    assert result.final_status == "complete"


def test_apply_twice_second_pass_is_all_skips_zero_side_effects() -> None:
    diff = _diff()
    state = {"value": "1.0"}
    calls = []

    def write(entry: ReconcileEntry) -> None:
        calls.append(entry.entity_id)
        state["value"] = str(entry.declared_value)

    def run() -> ChangeReceipt:
        return apply_reconcile_diff(
            diff,
            receipt=_receipt(diff),
            authorize=lambda _diff, _policy: True,
            observe=lambda _entry: _observe(state["value"]),
            apply_entry=write,
        )

    first = run()
    assert [attempt.outcome for attempt in first.changes] == ["success"]
    calls.clear()
    second = run()
    assert calls == []
    assert [attempt.outcome for attempt in second.changes] == ["skipped"]


def test_resume_applies_only_unsatisfied_entries() -> None:
    entries = tuple(replace(_change(), entity_id=name) for name in ("a", "b", "c"))
    diff = _diff(*entries)
    calls = []
    fail = {"enabled": True}

    def observe(entry: ReconcileEntry) -> tuple[SnapshotEntity, bool]:
        return SnapshotEntity(entry.kind, entry.entity_id, "1.0", 2, "fixture"), False

    def write(entry: ReconcileEntry) -> None:
        calls.append(entry.entity_id)
        if entry.entity_id == "b" and fail["enabled"]:
            raise RuntimeError("fixture failure")

    first = apply_reconcile_diff(
        diff, receipt=_receipt(diff), authorize=lambda _diff, _policy: True, observe=observe, apply_entry=write
    )
    assert calls == ["a", "b"]
    assert [attempt.outcome for attempt in first.changes] == ["success", "failure"]
    assert first.final_status == "partial"
    calls.clear()
    fail["enabled"] = False
    resumed = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, _policy: True,
        observe=observe,
        apply_entry=write,
        previous=first,
    )
    assert calls == ["b", "c"]
    assert [attempt.outcome for attempt in resumed.changes] == ["skipped", "success", "success"]
    assert resumed.final_status == "complete"


def test_reconcile_apply_authorization_is_separate_from_stable_intent_key() -> None:
    diff = _diff()
    calls = []
    with pytest.raises(PermissionError):
        apply_reconcile_diff(
            diff,
            receipt=_receipt(diff),
            authorize=lambda _diff, _policy: False,
            observe=lambda _entry: _observe("1.0"),
            apply_entry=calls.append,
        )
    assert calls == []
    assert diff.entries[0].idempotency_key == _change().idempotency_key


@pytest.mark.parametrize("field", ["plan_digest", "playbook_digest", "environment_digest", "approver_identity"])
def test_resume_rejects_changed_receipt_context_before_side_effects(field: str) -> None:
    diff = _diff()
    previous = _receipt(diff)
    previous = replace(previous, **{field: "different"})
    calls = []
    with pytest.raises(ValueError):
        apply_reconcile_diff(
            diff,
            receipt=_receipt(diff),
            authorize=lambda _diff, _policy: True,
            observe=lambda _entry: _observe("1.0"),
            apply_entry=calls.append,
            previous=previous,
        )
    assert calls == []


def test_reconcile_apply_never_treats_an_unread_removal_as_absent() -> None:
    entry = replace(_change(), action=DiffAction.REMOVE, declared_value=None)
    diff = _diff(entry)
    calls = []
    result = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, _policy: True,
        observe=lambda _entry: (None, False),
        apply_entry=calls.append,
    )
    assert calls == []
    assert result.final_status == "failed"
    assert [attempt.outcome for attempt in result.changes] == ["failure"]
    absent = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, _policy: True,
        observe=lambda _entry: (None, True),
        apply_entry=calls.append,
    )
    assert calls == []
    assert [attempt.outcome for attempt in absent.changes] == ["skipped"]


def test_reconcile_apply_rejects_a_receipt_for_other_diff_bytes() -> None:
    diff = _diff()
    calls = []
    with pytest.raises(ValueError):
        apply_reconcile_diff(
            diff,
            receipt=replace(_receipt(diff), plan_digest="wrong"),
            authorize=lambda _diff, _policy: True,
            observe=lambda _entry: _observe("1.0"),
            apply_entry=calls.append,
        )
    assert calls == []


def test_held_entry_cannot_be_applied() -> None:
    diff = _diff(replace(_change(), action=DiffAction.HOLD))
    calls = []
    result = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, _policy: True,
        observe=lambda _entry: _observe("1.0"),
        apply_entry=calls.append,
    )
    assert calls == []
    assert result.final_status == "failed"


@pytest.mark.parametrize(
    "action,desired", [(DiffAction.REMOVE, "1.5"), (DiffAction.ADD, None), (DiffAction.MUTATE, None)]
)
def test_inconsistent_change_intent_is_rejected_before_callbacks(action: DiffAction, desired: str | None) -> None:
    diff = _diff(replace(_change(), action=action, declared_value=desired))
    calls = []
    with pytest.raises(ValueError):
        apply_reconcile_diff(
            diff,
            receipt=_receipt(diff),
            authorize=lambda _diff, _policy: True,
            observe=lambda _entry: _observe("1.0"),
            apply_entry=calls.append,
        )
    assert calls == []


def test_fresh_unsatisfied_drift_cannot_be_overwritten_under_a_stale_diff() -> None:
    diff = _diff()
    calls = []
    result = apply_reconcile_diff(
        diff,
        receipt=_receipt(diff),
        authorize=lambda _diff, _policy: True,
        observe=lambda _entry: _observe("unexpected-value"),
        apply_entry=calls.append,
    )
    assert calls == []
    assert result.final_status == "failed"
    assert "changed since" in result.changes[0].error_message


def test_reconcile_apply_rejects_duplicate_entities_before_callbacks() -> None:
    diff = _diff(_change(), replace(_change(), declared_value="other"))
    calls = []
    with pytest.raises(ValueError):
        apply_reconcile_diff(
            diff,
            receipt=_receipt(diff),
            authorize=lambda _diff, _policy: True,
            observe=lambda _entry: _observe("1.0"),
            apply_entry=calls.append,
        )
    assert calls == []


def test_a_skip_before_failure_does_not_claim_a_partially_applied_run() -> None:
    satisfied = replace(_change(), entity_id="a", action=DiffAction.NONE)
    failing = replace(_change(), entity_id="b")
    diff = _diff(satisfied, failing)
    calls = []

    def observe(entry: ReconcileEntry) -> tuple[SnapshotEntity, bool]:
        value = "1.5" if entry.entity_id == "a" else "1.0"
        return SnapshotEntity(entry.kind, entry.entity_id, value, 2, "fixture"), False

    def write(entry: ReconcileEntry) -> None:
        calls.append(entry.entity_id)
        raise RuntimeError("fixture failure before write")

    result = apply_reconcile_diff(
        diff, receipt=_receipt(diff), authorize=lambda _diff, _policy: True, observe=observe, apply_entry=write
    )
    assert calls == ["b"]
    assert [attempt.outcome for attempt in result.changes] == ["skipped", "failure"]
    assert result.final_status == "failed"
