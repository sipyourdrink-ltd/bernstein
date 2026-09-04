"""Authority levels, actions, and containment receipts (Issue #5452).

Defines the 5-tier authority hierarchy (L0 to L4, plus L5 admin):
- L0: read-only (read files, search, inspect)
- L1: write inside worktree (create/modify/patch files in repo)
- L2: local execute (run commands, execute tests, compile/build)
- L3: push or publish (git push, package registry publish, release tags)
- L4: unattended side effects (external HTTP/webhook egress, cloud deployments)
- L5: admin / policy escalation (tamper audit, policy override - strictly gated)

A run declared at level L must not act above its declared level without
a verifiable approval receipt. Delegated sub-tasks cannot exceed the parent's level (#5047).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import Any


class AuthorityLevel(IntEnum):
    """Hierarchical authority levels for governed runs."""

    L0 = 0  # read-only
    L1 = 1  # write inside worktree
    L2 = 2  # local execute (L1 + build / run tests)
    L3 = 3  # push / publish (L2 + git push / release)
    L4 = 4  # unattended side effects (L3 + cloud / external egress)
    L5 = 5  # admin / policy escalation (strictly gated)

    @property
    def label(self) -> str:
        """Human-readable category label for the authority level."""
        _LABELS = {
            AuthorityLevel.L0: "read_only",
            AuthorityLevel.L1: "write_in_worktree",
            AuthorityLevel.L2: "local_execute",
            AuthorityLevel.L3: "push_publish",
            AuthorityLevel.L4: "unattended_side_effects",
            AuthorityLevel.L5: "admin_escalate",
        }
        return _LABELS[self]

    @classmethod
    def from_str(cls, value: str) -> AuthorityLevel:
        """Parse authority level from string (e.g. 'L0', 'read_only')."""
        normalized = value.strip().upper()
        for member in cls:
            if member.name == normalized or member.label.upper() == normalized:
                return member
        # Check lowercase label match
        for member in cls:
            if member.label == value.strip().lower():
                return member
        raise ValueError(f"Unknown authority level: {value!r}. Valid: {[m.name for m in cls]}")


class ContainmentOutcome(StrEnum):
    """Outcome of an authority containment evaluation."""

    PERMITTED_IN_LEVEL = "permitted_in_level"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    BLOCKED_BY_APPROVAL_GATE = "blocked_by_approval_gate"
    APPROVED_WITH_RECEIPT = "approved_with_receipt"
    CONTAINMENT_VIOLATION = "containment_violation"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class AuthorityAction:
    """An action attempted by an agent during execution."""

    name: str
    category: str
    required_level: AuthorityLevel
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuthorityReceipt:
    """Offline-verifiable containment receipt for an attempted action."""

    receipt_id: str
    task_id: str
    declared_level: str  # e.g. "L0"
    attempted_action: str
    required_level: str  # e.g. "L1"
    outcome: ContainmentOutcome
    control_id: str = "CTL-AUTH-01"
    reason: str = ""
    approval_receipt_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    receipt_hash: str = ""

    def __post_init__(self) -> None:
        if not self.receipt_hash:
            computed = self._compute_hash()
            object.__setattr__(self, "receipt_hash", computed)

    def _compute_hash(self) -> str:
        payload = {
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "declared_level": self.declared_level,
            "attempted_action": self.attempted_action,
            "required_level": self.required_level,
            "outcome": self.outcome.value if isinstance(self.outcome, ContainmentOutcome) else str(self.outcome),
            "control_id": self.control_id,
            "reason": self.reason,
            "approval_receipt_id": self.approval_receipt_id,
            "timestamp": self.timestamp,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    @property
    def is_contained(self) -> bool:
        """Return True if containment held (action stopped or properly authorized)."""
        return self.outcome in (
            ContainmentOutcome.PERMITTED_IN_LEVEL,
            ContainmentOutcome.BLOCKED_BY_POLICY,
            ContainmentOutcome.BLOCKED_BY_APPROVAL_GATE,
            ContainmentOutcome.APPROVED_WITH_RECEIPT,
            ContainmentOutcome.NOT_ATTEMPTED,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "task_id": self.task_id,
            "declared_level": self.declared_level,
            "attempted_action": self.attempted_action,
            "required_level": self.required_level,
            "outcome": self.outcome.value if isinstance(self.outcome, ContainmentOutcome) else str(self.outcome),
            "control_id": self.control_id,
            "reason": self.reason,
            "approval_receipt_id": self.approval_receipt_id,
            "timestamp": self.timestamp,
            "receipt_hash": self.receipt_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuthorityReceipt:
        return cls(
            receipt_id=data["receipt_id"],
            task_id=data["task_id"],
            declared_level=data["declared_level"],
            attempted_action=data["attempted_action"],
            required_level=data["required_level"],
            outcome=ContainmentOutcome(data["outcome"]),
            control_id=data.get("control_id", "CTL-AUTH-01"),
            reason=data.get("reason", ""),
            approval_receipt_id=data.get("approval_receipt_id"),
            timestamp=data.get("timestamp", 0.0),
            receipt_hash=data.get("receipt_hash", ""),
        )


def verify_authority_receipt(receipt: AuthorityReceipt) -> bool:
    """Verify that the receipt hash matches its contents byte-for-byte."""
    recomputed = receipt._compute_hash()
    return receipt.receipt_hash == recomputed


def evaluate_authority_action(
    task_id: str,
    declared_level: AuthorityLevel,
    action: AuthorityAction,
    approval_receipt: dict[str, Any] | None = None,
    control_id: str = "CTL-AUTH-01",
) -> AuthorityReceipt:
    """Evaluate whether an attempted action exceeds declared authority.

    Args:
        task_id: The identifier of the running task.
        declared_level: Declared maximum authority level of the run.
        action: The attempted action and required authority level.
        approval_receipt: Optional recorded approval receipt authorizing escalation.
        control_id: Compliance control identifier (default: CTL-AUTH-01).

    Returns:
        A verified :class:`AuthorityReceipt` recording the decision.
    """
    token = f"{task_id}:{action.name}:{time.time()}".encode()
    receipt_id = f"auth-rcpt-{hashlib.sha256(token).hexdigest()[:16]}"

    # Case 1: Action within declared authority level
    if action.required_level <= declared_level:
        return AuthorityReceipt(
            receipt_id=receipt_id,
            task_id=task_id,
            declared_level=declared_level.name,
            attempted_action=action.name,
            required_level=action.required_level.name,
            outcome=ContainmentOutcome.PERMITTED_IN_LEVEL,
            control_id=control_id,
            reason=(
                f"Action '{action.name}' requires {action.required_level.name} "
                f"<= declared authority {declared_level.name}."
            ),
        )

    # Case 2: Action exceeds authority level — check for approval receipt
    if approval_receipt is not None:
        appr_task = approval_receipt.get("task_id")
        appr_level_str = approval_receipt.get("approved_level", "")
        appr_id = approval_receipt.get("approval_id", "unknown_approval")

        try:
            appr_level = AuthorityLevel.from_str(appr_level_str)
        except ValueError:
            appr_level = AuthorityLevel.L0

        if appr_task == task_id and appr_level >= action.required_level:
            return AuthorityReceipt(
                receipt_id=receipt_id,
                task_id=task_id,
                declared_level=declared_level.name,
                attempted_action=action.name,
                required_level=action.required_level.name,
                outcome=ContainmentOutcome.APPROVED_WITH_RECEIPT,
                control_id=control_id,
                reason=(
                    f"Action '{action.name}' requires {action.required_level.name} > {declared_level.name}, "
                    f"authorized via approval receipt '{appr_id}'."
                ),
                approval_receipt_id=appr_id,
            )
        else:
            return AuthorityReceipt(
                receipt_id=receipt_id,
                task_id=task_id,
                declared_level=declared_level.name,
                attempted_action=action.name,
                required_level=action.required_level.name,
                outcome=ContainmentOutcome.BLOCKED_BY_APPROVAL_GATE,
                control_id=control_id,
                reason=(
                    f"Approval receipt '{appr_id}' is invalid or insufficient "
                    f"for action '{action.name}' requiring {action.required_level.name}."
                ),
                approval_receipt_id=appr_id,
            )

    # Case 3: No approval receipt — blocked by policy
    return AuthorityReceipt(
        receipt_id=receipt_id,
        task_id=task_id,
        declared_level=declared_level.name,
        attempted_action=action.name,
        required_level=action.required_level.name,
        outcome=ContainmentOutcome.BLOCKED_BY_POLICY,
        control_id=control_id,
        reason=(
            f"Action '{action.name}' blocked: requires {action.required_level.name} "
            f"but task has declared authority {declared_level.name} and no approval receipt."
        ),
    )


def evaluate_subtask_delegation(
    parent_task_id: str,
    parent_level: AuthorityLevel,
    subtask_id: str,
    requested_level: AuthorityLevel,
    control_id: str = "CTL-AUTH-01",
) -> AuthorityReceipt:
    """Enforce delegation containment: a delegated sub-task cannot exceed parent's level (#5047).

    Args:
        parent_task_id: Parent task ID.
        parent_level: Declared level of the parent task.
        subtask_id: Subtask ID to spawn.
        requested_level: Requested level for the delegated subtask.
        control_id: Compliance control identifier.

    Returns:
        An :class:`AuthorityReceipt` recording permission or delegation blockage.
    """
    token = f"{parent_task_id}:{subtask_id}:{time.time()}".encode()
    receipt_id = f"auth-deleg-{hashlib.sha256(token).hexdigest()[:16]}"

    if requested_level <= parent_level:
        return AuthorityReceipt(
            receipt_id=receipt_id,
            task_id=subtask_id,
            declared_level=parent_level.name,
            attempted_action=f"delegate_subtask:{subtask_id}",
            required_level=requested_level.name,
            outcome=ContainmentOutcome.PERMITTED_IN_LEVEL,
            control_id=control_id,
            reason=(
                f"Delegated subtask authority ({requested_level.name}) <= parent run authority ({parent_level.name})."
            ),
        )

    return AuthorityReceipt(
        receipt_id=receipt_id,
        task_id=subtask_id,
        declared_level=parent_level.name,
        attempted_action=f"delegate_subtask:{subtask_id}",
        required_level=requested_level.name,
        outcome=ContainmentOutcome.BLOCKED_BY_POLICY,
        control_id=control_id,
        reason=(
            f"Delegated subtask authority ({requested_level.name}) cannot exceed "
            f"parent run authority ({parent_level.name}). Delegation stopped under #5047."
        ),
    )
