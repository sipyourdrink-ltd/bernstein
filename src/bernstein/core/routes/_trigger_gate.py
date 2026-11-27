"""Shared direct-route ``triggers.yaml`` gating helpers for webhook endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

    from bernstein.core.orchestration.trigger_manager import TriggerGateResult, TriggerManager
    from bernstein.core.tasks.models import TriggerEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DirectTriggerGate:
    """Matched direct-route trigger gate carried across task creation."""

    manager: TriggerManager
    gate: TriggerGateResult
    source: str
    summary: str


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


def gate_direct_trigger(
    request: Request,
    event: TriggerEvent,
    *,
    summary: str,
) -> DirectTriggerGate | None:
    """Evaluate ``triggers.yaml`` governance for a direct-route ``TriggerEvent``.

    Returns ``None`` when no ``triggers.yaml`` is configured or no enabled rule
    matches *event* (transparent pass-through). Returns a
    :class:`DirectTriggerGate` when a rule matches or when the policy file is
    corrupt/unreadable (failing closed with ``gate.suppressed == True``).
    """
    from bernstein.core.orchestration.trigger_manager import TriggerGateResult

    mgr = get_trigger_manager(request)
    if mgr is None:
        return None
    try:
        gate = mgr.gate_direct_task(event)
    except (OSError, OverflowError, TypeError, ValueError):
        logger.warning("Could not evaluate %s trigger policy", event.source, exc_info=True)
        gate = TriggerGateResult("triggers.yaml", True, "trigger policy unavailable", None)
        return DirectTriggerGate(manager=mgr, gate=gate, source=event.source, summary=summary)
    if gate.trigger_name is None:
        return None
    return DirectTriggerGate(manager=mgr, gate=gate, source=event.source, summary=summary)


def record_direct_trigger_fire(pending: DirectTriggerGate, *, task_id: str) -> None:
    """Record an allowed direct-route trigger fire after task creation succeeds."""
    assert pending.gate.trigger_name is not None
    assert pending.gate.dedup_key is not None
    try:
        pending.manager.record_fire(
            trigger_name=pending.gate.trigger_name,
            source=pending.source,
            task_id=task_id,
            dedup_key=pending.gate.dedup_key,
            summary=pending.summary,
        )
    except OSError:
        logger.warning("Could not append %s trigger history", pending.source, exc_info=True)
