"""Authenticated, execution-path-neutral run closure (#3469).

The closure marker is a checkable statement at one audit-chain position, not
a physical write barrier.  A later event for the same run leaves the historical
marker intact but invalidates a whole-run completeness claim made at that
position.  Absence remains ``open``; it is never promoted from silence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from bernstein.core.security.audit import AuditEvent
from bernstein.core.security.audit_chain import (
    EVENT_RUN_CLOSURE,
    AuditChainStore,
    record_run_closure,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RunClosureError(RuntimeError):
    """Raised when a closure write is malformed or conflicts with history."""


class RunClosureOutcome(StrEnum):
    """The four distinct facts a terminal marker may record."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class RunClosureStatus(StrEnum):
    """Verdict derived by walking authenticated chain events."""

    OPEN = "open"
    CLOSED = "closed"
    INVALIDATED = "invalidated"
    CONFLICTING = "conflicting"
    INVALID = "invalid"
    TAMPERED = "tampered"


@dataclass(frozen=True, slots=True)
class RunClosureProjection:
    """The recomputed closure state for one run."""

    run_id: str
    status: RunClosureStatus
    outcome: RunClosureOutcome | None = None
    terminal_boundary: str | None = None
    anchor_kind: str | None = None
    anchor_head: str | None = None
    anchor_count: int = 0
    errors: tuple[str, ...] = ()

    @property
    def complete_range(self) -> bool:
        """Whether the walked range ends at a still-valid closure marker."""
        return self.status is RunClosureStatus.CLOSED


def _details(event: AuditEvent | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(event, AuditEvent):
        return event.details
    value = event.get("details")
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


def _event_type(event: AuditEvent | Mapping[str, Any]) -> str:
    return event.event_type if isinstance(event, AuditEvent) else str(event.get("event_type", ""))


def _event_run_id(event: AuditEvent | Mapping[str, Any]) -> str:
    details = _details(event)
    fallback = event.resource_id if isinstance(event, AuditEvent) else event.get("resource_id", "")
    return str(details.get("run_id", fallback)).strip()


def _source_hmac(event: AuditEvent | Mapping[str, Any], *, witnessed: bool) -> str:
    details = _details(event)
    original_hmac = details.get("_original_hmac")
    if witnessed and original_hmac is not None:
        return str(original_hmac).strip()
    return event.hmac if isinstance(event, AuditEvent) else str(event.get("hmac", "")).strip()


def _coerce_outcome(value: RunClosureOutcome | str) -> RunClosureOutcome:
    try:
        return value if isinstance(value, RunClosureOutcome) else RunClosureOutcome(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in RunClosureOutcome)
        raise RunClosureError(f"unsupported run closure outcome {value!r}; expected one of: {allowed}") from exc


def _anchor_from_details(details: Mapping[str, Any]) -> tuple[str, str, int] | None:
    journal_head = str(details.get("run_journal_head", "")).strip()
    ledger_head = str(details.get("work_ledger_head", "")).strip()
    journal_count = details.get("run_journal_event_count", 0)
    ledger_count = details.get("work_ledger_entry_count", 0)
    has_journal = bool(journal_head)
    has_ledger = bool(ledger_head)
    if has_journal == has_ledger:
        return None
    head = journal_head if has_journal else ledger_head
    count = journal_count if has_journal else ledger_count
    if not _SHA256_RE.fullmatch(head) or not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        return None
    absent_count = ledger_count if has_journal else journal_count
    if absent_count != 0:
        return None
    return ("run_journal", head, count) if has_journal else ("work_ledger", head, count)


def close_run(
    *,
    chain: AuditChainStore,
    run_id: str,
    outcome: RunClosureOutcome | str,
    actor: str,
    run_journal_head: str = "",
    run_journal_event_count: int = 0,
    work_ledger_head: str = "",
    work_ledger_entry_count: int = 0,
) -> AuditEvent:
    """Write one idempotent closure marker, or return the identical marker.

    Exactly one authoritative state anchor is required.  A retry with the same
    outcome and anchor is safe; any existing non-identical marker fails closed.
    The read and append share the audit chain's cross-process transaction, so
    two writers cannot both observe absence and append duplicates.
    """
    resolved_run_id = run_id.strip()
    resolved_actor = actor.strip()
    resolved_outcome = _coerce_outcome(outcome)
    if not resolved_run_id:
        raise RunClosureError("run_id must not be empty")
    if not resolved_actor:
        raise RunClosureError("actor must not be empty")
    requested = {
        "run_id": resolved_run_id,
        "outcome": resolved_outcome.value,
        "run_journal_head": run_journal_head,
        "run_journal_event_count": run_journal_event_count,
        "work_ledger_head": work_ledger_head,
        "work_ledger_entry_count": work_ledger_entry_count,
    }
    if _anchor_from_details(requested) is None:
        raise RunClosureError(
            "closure requires exactly one valid SHA-256 state anchor and positive count "
            "(run journal or work ledger, never both)"
        )

    with chain.chain_transaction():
        # Filter on the witnessed run id rather than trusting resource_id
        # alone. The high-level writer keeps both equal, but a malformed or
        # adversarial pre-existing marker must still block a second closure.
        existing = [
            event
            for event in chain.query(event_type=EVENT_RUN_CLOSURE, include_archived=True)
            if _event_run_id(event) == resolved_run_id
        ]
        for event in existing:
            if all(event.details.get(key) == value for key, value in requested.items()):
                return event
        if existing:
            prior = existing[-1]
            raise RunClosureError(
                f"run {resolved_run_id!r} already has a conflicting closure marker "
                f"({prior.details.get('outcome')!r} at {prior.hmac[:12]}...)"
            )
        return record_run_closure(
            chain=chain,
            run_id=resolved_run_id,
            outcome=resolved_outcome.value,
            run_journal_head=run_journal_head,
            run_journal_event_count=run_journal_event_count,
            work_ledger_head=work_ledger_head,
            work_ledger_entry_count=work_ledger_entry_count,
            actor=resolved_actor,
        )


def derive_run_closure(
    events: Sequence[AuditEvent | Mapping[str, Any]],
    run_id: str,
    *,
    witnessed: bool = False,
) -> RunClosureProjection:
    """Recompute closure from an already authenticated, ordered chain range.

    ``witnessed=True`` is reserved for a rebuilt receipt range whose rows carry
    an authenticated ``details._original_hmac`` witness. Payload field presence
    never selects its own provenance mode.
    """
    resolved_run_id = run_id.strip()
    if not resolved_run_id:
        raise ValueError("run_id must not be empty")
    target = [(index, event) for index, event in enumerate(events) if _event_run_id(event) == resolved_run_id]
    markers = [(index, event) for index, event in target if _event_type(event) == EVENT_RUN_CLOSURE]
    if not markers:
        return RunClosureProjection(resolved_run_id, RunClosureStatus.OPEN)
    if len(markers) != 1:
        return RunClosureProjection(
            resolved_run_id,
            RunClosureStatus.CONFLICTING,
            errors=(f"expected exactly one run.closure marker; found {len(markers)}",),
        )

    marker_index, marker = markers[0]
    details = _details(marker)
    try:
        outcome = RunClosureOutcome(str(details.get("outcome", "")))
    except ValueError:
        return RunClosureProjection(
            resolved_run_id,
            RunClosureStatus.INVALID,
            errors=(f"closure marker carries unsupported outcome {details.get('outcome')!r}",),
        )
    anchor = _anchor_from_details(details)
    if anchor is None:
        return RunClosureProjection(
            resolved_run_id,
            RunClosureStatus.INVALID,
            outcome=outcome,
            errors=("closure marker does not bind exactly one valid state anchor",),
        )
    boundary = _source_hmac(marker, witnessed=witnessed)
    if not boundary:
        return RunClosureProjection(
            resolved_run_id,
            RunClosureStatus.INVALID,
            outcome=outcome,
            errors=("closure marker has no authenticated chain boundary",),
        )
    later = [event for index, event in target if index > marker_index]
    status = RunClosureStatus.INVALIDATED if later else RunClosureStatus.CLOSED
    errors = (f"{len(later)} later event(s) for run appear after the closure marker",) if later else ()
    anchor_kind, anchor_head, anchor_count = anchor
    return RunClosureProjection(
        resolved_run_id,
        status,
        outcome=outcome,
        terminal_boundary=boundary,
        anchor_kind=anchor_kind,
        anchor_head=anchor_head,
        anchor_count=anchor_count,
        errors=errors,
    )


def project_run_closure(chain: AuditChainStore, run_id: str) -> RunClosureProjection:
    """Verify the complete audit chain and derive one run's closure state."""
    with chain.chain_transaction():
        ok, errors, events = chain.verify_and_query(include_archived=True)
    if not ok:
        return RunClosureProjection(
            run_id.strip(),
            RunClosureStatus.TAMPERED,
            errors=tuple(errors),
        )
    return derive_run_closure(events, run_id)


__all__ = [
    "RunClosureError",
    "RunClosureOutcome",
    "RunClosureProjection",
    "RunClosureStatus",
    "close_run",
    "derive_run_closure",
    "project_run_closure",
]
