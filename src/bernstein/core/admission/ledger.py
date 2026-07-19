"""Hash-chained admission ledger: grants as chained rows (#2544).

Every admission fact -- a grant, a release, a renewal, an expiry, a waiver, a
pool/tag/rate/queue change, a 429 observation, a quarantine -- is a row in a
hash-chained JSONL ledger. The ledger reuses the exact hashing contract of
:mod:`bernstein.core.persistence.work_ledger`
(:func:`~bernstein.core.persistence.work_ledger.compute_entry_hash`), so a
verifier that can walk a work ledger can walk an admission ledger, and mutating
or reordering any row surfaces as a hash mismatch at an exact position.

The admission ledger is stored under
``<sdd_dir>/runtime/admission/<ledger-id>/`` and is a plain
:class:`~bernstein.core.persistence.work_ledger.WorkLedger` whose ``kind``
values are the admission kinds below. Because the storage engine IS the work
ledger, the grant is not "a row plus a log"; the row's ``entry_hash`` *is* the
grant's identity. Strip the chain and there is no grant to speak of.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.persistence.work_ledger import (
    LedgerReader,
    WorkLedger,
)
from bernstein.core.security.path_containment import contained_path

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ADMISSION_KINDS",
    "KIND_EXPIRE",
    "KIND_GRANT",
    "KIND_POOL_CHANGED",
    "KIND_QUARANTINE",
    "KIND_QUEUE_CHANGED",
    "KIND_RATE_CHANGED",
    "KIND_RATE_OBSERVED",
    "KIND_RELEASE",
    "KIND_RENEW",
    "KIND_TAG_CHANGED",
    "KIND_WAIVE",
    "admission_ledger_dir",
    "open_admission_ledger",
    "open_admission_reader",
]

#: The default admission ledger id. A single fleet-wide admission chain is the
#: cross-process arbiter; per-run partitioning can key on a different id.
DEFAULT_ADMISSION_ID = "fleet"

# ---------------------------------------------------------------------------
# Row kinds (dotted, lowercase -- validated by WorkLedger._KIND_RE)
# ---------------------------------------------------------------------------

KIND_GRANT = "admission.grant"
KIND_RELEASE = "admission.release"
KIND_RENEW = "admission.renew"
KIND_EXPIRE = "admission.expire"
KIND_WAIVE = "admission.waive"
KIND_POOL_CHANGED = "admission.pool_changed"
KIND_TAG_CHANGED = "admission.tag_changed"
KIND_RATE_CHANGED = "admission.rate_changed"
KIND_RATE_OBSERVED = "admission.rate_observed"
KIND_QUEUE_CHANGED = "admission.queue_changed"
KIND_QUARANTINE = "admission.quarantine"

#: The complete set of admission kinds the projection understands. Any other
#: kind found in the chain is counted as unknown and otherwise ignored, exactly
#: like the work-ledger projection's forward-compatibility contract.
ADMISSION_KINDS: frozenset[str] = frozenset(
    {
        KIND_GRANT,
        KIND_RELEASE,
        KIND_RENEW,
        KIND_EXPIRE,
        KIND_WAIVE,
        KIND_POOL_CHANGED,
        KIND_TAG_CHANGED,
        KIND_RATE_CHANGED,
        KIND_RATE_OBSERVED,
        KIND_QUEUE_CHANGED,
        KIND_QUARANTINE,
    }
)


def admission_ledger_dir(sdd_dir: Path, ledger_id: str = DEFAULT_ADMISSION_ID) -> Path:
    """Return the on-disk directory backing an admission ledger.

    ``ledger_id`` names a directory, so it goes through the containment
    barrier: it must be a single safe path segment and the resolved
    directory must stay under the admission root. Callers build their
    reader or writer from the returned (checked) path.

    Raises:
        PathContainmentError: ``ledger_id`` is not a safe path segment, or
            the resolved directory escapes the admission root.
    """
    return contained_path(sdd_dir / "runtime" / "admission", ledger_id, label="admission ledger id")


def open_admission_ledger(sdd_dir: Path, ledger_id: str = DEFAULT_ADMISSION_ID) -> WorkLedger:
    """Open (or create) the append-only admission ledger.

    Recovery revalidates every row hash from genesis and fails closed on
    interior corruption -- inherited from the work-ledger writer.
    """
    return WorkLedger.open(admission_ledger_dir(sdd_dir, ledger_id))


def open_admission_reader(sdd_dir: Path, ledger_id: str = DEFAULT_ADMISSION_ID) -> LedgerReader:
    """Return a read-only view over the admission ledger."""
    return LedgerReader(admission_ledger_dir(sdd_dir, ledger_id))
