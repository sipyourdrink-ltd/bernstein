"""In-memory reference tracker adapter.

Used by every tracker unit test as the canonical
:class:`AbstractTrackerAdapter` implementation. Concrete adapters
(Linear, Jira, GitHub Projects v2, etc.) are exercised against the same
test cases via this fake so behavioural drift between adapters is
caught early.

Design notes
------------

* Ticket ids are sequential strings (``"T-1"``, ``"T-2"``, ...).
* Each ticket carries an opaque integer ``etag`` that is bumped on every
  mutation. ``claim_ticket`` and ``transition`` honour the etag.
* ``add_comment`` stores ``idempotency_key`` per ticket; replays with
  the same key plus identical body return the original ``CommentResult``;
  replays with a different body raise :class:`IdempotencyConflict`.
* ``rate_limit_after`` lets a test inject deterministic backoff: after
  N successful calls, the next call raises :class:`RateLimited`.
* ``unavailable`` toggles :class:`TrackerUnavailable` on every call --
  useful for "tracker is down" tests.

The fake does *not* try to mimic any specific vendor's API. It mimics
the *contract*: the orchestrator-facing surface every adapter must
provide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.trackers.contract import (
    AbstractTrackerAdapter,
    AttachResult,
    ClaimResult,
    CommentResult,
    IdempotencyConflict,
    OptimisticConcurrencyError,
    RateLimited,
    Ticket,
    TrackerError,
    TrackerUnavailable,
    TransitionResult,
)
from bernstein.core.trackers.synthetic import probe_body, probe_title

if TYPE_CHECKING:
    from collections.abc import Iterator


__all__ = ["InMemoryTicket", "InMemoryTracker"]


@dataclass
class InMemoryTicket:
    """Mutable internal representation used by :class:`InMemoryTracker`."""

    id: str
    title: str
    body: str
    status: str = "open"
    labels: tuple[str, ...] = ()
    etag: int = 1
    claimed_by: str | None = None
    comments: list[dict[str, Any]] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Per-ticket idempotency ledger keyed on (operation, idempotency_key).
    idempotency: dict[tuple[str, str], Any] = field(default_factory=dict)


class InMemoryTracker(AbstractTrackerAdapter):
    """Deterministic, in-process tracker fixture.

    Args:
        rate_limit_after: When set, the Nth call (1-indexed) raises
            :class:`RateLimited` once and the counter resets. Useful for
            verifying back-off propagation.
        retry_after: ``retry_after`` value attached to the injected
            :class:`RateLimited`.
        unavailable: When ``True`` every method raises
            :class:`TrackerUnavailable`.
    """

    name: str = "in_memory"

    def __init__(
        self,
        *,
        rate_limit_after: int | None = None,
        retry_after: float | None = 1.5,
        unavailable: bool = False,
    ) -> None:
        self._tickets: dict[str, InMemoryTicket] = {}
        self._next_id: int = 1
        self._call_count: int = 0
        self._rate_limit_after = rate_limit_after
        self._retry_after = retry_after
        self._unavailable = unavailable

    # ---- Test helpers ----------------------------------------------------

    def seed(
        self,
        title: str,
        body: str = "",
        *,
        status: str = "open",
        labels: tuple[str, ...] = (),
    ) -> InMemoryTicket:
        """Insert a ticket into the fake; return the stored record."""
        ticket = InMemoryTicket(
            id=f"T-{self._next_id}",
            title=title,
            body=body,
            status=status,
            labels=labels,
        )
        self._tickets[ticket.id] = ticket
        self._next_id += 1
        return ticket

    def set_unavailable(self, value: bool) -> None:
        """Toggle :class:`TrackerUnavailable` on every subsequent call."""
        self._unavailable = value

    # ---- AbstractTrackerAdapter ------------------------------------------

    def pull_open_tickets(self, filter: dict[str, Any] | None = None) -> Iterator[Ticket]:
        self._check_health()
        wanted_status = None
        if filter is not None:
            wanted_status = filter.get("status")
        for ticket in list(self._tickets.values()):
            if wanted_status is not None and ticket.status != wanted_status:
                continue
            if wanted_status is None and ticket.status != "open":
                continue
            yield self._snapshot(ticket)

    def claim_ticket(
        self,
        ticket_id: str,
        agent_id: str,
        *,
        etag: str | None = None,
    ) -> ClaimResult:
        self._check_health()
        ticket = self._require_ticket(ticket_id)
        self._enforce_etag(ticket, etag)
        if ticket.claimed_by is not None and ticket.claimed_by != agent_id:
            return ClaimResult(claimed=False, ticket_id=ticket_id, agent_id=agent_id, etag=str(ticket.etag))
        ticket.claimed_by = agent_id
        ticket.etag += 1
        return ClaimResult(claimed=True, ticket_id=ticket_id, agent_id=agent_id, etag=str(ticket.etag))

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> CommentResult:
        self._check_health()
        ticket = self._require_ticket(ticket_id)
        if idempotency_key is not None:
            ledger_key = ("comment", idempotency_key)
            previous = ticket.idempotency.get(ledger_key)
            if previous is not None:
                if previous["body"] != body:
                    msg = f"Idempotency key {idempotency_key!r} reused with different body on ticket {ticket_id}"
                    raise IdempotencyConflict(msg)
                return CommentResult(comment_id=previous["comment_id"], ticket_id=ticket_id)
        comment_id = f"C-{len(ticket.comments) + 1}"
        ticket.comments.append({"id": comment_id, "body": body})
        if idempotency_key is not None:
            ticket.idempotency[("comment", idempotency_key)] = {
                "comment_id": comment_id,
                "body": body,
            }
        return CommentResult(comment_id=comment_id, ticket_id=ticket_id)

    def transition(
        self,
        ticket_id: str,
        status_id: str,
        *,
        idempotency_key: str | None = None,
        etag: str | None = None,
    ) -> TransitionResult:
        self._check_health()
        ticket = self._require_ticket(ticket_id)
        self._enforce_etag(ticket, etag)
        if idempotency_key is not None:
            ledger_key = ("transition", idempotency_key)
            previous = ticket.idempotency.get(ledger_key)
            if previous is not None:
                if previous["status_id"] != status_id:
                    msg = f"Idempotency key {idempotency_key!r} reused with different status on ticket {ticket_id}"
                    raise IdempotencyConflict(msg)
                return TransitionResult(
                    ticket_id=ticket_id,
                    new_status=status_id,
                    etag=str(previous["etag"]),
                )
        ticket.status = status_id
        ticket.etag += 1
        if idempotency_key is not None:
            ticket.idempotency[("transition", idempotency_key)] = {
                "status_id": status_id,
                "etag": ticket.etag,
            }
        return TransitionResult(ticket_id=ticket_id, new_status=status_id, etag=str(ticket.etag))

    def attach_blob(
        self,
        ticket_id: str,
        blob: bytes,
        mime: str,
        *,
        idempotency_key: str | None = None,
    ) -> AttachResult:
        self._check_health()
        ticket = self._require_ticket(ticket_id)
        if idempotency_key is not None:
            ledger_key = ("attach", idempotency_key)
            previous = ticket.idempotency.get(ledger_key)
            if previous is not None:
                if previous["sha"] != _digest(blob) or previous["mime"] != mime:
                    msg = f"Idempotency key {idempotency_key!r} reused with different blob on ticket {ticket_id}"
                    raise IdempotencyConflict(msg)
                return AttachResult(attachment_id=previous["attachment_id"], ticket_id=ticket_id)
        attachment_id = f"A-{len(ticket.attachments) + 1}"
        ticket.attachments.append({"id": attachment_id, "mime": mime, "size": len(blob)})
        if idempotency_key is not None:
            ticket.idempotency[("attach", idempotency_key)] = {
                "attachment_id": attachment_id,
                "mime": mime,
                "sha": _digest(blob),
            }
        return AttachResult(attachment_id=attachment_id, ticket_id=ticket_id)

    # ---- Synthetic-transaction probe -------------------------------------

    def create_probe_ticket(self, marker: str) -> str:
        """Create a throwaway ticket titled with ``marker``; return its id."""
        return self.seed(probe_title(marker), probe_body(marker)).id

    def find_probe_tickets(self, marker: str) -> tuple[str, ...]:
        """Return the ids of every ticket whose title carries ``marker``.

        Scans every status, not just ``open``: the probe transitions its
        entity before deleting it.
        """
        return tuple(t.id for t in self._tickets.values() if marker in t.title)

    def delete_probe_ticket(self, ticket_id: str, marker: str) -> None:
        """Delete ``ticket_id``, refusing any ticket whose title lacks ``marker``."""
        ticket = self._require_ticket(ticket_id)
        if marker not in ticket.title:
            msg = f"Ticket {ticket_id} does not carry the synthetic marker {marker!r}; refusing to delete it."
            raise TrackerError(msg)
        del self._tickets[ticket_id]

    # ---- Internals -------------------------------------------------------

    def _check_health(self) -> None:
        if self._unavailable:
            raise TrackerUnavailable("InMemoryTracker is marked unavailable.")
        if self._rate_limit_after is not None:
            self._call_count += 1
            if self._call_count > self._rate_limit_after:
                self._call_count = 0
                raise RateLimited(
                    "InMemoryTracker rate limit reached.",
                    retry_after=self._retry_after,
                )

    def _require_ticket(self, ticket_id: str) -> InMemoryTicket:
        try:
            return self._tickets[ticket_id]
        except KeyError as exc:
            msg = f"Unknown ticket {ticket_id!r}"
            raise KeyError(msg) from exc

    @staticmethod
    def _enforce_etag(ticket: InMemoryTicket, etag: str | None) -> None:
        if etag is None:
            return
        if str(ticket.etag) != etag:
            msg = f"Stale etag {etag!r} for ticket {ticket.id}; current etag is {ticket.etag}."
            raise OptimisticConcurrencyError(msg)

    @staticmethod
    def _snapshot(ticket: InMemoryTicket) -> Ticket:
        return Ticket(
            id=ticket.id,
            external_url=f"https://in-memory.invalid/tickets/{ticket.id}",
            title=ticket.title,
            body=ticket.body,
            status=ticket.status,
            labels=ticket.labels,
            etag=str(ticket.etag),
        )


def _digest(blob: bytes) -> str:
    """Stable short digest for idempotency comparison.

    Uses ``hashlib.sha256`` rather than the built-in ``hash`` so the
    value is deterministic across processes.
    """
    import hashlib

    return hashlib.sha256(blob).hexdigest()
