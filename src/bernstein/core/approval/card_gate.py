"""Chain-anchored gate for approval card v2 issue / resolve.

Issue #2511. The gate is where the card stops being a message and becomes a
proof. It does three things, all against the HMAC audit chain rather than the
chat client:

* **Issue.** :meth:`ApprovalCardGate.issue` appends a
  ``chat.approval_card.issued`` event carrying the full canonical envelope,
  its ``card_hash``, and the previous chain digest. The issued envelope is now
  a signed, tamper-evident record.

* **Resolve with hash echo.** :meth:`ApprovalCardGate.resolve` refuses to
  settle unless the caller echoes the exact ``card_hash`` of an issued
  envelope. An echo that differs in any field the operator saw (impact,
  rollback text, expiry, args digest, reasoning) matches no issued card and is
  refused, with a ``chat.approval_card.refused`` event recorded and the tool
  call left un-executed.

* **Chain-side expiry.** Expiry is decided by the chain-side clock against the
  envelope's ``not_after`` -- never by what the chat client still renders. A
  resolve at or after ``not_after`` is refused and chain-recorded, including
  after a chat-process restart: the gate reconstructs issued cards from the
  audit chain, so a fresh process still refuses a stale approve whose buttons
  a client kept live.

Strip the audit chain and every guarantee above collapses: the "gate" becomes
an in-memory dict that a restart forgets. The chain is the substrate that
makes the card a decision record instead of a message with a log.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.approval.card import ApprovalCardV2, card_hash
from bernstein.core.security.audit_chain import (
    EVENT_APPROVAL_CARD_ISSUED,
    EVENT_APPROVAL_CARD_REFUSED,
    EVENT_APPROVAL_CARD_RESOLVED,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "REFUSAL_REASON_EXPIRED",
    "REFUSAL_REASON_HASH_MISMATCH",
    "ApprovalCardExpired",
    "ApprovalCardGate",
    "ApprovalCardHashMismatch",
    "IssuedCard",
]

#: Refusal reason recorded when the echoed ``card_hash`` matches no issued card.
REFUSAL_REASON_HASH_MISMATCH = "hash_mismatch"

#: Refusal reason recorded when a decision arrives at or after ``not_after``.
REFUSAL_REASON_EXPIRED = "expired"


class ApprovalCardHashMismatch(RuntimeError):
    """Raised when a resolve echoes a ``card_hash`` that no issued card matches.

    The gate records a ``chat.approval_card.refused`` event before raising so
    the tampered or unknown echo is visible in the chain, and the tool call is
    not allowed to proceed.
    """


class ApprovalCardExpired(RuntimeError):
    """Raised when a resolve arrives at or after the envelope's ``not_after``.

    Expiry is enforced by the chain-side clock regardless of what the chat
    client renders. The gate records a ``chat.approval_card.refused`` event
    before raising.
    """


@dataclass(frozen=True, slots=True)
class IssuedCard:
    """Bookkeeping for one issued approval card.

    Attributes:
        card: The canonical envelope.
        card_hash: Its committed hash (equals ``card_hash(card)``).
        worktree_id: Worktree the card is pinned to, when known.
        thread_id: Chat thread the card was delivered on, when known.
    """

    card: ApprovalCardV2
    card_hash: str
    worktree_id: str = ""
    thread_id: str = ""


class ApprovalCardGate:
    """Issues and resolves hash-committed approval cards against the audit chain.

    Args:
        chain: The audit chain store the gate appends events to and, on
            resolve after a restart, reconstructs issued cards from.
        install_id: Install identifier recorded on issued / resolved events.
        session_id: Session identifier recorded on issued / resolved events.
    """

    def __init__(self, chain: AuditChainStore, *, install_id: str = "", session_id: str = "") -> None:
        self._chain = chain
        self._install_id = install_id
        self._session_id = session_id
        self._issued: dict[str, IssuedCard] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Issue
    # ------------------------------------------------------------------

    def issue(
        self,
        card: ApprovalCardV2,
        *,
        worktree_id: str = "",
        thread_id: str = "",
        actor: str = "approval_card",
    ) -> IssuedCard:
        """Append a ``chat.approval_card.issued`` event and return the record.

        The event stores the full canonical envelope and its ``card_hash`` so
        a verifier can later reconstruct exactly the fields shown to the
        operator and detect any post-hoc mutation.
        """
        digest = card_hash(card)
        issued = IssuedCard(card=card, card_hash=digest, worktree_id=worktree_id, thread_id=thread_id)
        with self._lock:
            self._issued[digest] = issued
        self._chain.log_with_prev_digest(
            event_type=EVENT_APPROVAL_CARD_ISSUED,
            actor=actor,
            resource_type="approval_card",
            resource_id=digest,
            details={
                "card_hash": digest,
                "envelope": card.to_dict(),
                "worktree_id": worktree_id,
                "thread_id": thread_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )
        return issued

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    def resolve(
        self,
        *,
        card_hash: str,
        decision: str,
        approver: str = "",
        worktree_id: str = "",
        now: float | None = None,
    ) -> IssuedCard:
        """Resolve an issued card, enforcing hash echo and chain-side expiry.

        Args:
            card_hash: The ``card_hash`` echoed by the decision. Must match an
                issued envelope exactly.
            decision: ``approve`` or ``reject``.
            approver: Identifier of the operator who decided.
            worktree_id: Worktree the decision was made from (recorded).
            now: Injected clock for deterministic tests; defaults to
                ``time.time()``.

        Returns:
            The :class:`IssuedCard` that was resolved.

        Raises:
            ApprovalCardHashMismatch: When *card_hash* matches no issued card.
            ApprovalCardExpired: When the decision arrives at or after the
                envelope's ``not_after``.
        """
        current = time.time() if now is None else now
        echoed = card_hash
        issued = self._lookup(echoed)
        if issued is None:
            self._refuse(
                card_hash=echoed,
                reason=REFUSAL_REASON_HASH_MISMATCH,
                approver=approver,
                worktree_id=worktree_id,
                expected_card_hash="",
            )
            raise ApprovalCardHashMismatch(
                f"echoed card_hash {echoed!r} matches no issued approval card; refusing to resolve",
            )
        if issued.card.is_expired(now=current):
            self._refuse(
                card_hash=echoed,
                reason=REFUSAL_REASON_EXPIRED,
                approver=approver,
                worktree_id=worktree_id or issued.worktree_id,
                expected_card_hash=issued.card_hash,
            )
            raise ApprovalCardExpired(
                f"approval card {echoed!r} expired at not_after={issued.card.not_after:.0f}; "
                f"refusing to resolve at {current:.0f} regardless of the chat client's rendered buttons",
            )
        self._chain.log_with_prev_digest(
            event_type=EVENT_APPROVAL_CARD_RESOLVED,
            actor=approver or "operator",
            resource_type="approval_card",
            resource_id=echoed,
            details={
                "card_hash": echoed,
                "decision": decision,
                "approver": approver,
                "worktree_id": issued.worktree_id,
                "resolved_at": current,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )
        return issued

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _lookup(self, digest: str) -> IssuedCard | None:
        """Return the issued card for *digest*, reconstructing from the chain.

        In-memory issued cards are consulted first; on a miss (for example
        after a chat-process restart) the audit chain is walked for the
        matching ``chat.approval_card.issued`` event and the envelope is
        rehydrated so expiry can still be enforced.
        """
        with self._lock:
            hit = self._issued.get(digest)
        if hit is not None:
            return hit
        for event in self._chain.query(event_type=EVENT_APPROVAL_CARD_ISSUED):
            details: dict[str, Any] = event.details
            if str(details.get("card_hash", "")) != digest:
                continue
            envelope_any: Any = details.get("envelope")
            if not isinstance(envelope_any, dict):
                continue
            card = ApprovalCardV2.from_dict(cast("dict[str, Any]", envelope_any))
            # Only trust a reconstructed envelope whose stored hash still
            # matches its bytes; a mutated envelope is rejected as unknown.
            if card_hash(card) != digest:
                continue
            issued = IssuedCard(
                card=card,
                card_hash=digest,
                worktree_id=str(details.get("worktree_id", "")),
                thread_id=str(details.get("thread_id", "")),
            )
            with self._lock:
                self._issued.setdefault(digest, issued)
            return issued
        return None

    def _refuse(
        self,
        *,
        card_hash: str,
        reason: str,
        approver: str,
        worktree_id: str,
        expected_card_hash: str,
    ) -> None:
        """Append a ``chat.approval_card.refused`` event."""
        self._chain.log_with_prev_digest(
            event_type=EVENT_APPROVAL_CARD_REFUSED,
            actor=approver or "operator",
            resource_type="approval_card",
            resource_id=card_hash,
            details={
                "card_hash": card_hash,
                "reason": reason,
                "expected_card_hash": expected_card_hash,
                "approver": approver,
                "worktree_id": worktree_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )
