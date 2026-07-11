"""Continuity proof + lifecycle vocabulary for the detached run service (#2352).

The reattach artefact is a :class:`ContinuityProof`: a deterministic
projection of the durable work ledger that proves the current chain head is a
forward extension of the head the operator last saw at the detach boundary.

The proof is the whole point of reattaching -- it is the audit chain in the
shape of ``attach``. Strip the ledger chain and :func:`prove_continuity`
returns nothing meaningful; a boundary head that is absent from the chain, or
a chain that no longer verifies, fails closed so a reattaching operator can
prove nothing happened off the record while they were away.

:func:`prove_continuity` is a pure function of the ledger bytes: no clock, no
environment, no network. Two operators handed the same ledger and the same
boundary head derive byte-identical proof JSON.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bernstein.core.persistence.work_ledger import GENESIS_HASH, LedgerReader

# ---------------------------------------------------------------------------
# Lifecycle transition vocabulary (the five boundaries the issue enumerates)
# ---------------------------------------------------------------------------

TRANSITION_SUBMITTED = "submitted"
TRANSITION_DETACHED = "detached"
TRANSITION_REATTACHED = "reattached"
TRANSITION_DAEMON_RESTARTED = "daemon_restarted"
TRANSITION_COMPLETED = "completed"

#: The transitions in canonical lifecycle order.
LIFECYCLE_TRANSITIONS: tuple[str, ...] = (
    TRANSITION_SUBMITTED,
    TRANSITION_DETACHED,
    TRANSITION_REATTACHED,
    TRANSITION_DAEMON_RESTARTED,
    TRANSITION_COMPLETED,
)

#: Transitions that carry a ``from_head`` -> ``to_head`` continuity span (i.e.
#: an operator or supervisor re-joined a run that advanced while they were gone)
#: and therefore assert a continuity proof at verification time.
CONTINUITY_TRANSITIONS: frozenset[str] = frozenset({TRANSITION_REATTACHED, TRANSITION_DAEMON_RESTARTED})


@dataclass(frozen=True)
class ContinuityProof:
    """Deterministic proof that a ledger head extends a prior boundary head.

    Attributes:
        run_id: The run the proof concerns.
        ok: True when the chain verifies and the current head is a forward
            extension of ``boundary_head``.
        ledger_verified: True when the ledger chain recomputed end to end.
        boundary_head: The head the operator/supervisor last observed.
        current_head: The ledger head at proof time.
        entries_added: Entries appended after ``boundary_head``.
        reason: Human-readable failure cause (empty on success).
    """

    run_id: str
    ok: bool
    ledger_verified: bool
    boundary_head: str
    current_head: str
    entries_added: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "ledger_verified": self.ledger_verified,
            "boundary_head": self.boundary_head,
            "current_head": self.current_head,
            "entries_added": self.entries_added,
            "reason": self.reason,
        }

    def to_canonical_json(self) -> str:
        """Return the canonical (sorted, compact) JSON form of the proof."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def prove_continuity(reader: LedgerReader, boundary_head: str, *, run_id: str = "") -> ContinuityProof:
    """Prove the ledger head extends *boundary_head*.

    Verifies the chain end to end, then locates *boundary_head* within it. A
    ``GENESIS_HASH`` boundary (first attach) is satisfied by any verifying
    chain. A boundary that is absent from the chain means the visible ledger
    diverged from what the operator last saw -- the proof fails closed.

    Args:
        reader: Read-only view over the run's work ledger.
        boundary_head: The entry hash the operator/supervisor last observed.
        run_id: Recorded on the proof for downstream attribution.

    Returns:
        A :class:`ContinuityProof`; ``ok`` is True only when the chain
        verifies and the current head is a forward extension of the boundary.
    """
    verification = reader.verify()
    current_head = verification.head_hash
    if not verification.ok:
        reason = verification.errors[0] if verification.errors else "ledger chain does not verify"
        return ContinuityProof(
            run_id=run_id,
            ok=False,
            ledger_verified=False,
            boundary_head=boundary_head,
            current_head=current_head,
            entries_added=0,
            reason=reason,
        )

    hashes = [entry.entry_hash for entry in reader.entries()]

    if boundary_head == GENESIS_HASH:
        return ContinuityProof(
            run_id=run_id,
            ok=True,
            ledger_verified=True,
            boundary_head=boundary_head,
            current_head=current_head,
            entries_added=len(hashes),
        )

    try:
        index = hashes.index(boundary_head)
    except ValueError:
        return ContinuityProof(
            run_id=run_id,
            ok=False,
            ledger_verified=True,
            boundary_head=boundary_head,
            current_head=current_head,
            entries_added=0,
            reason="boundary head not found in ledger chain (off-record divergence)",
        )

    entries_added = (len(hashes) - 1) - index
    return ContinuityProof(
        run_id=run_id,
        ok=True,
        ledger_verified=True,
        boundary_head=boundary_head,
        current_head=current_head,
        entries_added=entries_added,
    )


__all__ = [
    "CONTINUITY_TRANSITIONS",
    "LIFECYCLE_TRANSITIONS",
    "TRANSITION_COMPLETED",
    "TRANSITION_DAEMON_RESTARTED",
    "TRANSITION_DETACHED",
    "TRANSITION_REATTACHED",
    "TRANSITION_SUBMITTED",
    "ContinuityProof",
    "prove_continuity",
]
