"""Ledger-projected missions: multi-day goals as a projection over the chain (#2509).

A goal that spans several days spans many runs, restarts, and machines, and the
pieces to run it already exist as disconnected substrate: the work ledger makes
the task graph portable and resumable
(:mod:`bernstein.core.persistence.work_ledger`), evidence bundles prove
individual tasks complete (:mod:`bernstein.core.evidence.bundle`), and cost
envelopes cap spend (:mod:`bernstein.core.cost.scheduling.dispatch_gate`). A
**mission** ties them into an outcome: a declared decomposition of a goal into
ordered phases, each with a verification gate and a budget envelope.

The load-bearing property is that **mission status is computed, never stored**.
:func:`project_mission` is a pure deterministic fold over the work-ledger chain
plus the evidence bundle records the phase receipts reference, following the
same fold discipline as the review-board projection
(:mod:`bernstein.core.replay.review_board`):

* **Determinism** -- the fold reads no wall clock and no host state, so two
  operators holding byte-identical ledgers render byte-identical
  :meth:`MissionStatus.canonical_bytes` with the same
  :meth:`MissionStatus.status_hash`.
* **Verifiability** -- a phase advances only by a *mission phase receipt*
  (a ``mission.phase_passed`` ledger entry, mirrored onto the HMAC audit chain
  via :func:`bernstein.core.security.audit_chain.record_mission_phase_receipt`)
  that binds the gate verdict, the evidence bundle hashes it verified, the
  ledger position, and the envelope spend at gate time. A phase without a
  receipt is by definition not passed, and a receipt that does not bind the
  exact gate its phase declares -- a failed verdict, a different or empty set
  of task ids, a ragged evidence binding, a hash that disagrees with its own
  contents, or a bundle since deleted or altered -- projects the phase as
  :data:`PHASE_UNVERIFIED` rather than best-effort ``passed``. Evidence for an
  unrelated task therefore proves nothing about a gate it does not name.

Strip the ledger and a mission collapses to a stored status row with a log:
there is no mission state on disk other than the hash-chained transitions, and
:func:`project_mission_from_ledger` rebuilds the whole mission purely by
replaying the chain -- the same resume path the work ledger already ships, so a
mission survives restart, reimage, and machine moves with no auxiliary state.

Related work: the mission timeline UI (#2510) renders this projection; resource
pools (#2544) generalise the per-phase envelope. This module owns only the
projection and the phase-gate / envelope wiring.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.cost.cost_rollup_by_envelope import EnvelopeRollupRow, rollup
from bernstein.core.cost.cost_tracker import EnvelopeConfig
from bernstein.core.cost.scheduling.dispatch_gate import RunDispatchOutcome, evaluate_run_dispatch
from bernstein.core.cost.scheduling.policy import CostCaps, DispatchCandidate
from bernstein.core.cost.scheduling.price_table import DEFAULT_PRICE_TABLE
from bernstein.core.evidence.bundle import read_evidence_bundle
from bernstein.core.persistence.journal import GENESIS_HASH
from bernstein.core.persistence.work_ledger import (
    KIND_MISSION_DEFINED,
    KIND_MISSION_PHASE_ENTERED,
    KIND_MISSION_PHASE_HALTED,
    KIND_MISSION_PHASE_PASSED,
    LedgerEntry,
    LedgerReader,
    WorkLedger,
    run_ledger_dir,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from bernstein.core.cost.cost_tracker import TokenUsage
    from bernstein.core.security.audit_chain import AuditChainStore

# ---------------------------------------------------------------------------
# Schema versions + vocabulary
# ---------------------------------------------------------------------------

#: Mission spec schema version. Bump only on a wire-format change.
MISSION_SPEC_SCHEMA_VERSION = 1

#: Mission status projection schema version. Bump on any change to the
#: canonical status shape *or* to the rules that derive it -- both move
#: :meth:`MissionStatus.status_hash`, and this field is how a verifier tells
#: "folded under different projection rules" apart from "tampered with".
#:
#: v2: phase isolation for halts. ``active_phase`` skips halted phases and the
#: mission reports halted only when no phase remains runnable, so an honest
#: ledger carrying a halt beside a runnable phase folds to a different hash
#: than it did under v1.
MISSION_STATUS_SCHEMA_VERSION = 2

#: Phase states the projection can assign, in lifecycle order.
PHASE_PENDING = "pending"
PHASE_ACTIVE = "active"
PHASE_PASSED = "passed"
PHASE_HALTED = "halted"
PHASE_UNVERIFIED = "unverified"

#: Phase states no further work proceeds from. A halt is terminal for its own
#: phase only: sibling phases run under isolated envelopes and are unaffected.
_TERMINAL_PHASE_STATES = (PHASE_PASSED, PHASE_HALTED)

#: Overall mission states.
MISSION_PENDING = "pending"
MISSION_ACTIVE = "active"
MISSION_COMPLETE = "complete"
MISSION_HALTED = "halted"
MISSION_UNVERIFIED = "unverified"

#: Identifier alphabet shared with the ledger's git-ref-safe ids.
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MissionSpecError(ValueError):
    """Raised when a mission spec fails boundary validation."""


# ---------------------------------------------------------------------------
# Canonical helpers
# ---------------------------------------------------------------------------


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Typed boundary readers
#
# A spec arrives as untrusted JSON. Coercing it (``str(value)``,
# ``float(value or 0)``) turns a malformed document into a plausible-looking
# mission whose spec hash binds something the operator never wrote, so every
# field is read with its declared type or refused outright.
# ---------------------------------------------------------------------------


def _require_mapping(raw: object, what: str) -> Mapping[str, Any]:
    """Return *raw* as a mapping, or raise naming what was expected."""
    if not isinstance(raw, Mapping):
        msg = f"{what} must be a JSON object, got {type(raw).__name__}"
        raise MissionSpecError(msg)
    return cast("Mapping[str, Any]", raw)


def _require_sequence(raw: object, what: str) -> Sequence[Any]:
    """Return *raw* as a list/tuple, or raise; a scalar is never coerced."""
    if not isinstance(raw, (list, tuple)):
        msg = f"{what} must be a JSON array, got {type(raw).__name__}"
        raise MissionSpecError(msg)
    return cast("Sequence[Any]", raw)


def _require_str(raw: Mapping[str, Any], key: str, what: str) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        msg = f"{what} field {key!r} must be a string, got {type(value).__name__}"
        raise MissionSpecError(msg)
    return value


def _require_int(raw: Mapping[str, Any], key: str, what: str, default: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{what} field {key!r} must be an integer, got {type(value).__name__}"
        raise MissionSpecError(msg)
    return value


def _require_finite_number(raw: Mapping[str, Any], key: str, what: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        msg = f"{what} field {key!r} must be a number, got {type(value).__name__}"
        raise MissionSpecError(msg)
    if not math.isfinite(value):
        msg = f"{what} field {key!r} must be finite, got {value!r}"
        raise MissionSpecError(msg)
    return float(value)


# ---------------------------------------------------------------------------
# Spec schema (validated at the boundary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseSpec:
    """One phase of a mission: a verification gate plus a budget envelope.

    Attributes:
        phase_id: Stable identifier, unique within the mission.
        name: Human-readable phase name.
        gate: The evidence producer task ids the gate requires; a phase
            advances only when every one has a passing sealed evidence bundle.
        envelope: The quota-envelope name the phase's spend is attributed to
            and capped under.
        budget_usd: The USD ceiling for the phase envelope (``0`` = unlimited).
    """

    phase_id: str
    name: str
    gate: tuple[str, ...]
    envelope: str
    budget_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "name": self.name,
            "gate": list(self.gate),
            "envelope": self.envelope,
            "budget_usd": self.budget_usd,
        }

    @classmethod
    def from_dict(cls, raw: object) -> PhaseSpec:
        """Parse one phase at the boundary; a malformed shape is refused.

        Raises:
            MissionSpecError: when the phase is not an object, or any field
                carries a type the schema does not declare.
        """
        obj = _require_mapping(raw, "mission phase")
        gate_items = _require_sequence(obj.get("gate", ()), "mission phase field 'gate'")
        gate: list[str] = []
        for item in gate_items:
            if not isinstance(item, str):
                msg = f"mission phase gate entries must be strings, got {type(item).__name__}"
                raise MissionSpecError(msg)
            gate.append(item)
        return cls(
            phase_id=_require_str(obj, "phase_id", "mission phase"),
            name=_require_str(obj, "name", "mission phase"),
            gate=tuple(gate),
            envelope=_require_str(obj, "envelope", "mission phase"),
            budget_usd=_require_finite_number(obj, "budget_usd", "mission phase", 0.0),
        )


@dataclass(frozen=True)
class MissionSpec:
    """A declared decomposition of a goal into ordered phases.

    The goal text is bound into the ledger by digest only (see
    :meth:`goal_digest`); the raw text never enters a ledger payload.
    """

    mission_id: str
    goal: str
    phases: tuple[PhaseSpec, ...]
    schema_version: int = MISSION_SPEC_SCHEMA_VERSION

    def validate(self) -> MissionSpec:
        """Validate the spec at the boundary; return self or raise.

        Raises:
            MissionSpecError: naming the exact violated rule.
        """
        if self.schema_version != MISSION_SPEC_SCHEMA_VERSION:
            msg = (
                f"unsupported mission spec schema_version {self.schema_version}: "
                f"this build reads version {MISSION_SPEC_SCHEMA_VERSION}"
            )
            raise MissionSpecError(msg)
        if not _ID_RE.match(self.mission_id):
            msg = f"invalid mission_id {self.mission_id!r}: must match {_ID_RE.pattern}"
            raise MissionSpecError(msg)
        if not self.phases:
            msg = "a mission must declare at least one phase"
            raise MissionSpecError(msg)
        seen: set[str] = set()
        for phase in self.phases:
            if not _ID_RE.match(phase.phase_id):
                msg = f"invalid phase_id {phase.phase_id!r}: must match {_ID_RE.pattern}"
                raise MissionSpecError(msg)
            if phase.phase_id in seen:
                msg = f"duplicate phase_id {phase.phase_id!r}"
                raise MissionSpecError(msg)
            seen.add(phase.phase_id)
            if not phase.envelope:
                msg = f"phase {phase.phase_id!r} must name a budget envelope"
                raise MissionSpecError(msg)
            if not math.isfinite(phase.budget_usd):
                # A non-finite budget serialises as invalid JSON and would poison
                # the canonical spec bytes every verifier recomputes.
                msg = f"phase {phase.phase_id!r} budget must be finite (got {phase.budget_usd!r})"
                raise MissionSpecError(msg)
            if phase.budget_usd < 0.0:
                msg = f"phase {phase.phase_id!r} budget must be >= 0 (got {phase.budget_usd})"
                raise MissionSpecError(msg)
            for task_id in phase.gate:
                if not task_id:
                    msg = f"phase {phase.phase_id!r} gate names an empty task id"
                    raise MissionSpecError(msg)
        return self

    def phase_ids(self) -> tuple[str, ...]:
        return tuple(phase.phase_id for phase in self.phases)

    def phase(self, phase_id: str) -> PhaseSpec:
        for phase in self.phases:
            if phase.phase_id == phase_id:
                return phase
        msg = f"unknown phase_id {phase_id!r}"
        raise MissionSpecError(msg)

    def goal_digest(self) -> str:
        """Return the SHA-256 hex digest of the goal text (never the text)."""
        return _sha256_hex(self.goal.encode("utf-8"))

    def _binding(self) -> dict[str, Any]:
        return {
            "v": self.schema_version,
            "mission_id": self.mission_id,
            "goal_digest": self.goal_digest(),
            "phases": [phase.to_dict() for phase in self.phases],
        }

    def to_canonical_bytes(self) -> bytes:
        return _canonical_bytes(self._binding())

    def spec_hash(self) -> str:
        return _sha256_hex(self.to_canonical_bytes())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "goal": self.goal,
            "phases": [phase.to_dict() for phase in self.phases],
        }

    @classmethod
    def from_dict(cls, raw: object) -> MissionSpec:
        """Parse and validate a spec at the boundary.

        Every malformed shape -- a non-object root, a scalar ``phases``, a
        phase that is not an object, a field of the wrong type -- raises
        :class:`MissionSpecError`, so a caller can map the whole boundary to a
        single documented exit code instead of leaking a ``TypeError`` or an
        ``AttributeError`` from deep inside the parse.

        Raises:
            MissionSpecError: naming the exact violated rule.
        """
        obj = _require_mapping(raw, "mission spec")
        phases_raw = _require_sequence(obj.get("phases", ()), "mission spec field 'phases'")
        return cls(
            mission_id=_require_str(obj, "mission_id", "mission spec"),
            goal=_require_str(obj, "goal", "mission spec"),
            phases=tuple(PhaseSpec.from_dict(p) for p in phases_raw),
            schema_version=_require_int(obj, "schema_version", "mission spec", MISSION_SPEC_SCHEMA_VERSION),
        ).validate()


# ---------------------------------------------------------------------------
# Phase receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseReceipt:
    """The binding a phase advancement records into the ledger and audit chain.

    A phase pass (or halt) is exactly this receipt: the gate verdict, the
    evidence bundle hashes it verified, the ledger position it landed at, the
    envelope, and the spend at gate time. ``receipt_hash`` is a pure function
    of the binding, so a verifier recomputes it byte-identically.
    """

    mission_id: str
    phase_id: str
    gate_passed: bool
    evidence_task_ids: tuple[str, ...]
    evidence_bundle_hashes: tuple[str, ...]
    ledger_seq: int
    envelope: str
    spend_usd: float
    reason: str = ""

    def _binding(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "phase_id": self.phase_id,
            "gate_passed": self.gate_passed,
            "evidence_task_ids": list(self.evidence_task_ids),
            "evidence_bundle_hashes": list(self.evidence_bundle_hashes),
            "ledger_seq": self.ledger_seq,
            "envelope": self.envelope,
            "spend_usd": self.spend_usd,
            "reason": self.reason,
        }

    def _sealed_binding(self) -> dict[str, Any]:
        """Return the binding in the exact form the ledger persists.

        The ledger redacts every payload string before hashing and writing, so
        a receipt that binds its own content hash must hash the redacted form.
        Hashing the raw binding instead would make an honest receipt whose
        envelope or task id the redactor rewrites (a ``$HOME`` path, a
        ``key=value`` secret) disagree with itself on read back, and the phase
        would project unverified forever on an append-only chain. Redaction is
        idempotent, so a receipt reconstructed from a persisted payload seals
        to the identical hash.
        """
        from bernstein.core.persistence.work_ledger import redact_ledger_payload

        return redact_ledger_payload(self._binding())

    def receipt_hash(self) -> str:
        """Return the sealed identity: SHA-256 over the persisted binding."""
        return _sha256_hex(_canonical_bytes(self._sealed_binding()))

    def to_payload(self) -> dict[str, Any]:
        return self._sealed_binding() | {"receipt_hash": self.receipt_hash()}


# ---------------------------------------------------------------------------
# Projected status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseStatus:
    """Projected state of one phase after folding the ledger chain."""

    phase_id: str
    name: str
    state: str
    gate: tuple[str, ...]
    gate_passed: bool
    evidence_bundle_hashes: tuple[str, ...]
    envelope: str
    budget_usd: float
    spend_usd: float
    receipt_hash: str
    ledger_seq: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "name": self.name,
            "state": self.state,
            "gate": list(self.gate),
            "gate_passed": self.gate_passed,
            "evidence_bundle_hashes": list(self.evidence_bundle_hashes),
            "envelope": self.envelope,
            "budget_usd": self.budget_usd,
            "spend_usd": self.spend_usd,
            "receipt_hash": self.receipt_hash,
            "ledger_seq": self.ledger_seq,
        }


@dataclass(frozen=True)
class MissionStatus:
    """The canonical, byte-stable projection of a mission over the ledger.

    The status is a pure function of the folded ledger entries plus the
    evidence bundle hashes the phase receipts reference. Two hosts with
    identical inputs derive identical :meth:`canonical_bytes` and
    :meth:`status_hash`.
    """

    schema_version: int
    mission_id: str
    goal_digest: str
    spec_hash: str
    phases: tuple[PhaseStatus, ...]
    active_phase: str
    overall: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "goal_digest": self.goal_digest,
            "spec_hash": self.spec_hash,
            "phases": [phase.to_dict() for phase in self.phases],
            "active_phase": self.active_phase,
            "overall": self.overall,
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 JSON bytes the status hash signs."""
        return _canonical_bytes(self.to_dict())

    def status_hash(self) -> str:
        """Return the SHA-256 hex digest of the canonical status bytes."""
        return _sha256_hex(self.canonical_bytes())


@dataclass(frozen=True)
class MissionProjection:
    """A projected mission bound to the ledger it was folded from.

    Attributes:
        mission_id: The mission projected.
        status: The canonical :class:`MissionStatus`.
        status_hash: SHA-256 of the canonical status bytes -- the projection's
            identity; two operators with the same ledger derive the same hash.
        ledger_head: The work-ledger head (last ``entry_hash``), or the genesis
            hash for an empty ledger.
        ledger_verified: Result of re-verifying the whole chain at projection
            time. ``False`` means a ledger entry was tampered with and the
            status must not be trusted; the projection forces ``overall`` to
            :data:`MISSION_UNVERIFIED`.
        evidence_verified: ``True`` only when every referenced evidence bundle
            is present and matches the hash its phase receipt bound.
        entry_count: Number of ledger entries folded.
    """

    mission_id: str
    status: MissionStatus
    status_hash: str
    ledger_head: str
    ledger_verified: bool
    evidence_verified: bool
    entry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "status": self.status.to_dict(),
            "mission_status_hash": self.status_hash,
            "ledger_head": self.ledger_head,
            "ledger_verified": self.ledger_verified,
            "evidence_verified": self.evidence_verified,
            "entry_count": self.entry_count,
        }


# ---------------------------------------------------------------------------
# Pure fold: ledger entries + evidence records -> mission status
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SealedReceipt:
    """A receipt read back from the chain, plus the hash its writer sealed.

    ``stored_hash`` is kept apart from the receipt because it is a *claim* about
    the binding, not part of it: the projection re-derives the hash from the
    binding and compares, so a receipt that does not hash to what it claims is
    self-inconsistent and cannot advance a phase.
    """

    receipt: PhaseReceipt
    stored_hash: str


@dataclass
class _PhaseAccum:
    """Running fold state for one phase."""

    entered_seq: int = -1
    passed: _SealedReceipt | None = None
    halted: _SealedReceipt | None = None


def _payload_float(payload: Mapping[str, Any], key: str) -> float:
    """Read a float from a persisted payload without losing its value.

    The obvious ``float(payload.get(key, 0.0) or 0.0)`` silently maps every
    falsy number to ``+0.0``, and ``-0.0`` is falsy. Since the receipt hash is
    recomputed from these values, that coercion would turn an honest ``-0.0``
    spend into a permanent hash mismatch. This parse is value-preserving.
    """
    value = payload.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _payload_int(payload: Mapping[str, Any], key: str) -> int:
    """Read an int from a persisted payload without losing its value."""
    value = payload.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _receipt_from_payload(payload: Mapping[str, Any]) -> _SealedReceipt:
    task_ids = payload.get("evidence_task_ids", [])
    hashes = payload.get("evidence_bundle_hashes", [])
    receipt = PhaseReceipt(
        mission_id=str(payload.get("mission_id", "")),
        phase_id=str(payload.get("phase_id", "")),
        gate_passed=bool(payload.get("gate_passed", False)),
        evidence_task_ids=(
            tuple(str(t) for t in cast("Sequence[object]", task_ids)) if isinstance(task_ids, (list, tuple)) else ()
        ),
        evidence_bundle_hashes=(
            tuple(str(h) for h in cast("Sequence[object]", hashes)) if isinstance(hashes, (list, tuple)) else ()
        ),
        ledger_seq=_payload_int(payload, "ledger_seq"),
        envelope=str(payload.get("envelope", "")),
        spend_usd=_payload_float(payload, "spend_usd"),
        reason=str(payload.get("reason", "")),
    )
    return _SealedReceipt(receipt=receipt, stored_hash=str(payload.get("receipt_hash", "")))


def _spec_skeleton_from_defined(
    payload: Mapping[str, Any],
) -> tuple[str, str, str, tuple[PhaseSpec, ...]] | None:
    """Rebuild ``(mission_id, goal_digest, spec_hash, phases)`` from a defined row.

    Returns ``None`` when the row is not a well-formed definition. The fold is
    a projection over untrusted bytes, so a malformed definition must render
    the mission unverified rather than raise out of the projection.
    """
    try:
        phases_raw = _require_sequence(payload.get("phases", []), "mission definition field 'phases'")
        phases = tuple(PhaseSpec.from_dict(p) for p in phases_raw)
    except MissionSpecError:
        return None
    return (
        str(payload.get("mission_id", "")),
        str(payload.get("goal_digest", "")),
        str(payload.get("spec_hash", "")),
        phases,
    )


def project_mission(
    entries: Sequence[LedgerEntry],
    evidence_hashes: Mapping[str, str],
) -> MissionStatus:
    """Fold ledger *entries* (chain order) into a canonical mission status.

    Pure function: no wall clock, no filesystem, no host state. The
    *evidence_hashes* mapping (``task_id -> "sha256:..."`` bundle hash,
    recomputed from the sealed bundles) is the only external input.

    A phase projects as passed only when its receipt binds the exact gate the
    phase declares (see :func:`_receipt_binds_gate`); every weaker shape is
    :data:`PHASE_UNVERIFIED`. A ledger declaring more than one mission, or one
    whose definition is malformed, projects as :data:`MISSION_UNVERIFIED`
    outright, because the projection and the evidence lookup could otherwise be
    reading different specs from the same chain.

    Args:
        entries: Work-ledger entries as yielded by :meth:`LedgerReader.entries`.
        evidence_hashes: Current content addresses of the referenced evidence
            bundles.

    Returns:
        A canonical :class:`MissionStatus`. When no ``mission.defined`` entry is
        present the status is an empty pending mission.
    """
    mission_id = ""
    goal_digest = ""
    spec_hash = ""
    phase_specs: tuple[PhaseSpec, ...] = ()
    accums: dict[str, _PhaseAccum] = {}
    defined_count = 0
    definition_malformed = False

    for entry in entries:
        if entry.kind == KIND_MISSION_DEFINED:
            defined_count += 1
            skeleton = _spec_skeleton_from_defined(entry.payload)
            if skeleton is None:
                definition_malformed = True
                continue
            mission_id, goal_digest, spec_hash, phase_specs = skeleton
            accums = {phase.phase_id: _PhaseAccum() for phase in phase_specs}
            continue
        phase_id = entry.task_id
        accum = accums.get(phase_id)
        if accum is None:
            # Transition for a phase not in the declared spec (forward compat).
            continue
        if entry.kind == KIND_MISSION_PHASE_ENTERED:
            accum.entered_seq = entry.seq
        elif entry.kind == KIND_MISSION_PHASE_PASSED:
            accum.passed = _receipt_from_payload(entry.payload)
        elif entry.kind == KIND_MISSION_PHASE_HALTED:
            accum.halted = _receipt_from_payload(entry.payload)

    phases: list[PhaseStatus] = []
    for spec in phase_specs:
        accum = accums.get(spec.phase_id, _PhaseAccum())
        phases.append(_project_phase(spec, accum, evidence_hashes))

    # The active phase is the first one still runnable. Passed phases are done;
    # halted phases are done too (a halt is terminal for that phase alone), so
    # neither can be reported as the phase work is proceeding in.
    active_phase = next((p.phase_id for p in phases if p.state not in _TERMINAL_PHASE_STATES), "")
    overall = _overall_state(phases)
    # Zero definitions is simply an empty ledger (pending). More than one, or a
    # malformed one, means the projection and the evidence lookup could be
    # reading different specs: refuse to render a trusted status.
    if defined_count > 1 or definition_malformed:
        overall = MISSION_UNVERIFIED

    return MissionStatus(
        schema_version=MISSION_STATUS_SCHEMA_VERSION,
        mission_id=mission_id,
        goal_digest=goal_digest,
        spec_hash=spec_hash,
        phases=tuple(phases),
        active_phase=active_phase,
        overall=overall,
    )


def _receipt_binds_gate(
    spec: PhaseSpec,
    sealed: _SealedReceipt,
    evidence_hashes: Mapping[str, str],
) -> bool:
    """Return True only when *sealed* proves the gate *spec* declares.

    A pass is the strongest claim the projection can make, so it is granted
    only when every link in the chain holds:

    1. the receipt asserts a passing gate verdict at all;
    2. it binds exactly the task ids the phase gates on -- no more, no fewer,
       so evidence for an unrelated task can never stand in for the gate;
    3. it binds one bundle hash per task id (a ragged binding is malformed);
    4. it hashes to the value its writer sealed, so the binding is
       self-consistent; and
    5. every bound bundle still hashes to what the receipt recorded.

    Any other shape is a refusal (:data:`PHASE_UNVERIFIED`), never a pass.
    """
    receipt = sealed.receipt
    if not receipt.gate_passed:
        return False
    if set(receipt.evidence_task_ids) != set(spec.gate):
        return False
    if len(receipt.evidence_task_ids) != len(receipt.evidence_bundle_hashes):
        return False
    if sealed.stored_hash != receipt.receipt_hash():
        return False
    return _evidence_matches(receipt, evidence_hashes)


def _project_phase(spec: PhaseSpec, accum: _PhaseAccum, evidence_hashes: Mapping[str, str]) -> PhaseStatus:
    if accum.halted is not None:
        receipt = accum.halted.receipt
        return PhaseStatus(
            phase_id=spec.phase_id,
            name=spec.name,
            state=PHASE_HALTED,
            gate=spec.gate,
            gate_passed=False,
            evidence_bundle_hashes=receipt.evidence_bundle_hashes,
            envelope=spec.envelope,
            budget_usd=spec.budget_usd,
            spend_usd=receipt.spend_usd,
            receipt_hash=receipt.receipt_hash(),
            ledger_seq=receipt.ledger_seq,
        )
    if accum.passed is not None:
        receipt = accum.passed.receipt
        gate_bound = _receipt_binds_gate(spec, accum.passed, evidence_hashes)
        return PhaseStatus(
            phase_id=spec.phase_id,
            name=spec.name,
            state=PHASE_PASSED if gate_bound else PHASE_UNVERIFIED,
            gate=spec.gate,
            gate_passed=gate_bound,
            evidence_bundle_hashes=receipt.evidence_bundle_hashes,
            envelope=spec.envelope,
            budget_usd=spec.budget_usd,
            spend_usd=receipt.spend_usd,
            receipt_hash=receipt.receipt_hash(),
            ledger_seq=receipt.ledger_seq,
        )
    state = PHASE_ACTIVE if accum.entered_seq >= 0 else PHASE_PENDING
    return PhaseStatus(
        phase_id=spec.phase_id,
        name=spec.name,
        state=state,
        gate=spec.gate,
        gate_passed=False,
        evidence_bundle_hashes=(),
        envelope=spec.envelope,
        budget_usd=spec.budget_usd,
        spend_usd=0.0,
        receipt_hash="",
        ledger_seq=max(accum.entered_seq, 0),
    )


def _evidence_matches(receipt: PhaseReceipt, evidence_hashes: Mapping[str, str]) -> bool:
    """Return True when every bound evidence bundle still hashes as recorded.

    Callers must have already proven the two tuples are the same length; the
    strict zip makes a ragged binding an error rather than a silent truncation
    that would let a short hash list satisfy a longer gate.
    """
    for task_id, bound in zip(receipt.evidence_task_ids, receipt.evidence_bundle_hashes, strict=True):
        if evidence_hashes.get(task_id) != bound:
            return False
    return True


def _overall_state(phases: Sequence[PhaseStatus]) -> str:
    """Fold phase states into the mission state.

    A halt is scoped to its own phase: phases run under isolated envelopes, so
    one exhausted envelope must not halt siblings that are still runnable. The
    mission halts only once nothing is left to run.
    """
    states = [phase.state for phase in phases]
    if not states:
        return MISSION_PENDING
    if PHASE_UNVERIFIED in states:
        return MISSION_UNVERIFIED
    if all(state == PHASE_PASSED for state in states):
        return MISSION_COMPLETE
    if all(state in _TERMINAL_PHASE_STATES for state in states):
        # Every phase is passed or halted and at least one halted: nothing
        # remains runnable, so the mission itself is halted.
        return MISSION_HALTED
    if PHASE_ACTIVE in states or PHASE_PASSED in states or PHASE_HALTED in states:
        return MISSION_ACTIVE
    return MISSION_PENDING


# ---------------------------------------------------------------------------
# Ledger location + evidence gathering
# ---------------------------------------------------------------------------


def mission_ledger_dir(sdd_dir: Path, mission_id: str) -> Path:
    """Return the per-mission ledger directory (keyed by mission id)."""
    return run_ledger_dir(sdd_dir, mission_id)


def list_missions(sdd_dir: Path) -> list[str]:
    """Return mission ids under *sdd_dir* that declare a mission, newest first.

    Missions and plain run ledgers share the ledger root; a directory is a
    mission only when its ledger's first entry is a ``mission.defined``
    transition. "Newest first" is by directory name (descending): mission ids
    embed their creation ordering, and name order is a stable property of the
    on-disk layout rather than of mtimes, which a copy can rewrite.
    """
    from bernstein.core.persistence.work_ledger import default_ledger_root

    root = default_ledger_root(sdd_dir)
    if not root.is_dir():
        return []
    found: list[str] = []
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        reader = LedgerReader(entry)
        if not reader.exists():
            continue
        first = next(iter(reader.entries()), None)
        if first is not None and first.kind == KIND_MISSION_DEFINED:
            found.append(entry.name)
    return sorted(found, reverse=True)


def gather_evidence_hashes(workdir: Path, task_ids: Iterable[str]) -> dict[str, str]:
    """Recompute the content address of each task's sealed evidence bundle.

    The map is never stored; it is recomputed from the on-disk bundles every
    time the projection runs, so a deleted or altered bundle is caught. A task
    whose bundle is absent or whose gate did not pass is simply omitted.
    """
    out: dict[str, str] = {}
    for task_id in task_ids:
        bundle = read_evidence_bundle(workdir, task_id)
        if bundle is None or not bundle.gate_passed:
            continue
        out[task_id] = bundle.bundle_hash()
    return out


def _referenced_task_ids(entries: Sequence[LedgerEntry]) -> tuple[str, ...]:
    """Return every gate task id any mission definition in *entries* names.

    A well-formed ledger carries exactly one definition. A ledger carrying more
    already projects as unverified; gathering the union rather than the first
    definition's gates keeps the evidence lookup from silently reading a
    different spec than the projection folds.
    """
    seen: list[str] = []
    for entry in entries:
        if entry.kind != KIND_MISSION_DEFINED:
            continue
        skeleton = _spec_skeleton_from_defined(entry.payload)
        if skeleton is None:
            continue
        for phase in skeleton[3]:
            for task_id in phase.gate:
                if task_id not in seen:
                    seen.append(task_id)
    return tuple(seen)


def project_mission_from_ledger(*, sdd_dir: Path, workdir: Path, mission_id: str) -> MissionProjection:
    """Rebuild mission state purely by replaying the on-disk ledger chain.

    This is the resume path: it needs only the ledger file and the sealed
    evidence bundles, so a mission projects identically on the host that ran it
    and on a fresh copy after a restart, reimage, or machine move. The chain is
    re-verified end to end; a tampered entry forces ``overall`` to
    :data:`MISSION_UNVERIFIED` and surfaces at its exact position via
    :attr:`MissionProjection.ledger_verified`.
    """
    reader = LedgerReader(mission_ledger_dir(sdd_dir, mission_id))
    verification = reader.verify()
    entries = list(reader.entries())
    evidence_hashes = gather_evidence_hashes(workdir, _referenced_task_ids(entries))
    status = project_mission(entries, evidence_hashes)

    evidence_verified = all(phase.state != PHASE_UNVERIFIED for phase in status.phases)
    if not verification.ok:
        status = replace(status, overall=MISSION_UNVERIFIED)

    ledger_head = entries[-1].entry_hash if entries else GENESIS_HASH
    return MissionProjection(
        mission_id=mission_id,
        status=status,
        status_hash=status.status_hash(),
        ledger_head=ledger_head,
        ledger_verified=verification.ok,
        evidence_verified=evidence_verified,
        entry_count=len(entries),
    )


# ---------------------------------------------------------------------------
# Ledger writers: define / enter / pass / halt
# ---------------------------------------------------------------------------


def define_mission(*, ledger: WorkLedger, spec: MissionSpec) -> LedgerEntry:
    """Append a ``mission.defined`` transition for a validated spec.

    The payload binds the spec hash, the goal digest, and the phase skeleton
    (ids, names, gates, envelopes, budgets) -- never the raw goal text. This is
    the only mission declaration; status is projected, never stored.

    A mission owns its ledger outright: the definition must be the first
    transition in it. A second definition would leave the projection and the
    evidence lookup free to read different specs from the same chain, so it is
    refused at the writer rather than detected later.

    Raises:
        MissionSpecError: when the spec fails validation, or the ledger is not
            an empty ledger awaiting its definition.
    """
    spec.validate()
    if ledger.next_seq != 0:
        existing = list(LedgerReader(ledger.ledger_dir).entries())
        if any(entry.kind == KIND_MISSION_DEFINED for entry in existing):
            msg = f"mission {spec.mission_id!r} is already defined in this ledger"
            raise MissionSpecError(msg)
        msg = f"a mission must be defined into an empty ledger (found {len(existing)} prior entries)"
        raise MissionSpecError(msg)
    return ledger.append(
        kind=KIND_MISSION_DEFINED,
        task_id="",
        payload={
            "mission_id": spec.mission_id,
            "spec_hash": spec.spec_hash(),
            "goal_digest": spec.goal_digest(),
            "schema_version": spec.schema_version,
            "phases": [phase.to_dict() for phase in spec.phases],
        },
    )


def _validated_spend(phase: PhaseSpec, spend_usd: object, *, enforce_budget: bool) -> float:
    """Return *spend_usd* as a sealable figure, or raise.

    The receipt is only as trustworthy as the figures it seals. A non-finite
    spend serialises as invalid JSON and would break the canonical receipt
    bytes every verifier recomputes; a negative spend is not a spend; and a
    pass claiming more than the phase envelope allows contradicts the ceiling
    the dispatch gate enforced. Each is refused before anything is appended,
    so the chain never carries an unvalidated figure.

    Args:
        phase: The phase whose envelope bounds the spend.
        spend_usd: The figure the caller proposes to seal.
        enforce_budget: Whether to apply the phase ceiling. A halt is exempt:
            an exhausted envelope is precisely why a phase halts, so its
            receipt legitimately records spend at or beyond the budget.

    Raises:
        MissionSpecError: naming the exact violated rule.
    """
    if isinstance(spend_usd, bool) or not isinstance(spend_usd, (int, float)):
        msg = f"phase {phase.phase_id!r} spend must be a number, got {type(spend_usd).__name__}"
        raise MissionSpecError(msg)
    if not math.isfinite(spend_usd):
        msg = f"phase {phase.phase_id!r} spend must be finite (got {spend_usd!r})"
        raise MissionSpecError(msg)
    if spend_usd < 0.0:
        msg = f"phase {phase.phase_id!r} spend must be >= 0 (got {spend_usd})"
        raise MissionSpecError(msg)
    if enforce_budget and phase.budget_usd > 0.0 and spend_usd > phase.budget_usd:
        msg = f"phase {phase.phase_id!r} spend {spend_usd} exceeds its budget envelope of {phase.budget_usd}"
        raise MissionSpecError(msg)
    # Normalise -0.0 to +0.0. It passes the >= 0 test (-0.0 < 0.0 is False) but
    # serialises as the distinct JSON token "-0.0", so sealing it would bind a
    # hash over a value that no round trip through the ledger can reproduce.
    return float(spend_usd) + 0.0


def enter_phase(*, ledger: WorkLedger, mission_id: str, phase_id: str) -> LedgerEntry:
    """Append a ``mission.phase_entered`` transition marking a phase active."""
    return ledger.append(
        kind=KIND_MISSION_PHASE_ENTERED,
        task_id=phase_id,
        payload={"mission_id": mission_id, "phase_id": phase_id},
    )


def pass_phase(
    *,
    ledger: WorkLedger,
    spec: MissionSpec,
    phase_id: str,
    evidence_hashes: Mapping[str, str],
    spend_usd: float,
    chain: AuditChainStore | None = None,
) -> PhaseReceipt:
    """Advance a phase by sealing a mission phase receipt into the ledger.

    The gate verdict is a pure function of the evidence: every task id the
    phase gates on must have a passing sealed evidence bundle
    (present in *evidence_hashes*). A phase whose gate is unsatisfied cannot
    advance -- there is no "pass" without a receipt, and no receipt without
    the evidence it binds.

    Args:
        ledger: The open mission work ledger.
        spec: The validated mission spec.
        phase_id: The phase to advance.
        evidence_hashes: Current content addresses of the sealed evidence
            bundles (from :func:`gather_evidence_hashes`).
        spend_usd: The phase envelope spend at gate time.
        chain: Optional audit chain to mirror the receipt onto.

    Returns:
        The sealed :class:`PhaseReceipt`.

    Raises:
        MissionSpecError: when the phase gate is unsatisfied, or the spend is
            not a finite, non-negative figure within the phase envelope.
    """
    phase = spec.phase(phase_id)
    sealable_spend = _validated_spend(phase, spend_usd, enforce_budget=True)
    gate_task_ids = tuple(sorted(phase.gate))
    missing = [task_id for task_id in gate_task_ids if task_id not in evidence_hashes]
    if missing:
        msg = f"phase {phase_id!r} gate unsatisfied: no passing evidence for {missing}"
        raise MissionSpecError(msg)

    bundle_hashes = tuple(evidence_hashes[task_id] for task_id in gate_task_ids)
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id=phase_id,
        gate_passed=True,
        evidence_task_ids=gate_task_ids,
        evidence_bundle_hashes=bundle_hashes,
        ledger_seq=ledger.next_seq,
        envelope=phase.envelope,
        spend_usd=sealable_spend,
    )
    entry = ledger.append(
        kind=KIND_MISSION_PHASE_PASSED,
        task_id=phase_id,
        payload=receipt.to_payload(),
    )
    if chain is not None:
        _mirror_receipt(chain, receipt, entry.entry_hash)
    return receipt


def halt_phase(
    *,
    ledger: WorkLedger,
    spec: MissionSpec,
    phase_id: str,
    spend_usd: float,
    reason: str,
    chain: AuditChainStore | None = None,
) -> PhaseReceipt:
    """Halt a phase (for example on an exhausted envelope) with a receipt.

    The halt is a first-class ledger transition: the projection derives the
    :data:`PHASE_HALTED` state from it, so a halted phase is provable from the
    chain alone. Other phases are unaffected -- a halt never propagates to a
    sibling phase, and the mission halts only once nothing remains runnable.

    The recorded spend may sit at or above the budget (an exhausted envelope is
    the usual reason to halt), but it must still be a finite, non-negative
    figure or the receipt bytes would not survive a round trip.

    Raises:
        MissionSpecError: when the spend is not finite and non-negative.
    """
    phase = spec.phase(phase_id)
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id=phase_id,
        gate_passed=False,
        evidence_task_ids=(),
        evidence_bundle_hashes=(),
        ledger_seq=ledger.next_seq,
        envelope=phase.envelope,
        spend_usd=_validated_spend(phase, spend_usd, enforce_budget=False),
        reason=reason,
    )
    entry = ledger.append(
        kind=KIND_MISSION_PHASE_HALTED,
        task_id=phase_id,
        payload=receipt.to_payload(),
    )
    if chain is not None:
        _mirror_receipt(chain, receipt, entry.entry_hash)
    return receipt


def _mirror_receipt(chain: AuditChainStore, receipt: PhaseReceipt, journal_entry_hash: str) -> None:
    from bernstein.core.security.audit_chain import record_mission_phase_receipt

    record_mission_phase_receipt(
        chain=chain,
        mission_id=receipt.mission_id,
        phase_id=receipt.phase_id,
        gate_passed=receipt.gate_passed,
        receipt_hash=receipt.receipt_hash(),
        evidence_bundle_hashes=receipt.evidence_bundle_hashes,
        ledger_seq=receipt.ledger_seq,
        envelope=receipt.envelope,
        spend_usd=receipt.spend_usd,
        journal_entry_hash=journal_entry_hash,
        reason=receipt.reason,
    )


# ---------------------------------------------------------------------------
# Per-phase budget envelope enforcement
# ---------------------------------------------------------------------------


def phase_envelope_key(mission_id: str, phase_id: str) -> str:
    """Return the run-scoped key a phase's spend entries are attributed to.

    Each phase runs under its own key so the existing dispatch gate's per-run
    ceiling enforces the phase envelope in isolation: exhausting one phase's
    envelope never gates another's.
    """
    return f"mission:{mission_id}:{phase_id}"


def enforce_phase_dispatch(
    *,
    mission_id: str,
    phase: PhaseSpec,
    entries: Sequence[Any],
    projected_cost_usd: float,
    now_ts: float,
) -> RunDispatchOutcome:
    """Gate a phase dispatch against its budget envelope via the dispatch gate.

    Routes through the shipped :func:`evaluate_run_dispatch`: the phase budget
    is the per-run USD ceiling and the candidate is attributed to the phase's
    envelope key, so a dispatch that would push the phase over its budget halts
    with a decision the caller seals into a :func:`halt_phase` receipt, while
    other phases (distinct keys) are untouched.

    Args:
        mission_id: The mission the phase belongs to.
        phase: The phase spec (its ``budget_usd`` becomes the ceiling).
        entries: The spend ledger
            (:class:`bernstein.core.cost.spend_ledger.LedgerEntry` rows).
        projected_cost_usd: The projected spend of the dispatch under test.
        now_ts: Timestamp used to bucket synthetic within-tick spend.

    Returns:
        A :class:`RunDispatchOutcome`; ``halt`` is set when the envelope is
        exhausted.
    """
    caps = CostCaps(per_run_usd=phase.budget_usd)
    candidate = DispatchCandidate(
        task_id=phase.phase_id,
        run_id=phase_envelope_key(mission_id, phase.phase_id),
        model="",
        projected_cost_usd=projected_cost_usd,
        day_key="",
        pool=phase.envelope,
    )
    return evaluate_run_dispatch(
        candidates=[candidate],
        entries=list(entries),
        caps=caps,
        price_table_hash=DEFAULT_PRICE_TABLE.content_hash(),
        now_ts=now_ts,
    )


def phase_spend_report(
    phase: PhaseSpec,
    usages: Sequence[TokenUsage],
    *,
    now: float | None = None,
) -> EnvelopeRollupRow:
    """Roll a phase's token-usage records up into its envelope report.

    The per-phase spend rollup uses the same
    :func:`bernstein.core.cost.cost_rollup_by_envelope.rollup` the cost surface
    ships, scoped to the phase's envelope and budget, so the envelope report a
    dashboard renders matches exactly the spend the dispatch gate enforces.
    """
    config = EnvelopeConfig(name=phase.envelope, budget_usd=phase.budget_usd)
    rows = rollup(list(usages), {phase.envelope: config}, now=now)
    return rows[phase.envelope]


__all__ = [
    "KIND_MISSION_DEFINED",
    "KIND_MISSION_PHASE_ENTERED",
    "KIND_MISSION_PHASE_HALTED",
    "KIND_MISSION_PHASE_PASSED",
    "MISSION_ACTIVE",
    "MISSION_COMPLETE",
    "MISSION_HALTED",
    "MISSION_PENDING",
    "MISSION_SPEC_SCHEMA_VERSION",
    "MISSION_STATUS_SCHEMA_VERSION",
    "MISSION_UNVERIFIED",
    "PHASE_ACTIVE",
    "PHASE_HALTED",
    "PHASE_PASSED",
    "PHASE_PENDING",
    "PHASE_UNVERIFIED",
    "MissionProjection",
    "MissionSpec",
    "MissionSpecError",
    "MissionStatus",
    "PhaseReceipt",
    "PhaseSpec",
    "PhaseStatus",
    "define_mission",
    "enforce_phase_dispatch",
    "enter_phase",
    "gather_evidence_hashes",
    "halt_phase",
    "list_missions",
    "mission_ledger_dir",
    "pass_phase",
    "phase_envelope_key",
    "phase_spend_report",
    "project_mission",
    "project_mission_from_ledger",
]
