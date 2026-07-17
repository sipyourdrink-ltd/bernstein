"""Idempotent, chain-anchored delivery of a mission digest to chat (#2510).

The recurring mission digest fire computes a canonical digest
(:mod:`bernstein.core.orchestration.mission_digest`), anchors it in the HMAC
audit chain, and posts it to a chat platform through the common bridge protocol
(:class:`bernstein.core.chat.bridge.BridgeProtocol`). Two properties are
load-bearing:

* **Deterministic fire projection** -- the fire is anchored on a
  :func:`bernstein.core.orchestration.schedule_projection.project` graph hash
  keyed by ``(mission_id, fire_time, mission_status_hash)``, so two operators
  provably fired the byte-identical digest for the byte-identical fire instant.
* **Idempotent delivery** -- delivery is keyed on the digest receipt id, a pure
  function of ``(mission_id, fire_time, digest_hash)``. :class:`DigestDeliveryLedger`
  records the delivered receipt id durably (append-only, fsynced), so a restart
  between fire computation and chat delivery does not double-post: a re-fire of
  the same instant recomputes the identical digest and receipt id, sees it
  already delivered, and is a no-op. A genuinely missed fire (never delivered)
  is recomputable to the identical digest after restart and delivered exactly
  once.

The digest travels only over :meth:`BridgeProtocol.send_message`, so delivery
works uniformly across every shipped driver (Slack, Discord, Telegram) with no
driver-specific code path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.orchestration.mission_digest import (
    build_mission_digest,
    record_digest_receipt,
    render_digest_message,
)
from bernstein.core.orchestration.missions import project_mission_from_ledger

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.chat.bridge import BridgeProtocol
    from bernstein.core.orchestration.mission_digest import MissionDigest
    from bernstein.core.security.audit_chain import AuditChainStore


def digest_deliveries_dir(sdd_dir: Path) -> Path:
    """Return the per-install digest-delivery ledger directory."""
    return sdd_dir / "runtime" / "mission_digests"


def digest_fire_graph_hash(
    *,
    mission_id: str,
    fire_time: int,
    mission_status_hash: str,
    recurrence: str = "",
) -> str:
    """Return the deterministic schedule-fire graph hash for a digest fire.

    A thin, pure use of the shipped schedule-fire projection: the digest fire is
    a recurring goal whose ``last_state`` is the mission status hash, so two
    operators with the same mission state at the same instant land on the same
    ``graph_hash``. Bound into the digest receipt, it proves the digest was
    fired under the byte-identical schedule definition.
    """
    from bernstein.core.orchestration.schedule_projection import project

    result = project(
        schedule_id=f"mission-digest:{mission_id}",
        fire_time=fire_time,
        last_state={"mission_status_hash": mission_status_hash},
        goal=f"mission digest {mission_id}",
        recurrence=recurrence,
    )
    return result.graph_hash


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of a (possibly idempotent no-op) digest delivery."""

    receipt_id: str
    digest_hash: str
    posted: bool
    message_id: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "receipt_id": self.receipt_id,
            "digest_hash": self.digest_hash,
            "posted": self.posted,
            "message_id": self.message_id,
            "reason": self.reason,
        }


class DigestDeliveryLedger:
    """Durable append-only record of delivered digest receipt ids.

    One JSONL file per mission under ``<sdd>/runtime/mission_digests/``. The
    delivered set is the idempotency substrate: a receipt id present here has
    already been posted, so a re-fire (including after a restart) is a no-op.
    Reads scan the file fresh each call so a ledger constructed after a restart
    sees every prior delivery.
    """

    __slots__ = ("_path",)

    def __init__(self, sdd_dir: Path, mission_id: str) -> None:
        self._path = digest_deliveries_dir(sdd_dir) / f"{mission_id}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def delivered(self, receipt_id: str) -> bool:
        """Return ``True`` when *receipt_id* was already delivered."""
        return receipt_id in self._delivered_ids()

    def _delivered_ids(self) -> set[str]:
        if not self._path.exists():
            return set()
        out: set[str] = set()
        with self._path.open(encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    # Torn tail from a crash mid-write; prior rows stay valid.
                    continue
                receipt_id = row.get("receipt_id")
                if isinstance(receipt_id, str):
                    out.add(receipt_id)
        return out

    def mark_delivered(self, receipt_id: str, *, digest_hash: str, message_id: str, fire_time: int) -> None:
        """Durably record that *receipt_id* was delivered (append + fsync)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "receipt_id": receipt_id,
            "digest_hash": digest_hash,
            "message_id": message_id,
            "fire_time": fire_time,
        }
        line = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


async def deliver_digest(
    *,
    bridge: BridgeProtocol,
    thread_id: str,
    digest: MissionDigest,
    ledger: DigestDeliveryLedger,
) -> DeliveryOutcome:
    """Post *digest* to *thread_id* through *bridge*, idempotent on receipt id.

    If the digest's receipt id is already recorded in *ledger*, this is a no-op
    (``posted=False``) -- the guarantee a restart relies on. Otherwise the
    verbatim digest projection is sent through the common bridge send path and
    the receipt id is durably recorded before returning.
    """
    receipt_id = digest.receipt_id()
    digest_hash = digest.digest_hash()
    if ledger.delivered(receipt_id):
        return DeliveryOutcome(
            receipt_id=receipt_id,
            digest_hash=digest_hash,
            posted=False,
            message_id="",
            reason="already_delivered",
        )
    text = render_digest_message(digest)
    message_id = await bridge.send_message(thread_id, text)
    ledger.mark_delivered(receipt_id, digest_hash=digest_hash, message_id=message_id, fire_time=digest.fire_time)
    return DeliveryOutcome(
        receipt_id=receipt_id,
        digest_hash=digest_hash,
        posted=True,
        message_id=message_id,
        reason="posted",
    )


@dataclass(frozen=True)
class DigestFireResult:
    """The full outcome of one mission digest fire."""

    digest: MissionDigest
    outcome: DeliveryOutcome
    recorded_receipt: bool
    fire_graph_hash: str


async def run_digest_fire(
    *,
    sdd_dir: Path,
    workdir: Path,
    mission_id: str,
    fire_time: int,
    chain: AuditChainStore,
    bridge: BridgeProtocol,
    thread_id: str,
    recurrence: str = "",
) -> DigestFireResult:
    """Compute, anchor, and deliver a mission digest for one fire instant.

    The whole fire is gated on the durable delivery ledger keyed by the digest
    receipt id, so a re-fire of an already-delivered instant records nothing new
    and posts nothing -- delivery and the chain receipt are both idempotent. The
    digest is a pure fold over the ledger at ``fire_time``, so a missed fire
    recomputes to the identical digest and receipt id after a restart.

    Args:
        sdd_dir: The install ``.sdd`` directory.
        workdir: The project root (for evidence bundle recomputation).
        mission_id: The mission to summarise.
        fire_time: Integer Unix epoch of the canonical fire instant.
        chain: The HMAC audit chain to anchor the digest receipt in.
        bridge: The chat driver to post through (common bridge protocol).
        thread_id: The chat thread / channel to post to.
        recurrence: Canonical recurrence rule of the fire, when any.

    Returns:
        A :class:`DigestFireResult`.
    """
    projection = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=workdir, mission_id=mission_id)
    digest = build_mission_digest(projection, fire_time=fire_time)
    ledger = DigestDeliveryLedger(sdd_dir, mission_id)

    fire_graph = digest_fire_graph_hash(
        mission_id=mission_id,
        fire_time=fire_time,
        mission_status_hash=digest.mission_status_hash,
        recurrence=recurrence,
    )

    if ledger.delivered(digest.receipt_id()):
        return DigestFireResult(
            digest=digest,
            outcome=DeliveryOutcome(
                receipt_id=digest.receipt_id(),
                digest_hash=digest.digest_hash(),
                posted=False,
                message_id="",
                reason="already_delivered",
            ),
            recorded_receipt=False,
            fire_graph_hash=fire_graph,
        )

    record_digest_receipt(
        chain,
        digest,
        schedule_id=f"mission-digest:{mission_id}",
        recurrence=recurrence,
        fire_graph_hash=fire_graph,
    )
    outcome = await deliver_digest(bridge=bridge, thread_id=thread_id, digest=digest, ledger=ledger)
    return DigestFireResult(
        digest=digest,
        outcome=outcome,
        recorded_receipt=True,
        fire_graph_hash=fire_graph,
    )


__all__ = [
    "DeliveryOutcome",
    "DigestDeliveryLedger",
    "DigestFireResult",
    "deliver_digest",
    "digest_deliveries_dir",
    "digest_fire_graph_hash",
    "run_digest_fire",
]
