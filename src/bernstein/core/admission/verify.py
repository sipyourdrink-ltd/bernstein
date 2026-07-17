"""Fail-closed verification of the admission ledger (#2544).

``bernstein limits verify`` recomputes admission state from genesis and fails
closed on any hash mismatch or any grant the projection would not have issued.
Two independent checks compose:

1. **Chain integrity** -- reuse the work-ledger walker
   (:meth:`~bernstein.core.persistence.work_ledger.LedgerReader.verify`), which
   recomputes every entry hash and names the exact position of a mutated
   payload or a swapped pair (a reorder breaks the ``prev_hash`` linkage).
2. **Admission soundness** -- replay the chain and, at each grant row under an
   ENFORCE pool, assert a slot was actually free. A forged grant injected past
   capacity is a grant the projection would not have issued, and is reported at
   its exact position.

The verifier is offline: it needs only the ledger bytes. "Who held pool P at
time T" is then answerable from the verified projection alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.admission.ledger import (
    KIND_EXPIRE,
    KIND_GRANT,
    KIND_POOL_CHANGED,
    KIND_RELEASE,
)
from bernstein.core.admission.models import PoolSpec, Posture

if TYPE_CHECKING:
    from bernstein.core.persistence.work_ledger import LedgerReader

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

    # Admission soundness: replay grant/release/expire under ENFORCE pools and
    # flag any grant issued past capacity. One pass; the grant->pool map is
    # built as grants are seen (a grant always precedes its release/expire).
    pools: dict[str, PoolSpec] = {}
    occupancy: dict[str, int] = {}
    grant_pool: dict[str, str] = {}
    for index, entry in enumerate(reader.entries()):
        kind = entry.kind
        if kind == KIND_POOL_CHANGED:
            spec = PoolSpec.from_dict(entry.payload)
            pools[spec.name] = spec
        elif kind == KIND_GRANT:
            pool = str(entry.payload.get("pool", ""))
            over_limit = bool(entry.payload.get("over_limit", False))
            spec = pools.get(pool)
            if (
                pool
                and spec is not None
                and spec.posture is Posture.ENFORCE
                and not over_limit
                and occupancy.get(pool, 0) >= spec.slots
            ):
                errors.append(
                    f"entry {index} (grant {entry.entry_hash[:16]}...): "
                    f"pool {pool!r} was at capacity ({spec.slots}); "
                    f"the projection would not have issued this grant"
                )
            if pool:
                grant_pool[entry.entry_hash] = pool
                occupancy[pool] = occupancy.get(pool, 0) + 1
        elif kind in (KIND_RELEASE, KIND_EXPIRE):
            pool = grant_pool.get(str(entry.payload.get("grant", "")), "")
            if pool:
                occupancy[pool] = max(0, occupancy.get(pool, 0) - 1)

    return AdmissionVerification(
        ok=not errors,
        head_hash=chain.head_hash,
        entries=chain.entries,
        errors=tuple(errors),
    )
