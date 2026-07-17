"""Signed daily progress digest: a mission projection folded to a chat receipt (#2510).

A multi-day mission runs mostly while nobody is watching. The morning question
is "what moved, what verified, what did it cost, what is blocked"; answering it
today means opening the dashboard and mentally diffing against yesterday. A
digest that is merely generated text would create a new problem -- a status
message nobody can check against what actually ran -- so the digest carries the
same guarantees as the run itself.

The load-bearing property mirrors the mission projection
(:mod:`bernstein.core.orchestration.missions`): the digest is a **pure
deterministic projection** of ``(MissionProjection, fire_time)``. Because the
projection is itself a pure fold over the work-ledger chain, the digest is a
pure fold over the ledger too:

* **Determinism** -- :func:`build_mission_digest` reads no wall clock and no
  host state. Two operators holding byte-identical ledgers, folding at the same
  ``fire_time``, derive byte-identical :meth:`MissionDigest.canonical_bytes`
  and the same :meth:`MissionDigest.digest_hash`. A missed fire recomputes to
  the identical digest after a restart.
* **Verifiability** -- the posted chat message is the verbatim projection of
  the digest (:func:`render_digest_message`), and the digest hash is embedded in
  the message and recorded in the HMAC audit chain
  (:func:`bernstein.core.security.audit_chain.record_mission_digest_receipt`).
  :func:`verify_message_matches` recomputes the digest from the ledger and the
  rendered message from the digest, so an edited or truncated chat message is
  detected as a mismatch, and a tampered receipt fails chain verification at its
  exact position.
* **Idempotency** -- delivery is keyed on the digest receipt id
  (:meth:`MissionDigest.receipt_id`), a pure function of
  ``(mission_id, fire_time, digest_hash)``. A restart between fire computation
  and chat delivery does not double-post: :class:`DigestDeliveryLedger` records
  the delivered receipt id durably, and a re-fire of the same instant is a
  no-op.

Strip the ledger, the audit chain, and the deterministic fold, and the digest
collapses to unverifiable prose. The digest is not "a status message plus a
hash": it is the audit chain in the shape of a morning summary. What the
operator reads is exactly what a verifier recomputes.

Related work: the mission timeline UI (#2510) renders the same projection over
the web surface; the deterministic fire projection
(:mod:`bernstein.core.orchestration.schedule_projection`) anchors the recurring
fire that computes the digest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.missions import (
    MISSION_UNVERIFIED,
    PHASE_ACTIVE,
    PHASE_HALTED,
    PHASE_PASSED,
    PHASE_PENDING,
    PHASE_UNVERIFIED,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from bernstein.core.orchestration.missions import MissionProjection, MissionStatus
    from bernstein.core.security.audit_chain import AuditChainStore, AuditEvent

#: Digest document schema version. Bump on any change to the canonical digest
#: shape (the byte-identity witness across hosts). Bumping changes every
#: ``digest_hash`` and is the single source of truth for when the deterministic
#: contract is allowed to evolve.
MISSION_DIGEST_SCHEMA_VERSION = 1

#: Prefix of the deterministic digest receipt id (the delivery idempotency key).
_RECEIPT_ID_PREFIX = "missiondigest-"


# ---------------------------------------------------------------------------
# Canonical helpers (identical discipline to missions.py so the two cannot drift)
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Digest document
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseDigest:
    """The digest projection of one phase: progress plus its provenance handle.

    Every field is derived from the mission projection. ``receipt_hash`` and
    ``evidence_bundle_hashes`` are the provenance the timeline and the digest
    element link back to, so no line of the digest renders without a chain- or
    ledger-backed origin.
    """

    phase_id: str
    name: str
    state: str
    envelope: str
    budget_usd: float
    spend_usd: float
    gate_passed: bool
    receipt_hash: str
    ledger_seq: int
    evidence_bundle_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "name": self.name,
            "state": self.state,
            "envelope": self.envelope,
            "budget_usd": self.budget_usd,
            "spend_usd": self.spend_usd,
            "gate_passed": self.gate_passed,
            "receipt_hash": self.receipt_hash,
            "ledger_seq": self.ledger_seq,
            "evidence_bundle_hashes": list(self.evidence_bundle_hashes),
        }


@dataclass(frozen=True)
class EnvelopeSpend:
    """Per-envelope spend rollup, summed across phases that share an envelope."""

    envelope: str
    spend_usd: float
    budget_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope,
            "spend_usd": self.spend_usd,
            "budget_usd": self.budget_usd,
        }


@dataclass(frozen=True)
class Blocker:
    """A phase that is holding the mission back (halted or unverified)."""

    phase_id: str
    state: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"phase_id": self.phase_id, "state": self.state, "reason": self.reason}


@dataclass(frozen=True)
class MissionDigest:
    """The canonical, byte-stable daily progress digest of a mission.

    A pure function of ``(MissionProjection, fire_time)``. Two hosts with the
    same ledger and the same ``fire_time`` derive identical
    :meth:`canonical_bytes` and :meth:`digest_hash`.
    """

    schema_version: int
    mission_id: str
    fire_time: int
    mission_status_hash: str
    spec_hash: str
    goal_digest: str
    overall: str
    ledger_head: str
    ledger_verified: bool
    evidence_verified: bool
    entry_count: int
    phases: tuple[PhaseDigest, ...]
    phases_passed: int
    phases_active: int
    phases_pending: int
    phases_unverified: int
    gates_passed: int
    gates_failed: int
    envelope_spend: tuple[EnvelopeSpend, ...]
    total_spend_usd: float
    blockers: tuple[Blocker, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "fire_time": self.fire_time,
            "mission_status_hash": self.mission_status_hash,
            "spec_hash": self.spec_hash,
            "goal_digest": self.goal_digest,
            "overall": self.overall,
            "ledger_head": self.ledger_head,
            "ledger_verified": self.ledger_verified,
            "evidence_verified": self.evidence_verified,
            "entry_count": self.entry_count,
            "phases": [phase.to_dict() for phase in self.phases],
            "phases_passed": self.phases_passed,
            "phases_active": self.phases_active,
            "phases_pending": self.phases_pending,
            "phases_unverified": self.phases_unverified,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
            "envelope_spend": [row.to_dict() for row in self.envelope_spend],
            "total_spend_usd": self.total_spend_usd,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 JSON bytes the digest hash signs."""
        return _canonical_bytes(self.to_dict())

    def digest_hash(self) -> str:
        """Return the SHA-256 hex digest of the canonical digest bytes."""
        return _sha256_hex(self.canonical_bytes())

    def receipt_id(self) -> str:
        """Return the deterministic digest receipt id (delivery idempotency key).

        A pure function of ``(mission_id, fire_time, digest_hash)``: the same
        ledger state folded at the same fire instant yields the same id on any
        host and after any restart, so delivery keyed on it never double-posts
        and a missed fire recomputes to the identical key.
        """
        seed = f"{self.mission_id}|{self.fire_time}|{self.digest_hash()}".encode()
        return _RECEIPT_ID_PREFIX + _sha256_hex(seed)[:24]


# ---------------------------------------------------------------------------
# Pure build: mission projection + fire time -> canonical digest
# ---------------------------------------------------------------------------


def _phase_digests(status: MissionStatus) -> tuple[PhaseDigest, ...]:
    return tuple(
        PhaseDigest(
            phase_id=phase.phase_id,
            name=phase.name,
            state=phase.state,
            envelope=phase.envelope,
            budget_usd=phase.budget_usd,
            spend_usd=phase.spend_usd,
            gate_passed=phase.gate_passed,
            receipt_hash=phase.receipt_hash,
            ledger_seq=phase.ledger_seq,
            evidence_bundle_hashes=phase.evidence_bundle_hashes,
        )
        for phase in status.phases
    )


def _envelope_spend(phases: Sequence[PhaseDigest]) -> tuple[EnvelopeSpend, ...]:
    """Roll spend up by envelope, summed across phases sharing an envelope.

    Ordered by envelope name so the canonical bytes are order-independent.
    """
    spend: dict[str, float] = {}
    budget: dict[str, float] = {}
    for phase in phases:
        spend[phase.envelope] = spend.get(phase.envelope, 0.0) + phase.spend_usd
        budget[phase.envelope] = budget.get(phase.envelope, 0.0) + phase.budget_usd
    return tuple(EnvelopeSpend(envelope=env, spend_usd=spend[env], budget_usd=budget[env]) for env in sorted(spend))


def _blockers(phases: Sequence[PhaseDigest]) -> tuple[Blocker, ...]:
    out: list[Blocker] = []
    for phase in phases:
        if phase.state == PHASE_HALTED:
            out.append(Blocker(phase_id=phase.phase_id, state=phase.state, reason="halted"))
        elif phase.state == PHASE_UNVERIFIED:
            out.append(Blocker(phase_id=phase.phase_id, state=phase.state, reason="evidence_unverified"))
    return tuple(out)


def build_mission_digest(projection: MissionProjection, *, fire_time: int) -> MissionDigest:
    """Fold a mission projection at *fire_time* into a canonical digest.

    PURE function: no wall clock, no filesystem, no host state. The only inputs
    are the projection (itself a pure fold over the ledger) and the integer
    fire instant, so two hosts holding byte-identical ledgers derive identical
    :meth:`MissionDigest.canonical_bytes` and :meth:`MissionDigest.digest_hash`.

    Args:
        projection: The mission projection to summarise (see
            :func:`bernstein.core.orchestration.missions.project_mission_from_ledger`).
        fire_time: Unix epoch (integer seconds) of the canonical fire instant.
            An ``int`` is required so sub-second drift cannot fork two
            operators' digests (mirrors the schedule-fire float guard).

    Returns:
        A canonical :class:`MissionDigest`.

    Raises:
        TypeError: When ``fire_time`` is not an ``int``.
    """
    if not isinstance(fire_time, int) or isinstance(fire_time, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("fire_time must be an integer epoch second; floats permit cross-host drift")

    status = projection.status
    phases = _phase_digests(status)

    phases_passed = sum(1 for p in phases if p.state == PHASE_PASSED)
    phases_active = sum(1 for p in phases if p.state == PHASE_ACTIVE)
    phases_pending = sum(1 for p in phases if p.state == PHASE_PENDING)
    phases_unverified = sum(1 for p in phases if p.state == PHASE_UNVERIFIED)
    gates_passed = sum(1 for p in phases if p.gate_passed)
    gates_failed = sum(1 for p in phases if p.state == PHASE_HALTED)
    total_spend = round(sum(p.spend_usd for p in phases), 6)

    return MissionDigest(
        schema_version=MISSION_DIGEST_SCHEMA_VERSION,
        mission_id=status.mission_id,
        fire_time=fire_time,
        mission_status_hash=projection.status_hash,
        spec_hash=status.spec_hash,
        goal_digest=status.goal_digest,
        overall=status.overall,
        ledger_head=projection.ledger_head,
        ledger_verified=projection.ledger_verified,
        evidence_verified=projection.evidence_verified,
        entry_count=projection.entry_count,
        phases=phases,
        phases_passed=phases_passed,
        phases_active=phases_active,
        phases_pending=phases_pending,
        phases_unverified=phases_unverified,
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        envelope_spend=_envelope_spend(phases),
        total_spend_usd=total_spend,
        blockers=_blockers(phases),
    )


# ---------------------------------------------------------------------------
# Deterministic chat rendering (the message is the verbatim digest projection)
# ---------------------------------------------------------------------------


def render_digest_message(digest: MissionDigest) -> str:
    """Render the digest into the exact chat message text, deterministically.

    The message is a pure function of the digest, so what an operator reads is
    exactly what :func:`verify_message_matches` recomputes. The digest hash and
    receipt id are embedded so a recipient (or a bot) can recompute the digest
    from the ledger and prove the message was not edited or truncated.
    """
    d = digest
    trusted = d.ledger_verified and d.evidence_verified and d.overall != MISSION_UNVERIFIED
    verified = "verified" if trusted else "UNVERIFIED"
    lines: list[str] = [
        f"Mission {d.mission_id} - daily progress digest",
        f"overall: {d.overall} ({verified})",
        f"phases: {d.phases_passed} passed, {d.phases_active} active, "
        f"{d.phases_pending} pending, {d.phases_unverified} unverified",
        f"gates: {d.gates_passed} passed, {d.gates_failed} failed",
        f"spend: ${d.total_spend_usd:.2f} across {len(d.envelope_spend)} envelope(s)",
    ]
    for row in d.envelope_spend:
        budget = f"/${row.budget_usd:.2f}" if row.budget_usd else ""
        lines.append(f"  - {row.envelope}: ${row.spend_usd:.2f}{budget}")
    if d.blockers:
        lines.append(f"blockers: {len(d.blockers)}")
        for blocker in d.blockers:
            lines.append(f"  - {blocker.phase_id}: {blocker.reason}")
    else:
        lines.append("blockers: none")
    lines.append(f"mission_status_hash: {d.mission_status_hash}")
    lines.append(f"digest_hash: {d.digest_hash()}")
    lines.append(f"receipt_id: {d.receipt_id()}")
    return "\n".join(lines)


@dataclass(frozen=True)
class DigestVerification:
    """Outcome of verifying a posted chat message against a recomputed digest."""

    matches: bool
    reason: str
    digest_hash: str
    receipt_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": self.matches,
            "reason": self.reason,
            "digest_hash": self.digest_hash,
            "receipt_id": self.receipt_id,
        }


def verify_message_matches(posted_text: str, digest: MissionDigest) -> DigestVerification:
    """Verify a posted chat message equals the digest's verbatim projection.

    Recomputes the canonical message from *digest* (which a caller recomputes
    from the ledger) and compares it byte for byte against *posted_text*. Any
    edit or truncation of the posted message surfaces as ``matches=False`` with
    a reason. This is the message-side half of the proof; the receipt-side half
    is chain verification of the recorded digest receipt.
    """
    expected = render_digest_message(digest)
    digest_hash = digest.digest_hash()
    receipt_id = digest.receipt_id()
    if posted_text == expected:
        return DigestVerification(matches=True, reason="ok", digest_hash=digest_hash, receipt_id=receipt_id)
    if digest_hash not in posted_text:
        reason = "digest_hash absent from posted message (edited or truncated)"
    elif len(posted_text) != len(expected):
        reason = "posted message length differs from digest projection (edited or truncated)"
    else:
        reason = "posted message body differs from digest projection (edited)"
    return DigestVerification(matches=False, reason=reason, digest_hash=digest_hash, receipt_id=receipt_id)


# ---------------------------------------------------------------------------
# Chain anchoring: record the digest receipt into the HMAC audit chain
# ---------------------------------------------------------------------------


def record_digest_receipt(
    chain: AuditChainStore,
    digest: MissionDigest,
    *,
    schedule_id: str = "",
    recurrence: str = "",
    fire_graph_hash: str = "",
    journal_entry_hash: str = "",
) -> AuditEvent:
    """Anchor *digest* into the HMAC audit chain as a mission digest receipt.

    Thin wrapper over
    :func:`bernstein.core.security.audit_chain.record_mission_digest_receipt`
    that unpacks the digest's identity fields. Only hashes, counts, and the
    envelope spend are recorded -- never goal text or task payloads.
    """
    from bernstein.core.security.audit_chain import record_mission_digest_receipt

    return record_mission_digest_receipt(
        chain=chain,
        mission_id=digest.mission_id,
        fire_time=digest.fire_time,
        digest_hash=digest.digest_hash(),
        receipt_id=digest.receipt_id(),
        mission_status_hash=digest.mission_status_hash,
        ledger_head=digest.ledger_head,
        phases_passed=digest.phases_passed,
        gates_passed=digest.gates_passed,
        gates_failed=digest.gates_failed,
        total_spend_usd=digest.total_spend_usd,
        schedule_id=schedule_id,
        recurrence=recurrence,
        fire_graph_hash=fire_graph_hash,
        journal_entry_hash=journal_entry_hash,
    )


__all__ = [
    "MISSION_DIGEST_SCHEMA_VERSION",
    "Blocker",
    "DigestVerification",
    "EnvelopeSpend",
    "MissionDigest",
    "PhaseDigest",
    "build_mission_digest",
    "record_digest_receipt",
    "render_digest_message",
    "verify_message_matches",
]
