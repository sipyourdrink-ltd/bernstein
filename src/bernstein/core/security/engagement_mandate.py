"""EngagementMandate types and gating logic for security phase enforcement.

This module defines the canonical types for engagement mandates: a
content-addressed scope grant that gates each phase action (recon,
enumerate, scan, verify, report). The mandate resolver issues receipts
that the engagement projection uses to tag each TaskNode with its
mandate status -- admitted or refused -- without aborting sibling phases.

Phase refusal semantics
-----------------------
When a phase's scope is not admitted by the active mandate, the phase
produces a TaskNode with ``mandate_status = REFUSED`` and refusal
metadata. Sibling phases continue executing. The refusal is a first-class
audit record, not a silent skip.

Zero-trust principle
--------------------
Mandate resolution is treated as untrusted input to the projection.
Even if a mandate was previously granted, each projection run evaluates
the scope containment independently so that two identical replay inputs
always produce the same refusal/mandate-status tags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Public type aliases
# ---------------------------------------------------------------------------

#: Content-addressed scope grant reference.
#: Format is resolver-defined; callers MUST treat it as an opaque string.
#: Examples: ``"scope:repo/*"``, ``"scope:org/unit-42"``, ``"scope:*"``.
ScopeReference = str


# ---------------------------------------------------------------------------
# Action enumeration
# ---------------------------------------------------------------------------


class MandateAction(Enum):
    """Enumeration of actions a mandate may permit.

    Each phase in an engagement playbook declares the action it performs.
    A mandate grants a subset of these actions. Any phase whose declared
    action is not in the mandate's permitted set is refused.
    """

    RECON = "recon"
    ENUMERATE = "enumerate"
    SCAN = "scan"
    VERIFY = "verify"
    REPORT = "report"


# ---------------------------------------------------------------------------
# Core mandate types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngagementMandate:
    """Scope-grant check for an engagement phase action.

    Represents a content-addressed admission receipt issued by the
    orchestrator's mandate resolver. The mandate defines a validity
    window and a set of permitted actions; phases are evaluated against
    both the scope containment and the action inclusion.

    Attributes:
        mandate_id: Unique identifier for this mandate instance.
        scope: The scope granted by this mandate (e.g. ``"scope:repo/*"``).
        issued_by: Identifier of the authority that issued this mandate.
        valid_from: Timestamp (UTC) at which this mandate becomes effective.
        valid_to: Timestamp (UTC) at which this mandate expires.
        permitted_actions: Tuple of actions this mandate authorizes.
    """

    mandate_id: str
    scope: ScopeReference
    issued_by: str
    valid_from: datetime
    valid_to: datetime
    permitted_actions: tuple[MandateAction, ...] = field(
        default_factory=lambda: (
            MandateAction.RECON,
            MandateAction.ENUMERATE,
            MandateAction.SCAN,
            MandateAction.VERIFY,
            MandateAction.REPORT,
        )
    )

    def is_valid_at(self, instant: datetime | None = None) -> bool:
        """Return True when *instant* falls within the mandate validity window."""
        if instant is None:
            instant = datetime.now(UTC)
        return self.valid_from <= instant <= self.valid_to

    def permits(self, action: MandateAction) -> bool:
        """Return True when this mandate grants *action*."""
        return action in self.permitted_actions

    def scope_contains(self, other: ScopeReference) -> bool:
        """Return True when this mandate's scope covers *other*.

        containment is satisfied when:
        - ``other`` equals this mandate's scope exactly, OR
        - ``other`` is a sub-scope of this mandate's scope (prefix match
          on ``scope + ":"``), OR
        - this mandate's scope is ``"scope:*"`` (wildcard).
        """
        if self.scope == "scope:*":
            return True
        if self.scope == other:
            return True
        return other.startswith(self.scope + ":")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for audit / chaining."""
        return {
            "mandate_id": self.mandate_id,
            "scope": self.scope,
            "issued_by": self.issued_by,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "permitted_actions": [a.value for a in self.permitted_actions],
        }


@dataclass(frozen=True, slots=True)
class MandateReceipt:
    """Receipt returned by :func:`check_mandate`.

    Describes the outcome of evaluating a mandate against a requested
    scope and action. The receipt is immutable and safe to include in
    audit records.

    Attributes:
        mandate_id: The mandate evaluated.
        requested_scope: The scope the phase requested.
        requested_action: The action the phase declared.
        admitted: True when the scope is contained and the action is permitted.
        refusal_reason: Human-readable reason when *admitted* is False.
        evaluated_at: Timestamp (UTC) of the evaluation.
    """

    mandate_id: str
    requested_scope: ScopeReference
    requested_action: MandateAction
    admitted: bool
    refusal_reason: str = ""
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for audit / chaining."""
        return {
            "mandate_id": self.mandate_id,
            "requested_scope": self.requested_scope,
            "requested_action": self.requested_action.value,
            "admitted": self.admitted,
            "refusal_reason": self.refusal_reason,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Mandate resolution stub
# ---------------------------------------------------------------------------


def check_mandate(
    mandate: EngagementMandate,
    requested_scope: ScopeReference,
    requested_action: MandateAction,
    *,
    at_instant: datetime | None = None,
) -> MandateReceipt:
    """Evaluate whether *mandate* admits a phase with the given scope and action.

    **STUB IMPLEMENTATION** -- replace with real resolver once the mandate
    store is available.

    Evaluation order
    ~~~~~~~~~~~~~~~~
    1. Validity window check. If the mandate is expired or not yet valid,
       refuse with ``"Mandate not currently valid"``.
    2. Scope containment check. If the requested scope is not contained
       within the mandate's scope, refuse with a descriptive reason.
    3. Action permit check. If the requested action is not in the
       mandate's permitted actions, refuse with a descriptive reason.
    4. All checks pass: return a receipt with ``admitted = True``.

    Args:
        mandate: The active mandate to evaluate against.
        requested_scope: The scope the phase is requesting.
        requested_action: The action the phase is performing.
        at_instant: Override the evaluation time. Defaults to
            :func:`datetime.now(timezone.utc)`. Supply a deterministic
            instant during replay to preserve idempotence.

    Returns:
        A :class:`MandateReceipt` describing the outcome.
    """
    _ = at_instant  # reserved for deterministic replay; currently unused in stub

    if not mandate.is_valid_at():
        return MandateReceipt(
            mandate_id=mandate.mandate_id,
            requested_scope=requested_scope,
            requested_action=requested_action,
            admitted=False,
            refusal_reason="Mandate not currently valid",
        )

    if not mandate.scope_contains(requested_scope):
        return MandateReceipt(
            mandate_id=mandate.mandate_id,
            requested_scope=requested_scope,
            requested_action=requested_action,
            admitted=False,
            refusal_reason=(f"Requested scope {requested_scope!r} is not covered by mandate scope {mandate.scope!r}"),
        )

    if not mandate.permits(requested_action):
        return MandateReceipt(
            mandate_id=mandate.mandate_id,
            requested_scope=requested_scope,
            requested_action=requested_action,
            admitted=False,
            refusal_reason=(
                f"Action {requested_action.value!r} is not permitted "
                f"by mandate {mandate.mandate_id!r}; "
                f"permitted actions: {[a.value for a in mandate.permitted_actions]}"
            ),
        )

    return MandateReceipt(
        mandate_id=mandate.mandate_id,
        requested_scope=requested_scope,
        requested_action=requested_action,
        admitted=True,
    )
