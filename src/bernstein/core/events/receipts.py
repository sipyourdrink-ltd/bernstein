"""Receipts: fire receipts and absence (negative-proof) receipts (#2548).

Two receipt shapes anchor automation to the chain:

* A **fire receipt** commits, before any effect runs, to the rule hash, the
  matched triggering-event HMACs, and the rendered action's digest. An executed
  action whose digest is not covered by a receipt is rejected. The same rule
  against the same event bytes renders a byte-identical action, so the receipt's
  commitment is reproducible by any verifier.
* An **absence receipt** is a negative proof: it asserts that no event matching a
  label exists between two named chain positions. A verifier confirms this
  against the stored window; injecting a matching event into the window makes the
  receipt fail.

The chain-writing dispatch lives here too: :func:`dispatch_action` writes the
fire receipt to the audit chain *before* invoking the effect, then records the
executed action as its own chain event, so an automated intervention is as
explainable in a postmortem as a human one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.events.actions import RenderedAction, render_action, rule_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from bernstein.core.events.actions import ActionSpec
    from bernstein.core.events.grammar import CanonicalEvent
    from bernstein.core.events.triggers import AbsenceViolation
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore


class ActionRejected(RuntimeError):
    """Raised when an action is dispatched without a matching fire receipt."""


# ---------------------------------------------------------------------------
# Fire receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FireReceipt:
    """A commitment to a rule fire, minted before the effect executes.

    Attributes:
        rule_hash: The ``sha256:`` identity of the rule that fired.
        matched_event_hmacs: The HMACs of the events that matched the rule, in
            chain order. The triggering event's HMAC is one of these.
        action_kind: The rendered action's kind.
        action_digest: The rendered action's digest - the effect this receipt
            authorises and nothing else.
        rendered_action: The rendered action's canonical mapping, for the record.
    """

    rule_hash: str
    matched_event_hmacs: tuple[str, ...]
    action_kind: str
    action_digest: str
    rendered_action: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialisable mapping for this receipt."""
        return {
            "rule_hash": self.rule_hash,
            "matched_event_hmacs": list(self.matched_event_hmacs),
            "action_kind": self.action_kind,
            "action_digest": self.action_digest,
            "rendered_action": self.rendered_action,
        }


def build_fire_receipt(
    *,
    rule_spec: Mapping[str, Any],
    matched_events: Sequence[CanonicalEvent],
    action: ActionSpec,
    triggering_event: CanonicalEvent,
) -> FireReceipt:
    """Build a fire receipt binding a rule, its matched events, and its action.

    Args:
        rule_spec: The operator's full rule mapping (hashed into the receipt).
        matched_events: The events that satisfied the rule; their HMACs are
            recorded so a verifier can locate them in the window.
        action: The action to render and authorise.
        triggering_event: The event the action is rendered against; it must be
            among ``matched_events``.

    Returns:
        The fire receipt.
    """
    rendered = render_action(action, triggering_event)
    matched_hmacs = tuple(event.hmac for event in matched_events)
    if triggering_event.hmac not in matched_hmacs:
        # The receipt must not authorise an action rendered against an event
        # outside the matched set: that would let a rule fire on an event it
        # never actually matched.
        raise ValueError("triggering event must be among the matched events")
    return FireReceipt(
        rule_hash=rule_hash(rule_spec),
        matched_event_hmacs=matched_hmacs,
        action_kind=rendered.kind,
        action_digest=rendered.digest,
        rendered_action=rendered.to_dict(),
    )


def authorize_action(receipt: FireReceipt, rendered_action: RenderedAction) -> bool:
    """Return whether ``receipt`` authorises ``rendered_action``.

    The receipt authorises exactly one effect: the digest it committed to, whose
    triggering event is among the receipt's matched HMACs. Any other action -
    including one that differs by a single rendered byte - is unauthorised.
    """
    return (
        receipt.action_digest == rendered_action.digest
        and rendered_action.triggering_event_hmac in receipt.matched_event_hmacs
    )


def verify_fire_receipt(
    receipt: FireReceipt,
    *,
    rule_spec: Mapping[str, Any],
    action: ActionSpec,
    events_by_hmac: Mapping[str, CanonicalEvent],
) -> tuple[bool, list[str]]:
    """Verify a fire receipt against the rule, action, and the event window.

    Confirms the rule hash, that every matched HMAC is present in the window,
    that the triggering event is itself among the matched events, that the
    receipt's ``action_kind`` matches what the action re-renders to, and that
    re-rendering from the recorded triggering event reproduces the committed
    digest byte-for-byte.
    """
    errors: list[str] = []
    if rule_hash(rule_spec) != receipt.rule_hash:
        errors.append("rule hash mismatch")

    for hmac in receipt.matched_event_hmacs:
        if hmac not in events_by_hmac:
            errors.append(f"matched event not present in window: {hmac[:16]}")

    triggering_hmac = str(receipt.rendered_action.get("triggering_event_hmac", ""))
    if triggering_hmac not in receipt.matched_event_hmacs:
        # A receipt whose action was rendered against an event outside its own
        # matched set is not a valid fire commitment.
        errors.append("triggering event not among matched events")
    triggering_event = events_by_hmac.get(triggering_hmac)
    if triggering_event is None:
        errors.append("triggering event not present in window")
    else:
        re_rendered = render_action(action, triggering_event)
        if re_rendered.kind != receipt.action_kind:
            errors.append("action kind diverges from receipt")
        if re_rendered.digest != receipt.action_digest:
            errors.append("re-rendered action digest diverges from receipt")
        if re_rendered.to_dict() != receipt.rendered_action:
            errors.append("re-rendered action body diverges from receipt")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Absence (negative-proof) receipts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AbsenceReceipt:
    """A negative proof: no matching event between two named chain positions.

    Attributes:
        after_hmac: The lower named chain position (the anchoring event A).
        to_hmac: The upper named chain position observed.
        expect: The label glob asserted absent across the span.
    """

    after_hmac: str
    to_hmac: str
    expect: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialisable mapping for this receipt."""
        return {"after_hmac": self.after_hmac, "to_hmac": self.to_hmac, "expect": self.expect}


def build_absence_receipt(violation: AbsenceViolation) -> AbsenceReceipt:
    """Build an absence receipt from an evaluated :class:`AbsenceViolation`."""
    return AbsenceReceipt(after_hmac=violation.after_hmac, to_hmac=violation.to_hmac, expect=violation.expect)


def verify_absence_receipt(
    receipt: AbsenceReceipt,
    window_events: Sequence[CanonicalEvent],
) -> tuple[bool, list[str]]:
    """Confirm the negative proof against a stored window.

    The named positions must both exist in the window, and no event strictly
    after ``after_hmac`` up to and including ``to_hmac`` may match the asserted
    label. Injecting a matching event into that span makes verification fail.

    Uses the ``fnmatch`` label semantics of the trigger layer.
    """
    from fnmatch import fnmatchcase

    errors: list[str] = []
    by_hmac = {event.hmac: event for event in window_events}
    after = by_hmac.get(receipt.after_hmac)
    upper = by_hmac.get(receipt.to_hmac)
    if after is None:
        errors.append("after position not present in window")
    if upper is None:
        errors.append("upper position not present in window")
    if after is None or upper is None:
        return False, errors
    if upper.position < after.position:
        errors.append("upper position precedes after position")
        return False, errors

    for event in window_events:
        if after.position < event.position <= upper.position and fnmatchcase(event.label, receipt.expect):
            errors.append(f"matching event present at position {event.position}: {event.hmac[:16]}")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Chain-writing dispatch: receipt before effect
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Outcome of dispatching an action under receipt discipline.

    Attributes:
        fire_receipt_event: The chain event recording the fire receipt.
        action_event: The chain event recording the executed action.
        effect_result: Whatever the effect callable returned.
    """

    fire_receipt_event: AuditEvent
    action_event: AuditEvent
    effect_result: Any


def _find_completed_action_event(chain: AuditChainStore, *, action_digest: str) -> AuditEvent | None:
    """Return a prior *completed* automation-action event for ``action_digest``.

    A completed record (``result_status == "dispatched"``) proves the effect
    already ran to success, so a replay must not run it again.
    """
    from bernstein.core.security.audit_chain import EVENT_AUTOMATION_ACTION

    for event in chain.query(event_type=EVENT_AUTOMATION_ACTION, resource_id=action_digest):
        details = event.details
        status = str(details.get("result_status", ""))
        if status == "dispatched" and str(details.get("action_digest", "")) == action_digest:
            return event
    return None


def _find_fire_receipt_event(chain: AuditChainStore, *, action_digest: str) -> AuditEvent | None:
    """Return the fire-receipt event that committed to ``action_digest``, if any."""
    from bernstein.core.security.audit_chain import EVENT_RULE_FIRE_RECEIPT

    events = chain.query(event_type=EVENT_RULE_FIRE_RECEIPT, resource_id=action_digest)
    return events[0] if events else None


def dispatch_action(
    *,
    chain: AuditChainStore,
    receipt: FireReceipt,
    rendered_action: RenderedAction,
    effect: Callable[[RenderedAction], Any],
    actor: str = "events_automation",
) -> DispatchResult:
    """Authorise, commit a fire receipt, run the effect, and record the outcome.

    The chain records leave no audit gap across a failure:

    1. The action is authorised against the receipt (a pure check, no write).
    2. The fire receipt lands on the chain as the pre-effect commitment.
    3. An *intent* record (``result_status="pending"``) is written **before** the
       effect runs, so a crash mid-effect still shows the attempt.
    4. The effect runs. A success writes a ``dispatched`` outcome; an exception
       writes a ``failed`` outcome and then re-raises - either way the outcome is
       on the chain.

    Dispatch is idempotent, keyed on the fire receipt's action digest: if a
    ``dispatched`` outcome for this exact action already exists, the effect is not
    run again and the recorded events are returned. This makes a retry after a
    post-effect record failure safe (the completed effect is not repeated).

    Raises:
        ActionRejected: When ``receipt`` does not authorise ``rendered_action``.
    """
    from bernstein.core.security.audit_chain import (
        record_automation_action,
        record_rule_fire_receipt,
    )

    if not authorize_action(receipt, rendered_action):
        raise ActionRejected(
            f"action {rendered_action.digest[:16]} is not covered by fire receipt {receipt.action_digest[:16]}"
        )

    # Idempotent replay: a completed outcome for this action digest means the
    # effect already succeeded; return the recorded events without re-running it.
    prior = _find_completed_action_event(chain, action_digest=rendered_action.digest)
    if prior is not None:
        fire_event = _find_fire_receipt_event(chain, action_digest=receipt.action_digest) or prior
        return DispatchResult(fire_receipt_event=fire_event, action_event=prior, effect_result=None)

    fire_event = record_rule_fire_receipt(
        chain=chain,
        rule_hash=receipt.rule_hash,
        matched_event_hmacs=list(receipt.matched_event_hmacs),
        action_kind=receipt.action_kind,
        action_digest=receipt.action_digest,
        actor=actor,
    )

    # Intent before the effect: closes the pre-effect audit gap.
    record_automation_action(
        chain=chain,
        action_kind=rendered_action.kind,
        action_digest=rendered_action.digest,
        fire_receipt_hmac=fire_event.hmac,
        triggering_event_hmac=rendered_action.triggering_event_hmac,
        result_status="pending",
        actor=actor,
    )

    try:
        effect_result = effect(rendered_action)
    except Exception:
        # Record the failure outcome before propagating, so the effect exception
        # never leaves the chain without an outcome.
        record_automation_action(
            chain=chain,
            action_kind=rendered_action.kind,
            action_digest=rendered_action.digest,
            fire_receipt_hmac=fire_event.hmac,
            triggering_event_hmac=rendered_action.triggering_event_hmac,
            result_status="failed",
            actor=actor,
        )
        raise

    action_event = record_automation_action(
        chain=chain,
        action_kind=rendered_action.kind,
        action_digest=rendered_action.digest,
        fire_receipt_hmac=fire_event.hmac,
        triggering_event_hmac=rendered_action.triggering_event_hmac,
        result_status="dispatched",
        actor=actor,
    )
    return DispatchResult(fire_receipt_event=fire_event, action_event=action_event, effect_result=effect_result)


__all__ = [
    "AbsenceReceipt",
    "ActionRejected",
    "DispatchResult",
    "FireReceipt",
    "authorize_action",
    "build_absence_receipt",
    "build_fire_receipt",
    "dispatch_action",
    "verify_absence_receipt",
    "verify_fire_receipt",
]
