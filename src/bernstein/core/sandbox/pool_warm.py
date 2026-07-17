"""Warm claim-ahead keyed by effective manifest hash (#2547).

Warm-pool slots (:mod:`bernstein.core.agents.warm_pool`) are pre-provisioned so
pool-scoped dispatch has no cold-start latency. To keep the manifest contract
intact, a warm slot is keyed by the exact ``effective_manifest_hash`` it was
provisioned against, and a dispatch may only attach to a slot when the hashes
are *equal*. Infra drift between provisioning and claim surfaces as hash
divergence, not as a silently-wrong environment: the slot is quarantined with a
chained receipt and the dispatch falls back to cold provisioning (AC:
warm claim-ahead).

This module is deliberately a pair of pure predicates plus a chain-mirror
helper, so it can be wired into the existing warm-pool machinery without
changing any behaviour when no pool is configured.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore


class WarmClaimOutcome(Enum):
    """Result of testing a warm slot against a dispatch."""

    ATTACH = "attach"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class WarmSlotKey:
    """The identity of a pre-provisioned warm slot.

    Attributes:
        slot_id: Stable identifier of the warm slot.
        pool_hash: The pool the slot was provisioned for.
        effective_manifest_hash: The exact effective manifest the slot was
            provisioned against. A dispatch attaches only when its effective
            hash equals this value.
    """

    slot_id: str
    pool_hash: str
    effective_manifest_hash: str


def slot_matches(slot: WarmSlotKey, dispatch_effective_hash: str) -> bool:
    """Return whether *slot* may serve a dispatch with *dispatch_effective_hash*.

    Equality of the effective manifest hash is the whole contract: a warm slot
    provisioned for one manifest can never be handed to a dispatch that computed
    a different manifest, no matter how small the divergence.
    """
    return bool(slot.effective_manifest_hash) and slot.effective_manifest_hash == dispatch_effective_hash


def evaluate_warm_claim(slot: WarmSlotKey, dispatch_effective_hash: str) -> WarmClaimOutcome:
    """Return :class:`WarmClaimOutcome.ATTACH` on hash equality, else quarantine."""
    return WarmClaimOutcome.ATTACH if slot_matches(slot, dispatch_effective_hash) else WarmClaimOutcome.QUARANTINE


def record_quarantine(
    *,
    chain: AuditChainStore,
    slot: WarmSlotKey,
    dispatch_effective_hash: str,
) -> None:
    """Mirror a warm-slot quarantine into the HMAC audit chain.

    Records both the provisioned and dispatch effective hashes so the infra
    drift that caused the divergence is chain-attested, and the caller then
    falls back to cold provisioning.
    """
    from bernstein.core.security.audit_chain import record_pool_warm_quarantine

    record_pool_warm_quarantine(
        chain=chain,
        pool_hash=slot.pool_hash,
        provisioned_manifest_hash=slot.effective_manifest_hash,
        dispatch_manifest_hash=dispatch_effective_hash,
        slot_id=slot.slot_id,
    )


__all__ = [
    "WarmClaimOutcome",
    "WarmSlotKey",
    "evaluate_warm_claim",
    "record_quarantine",
    "slot_matches",
]
