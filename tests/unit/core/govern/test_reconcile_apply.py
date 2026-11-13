"""Tests for the pure reconcile apply identity and observation decision."""

from __future__ import annotations

from dataclasses import replace

from bernstein.core.govern.reconcile_models import (
    DiffAction,
    EntityKind,
    EntityStatus,
    ReconcileEntry,
    SnapshotEntity,
)


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
