"""Tests for restore: the inverse plan is built from the receipt, not from the environment.

Coverage:

* ``ChangeAttempt`` records the value observed immediately before the change
  alongside the value written.
* :func:`build_restore_plan` sources every restore value from the receipt; the
  observation map is consulted only to detect drift.
* The restore plan links back to the apply record it inverts.
* A target that drifted since the apply, or that cannot be observed at all, is
  refused per entry unless that entry is forced.
* Applying a restore plan reproduces the exact bytes that existed before the
  original apply.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.govern.restore import (
    RESTORE_REASON_DRIFTED,
    RESTORE_REASON_UNOBSERVABLE,
    build_restore_plan,
)
from bernstein.core.security.change_receipt import (
    ChangeAttempt,
    ChangeReceipt,
    change_receipt_payload_errors,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def applied_change() -> ChangeAttempt:
    """One successfully applied update, with the value it replaced on file."""
    return ChangeAttempt(
        change_id="change-001",
        change_type="update",
        target="iam.Role:deploy",
        attempted_at="2025-01-15T10:30:00Z",
        outcome="success",
        prior_value="read-only",
        written_value="read-write",
    )


@pytest.fixture()
def apply_receipt(applied_change: ChangeAttempt) -> ChangeReceipt:
    """A complete apply record carrying one successful change."""
    return ChangeReceipt(
        plan_id="plan-abc123",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(applied_change,),
        final_status="complete",
        timestamp="2025-01-15T10:31:00Z",
    )


# ---------------------------------------------------------------------------
# 1. The apply record keeps the prior value
# ---------------------------------------------------------------------------


def test_apply_record_stores_prior_value_alongside_written_value(
    apply_receipt: ChangeReceipt,
) -> None:
    """Each entry carries both the value replaced and the value written."""
    entry = apply_receipt.to_dict()["changes"][0]
    assert entry["prior_value"] == "read-only"
    assert entry["written_value"] == "read-write"

    # The extended receipt still verifies offline.
    assert change_receipt_payload_errors(apply_receipt.to_dict()) == ()


def test_receipt_without_prior_values_still_verifies() -> None:
    """The new fields are additive: a receipt written without them still verifies."""
    data = ChangeReceipt(
        plan_id="plan-legacy",
        plan_digest="d" * 64,
        playbook_digest="e" * 64,
        environment_digest="f" * 64,
        approver_identity="ops@example.com",
        changes=(
            ChangeAttempt(
                change_id="change-legacy",
                change_type="create",
                target="kv.Secret:db-pass",
                attempted_at="2025-01-15T10:30:00Z",
                outcome="success",
            ),
        ),
        timestamp="2025-01-15T10:31:00Z",
    ).to_dict()
    for change in data["changes"]:
        change.pop("prior_value")
        change.pop("written_value")
    data.pop("restores_receipt_digest")

    assert change_receipt_payload_errors(data) == ()


# ---------------------------------------------------------------------------
# 2. The inverse plan comes from the receipt, never from re-observation
# ---------------------------------------------------------------------------


def test_restore_builds_inverse_plan_from_stored_prior_values_not_reobservation() -> None:
    """Restore values are read off the receipt; the environment cannot supply them.

    The observation map is deliberately given values that match what was
    written (so nothing drifted) but differ from the prior values. If the plan
    were built by re-observing the target, the restore values would equal the
    observed ones.
    """
    receipt = ChangeReceipt(
        plan_id="plan-two",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(
            ChangeAttempt(
                change_id="change-001",
                change_type="update",
                target="iam.Role:deploy",
                attempted_at="2025-01-15T10:30:00Z",
                outcome="success",
                prior_value="read-only",
                written_value="read-write",
            ),
            ChangeAttempt(
                change_id="change-002",
                change_type="create",
                target="kv.Secret:db-pass",
                attempted_at="2025-01-15T10:30:05Z",
                outcome="success",
                prior_value="",
                written_value="hunter2",
            ),
        ),
        timestamp="2025-01-15T10:31:00Z",
    )
    observed = {"iam.Role:deploy": "read-write", "kv.Secret:db-pass": "hunter2"}

    plan = build_restore_plan(receipt=receipt, observed=observed)

    assert plan.refusals == ()
    # Inverse order: the last change applied is the first one undone.
    assert [e.change_id for e in plan.entries] == ["change-002", "change-001"]

    by_id = {e.change_id: e for e in plan.entries}
    assert by_id["change-001"].restore_value == "read-only"
    assert by_id["change-001"].change_type == "update"
    assert by_id["change-002"].restore_value == ""
    assert by_id["change-002"].change_type == "delete"

    # No restore value equals what the environment currently reports.
    assert all(e.restore_value != observed[e.target] for e in plan.entries)


def test_restore_skips_entries_that_never_changed_state(applied_change: ChangeAttempt) -> None:
    """Only entries the receipt records as applied are inverted."""
    receipt = ChangeReceipt(
        plan_id="plan-mixed",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(
            applied_change,
            ChangeAttempt(
                change_id="change-skipped",
                change_type="update",
                target="iam.Role:audit",
                attempted_at="2025-01-15T10:30:05Z",
                outcome="skipped",
                prior_value="read-only",
                written_value="",
            ),
        ),
        final_status="partial",
        timestamp="2025-01-15T10:31:00Z",
    )

    plan = build_restore_plan(receipt=receipt, observed={"iam.Role:deploy": "read-write"})

    assert [e.change_id for e in plan.entries] == ["change-001"]
    assert plan.refusals == ()


# ---------------------------------------------------------------------------
# 3. The restore record links back to the apply record it inverts
# ---------------------------------------------------------------------------


def test_restore_links_to_original_apply_record(apply_receipt: ChangeReceipt) -> None:
    """The plan names the apply record's id, and a restore receipt carries it too."""
    plan = build_restore_plan(receipt=apply_receipt, observed={"iam.Role:deploy": "read-write"})

    assert plan.original_receipt_digest == apply_receipt.digest
    assert plan.to_dict()["original_receipt_digest"] == apply_receipt.digest

    restore_receipt = ChangeReceipt(
        plan_id=plan.plan_id,
        plan_digest="1" * 64,
        playbook_digest=apply_receipt.playbook_digest,
        environment_digest=apply_receipt.environment_digest,
        approver_identity="alice@example.com",
        changes=(
            ChangeAttempt(
                change_id="change-001",
                change_type="update",
                target="iam.Role:deploy",
                attempted_at="2025-01-16T09:00:00Z",
                outcome="success",
                prior_value="read-write",
                written_value="read-only",
            ),
        ),
        timestamp="2025-01-16T09:00:01Z",
        restores_receipt_digest=apply_receipt.digest,
    )

    assert restore_receipt.to_dict()["restores_receipt_digest"] == apply_receipt.digest
    assert change_receipt_payload_errors(restore_receipt.to_dict()) == ()
    assert restore_receipt.digest != apply_receipt.digest


# ---------------------------------------------------------------------------
# 4. Drift refuses per entry unless forced
# ---------------------------------------------------------------------------


def test_restore_refuses_drifted_entry_unless_forced(apply_receipt: ChangeReceipt) -> None:
    """A target changed since the apply is refused, and reports the drift."""
    drifted = {"iam.Role:deploy": "admin"}

    plan = build_restore_plan(receipt=apply_receipt, observed=drifted)

    assert plan.entries == ()
    assert len(plan.refusals) == 1
    refusal = plan.refusals[0]
    assert refusal.change_id == "change-001"
    assert refusal.reason == RESTORE_REASON_DRIFTED
    assert refusal.expected_value == "read-write"
    assert refusal.observed_value == "admin"

    forced = build_restore_plan(
        receipt=apply_receipt,
        observed=drifted,
        forced_entry_ids=("change-001",),
    )

    assert forced.refusals == ()
    assert len(forced.entries) == 1
    assert forced.entries[0].forced is True
    assert forced.entries[0].restore_value == "read-only"


def test_restore_refuses_entry_whose_target_cannot_be_observed(
    apply_receipt: ChangeReceipt,
) -> None:
    """A target that could not be read is refused: absence of drift is not proven."""
    plan = build_restore_plan(receipt=apply_receipt, observed={})

    assert plan.entries == ()
    assert len(plan.refusals) == 1
    assert plan.refusals[0].reason == RESTORE_REASON_UNOBSERVABLE
    assert plan.refusals[0].observed_value == ""


# ---------------------------------------------------------------------------
# 5. Apply then restore reproduces the exact prior bytes
# ---------------------------------------------------------------------------


def test_apply_then_restore_is_byte_identical_to_before(tmp_path: Path) -> None:
    """Replaying the plan's restore values reproduces the pre-apply file bytes."""
    target = tmp_path / "policy.txt"
    target.write_text("permit: read\n", encoding="utf-8")
    before = target.read_bytes()

    prior_value = target.read_text(encoding="utf-8")
    written_value = "permit: read,write\n"
    target.write_text(written_value, encoding="utf-8")
    assert target.read_bytes() != before

    receipt = ChangeReceipt(
        plan_id="plan-file",
        plan_digest="a" * 64,
        playbook_digest="b" * 64,
        environment_digest="c" * 64,
        approver_identity="alice@example.com",
        changes=(
            ChangeAttempt(
                change_id="change-file",
                change_type="update",
                target=str(target),
                attempted_at="2025-01-15T10:30:00Z",
                outcome="success",
                prior_value=prior_value,
                written_value=written_value,
            ),
        ),
        timestamp="2025-01-15T10:31:00Z",
    )

    plan = build_restore_plan(
        receipt=receipt,
        observed={str(target): target.read_text(encoding="utf-8")},
    )

    assert plan.refusals == ()
    for entry in plan.entries:
        Path(entry.target).write_text(entry.restore_value, encoding="utf-8")

    assert target.read_bytes() == before
