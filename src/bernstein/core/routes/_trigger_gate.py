"""Shared direct-route ``triggers.yaml`` gating helpers for webhook endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from fastapi import Request

    from bernstein.core.orchestration.trigger_manager import TriggerGateResult, TriggerManager
    from bernstein.core.tasks.models import TriggerEvent

logger = logging.getLogger(__name__)

_POLICY_UNAVAILABLE_REASONS: Final[frozenset[str]] = frozenset(
    {"trigger policy unavailable", "declaration quarantined"}
)


@dataclass(frozen=True)
class DirectTriggerGate:
    """Matched direct-route trigger gate carried across task creation."""

    manager: TriggerManager
    gate: TriggerGateResult
    source: str
    summary: str
    dedup_key: str
    cooldown_scope: str | None = None
    unconfirmed_dedup: bool = False
    policy_unavailable: bool = False


def get_trigger_manager(request: Request) -> TriggerManager | None:
    """Return a ``TriggerManager`` scoped to this install's ``.sdd`` dir.

    Returns ``None`` when there is nowhere to load ``triggers.yaml`` from (no
    ``.sdd`` layout on ``request.app.state``, mirroring ``_bridge_paths``) or
    when no ``triggers.yaml`` is configured - the transparent pass-through case
    must not create ``TriggerManager``'s runtime state directory for installs
    that never opted into trigger rules, the same way ``bernstein triggers
    list`` skips construction when the file is absent.
    """
    from bernstein.core.orchestration.trigger_manager import TriggerManager
    from bernstein.core.security.declaration_quarantine import DeclarationQuarantineStore

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return None
    try:
        (sdd_dir / "config" / "triggers.yaml").stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        # Let the manager report unavailable policy and suppress mapped tasks.
        pass
    return TriggerManager(sdd_dir, quarantine_store=DeclarationQuarantineStore(sdd_dir.parent))


def _is_dedup_reservation_confirmed(
    manager: TriggerManager,
    dedup_key: str,
    dedup_expires_at: float | None,
) -> bool:
    """Return True when ``fire_log.jsonl`` confirms a produced task for this reservation."""
    if dedup_expires_at is None:
        return False
    try:
        history = manager.get_fire_history(limit=None)
    except (OSError, ValueError):
        logger.warning("Could not read trigger history for dedup confirmation", exc_info=True)
        return False
    for raw_row in cast("list[object]", history):
        if not isinstance(raw_row, dict):
            continue
        row = cast("dict[str, Any]", raw_row)
        if (
            row.get("dedup_key") == dedup_key
            and row.get("dedup_expires_at") == dedup_expires_at
            and bool(row.get("task_id"))
            and row.get("produced", True)
        ):
            return True
    return False


def gate_direct_trigger(
    request: Request,
    event: TriggerEvent,
    *,
    summary: str,
    reconcile_dedup: bool = False,
) -> DirectTriggerGate | None:
    """Evaluate ``triggers.yaml`` governance for a direct-route ``TriggerEvent``.

    Returns ``None`` when no ``triggers.yaml`` is configured or no enabled rule
    matches *event* (transparent pass-through). Returns a
    :class:`DirectTriggerGate` when a rule matches or when the policy file is
    corrupt/unreadable/quarantined (failing closed with ``gate.suppressed == True``).
    When *reconcile_dedup* is ``True``, a ``deduplicated`` suppression is checked
    against ``fire_log.jsonl`` so unconfirmed reservations set
    ``unconfirmed_dedup=True`` for retryable ``503`` handling.
    """
    from bernstein.core.orchestration.trigger_manager import TriggerGateResult, compute_dedup_key

    mgr = get_trigger_manager(request)
    if mgr is None:
        return None
    raw_scope = event.metadata.get("cooldown_scope")
    cooldown_scope = raw_scope if isinstance(raw_scope, str) and raw_scope else None
    try:
        gate = mgr.gate_direct_task(event)
    except (OSError, OverflowError, TypeError, ValueError):
        logger.warning("Could not evaluate %s trigger policy", event.source, exc_info=True)
        gate = TriggerGateResult("triggers.yaml", True, "trigger policy unavailable", None)
        return DirectTriggerGate(
            manager=mgr,
            gate=gate,
            source=event.source,
            summary=summary,
            dedup_key="",
            cooldown_scope=cooldown_scope,
            policy_unavailable=True,
        )
    if gate.trigger_name is None:
        return None
    dedup_key = gate.dedup_key or compute_dedup_key(gate.trigger_name, event)
    if gate.suppressed:
        if gate.reason in _POLICY_UNAVAILABLE_REASONS:
            return DirectTriggerGate(
                manager=mgr,
                gate=gate,
                source=event.source,
                summary=summary,
                dedup_key=dedup_key,
                cooldown_scope=cooldown_scope,
                policy_unavailable=True,
            )
        unconfirmed = (
            reconcile_dedup
            and gate.reason == "deduplicated"
            and not _is_dedup_reservation_confirmed(mgr, dedup_key, gate.dedup_expires_at)
        )
        return DirectTriggerGate(
            manager=mgr,
            gate=gate,
            source=event.source,
            summary=summary,
            dedup_key=dedup_key,
            cooldown_scope=cooldown_scope,
            unconfirmed_dedup=unconfirmed,
        )
    return DirectTriggerGate(
        manager=mgr,
        gate=gate,
        source=event.source,
        summary=summary,
        dedup_key=dedup_key,
        cooldown_scope=cooldown_scope,
    )


def record_direct_trigger_fire(pending: DirectTriggerGate, *, task_id: str = "") -> None:
    """Record a direct-route trigger outcome (allowed fire or confirmed suppression)."""
    if pending.gate.trigger_name is None:
        return
    suppressed = pending.gate.suppressed
    summary = f"{pending.summary} (suppressed: {pending.gate.reason})" if suppressed else pending.summary
    try:
        pending.manager.record_fire(
            trigger_name=pending.gate.trigger_name,
            source=pending.source,
            task_id=task_id,
            dedup_key=pending.dedup_key,
            summary=summary,
            produced=bool(task_id),
            cooldown_scope=pending.cooldown_scope,
            dedup_expires_at=None if suppressed else pending.gate.dedup_expires_at,
        )
    except OSError:
        logger.warning("Could not append %s trigger history", pending.source, exc_info=True)


def release_direct_trigger_reservation(pending: DirectTriggerGate) -> None:
    """Release an unconfirmed dedup reservation after task creation raises before persisting."""
    if pending.gate.suppressed or not pending.dedup_key or pending.gate.dedup_expires_at is None:
        return
    try:
        pending.manager.release_dedup_reservation(pending.dedup_key, pending.gate.dedup_expires_at)
    except OSError:
        logger.warning("Could not release %s trigger dedup reservation", pending.source, exc_info=True)
