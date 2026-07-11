"""Dispatch receipts: the budget decision IS a verifiable record (#2354).

Every budget decision is anchored, not merely logged. :func:`build_dispatch_receipt`
seals a :class:`~bernstein.core.cost.scheduling.policy.DispatchDecision` two ways:

* the decision's canonical bytes are appended to the ``cost-dispatch`` run of
  the Merkle+HMAC lineage spine (:class:`~bernstein.core.lineage.spine.LineageSpine`),
  and the spine entry hash becomes the receipt's ``journal_entry_hash``; and
* the receipt identity is mirrored into the HMAC audit chain via
  :func:`~bernstein.core.security.audit_chain.record_cost_dispatch_receipt`.

The receipt is the proof, not a decoration on a log line: a halt receipt names
the exact policy inputs (the pinned ``price_table_hash``, the ``ledger_state_hash``
over the projected prior spend, and the ``policy_hash`` over the caps) and the
projected overrun, and :func:`verify_dispatch_receipt` re-derives the decision
hash from the stored bytes and re-checks the spine anchor offline. A forged
receipt (an ``admit`` flipped, an overrun zeroed) recomputes to a different
decision hash and fails verification exactly like a tampered chain entry.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.scheduling.policy import DispatchDecision
from bernstein.core.lineage.spine import LineageSpine, content_hash_of

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Version stamped into every receipt binding. Bump only on a wire-format
#: change.
DISPATCH_RECEIPT_SCHEMA_VERSION = 1

#: Lineage run id under which every dispatch receipt is anchored, kept separate
#: so budget receipts never interleave with per-task journals.
DISPATCH_RUN_ID = "cost-dispatch"

_DISPATCH_ACTOR = "bernstein.cost_policy"
_DISPATCH_SUBPATH = (".sdd", "cost", "dispatch")
_DECISION_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def dispatch_receipt_path(workdir: Path, decision_hash: str) -> Path:
    """Return the on-disk receipt path for *decision_hash* under *workdir*.

    The decision hash is validated against ``sha256:<64 hex>`` and the
    resulting path is asserted to stay under the dispatch directory, so a
    caller-influenced hash can never escape the receipt store (CodeQL
    path-injection defense in depth).

    Raises:
        ValueError: The decision hash is not a canonical ``sha256:`` digest,
            or the resolved path escapes the dispatch directory.
    """
    if not _DECISION_HASH_RE.match(decision_hash):
        raise ValueError(f"decision_hash is not a canonical sha256 digest: {decision_hash!r}")
    base = workdir.joinpath(*_DISPATCH_SUBPATH)
    candidate = base / f"{decision_hash}.json"
    base_real = os.path.realpath(base)
    cand_real = os.path.realpath(candidate)
    if os.path.commonpath([base_real, cand_real]) != base_real:
        raise ValueError(f"receipt path escapes dispatch directory: {decision_hash!r}")
    return candidate


@dataclass(frozen=True)
class DispatchReceipt:
    """A sealed dispatch-decision receipt.

    The receipt wraps a :class:`DispatchDecision` plus a schema version and a
    caller-supplied timestamp. Its on-disk form flattens the decision fields to
    the top level (so ``price_table_hash``, ``admit``, ``projected_overrun_usd``
    read directly) alongside the receipt metadata.
    """

    decision: DispatchDecision
    schema_version: int = DISPATCH_RECEIPT_SCHEMA_VERSION
    timestamp: int = 0
    journal_entry_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = self.decision.to_dict()
        data["schema_version"] = self.schema_version
        data["timestamp"] = self.timestamp
        data["journal_entry_hash"] = self.journal_entry_hash
        return data

    def to_canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DispatchReceipt:
        return cls(
            decision=DispatchDecision.from_dict(raw),
            schema_version=int(raw.get("schema_version", DISPATCH_RECEIPT_SCHEMA_VERSION)),
            timestamp=int(raw.get("timestamp", 0)),
            journal_entry_hash=str(raw.get("journal_entry_hash", "")),
        )


def build_dispatch_receipt(
    *,
    decision: DispatchDecision,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    timestamp: int,
    chain: AuditChainStore | None = None,
) -> DispatchReceipt:
    """Seal *decision* into the lineage spine, disk, and (optionally) the chain.

    The decision's canonical bytes are anchored in the ``cost-dispatch`` spine
    run; the entry hash becomes the receipt ``journal_entry_hash``. The receipt
    is written to :func:`dispatch_receipt_path`. When *chain* is supplied the
    receipt identity is mirrored into the HMAC audit chain.

    Args:
        decision: The decision to seal.
        workdir: Project root (receipt is written under ``.sdd/cost/dispatch``).
        lineage_root: ``.sdd/lineage`` root for the spine.
        hmac_key: Audit-chain HMAC key for the spine seal.
        timestamp: Integer timestamp anchored into the spine entry (stable, so
            identical decisions seal byte-identically).
        chain: Optional :class:`AuditChainStore` accepting the mirror.

    Returns:
        The sealed :class:`DispatchReceipt` with its ``journal_entry_hash``.
    """
    content = decision.canonical_bytes()
    spine = LineageSpine(lineage_root, run_id=DISPATCH_RUN_ID, hmac_key=hmac_key)
    artifact_path = "/".join((*_DISPATCH_SUBPATH, f"{decision.decision_hash}.json"))
    anchor = spine.record(
        artifact_path=artifact_path,
        content=content,
        actor=_DISPATCH_ACTOR,
        step_id=decision.decision_hash,
        model=decision.model,
        timestamp=timestamp,
    )

    sealed = replace(
        DispatchReceipt(decision=decision, schema_version=DISPATCH_RECEIPT_SCHEMA_VERSION, timestamp=timestamp),
        journal_entry_hash=anchor,
    )
    path = dispatch_receipt_path(workdir, decision.decision_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sealed.to_canonical_json(), encoding="utf-8")

    if chain is not None:
        from bernstein.core.security.audit_chain import record_cost_dispatch_receipt

        record_cost_dispatch_receipt(
            chain=chain,
            decision_hash=decision.decision_hash,
            run_id=decision.run_id,
            task_id=decision.task_id,
            admit=decision.admit,
            breached_dimension=decision.breached_dimension,
            projected_overrun_usd=decision.projected_overrun_usd,
            price_table_hash=decision.price_table_hash,
            ledger_state_hash=decision.ledger_state_hash,
            policy_hash=decision.policy_hash,
            journal_entry_hash=anchor,
        )
    return sealed


def read_dispatch_receipt(workdir: Path, decision_hash: str) -> DispatchReceipt | None:
    """Return the sealed receipt for *decision_hash* or ``None`` if absent/bad."""
    try:
        path = dispatch_receipt_path(workdir, decision_hash)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return DispatchReceipt.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        logger.warning("cost: malformed dispatch receipt at %s", path)
        return None


@dataclass(frozen=True, slots=True)
class DispatchVerifyResult:
    """Outcome of an offline dispatch-receipt verification."""

    ok: bool
    reason: str
    receipt: DispatchReceipt | None


def verify_dispatch_receipt(
    *,
    workdir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    decision_hash: str,
) -> DispatchVerifyResult:
    """Re-verify the receipt for *decision_hash* offline.

    Checks, from the stored receipt alone: the decision hash recomputes from
    the stored fields (catches a forged ``admit`` / zeroed overrun), the
    lineage spine verifies, and the spine contains an entry whose content hash
    matches the decision's canonical bytes and whose entry hash matches the
    receipt's ``journal_entry_hash``.
    """
    receipt = read_dispatch_receipt(workdir, decision_hash)
    if receipt is None:
        return DispatchVerifyResult(ok=False, reason=f"no dispatch receipt for {decision_hash!r}", receipt=None)
    decision = receipt.decision
    if decision.decision_hash != decision_hash:
        return DispatchVerifyResult(ok=False, reason="receipt decision_hash does not match request", receipt=receipt)
    if not decision.verify_self_hash():
        return DispatchVerifyResult(
            ok=False, reason="decision_hash does not recompute from the receipt body (tampered)", receipt=receipt
        )

    spine = LineageSpine(lineage_root, run_id=DISPATCH_RUN_ID, hmac_key=hmac_key)
    report = spine.verify()
    if not report.ok:
        detail = "; ".join(report.errors) if report.errors else report.status.value
        return DispatchVerifyResult(ok=False, reason=f"dispatch spine failed verification: {detail}", receipt=receipt)

    expected_content = content_hash_of(decision.canonical_bytes())
    anchored = any(
        entry.entry_hash == receipt.journal_entry_hash and entry.content_hash == expected_content
        for entry in spine.iter_entries()
    )
    if not anchored:
        return DispatchVerifyResult(ok=False, reason="receipt is not anchored in the dispatch spine", receipt=receipt)
    return DispatchVerifyResult(ok=True, reason="", receipt=receipt)


__all__ = [
    "DISPATCH_RECEIPT_SCHEMA_VERSION",
    "DISPATCH_RUN_ID",
    "DispatchReceipt",
    "DispatchVerifyResult",
    "build_dispatch_receipt",
    "dispatch_receipt_path",
    "read_dispatch_receipt",
    "verify_dispatch_receipt",
]
