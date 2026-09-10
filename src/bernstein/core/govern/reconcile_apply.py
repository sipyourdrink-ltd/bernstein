"""Idempotent reconciliation apply loop and ChangeReceipt construction (#5086).

A reconciliation diff describes desired versus observed state. Applying a diff
must be idempotent: applying an already-satisfied diff entry produces no side
effect and records a `ChangeAttempt` with `outcome="skipped"`. Re-running the
entire diff against an unchanged environment writes a receipt whose attempts are
all skips, proving convergence without mutation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bernstein.core.govern.reconcile_models import DiffAction, EntityStatus, ReconcileDiff, ReconcileEntry
from bernstein.core.security.change_receipt import (
    ChangeAttempt,
    ChangeReceipt,
    FinalStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


def compute_idempotency_key(
    entity_id: str,
    desired_value: str | None,
    policy_set_hash: str = "",
) -> str:
    """Derive an idempotency key from entity_id, desired value, and policy set.

    Hashing ``(entity_id, sha256(desired_value), policy_set_hash)`` guarantees
    that a change in the active policy set (#5117) forces re-evaluation even if
    the desired value string is unchanged, while identical configurations yield
    identical keys.
    """
    val_bytes = (desired_value or "").encode()
    val_hash = hashlib.sha256(val_bytes).hexdigest()
    preimage = f"{entity_id}:{val_hash}:{policy_set_hash}".encode()
    return hashlib.sha256(preimage).hexdigest()


def apply_reconcile_entry(
    entry: ReconcileEntry,
    observed_value: str | None,
    *,
    applier_fn: Callable[[ReconcileEntry], tuple[str, str | None]] | None = None,
    timestamp: str | None = None,
    policy_set_hash: str = "",
) -> ChangeAttempt:
    """Apply a single diff entry idempotently.

    If the entry is already satisfied (observed matches declared value, or action is NONE),
    no side effect is performed and outcome is "skipped". Otherwise, the change
    is executed and an attempt record with "success" or "failure" is returned.
    """
    ts = timestamp or datetime.now(UTC).isoformat()
    change_id = compute_idempotency_key(
        entity_id=entry.entity_id,
        desired_value=entry.declared_value,
        policy_set_hash=policy_set_hash,
    )
    target = f"{entry.kind.value}:{entry.entity_id}"
    prior = observed_value or ""

    # Check if already satisfied: observed value matches declared value or no action needed
    is_satisfied = (
        entry.action is DiffAction.NONE
        or entry.status is EntityStatus.UNCHANGED
        or (entry.declared_value is not None and observed_value == entry.declared_value)
    )

    if is_satisfied:
        return ChangeAttempt(
            change_id=change_id,
            change_type=entry.action.value,
            target=target,
            attempted_at=ts,
            outcome="skipped",
            error_message="",
            prior_value=prior,
            written_value="",
        )

    # Needs mutation: invoke applier_fn if provided
    if applier_fn is not None:
        try:
            written, err = applier_fn(entry)
        except Exception as exc:
            return ChangeAttempt(
                change_id=change_id,
                change_type=entry.action.value,
                target=target,
                attempted_at=ts,
                outcome="failure",
                error_message=str(exc),
                prior_value=prior,
                written_value="",
            )
        if err:
            return ChangeAttempt(
                change_id=change_id,
                change_type=entry.action.value,
                target=target,
                attempted_at=ts,
                outcome="failure",
                error_message=err,
                prior_value=prior,
                written_value="",
            )
        return ChangeAttempt(
            change_id=change_id,
            change_type=entry.action.value,
            target=target,
            attempted_at=ts,
            outcome="success",
            error_message="",
            prior_value=prior,
            written_value=written,
        )

    # Default without applier: record success with declared value
    return ChangeAttempt(
        change_id=change_id,
        change_type=entry.action.value,
        target=target,
        attempted_at=ts,
        outcome="success",
        error_message="",
        prior_value=prior,
        written_value=entry.declared_value or "",
    )


def apply_reconcile_diff(
    diff: ReconcileDiff,
    current_state: dict[str, str | None],
    *,
    applier_fn: Callable[[ReconcileEntry], tuple[str, str | None]] | None = None,
    prior_attempts: Sequence[ChangeAttempt] = (),
    timestamp: str | None = None,
    policy_set_hash: str = "",
) -> tuple[ChangeAttempt, ...]:
    """Execute a reconciliation diff against current state with resume support.

    Entries already marked "success" in `prior_attempts` are skipped during a resume.
    When a change succeeds, `current_state` is updated in place.
    """
    ts = timestamp or datetime.now(UTC).isoformat()
    already_succeeded_targets = {attempt.target: attempt for attempt in prior_attempts if attempt.outcome == "success"}

    results: list[ChangeAttempt] = []
    for entry in diff.entries:
        target = f"{entry.kind.value}:{entry.entity_id}"
        # If this entry already succeeded in a prior partial run, reuse the prior successful attempt
        if target in already_succeeded_targets:
            results.append(already_succeeded_targets[target])
            continue

        observed = current_state.get(entry.entity_id)
        attempt = apply_reconcile_entry(
            entry=entry,
            observed_value=observed,
            applier_fn=applier_fn,
            timestamp=ts,
            policy_set_hash=policy_set_hash,
        )
        results.append(attempt)
        if attempt.outcome == "success":
            current_state[entry.entity_id] = attempt.written_value

    return tuple(results)


def build_reconcile_change_receipt(
    diff: ReconcileDiff,
    attempts: Sequence[ChangeAttempt],
    *,
    approver: str = "operator",
    playbook_digest: str = "",
    environment_digest: str = "",
    timestamp: str | None = None,
) -> ChangeReceipt:
    """Build a ChangeReceipt attestation covering the reconciliation attempts."""
    ts = timestamp or datetime.now(UTC).isoformat()
    final_status: FinalStatus
    has_failure = any(a.outcome == "failure" for a in attempts)
    has_success_or_skip = any(a.outcome in ("success", "skipped") for a in attempts)

    if has_failure and has_success_or_skip:
        final_status = "partial"
    elif has_failure:
        final_status = "failed"
    else:
        final_status = "complete"

    return ChangeReceipt(
        plan_id=diff.run_id,
        plan_digest=diff.inputs_hash,
        playbook_digest=playbook_digest or diff.inputs_hash,
        environment_digest=environment_digest or diff.inputs_hash,
        approver_identity=approver,
        changes=tuple(attempts),
        final_status=final_status,
        timestamp=ts,
    )
