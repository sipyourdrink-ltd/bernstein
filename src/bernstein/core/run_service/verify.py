"""Offline verification of a detached run's lifecycle receipts (#2352).

:func:`verify_run` re-checks, from bytes on disk and with no network, that:

* the HMAC audit chain is intact (the same check ``bernstein audit verify``
  runs, so a tampered receipt fails here too);
* the work ledger chain recomputes end to end; and
* every lifecycle receipt binds a ledger head that actually exists in the
  chain, and every reattach / daemon-restart boundary is a genuine ancestor of
  the head recorded at that boundary (no forged off-record continuity).

The three checks together are what makes a reattach trustworthy after the
fact: the visible receipts are provably a projection of the executed chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bernstein.core.persistence.work_ledger import GENESIS_HASH, LedgerReader, run_ledger_dir
from bernstein.core.run_service.receipts import (
    CONTINUITY_TRANSITIONS,
    TRANSITION_SUBMITTED,
)
from bernstein.core.security.audit_chain import EVENT_RUN_LIFECYCLE, AuditChainStore


@dataclass(frozen=True)
class RunVerification:
    """Outcome of :func:`verify_run`."""

    run_id: str
    ok: bool
    audit_ok: bool
    ledger_ok: bool
    continuity_ok: bool
    receipts_seen: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "ok": self.ok,
            "audit_ok": self.audit_ok,
            "ledger_ok": self.ledger_ok,
            "continuity_ok": self.continuity_ok,
            "receipts_seen": self.receipts_seen,
            "errors": list(self.errors),
        }


def _hash_index(hashes: list[str], value: str) -> int:
    """Return the chain index of *value* (GENESIS is -1), or -2 when absent."""
    if value == GENESIS_HASH:
        return -1
    try:
        return hashes.index(value)
    except ValueError:
        return -2


def verify_run(root: Path, run_id: str) -> RunVerification:
    """Verify a run's lifecycle receipts, audit chain, and ledger, offline."""
    root = Path(root)
    sdd = root / ".sdd"
    errors: list[str] = []

    chain = AuditChainStore(sdd / "audit")
    audit_ok, audit_errors = chain.verify()
    if not audit_ok:
        errors.extend(f"audit: {e}" for e in audit_errors)

    reader = LedgerReader(run_ledger_dir(sdd, run_id))
    ledger_result = reader.verify()
    ledger_ok = ledger_result.ok
    if not ledger_ok:
        errors.extend(f"ledger: {e}" for e in ledger_result.errors)

    hashes = [entry.entry_hash for entry in reader.entries()] if ledger_ok else []

    receipts = [e for e in chain.query(event_type=EVENT_RUN_LIFECYCLE) if e.details.get("run_id") == run_id]
    receipts_seen = len(receipts)

    continuity_ok = True
    if receipts_seen == 0:
        continuity_ok = False
        errors.append("continuity: no lifecycle receipts for this run")
    if receipts and receipts[0].details.get("transition") != TRANSITION_SUBMITTED:
        continuity_ok = False
        errors.append("continuity: first receipt is not a submit")

    if ledger_ok:
        for event in receipts:
            details = event.details
            transition = str(details.get("transition"))
            head = str(details.get("ledger_head", ""))
            if _hash_index(hashes, head) == -2:
                continuity_ok = False
                errors.append(f"continuity: {transition} receipt binds a ledger head absent from the chain")
            if transition in CONTINUITY_TRANSITIONS:
                from_idx = _hash_index(hashes, str(details.get("from_head", "")))
                to_idx = _hash_index(hashes, str(details.get("to_head", "")))
                if from_idx == -2 or to_idx == -2:
                    continuity_ok = False
                    errors.append(f"continuity: {transition} boundary head is not an ancestor in the chain")
                elif from_idx > to_idx:
                    continuity_ok = False
                    errors.append(f"continuity: {transition} boundary runs backwards (from ahead of to)")

    ok = audit_ok and ledger_ok and continuity_ok
    return RunVerification(
        run_id=run_id,
        ok=ok,
        audit_ok=audit_ok,
        ledger_ok=ledger_ok,
        continuity_ok=continuity_ok,
        receipts_seen=receipts_seen,
        errors=errors,
    )


__all__ = [
    "RunVerification",
    "verify_run",
]
