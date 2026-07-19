"""Offline verification of resolved approval cards (issue #2511).

A resolved approval must be re-checkable offline from the audit chain alone.
:func:`verify_approval_cards` walks the ``chat.approval_card.issued`` and
``chat.approval_card.resolved`` events and proves, for every resolved card:

* the stored envelope still hashes to the recorded ``card_hash`` -- any
  post-hoc mutation of the stored envelope is detected, because a mutated
  envelope no longer hashes to the committed value,
* the decision echoed the issued envelope's ``card_hash`` (the operator
  decided against the fields that were hashed, not a divergent view),
* the issue event was recorded *before* the resolution that settles it, so a
  settlement cannot be legitimised by an issue backfilled afterwards,
* no card is settled twice. The gate refuses a replay live, but a chain written
  by an unpatched build or a second writer can still carry two settlements of
  one card, so exactly-once is reconstructed here rather than assumed,
* the decision carries a usable timestamp and landed inside the envelope's
  window: ``created_at <= resolved_at < not_after``, with ``resolved_at``
  required to be finite and strictly positive. Expiry is reconstructable, not
  merely enforced live.

This is orthogonal to the HMAC chain check: the HMAC chain proves the event
bytes were not altered; this check proves the *card semantics* hold across the
issue/resolve pair. Together they make the card a decision record whose whole
context is verifiable after the fact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.approval.card import ApprovalCardV2, card_hash
from bernstein.core.approval.card_gate import ALLOWED_DECISIONS
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    EVENT_APPROVAL_CARD_RESOLVED,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent

__all__ = ["ApprovalCardVerifyResult", "verify_approval_cards"]


@dataclass(frozen=True)
class ApprovalCardVerifyResult:
    """Outcome of :func:`verify_approval_cards`.

    Failures are split by *who they accuse*, because the two demand opposite
    responses from an operator:

    * ``errors`` -- the record was evaluated and it failed. This accuses the
      audit data: something was mutated, replayed, or settled illegitimately.
      The operator should treat it as a possible breach.
    * ``verifier_errors`` -- the record could **not** be evaluated because this
      code raised unexpectedly. This accuses *us*. The operator should file a
      bug, not a security incident.

    Conflating them is its own defect: reporting an internal fault through the
    same channel as "envelope was mutated after issue" tells an operator their
    log was tampered with when in fact our verifier is broken.

    Both keep ``ok`` at ``False``. An unevaluable record is not a passing
    record, and a bug in the verifier must never produce a clean bill of health.

    Attributes:
        ok: ``True`` only when neither list has entries.
        errors: Records that were evaluated and failed. Accuses the data.
        verifier_errors: Records that could not be evaluated. Accuses this code.
    """

    ok: bool
    errors: list[str]
    issued_count: int = 0
    resolved_count: int = 0
    reconstructed_count: int = 0
    verifier_errors: list[str] = field(default_factory=lambda: [])


def _admit_issue(
    event: AuditEvent,
    issued: dict[str, ApprovalCardV2],
    origins: dict[str, tuple[str, str]],
    errors: list[str],
) -> None:
    """Admit one issue event into *issued*, flagging mutation or a bad envelope."""
    details: dict[str, Any] = event.details
    stored_hash = str(details.get("card_hash", ""))
    envelope_any: Any = details.get("envelope")
    if not stored_hash or not isinstance(envelope_any, dict):
        errors.append(f"approval card issue event {event.resource_id!r} is missing card_hash or envelope")
        return
    # Both the rebuild and the recompute run inside the guard. ``card_hash``
    # raises on a non-finite value because canonical JSON refuses ``NaN``, so
    # leaving it outside would turn a detection into an escaping exception: a
    # tamperer could plant one ``NaN`` and abort the whole `audit verify` run,
    # suppressing the unrelated pillars that execute after this one.
    try:
        card = ApprovalCardV2.from_dict(cast("dict[str, Any]", envelope_any))
        recomputed = card_hash(card)
    except (TypeError, ValueError) as exc:
        errors.append(f"approval card {stored_hash!r} issue envelope is not a valid card ({exc})")
        return
    if recomputed != stored_hash:
        errors.append(
            f"approval card {stored_hash!r} envelope was mutated after issue "
            f"(stored hash {stored_hash[:16]}, envelope hashes to {recomputed[:16]})",
        )
        return
    issued[stored_hash] = card
    origins[stored_hash] = (
        str(details.get("worktree_id", "")),
        str(details.get("thread_id", "")),
    )


def _resolved_at(details: dict[str, Any]) -> float | None:
    """Return a finite, strictly positive ``resolved_at``, or ``None``.

    Anything else -- absent, non-numeric, zero, negative, or non-finite -- is
    rejected by the caller. Zero and negative readings are not merely odd: the
    previous check short-circuited on a falsy ``resolved_at``, so a resolution
    recorded with ``0`` skipped the expiry comparison entirely, and ``NaN``
    made every ordering comparison false.
    """
    raw: Any = details.get("resolved_at")
    # bool is an int subclass, so it is excluded before the numeric check: a
    # ``True`` timestamp silently meaning 1.0 would pass every later test.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _check_decision(echoed: str, details: dict[str, Any], errors: list[str]) -> bool:
    """Reject a settlement whose decision is not one the gate would accept."""
    decision = details.get("decision")
    if decision in ALLOWED_DECISIONS:
        return True
    errors.append(
        f"resolved approval card {echoed!r} carries decision {decision!r}, "
        f"which is not one of {sorted(ALLOWED_DECISIONS)}",
    )
    return False


def _check_origin(
    echoed: str,
    details: dict[str, Any],
    issue_origin: tuple[str, str] | None,
    errors: list[str],
) -> bool:
    """Reject a settlement that came from an origin the card was not pinned to.

    The gate refuses this live, but the same argument that makes
    double-settlement worth reconstructing applies here: a chain written by an
    unpatched build or a second writer still carries the violation, and the
    settlement event records both origins, so it is recoverable.

    The pinned origin is taken from the *issue* event when it is available,
    rather than from the ``issued_*`` keys on the settlement, because those keys
    sit on the record under suspicion: a forger who set both halves to the same
    value would otherwise clear its own check.

    The ``issued_*`` keys are also the format marker. Settlements written before
    this attribution existed recorded the *issuing* origin in ``worktree_id``
    and no conversation at all, so their ``worktree_id`` does not mean "where
    the decision came from" and there is nothing to compare it against. Those
    records are skipped rather than failed: the audit log is append-only, so
    reporting a violation the record's own format cannot express would
    permanently red-flag chains that are in fact intact.
    """
    claimed: dict[str, Any] = {
        "worktree": details.get("issued_worktree_id"),
        "conversation": details.get("issued_thread_id"),
    }
    if all(claim is None for claim in claimed.values()):
        return True

    resolving = {
        "worktree": str(details.get("worktree_id", "")),
        "conversation": str(details.get("thread_id", "")),
    }
    pinned = (
        {"worktree": issue_origin[0], "conversation": issue_origin[1]}
        if issue_origin is not None
        else {label: str(claim or "") for label, claim in claimed.items()}
    )

    ok = True
    for label, claim in claimed.items():
        if claim is None:
            continue
        expected = pinned[label]
        if not expected:
            continue
        if resolving[label] != expected:
            errors.append(
                f"resolved approval card {echoed!r} was settled from {label} {resolving[label]!r} "
                f"but was issued into {label} {expected!r}",
            )
            ok = False
        elif str(claim) != expected:
            errors.append(
                f"resolved approval card {echoed!r} claims issuing {label} {str(claim)!r} "
                f"but the issue event recorded {expected!r}",
            )
            ok = False
    return ok


def _check_resolution(
    event: AuditEvent,
    issued: dict[str, ApprovalCardV2],
    origins: dict[str, tuple[str, str]],
    settled: set[str],
    errors: list[str],
) -> bool:
    """Validate one resolve event against the issues seen earlier in the chain.

    Returns ``True`` when the resolution is fully reconstructable.
    """
    details: dict[str, Any] = event.details
    echoed = str(details.get("card_hash", ""))
    if echoed in settled:
        # Exactly-once is the whole point of the gate's terminality guard, so it
        # has to be provable from the chain and not only enforced live. A chain
        # written by an unpatched build, or by a second writer, carries the
        # double settlement as evidence; refusing to flag it here would leave
        # the invariant unauditable.
        errors.append(
            f"approval card {echoed!r} was settled more than once; a card settles exactly once",
        )
        return False
    card = issued.get(echoed)
    if card is None:
        # Either the hash names no issued envelope at all, or the issue event
        # is recorded *after* this resolution. Both are rejected: a settlement
        # cannot legitimately precede the issue it settles.
        errors.append(
            f"resolved approval card {echoed!r} has no matching issued envelope with an intact hash "
            f"recorded before it in the chain",
        )
        return False
    resolved_at = _resolved_at(details)
    if resolved_at is None:
        errors.append(
            f"resolved approval card {echoed!r} carries a missing or invalid resolved_at "
            f"({details.get('resolved_at')!r}); a settlement with no usable timestamp cannot be "
            f"checked against the envelope's window",
        )
        return False
    if resolved_at < card.created_at:
        errors.append(
            f"resolved approval card {echoed!r} was decided at {resolved_at:.0f}, "
            f"before its envelope's created_at {card.created_at:.0f}",
        )
        return False
    if resolved_at >= card.not_after:
        errors.append(
            f"resolved approval card {echoed!r} was decided at {resolved_at:.0f} "
            f"at or after its not_after {card.not_after:.0f}",
        )
        return False
    # Both are evaluated (not short-circuited) so one settlement reports every
    # way in which it is invalid rather than only the first.
    decision_ok = _check_decision(echoed, details, errors)
    origin_ok = _check_origin(echoed, details, origins.get(echoed), errors)
    return decision_ok and origin_ok


def verify_approval_cards(audit_dir: Path, *, key: bytes | None = None) -> ApprovalCardVerifyResult:
    """Verify every resolved approval card in *audit_dir* offline.

    Args:
        audit_dir: Directory holding the HMAC-chained audit JSONL files.
        key: Optional HMAC key. Only used to read the events; the semantic
            checks here do not depend on the key (the HMAC chain check does).

    Returns:
        An :class:`ApprovalCardVerifyResult`. ``ok`` is ``True`` when no
        resolved card references a mutated envelope, an unknown ``card_hash``,
        or a decision made after expiry (and when there are no cards at all).
        Records that failed evaluation land in ``errors``; records this code
        could not evaluate land in ``verifier_errors``. Either keeps ``ok`` at
        ``False``.
    """
    log = AuditLog(audit_dir=audit_dir, key=key) if key is not None else AuditLog(audit_dir=audit_dir)

    # One ordered pass over the chain rather than two independent queries. The
    # order is the evidence: reading issues and resolutions separately loses the
    # happens-before relation between them, which is exactly what lets a
    # backfilled issue event legitimise a resolution that was recorded first.
    events = [
        event for event in log.query() if event.event_type in {EVENT_APPROVAL_CARD_ISSUED, EVENT_APPROVAL_CARD_RESOLVED}
    ]

    errors: list[str] = []
    verifier_errors: list[str] = []
    issued: dict[str, ApprovalCardV2] = {}
    origins: dict[str, tuple[str, str]] = {}
    settled: set[str] = set()
    issued_count = 0
    resolved_count = 0
    reconstructed = 0

    for event in events:
        # A verifier that its own input can crash is a denial-of-audit
        # primitive: `bernstein audit verify` runs this pillar before three
        # others, with no try/except of its own, so an escaping exception
        # suppresses detection of unrelated tampering. Every event is therefore
        # processed under a guard that turns any residual fault into a reported
        # failure. The specific faults are handled above; this exists so a
        # future lossy path cannot silently reopen that hole.
        #
        # The catch is deliberately broad, and the result is reported through
        # ``verifier_errors`` rather than ``errors``: a bug in this code must
        # not be presented to an operator as evidence that their audit log was
        # tampered with.
        try:
            if event.event_type == EVENT_APPROVAL_CARD_ISSUED:
                issued_count += 1
                _admit_issue(event, issued, origins, errors)
                continue
            resolved_count += 1
            if _check_resolution(event, issued, origins, settled, errors):
                reconstructed += 1
            settled.add(str(event.details.get("card_hash", "")))
        except Exception as exc:
            verifier_errors.append(
                f"internal verifier fault on approval card event {event.resource_id!r}: "
                f"{type(exc).__name__}: {exc}. This is a bug in bernstein's approval-card "
                f"verifier, not evidence of audit tampering; the record could not be "
                f"evaluated either way. Please report it.",
            )

    return ApprovalCardVerifyResult(
        # An unevaluable record is not a passing record: a fault in this code
        # must never be able to produce a clean bill of health.
        ok=not errors and not verifier_errors,
        errors=errors,
        issued_count=issued_count,
        resolved_count=resolved_count,
        reconstructed_count=reconstructed,
        verifier_errors=verifier_errors,
    )
