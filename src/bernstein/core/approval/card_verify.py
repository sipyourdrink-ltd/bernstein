"""Offline verification of resolved approval cards (issue #2511).

A resolved approval must be re-checkable offline from the audit chain alone.
:func:`verify_approval_cards` walks the ``chat.approval_card.issued`` and
``chat.approval_card.resolved`` events and proves, for every resolved card:

* the stored envelope still hashes to the recorded ``card_hash`` -- any
  post-hoc mutation of the stored envelope is detected, because a mutated
  envelope no longer hashes to the committed value,
* the decision echoed the issued envelope's ``card_hash`` (the operator
  decided against the fields that were hashed, not a divergent view),
* the decision landed before the envelope's ``not_after`` (expiry is
  reconstructable, not merely enforced live).

This is orthogonal to the HMAC chain check: the HMAC chain proves the event
bytes were not altered; this check proves the *card semantics* hold across the
issue/resolve pair. Together they make the card a decision record whose whole
context is verifiable after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.approval.card import ApprovalCardV2, card_hash
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
    """Outcome of :func:`verify_approval_cards`."""

    ok: bool
    errors: list[str]
    issued_count: int = 0
    resolved_count: int = 0
    reconstructed_count: int = 0


def _reconstruct_issued(events: list[AuditEvent], errors: list[str]) -> dict[str, ApprovalCardV2]:
    """Return ``card_hash -> envelope`` for every issue event, flagging mutation."""
    issued: dict[str, ApprovalCardV2] = {}
    for event in events:
        details: dict[str, Any] = event.details
        stored_hash = str(details.get("card_hash", ""))
        envelope_any: Any = details.get("envelope")
        if not stored_hash or not isinstance(envelope_any, dict):
            errors.append(f"approval card issue event {event.resource_id!r} is missing card_hash or envelope")
            continue
        card = ApprovalCardV2.from_dict(cast("dict[str, Any]", envelope_any))
        recomputed = card_hash(card)
        if recomputed != stored_hash:
            errors.append(
                f"approval card {stored_hash!r} envelope was mutated after issue "
                f"(stored hash {stored_hash[:16]}…, envelope hashes to {recomputed[:16]}…)",
            )
            continue
        issued[stored_hash] = card
    return issued


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
    """
    log = AuditLog(audit_dir=audit_dir, key=key) if key is not None else AuditLog(audit_dir=audit_dir)
    issued_events = log.query(event_type=EVENT_APPROVAL_CARD_ISSUED)
    resolved_events = log.query(event_type=EVENT_APPROVAL_CARD_RESOLVED)

    errors: list[str] = []
    issued = _reconstruct_issued(issued_events, errors)

    reconstructed = 0
    for event in resolved_events:
        details: dict[str, Any] = event.details
        echoed = str(details.get("card_hash", ""))
        card = issued.get(echoed)
        if card is None:
            errors.append(
                f"resolved approval card {echoed!r} has no matching issued envelope with an intact hash",
            )
            continue
        resolved_at = float(details.get("resolved_at", 0.0))
        if resolved_at and resolved_at >= card.not_after:
            errors.append(
                f"resolved approval card {echoed!r} was decided at {resolved_at:.0f} "
                f"at or after its not_after {card.not_after:.0f}",
            )
            continue
        reconstructed += 1

    return ApprovalCardVerifyResult(
        ok=not errors,
        errors=errors,
        issued_count=len(issued_events),
        resolved_count=len(resolved_events),
        reconstructed_count=reconstructed,
    )
