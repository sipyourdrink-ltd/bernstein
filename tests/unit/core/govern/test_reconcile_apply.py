"""Tests for idempotent reconciliation apply and ChangeReceipt generation (#5086)."""

from __future__ import annotations

from bernstein.core.govern.reconcile_apply import (
    apply_reconcile_diff,
    apply_reconcile_entry,
    build_reconcile_change_receipt,
    compute_idempotency_key,
)
from bernstein.core.govern.reconcile_models import (
    DiffAction,
    EntityKind,
    EntityStatus,
    ReconcileDiff,
    ReconcileEntry,
)
from bernstein.core.security.change_receipt import ChangeAttempt


def _make_entry(
    entity_id: str,
    declared: str | None,
    observed: str | None,
    action: DiffAction = DiffAction.MUTATE,
    status: EntityStatus = EntityStatus.CHANGED,
) -> ReconcileEntry:
    return ReconcileEntry(
        kind=EntityKind.ADAPTER,
        entity_id=entity_id,
        status=status,
        action=action,
        declared_value=declared,
        observed_value=observed,
        observed_at=1700000000,
        evidence_ref="ev-1",
    )


def test_idempotency_key_is_stable_for_same_entity_and_desired_value() -> None:
    """Stable hashing across invocations, sensitive to desired value and policy set."""
    key1 = compute_idempotency_key("claude", "val1", policy_set_hash="pol-a")
    key2 = compute_idempotency_key("claude", "val1", policy_set_hash="pol-a")
    assert key1 == key2

    # Different desired value -> different key
    key3 = compute_idempotency_key("claude", "val2", policy_set_hash="pol-a")
    assert key1 != key3

    # Different policy set -> different key (forces re-apply when policy shifts #5117)
    key4 = compute_idempotency_key("claude", "val1", policy_set_hash="pol-b")
    assert key1 != key4


def test_already_satisfied_entry_applies_as_skip_with_no_side_effect() -> None:
    """Acceptance criterion: satisfied entry produces outcome='skipped' and zero side effect."""
    side_effects: list[str] = []

    def mock_applier(e: ReconcileEntry) -> tuple[str, str | None]:
        side_effects.append(e.entity_id)
        return (e.declared_value or "", None)

    # Observed already equals declared value
    entry = _make_entry("claude", declared="v1", observed="v1", action=DiffAction.MUTATE)
    attempt = apply_reconcile_entry(entry, observed_value="v1", applier_fn=mock_applier)

    assert attempt.outcome == "skipped"
    assert attempt.written_value == ""
    assert attempt.prior_value == "v1"
    assert side_effects == [], "Must not execute any side effects for satisfied entries"


def test_apply_twice_second_pass_is_all_skips_zero_side_effects() -> None:
    """Run-twice test: pass 1 applies mutations; pass 2 is 100% skipped with 0 mutations."""
    entries = (
        _make_entry("adapter-1", declared="enabled", observed="disabled"),
        _make_entry("adapter-2", declared="v2", observed="v1"),
    )
    diff = ReconcileDiff(
        run_id="reconcile-run-1",
        entries=entries,
        inputs_hash="hash-abc",
        timestamp=1700000000,
    )

    state: dict[str, str | None] = {"adapter-1": "disabled", "adapter-2": "v1"}
    calls: list[str] = []

    def tracking_applier(e: ReconcileEntry) -> tuple[str, str | None]:
        calls.append(e.entity_id)
        return (e.declared_value or "", None)

    # First pass: both entries apply
    attempts_1 = apply_reconcile_diff(diff, state, applier_fn=tracking_applier)
    assert len(attempts_1) == 2
    assert all(a.outcome == "success" for a in attempts_1)
    assert calls == ["adapter-1", "adapter-2"]
    assert state == {"adapter-1": "enabled", "adapter-2": "v2"}

    # Second pass: identical diff reapplied against current state
    calls.clear()
    attempts_2 = apply_reconcile_diff(diff, state, applier_fn=tracking_applier)
    assert len(attempts_2) == 2
    assert all(a.outcome == "skipped" for a in attempts_2)
    assert calls == [], "Second pass must perform zero side-effects"

    # Verify receipt built from second pass
    receipt = build_reconcile_change_receipt(diff, attempts_2)
    assert receipt.final_status == "complete"
    assert all(c.outcome == "skipped" for c in receipt.changes)


def test_resume_applies_only_unsatisfied_entries() -> None:
    """Partial apply resumption: only non-succeeded / unsatisfied entries run."""
    entries = (
        _make_entry("adapter-1", declared="v1", observed="v0"),
        _make_entry("adapter-2", declared="v2", observed="v0"),
        _make_entry("adapter-3", declared="v3", observed="v0"),
    )
    diff = ReconcileDiff(
        run_id="reconcile-run-partial",
        entries=entries,
        inputs_hash="hash-xyz",
        timestamp=1700000000,
    )

    state: dict[str, str | None] = {"adapter-1": "v0", "adapter-2": "v0", "adapter-3": "v0"}

    # Simulate run 1 where adapter-1 succeeded, adapter-2 failed, adapter-3 was not attempted
    prior_attempts = (
        ChangeAttempt(
            change_id="c1",
            change_type="mutate",
            target="adapter:adapter-1",
            attempted_at="2026-09-08T00:00:00Z",
            outcome="success",
            written_value="v1",
        ),
        ChangeAttempt(
            change_id="c2",
            change_type="mutate",
            target="adapter:adapter-2",
            attempted_at="2026-09-08T00:00:00Z",
            outcome="failure",
            error_message="timeout",
        ),
    )
    state["adapter-1"] = "v1"

    calls: list[str] = []

    def resume_applier(e: ReconcileEntry) -> tuple[str, str | None]:
        calls.append(e.entity_id)
        return (e.declared_value or "", None)

    # Resume run
    resumed_attempts = apply_reconcile_diff(
        diff,
        state,
        applier_fn=resume_applier,
        prior_attempts=prior_attempts,
    )

    # adapter-1 was reused from prior_attempts, adapter-2 and adapter-3 were executed
    assert calls == ["adapter-2", "adapter-3"]
    assert len(resumed_attempts) == 3
    assert [a.outcome for a in resumed_attempts] == ["success", "success", "success"]
    assert state == {"adapter-1": "v1", "adapter-2": "v2", "adapter-3": "v3"}
