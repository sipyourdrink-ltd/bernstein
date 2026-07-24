"""Fail-closed verification of the admission ledger (#2544).

``bernstein limits verify`` recomputes admission state from genesis and fails
closed on any hash mismatch or any grant the projection would not have issued.
Two independent checks compose:

1. **Chain integrity** -- reuse the work-ledger walker
   (:meth:`~bernstein.core.persistence.work_ledger.LedgerReader.verify`), which
   recomputes every entry hash and names the exact position of a mutated
   payload or a swapped pair (a reorder breaks the ``prev_hash`` linkage).
2. **Admission soundness** -- project the chain and, at each grant row, replay
   the same ENFORCE predicate the engine admitted it with (pool capacity *and*
   tag limits), reading the state before the grant. A forged grant injected
   past any ENFORCE gate -- or one that references an undeclared pool, or
   carries an unbacked ``over_limit`` flag -- is a grant the projection would
   not have issued, and is reported at its exact position.

The verifier is offline: it needs only the ledger bytes. "Who held pool P at
time T" is then answerable from the verified projection alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.admission.ledger import KIND_GRANT
from bernstein.core.admission.projection import (
    AdmissionState,
    advise_over_gates,
    enforce_gate_refusal,
)

if TYPE_CHECKING:
    from bernstein.core.persistence.work_ledger import LedgerEntry, LedgerReader

__all__ = ["AdmissionVerification", "verify_admission_ledger"]


@dataclass(frozen=True, slots=True)
class AdmissionVerification:
    """Outcome of :func:`verify_admission_ledger`."""

    ok: bool
    head_hash: str
    entries: int
    errors: tuple[str, ...] = field(default_factory=tuple)


def verify_admission_ledger(reader: LedgerReader) -> AdmissionVerification:
    """Recompute admission state from genesis and fail closed on any drift."""
    if not reader.exists():
        return AdmissionVerification(ok=True, head_hash="0" * 64, entries=0)

    chain = reader.verify()
    errors: list[str] = list(chain.errors)

    # Admission soundness: project the chain row by row and, at each grant,
    # replay the *same* ENFORCE predicate the engine used to admit it, reading
    # the state projected from the rows before the grant. The projection tracks
    # held grants by id, so a repeated terminal row is idempotent (it cannot
    # under-count occupancy) and occupancy/limits reflect exactly what admission
    # saw. Every ENFORCE gate is replayed -- pool capacity and tag limits --
    # never only pool capacity, and the attacker-controlled over_limit flag can
    # neither buy an exemption nor stand unbacked by an exceeded ADVISE gate.
    state = AdmissionState()
    for index, entry in enumerate(reader.entries()):
        if entry.kind == KIND_GRANT:
            _check_grant_admissible(state, entry, index, errors)
        state.apply(entry)

    return AdmissionVerification(
        ok=not errors,
        head_hash=chain.head_hash,
        entries=chain.entries,
        errors=tuple(errors),
    )


def _check_grant_admissible(state: AdmissionState, entry: LedgerEntry, index: int, errors: list[str]) -> None:
    """Append an error for a grant the projection would not have issued."""
    payload = entry.payload
    pool = str(payload.get("pool", ""))
    declared = tuple(sorted({str(t) for t in payload.get("tags", []) if isinstance(t, str)}))
    over_limit = bool(payload.get("over_limit", False))
    prefix = f"entry {index} (grant {entry.entry_hash[:16]}...):"

    # A grant can only hold a slot in a declared pool. An undeclared pool means
    # the projection never sized the class, so it would not have issued this.
    if pool and pool not in state.pools:
        errors.append(f"{prefix} references undeclared pool {pool!r}; the projection would not have issued this grant")
        return

    refusal = enforce_gate_refusal(state, pool, declared)
    if refusal is not None:
        errors.append(f"{prefix} {refusal}; the projection would not have issued this grant")
        return

    # over_limit is only legitimate when an ADVISE gate is genuinely exceeded.
    # A grant that carries the flag without any over-limit ADVISE gate is a
    # forged waiver flag (the classic attempt to slip past an ENFORCE gate).
    if over_limit and not advise_over_gates(state, pool, declared):
        errors.append(
            f"{prefix} carries over_limit=True but no ADVISE gate was over limit; "
            f"the projection would not have set this flag"
        )
