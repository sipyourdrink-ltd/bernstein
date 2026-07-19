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

* **Exactly-once settlement.** A card settles once. The check-and-commit runs
  under one lock, so concurrent decisions on the same ``card_hash`` cannot both
  append a settlement, and the settled set is rebuilt from the chain's
  ``resolved`` / terminally-``refused`` events rather than from memory alone.
  A restart therefore does not reopen a card the chain already shows as
  decided, which is what turns a captured ``card_hash`` from a reusable
  bearer token into a single-use one.

* **Origin pinning.** A card issued into a worktree and a conversation commits
  to that origin, and a decision arriving from a different one is refused and
  chain-recorded rather than honoured.

Strip the audit chain and every guarantee above collapses: the "gate" becomes
an in-memory dict that a restart forgets. The chain is the substrate that
makes the card a decision record instead of a message with a log.
"""

from __future__ import annotations

import math
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
    "ALLOWED_DECISIONS",
    "REFUSAL_REASON_ALREADY_SETTLED",
    "REFUSAL_REASON_BEFORE_ISSUE",
    "REFUSAL_REASON_CROSS_CONVERSATION",
    "REFUSAL_REASON_CROSS_WORKTREE",
    "REFUSAL_REASON_EXPIRED",
    "REFUSAL_REASON_HASH_MISMATCH",
    "REFUSAL_REASON_INVALID_DECISION",
    "ApprovalCardAlreadySettled",
    "ApprovalCardBindingMismatch",
    "ApprovalCardClockSkew",
    "ApprovalCardExpired",
    "ApprovalCardGate",
    "ApprovalCardHashMismatch",
    "ApprovalCardInvalidDecision",
    "IssuedCard",
]

#: Refusal reason recorded when the echoed ``card_hash`` matches no issued card.
REFUSAL_REASON_HASH_MISMATCH = "hash_mismatch"

#: Refusal reason recorded when a decision arrives at or after ``not_after``.
REFUSAL_REASON_EXPIRED = "expired"

#: Refusal reason recorded when the card has already reached a terminal state.
REFUSAL_REASON_ALREADY_SETTLED = "already_settled"

#: Refusal reason recorded when the decision value is not in :data:`ALLOWED_DECISIONS`.
REFUSAL_REASON_INVALID_DECISION = "invalid_decision"

#: Refusal reason recorded when a pinned card is resolved from another worktree.
REFUSAL_REASON_CROSS_WORKTREE = "cross_worktree"

#: Refusal reason recorded when a pinned card is resolved from another conversation.
REFUSAL_REASON_CROSS_CONVERSATION = "cross_conversation"

#: Refusal reason recorded when the decision clock predates the envelope's
#: ``created_at``. The offline verifier requires
#: ``created_at <= resolved_at < not_after``, so accepting such a decision would
#: append a permanently unverifiable record to an append-only chain.
REFUSAL_REASON_BEFORE_ISSUE = "before_issue"

#: The only decision values a card may settle with. Anything else (an empty
#: string, a different case, a driver-specific synonym such as ``approve_all``)
#: is refused: an unrecognised decision that reached the chain unvalidated would
#: be recorded as a settlement whose meaning no verifier could reconstruct.
ALLOWED_DECISIONS = frozenset({"approve", "reject"})

#: Refusal reasons that settle a card permanently.
#:
#: Only expiry qualifies. Expiry is monotone -- once the chain has seen a card
#: pass its ``not_after`` no later clock reading can make it live again -- so
#: replaying it is sound. The other reasons describe a rejected *attempt*, not a
#: settled card: burning the card on a ``cross_worktree`` or ``hash_mismatch``
#: refusal would hand any party who can reach the chat surface a denial of
#: service against the legitimate operator's pending decision.
_TERMINAL_REFUSAL_REASONS = frozenset({REFUSAL_REASON_EXPIRED})


def _is_terminal_event(event_type: str, details: dict[str, Any]) -> bool:
    """Return ``True`` when a chain event settles its card permanently.

    A ``resolved`` event always settles. A ``refused`` event settles only when
    its reason is in :data:`_TERMINAL_REFUSAL_REASONS`.
    """
    if event_type == EVENT_APPROVAL_CARD_RESOLVED:
        return True
    return event_type == EVENT_APPROVAL_CARD_REFUSED and str(details.get("reason", "")) in _TERMINAL_REFUSAL_REASONS


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


class ApprovalCardAlreadySettled(RuntimeError):
    """Raised when a resolve targets a card that already reached a terminal state.

    This is the replay guard. Without it one issued ``card_hash`` could be
    settled arbitrarily many times, and a process restart reopened an
    already-approved card because reconstruction replayed only the issue
    events.
    """


class ApprovalCardInvalidDecision(RuntimeError):
    """Raised when the decision value is not in :data:`ALLOWED_DECISIONS`."""


class ApprovalCardClockSkew(RuntimeError):
    """Raised when the decision clock predates the envelope's ``created_at``.

    Refusing here keeps the gate and the offline verifier symmetric: the gate
    cannot append a settlement that ``bernstein audit verify`` would then reject
    forever on an append-only chain.
    """


class ApprovalCardBindingMismatch(RuntimeError):
    """Raised when a resolve arrives from a worktree or conversation the card is not pinned to.

    A card issued into worktree ``W`` and conversation ``C`` commits to that
    origin. Settling it from elsewhere would let a party who observed the
    ``card_hash`` in one context exercise the approval in another.
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
        #: Hashes known to have reached a terminal state in this process. It is
        #: a cache, not the source of truth: :meth:`_state_for` falls back to
        #: the chain so a restart (or a second gate over the same audit dir)
        #: still sees settlements this process never made.
        self._settled: set[str] = set()
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

        The chain append happens *before* the card is published to the
        in-memory index. A card that is resolvable but absent from the chain
        would be an approval with no issue record: the offline verifier would
        report the later resolution as referencing an unknown envelope, and a
        durability fault on the append would leave a settleable card behind. By
        ordering the write first, a failed append raises and leaves no
        resolvable card anywhere.
        """
        digest = card_hash(card)
        issued = IssuedCard(card=card, card_hash=digest, worktree_id=worktree_id, thread_id=thread_id)
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
        with self._lock:
            self._issued[digest] = issued
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
        thread_id: str = "",
        now: float | None = None,
    ) -> IssuedCard:
        """Resolve an issued card, atomically and exactly once.

        The whole check-and-commit sequence runs under the gate lock, so two
        concurrent decisions on one ``card_hash`` cannot both observe an
        unsettled card and both append a settlement. The card is marked settled
        in the same critical section that writes the ``resolved`` event.

        Checks, in order, each refused into the chain before raising:

        1. the echoed hash names an issued envelope,
        2. the card has not already reached a terminal state,
        3. the decision is in :data:`ALLOWED_DECISIONS`,
        4. the chain-side clock is at or after ``created_at``,
        5. the chain-side clock is before ``not_after``,
        6. the origin worktree matches, when the card was pinned to one,
        7. the origin conversation matches, when the card was pinned to one.

        The settlement event records the origin the decision *arrived from*,
        keeping the issuing origin under ``issued_worktree_id`` /
        ``issued_thread_id``, so the chain attributes the decision to whoever
        actually made it.

        Args:
            card_hash: The ``card_hash`` echoed by the decision. Must match an
                issued envelope exactly.
            decision: ``approve`` or ``reject``.
            approver: Identifier of the operator who decided.
            worktree_id: Worktree the decision was made from. Compared against
                the issuing worktree whenever the card was pinned to one, empty
                value included, and recorded on the settlement event.
            thread_id: Conversation the decision arrived on. Compared against
                the issuing conversation whenever the card was pinned to one,
                empty value included, and recorded on the settlement event.
            now: Injected clock for deterministic tests; defaults to
                ``time.time()``.

        Returns:
            The :class:`IssuedCard` that was resolved.

        Raises:
            ApprovalCardHashMismatch: When *card_hash* matches no issued card.
            ApprovalCardAlreadySettled: When the card is already terminal.
            ApprovalCardInvalidDecision: When *decision* is not allowed.
            ApprovalCardClockSkew: When the decision clock predates the
                envelope's ``created_at``.
            ApprovalCardExpired: When the decision arrives at or after the
                envelope's ``not_after``.
            ApprovalCardBindingMismatch: When the origin worktree or
                conversation differs from the one the card was pinned to.
        """
        current = time.time() if now is None else now
        echoed = card_hash
        with self._lock:
            issued, settled = self._state_for(echoed)
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
            self._guard_settled(echoed, settled=settled, approver=approver, worktree_id=worktree_id, issued=issued)
            self._guard_decision(decision, echoed, approver=approver, worktree_id=worktree_id, issued=issued)
            self._guard_clock(echoed, approver=approver, worktree_id=worktree_id, issued=issued, current=current)
            self._guard_expiry(echoed, approver=approver, worktree_id=worktree_id, issued=issued, current=current)
            self._guard_binding(
                echoed,
                approver=approver,
                worktree_id=worktree_id,
                thread_id=thread_id,
                issued=issued,
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
                    # The origin the decision actually arrived from. Recording
                    # the issuing origin here instead would make the chain
                    # attest that the legitimate worktree and conversation made
                    # a decision that in fact came from somewhere else, which
                    # is precisely the attribution this gate exists to prove.
                    # The issuing origin is kept alongside, under its own keys.
                    "worktree_id": worktree_id,
                    "thread_id": thread_id,
                    "issued_worktree_id": issued.worktree_id,
                    "issued_thread_id": issued.thread_id,
                    "resolved_at": current,
                    "install_id": self._install_id,
                    "session_id": self._session_id,
                },
            )
            self._settled.add(echoed)
        return issued

    # ------------------------------------------------------------------
    # Resolve guards
    # ------------------------------------------------------------------

    def _guard_settled(
        self,
        echoed: str,
        *,
        settled: bool,
        approver: str,
        worktree_id: str,
        issued: IssuedCard,
    ) -> None:
        """Refuse a card that already reached a terminal state."""
        if not settled:
            return
        self._refuse(
            card_hash=echoed,
            reason=REFUSAL_REASON_ALREADY_SETTLED,
            approver=approver,
            worktree_id=worktree_id or issued.worktree_id,
            expected_card_hash=issued.card_hash,
        )
        raise ApprovalCardAlreadySettled(
            f"approval card {echoed!r} is already settled on the audit chain; refusing to settle it a second time",
        )

    def _guard_decision(
        self,
        decision: str,
        echoed: str,
        *,
        approver: str,
        worktree_id: str,
        issued: IssuedCard,
    ) -> None:
        """Refuse a decision value outside :data:`ALLOWED_DECISIONS`."""
        if decision in ALLOWED_DECISIONS:
            return
        self._refuse(
            card_hash=echoed,
            reason=REFUSAL_REASON_INVALID_DECISION,
            approver=approver,
            worktree_id=worktree_id or issued.worktree_id,
            expected_card_hash=issued.card_hash,
        )
        raise ApprovalCardInvalidDecision(
            f"decision {decision!r} is not one of {sorted(ALLOWED_DECISIONS)}; refusing to resolve",
        )

    def _guard_clock(
        self,
        echoed: str,
        *,
        approver: str,
        worktree_id: str,
        issued: IssuedCard,
        current: float,
    ) -> None:
        """Refuse a decision whose clock predates the envelope's ``created_at``.

        This is the lower half of the window the offline verifier enforces
        (``created_at <= resolved_at < not_after``). Without it the gate would
        happily append a settlement that its own verifier then rejects, and
        because the audit log is append-only and HMAC-chained, that record would
        make ``bernstein audit verify`` fail permanently with no remediation.
        The gate must never be able to write a chain it cannot verify, so this
        mirrors the verifier's rule in full. The verifier requires
        ``resolved_at`` to be finite and strictly positive as well as at or
        after ``created_at``; checking only the lower bound would still let a
        card with ``created_at <= 0`` settle at ``now=0.0`` and write a
        permanently unverifiable record, which is the exact outcome this guard
        exists to prevent. Reachable from any caller passing ``now`` explicitly
        and from a clock at or near the epoch.
        """
        if math.isfinite(current) and current > 0.0 and current >= issued.card.created_at:
            return
        self._refuse(
            card_hash=echoed,
            reason=REFUSAL_REASON_BEFORE_ISSUE,
            approver=approver,
            worktree_id=worktree_id,
            expected_card_hash=issued.card_hash,
        )
        raise ApprovalCardClockSkew(
            f"approval card {echoed!r} was issued at created_at={issued.card.created_at!r} "
            f"but the decision clock reads {current!r}; a settlement timestamp must be finite, "
            f"strictly positive, and at or after created_at, so recording this one would "
            f"produce a chain the offline verifier permanently rejects",
        )

    def _guard_expiry(
        self,
        echoed: str,
        *,
        approver: str,
        worktree_id: str,
        issued: IssuedCard,
        current: float,
    ) -> None:
        """Refuse a decision at or after the envelope's ``not_after``."""
        if not issued.card.is_expired(now=current):
            return
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

    def _guard_binding(
        self,
        echoed: str,
        *,
        approver: str,
        worktree_id: str,
        thread_id: str,
        issued: IssuedCard,
    ) -> None:
        """Refuse a decision whose origin differs from the card's pinned origin.

        Each check is skipped only when the card was issued *without* the
        corresponding pin. Once a card carries a pin the comparison is
        unconditional, including against an empty incoming origin: the value the
        guard exists to distrust must not be able to disable the guard by being
        absent. A caller that cannot state where a decision came from cannot
        settle a card that was pinned to a specific origin.
        """
        if issued.worktree_id and worktree_id != issued.worktree_id:
            self._refuse(
                card_hash=echoed,
                reason=REFUSAL_REASON_CROSS_WORKTREE,
                approver=approver,
                worktree_id=worktree_id,
                expected_card_hash=issued.card_hash,
                expected_worktree_id=issued.worktree_id,
            )
            raise ApprovalCardBindingMismatch(
                f"approval card {echoed!r} is pinned to worktree {issued.worktree_id!r} "
                f"and cannot be resolved from worktree {worktree_id!r}",
            )
        if issued.thread_id and thread_id != issued.thread_id:
            self._refuse(
                card_hash=echoed,
                reason=REFUSAL_REASON_CROSS_CONVERSATION,
                approver=approver,
                worktree_id=worktree_id or issued.worktree_id,
                expected_card_hash=issued.card_hash,
                thread_id=thread_id,
                expected_thread_id=issued.thread_id,
            )
            raise ApprovalCardBindingMismatch(
                f"approval card {echoed!r} was issued on conversation {issued.thread_id!r} "
                f"and cannot be resolved from conversation {thread_id!r}",
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rehydrate(self, digest: str, details: dict[str, Any]) -> IssuedCard | None:
        """Rebuild an :class:`IssuedCard` from a stored issue event, or ``None``."""
        envelope_any: Any = details.get("envelope")
        if not isinstance(envelope_any, dict):
            return None
        try:
            card = ApprovalCardV2.from_dict(cast("dict[str, Any]", envelope_any))
            # The recompute is inside the guard, not after it. ``card_hash``
            # raises on a non-finite value because canonical JSON refuses
            # ``NaN``, and an escaping exception here is worse than a rejected
            # envelope: because ``query`` does not check HMAC, anyone who can
            # write the log could prepend one crafted issue event claiming a
            # victim's ``card_hash`` and make that legitimate card raise on
            # every resolve, forever, on an append-only log -- with nothing
            # recorded, so the denial of service would itself be unaudited.
            # Returning None instead leaves the scan free to find the real
            # issue event, and an unmatched digest is refused on the chain.
            recomputed = card_hash(card)
        except (TypeError, ValueError):
            # A stored envelope carrying a non-finite or non-numeric value is
            # treated as unknown rather than rehydrated: a NaN not_after would
            # produce a card that never expires.
            return None
        # Only trust a reconstructed envelope whose stored hash still matches
        # its bytes; a mutated envelope is rejected as unknown.
        if recomputed != digest:
            return None
        return IssuedCard(
            card=card,
            card_hash=digest,
            worktree_id=str(details.get("worktree_id", "")),
            thread_id=str(details.get("thread_id", "")),
        )

    def _chain_state(self, digest: str) -> tuple[IssuedCard | None, bool]:
        """Return ``(issued_card, settled)`` for *digest* from the audit chain.

        One ordered pass answers both questions. They are deliberately resolved
        together: the settlement check cannot be served from memory alone (a
        second process over the same audit dir may have settled the card), so
        it always costs a chain read, and folding the issue lookup into the same
        read keeps a resolve at one pass over the log instead of three.

        The read is scoped to this card's ``resource_id`` (every issue, resolve
        and refuse event for the card is written with ``resource_id == digest``).
        The store rejects non-matching lines before parsing them, so a
        first-time resolve reads only this card's handful of events rather than
        scanning the whole log. Without that scope a stream of unknown
        ``card_hash`` values - none of which is ever cached, because no issued
        card is found for them - would each force a full O(chain) parse, a
        denial-of-service amplifier that worsens as the chain ages.
        """
        issued: IssuedCard | None = None
        settled = False
        for event in self._chain.query(resource_id=digest):
            details: dict[str, Any] = event.details
            # Belt and braces: the query already scoped to resource_id == digest,
            # but the settlement meaning is carried by details.card_hash, so a
            # crafted event that reused the resource_id without matching the
            # committed hash is not allowed to count as this card's.
            if str(details.get("card_hash", "")) != digest:
                continue
            if event.event_type == EVENT_APPROVAL_CARD_ISSUED:
                if issued is None:
                    issued = self._rehydrate(digest, details)
            elif _is_terminal_event(event.event_type, details):
                settled = True
        return issued, settled

    def _state_for(self, digest: str) -> tuple[IssuedCard | None, bool]:
        """Return ``(issued_card, settled)`` for *digest*, chain-backed.

        The in-memory index only ever short-circuits the *envelope* lookup. The
        settled flag is always taken from the chain unless this process already
        recorded the settlement, because a card settled by another process (or
        before a restart) is invisible to this one's memory. That fallback is
        what stops a restart from reopening a decided card: reconstructing only
        the issue events, as the gate previously did, made every settled
        approval replayable by anyone who kept its ``card_hash``.
        """
        with self._lock:
            cached = self._issued.get(digest)
            if digest in self._settled:
                return cached, True
        scanned, settled = self._chain_state(digest)
        issued = cached if cached is not None else scanned
        with self._lock:
            if issued is not None:
                self._issued.setdefault(digest, issued)
            if settled:
                self._settled.add(digest)
        return issued, settled

    def _refuse(
        self,
        *,
        card_hash: str,
        reason: str,
        approver: str,
        worktree_id: str,
        expected_card_hash: str,
        thread_id: str = "",
        expected_worktree_id: str = "",
        expected_thread_id: str = "",
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
                "thread_id": thread_id,
                "expected_worktree_id": expected_worktree_id,
                "expected_thread_id": expected_thread_id,
                "install_id": self._install_id,
                "session_id": self._session_id,
            },
        )
