"""Suppression of audit findings via GovernanceDecision anchoring.

Issue #5078. An operator may suppress a finding that has been raised but
cannot or should not be acted upon within the current audit cycle. Suppression
is not dismissal: it is a recorded, chain-anchored decision that accepts the
finding as acknowledged for a defined window.

Each suppression is a :class:`~bernstein.core.security.governance.GovernanceDecision` with:

- ``verdict``: ``accepted``
- ``subject``: the finding id
- ``action``: ``suppress``
- ``context``: ``{"reason": "...", "expiry": "YYYY-MM-DD"}``

The decision is anchored in the govern-audit spine using the same
colocation pattern as :class:`GovernanceDecision` from
:mod:`bernstein.core.security.governance`, sharing the same
``GOVERN_AUDIT_RUN_ID`` spine so both artefacts live on one chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import LineageSpine

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.governance import GovernanceDecision

logger = logging.getLogger(__name__)

#: Actor recorded on the spine entry that anchors a suppression.
SUPPRESS_ACTOR = "bernstein.audit.suppress"

#: Model string recorded on suppression spine entries (no model runs at suppress time;
#: the field is part of the spine schema).
_SUPPRESS_MODEL = "none"

#: The spine run id every suppression anchors to. Uses the same govern-audit spine
#: as the audit reports so both artefacts share one chain.
_SUPPRESS_RUN_ID = "govern-audit"

#: Sub-path (relative to the audit run's spine dir) the persisted suppression records
#: land in, colocated with the spine so the artefact and its anchor share one root.
_SUPPRESS_SUBPATH = ("suppressions",)


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes (sorted keys, minimal separators, UTF-8)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _inputs_hash(finding_id: str, reason: str, expiry: str) -> str:
    """Return the content hash of the suppression inputs."""
    return _sha256(
        {
            "kind": "suppress",
            "finding_id": finding_id,
            "reason": reason,
            "expiry": expiry,
        }
    )


# ---------------------------------------------------------------------------
# Persistence (colocated with the govern-audit spine)
# ---------------------------------------------------------------------------


def suppressions_dir(lineage_root: Path) -> Path:
    """Return the directory holding persisted suppression records."""
    return lineage_root / _SUPPRESS_RUN_ID / _SUPPRESS_SUBPATH[0]


def _suppress_filename(finding_id: str, inputs_hash: str) -> str:
    """Return a stable artefact filename for a suppression of *finding_id*."""
    frag = inputs_hash[7:23] if inputs_hash.startswith("sha256:") else inputs_hash[:16]
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in finding_id) or "finding"
    return f"{safe_id}-{frag}.json"


def _next_seq(out_dir: Path) -> int:
    """Return the next zero-based emit index for *out_dir* (append order)."""
    if not out_dir.is_dir():
        return 0
    return sum(1 for _ in out_dir.glob("*.json"))


def anchor_suppress_decision(
    *,
    lineage_root: Path,
    hmac_key: bytes,
    finding_id: str,
    reason: str,
    expiry: str,
    timestamp: int,
) -> GovernanceDecision:
    """Anchor a suppression decision for *finding_id* and persist it.

    Creates a :class:`~bernstein.core.security.governance.GovernanceDecision`
    with ``verdict=accepted``, ``subject=finding_id``, and
    ``context={reason, expiry}``, then writes it to the govern-audit spine
    and persists the artefact.

    Returns the anchored copy.

    Args:
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: Audit-chain HMAC key that tags spine entries.
        finding_id: The finding id this suppression targets.
        reason: Human-readable justification for the suppression.
        expiry: ``YYYY-MM-DD`` date string after which the suppression lapses.
        timestamp: Integer timestamp for the decision and spine entry.

    Returns:
        The anchored :class:`~bernstein.core.security.governance.GovernanceDecision`.
    """
    from bernstein.core.security.governance import GovernanceDecision

    inputs_hash = _inputs_hash(finding_id, reason, expiry)
    decision = GovernanceDecision(
        run_id=_SUPPRESS_RUN_ID,
        subject=finding_id,
        action="suppress",
        verdict="accepted",
        inputs_hash=inputs_hash,
        timestamp=timestamp,
        context={"reason": reason, "expiry": expiry},
    )

    out_dir = suppressions_dir(lineage_root)
    _next_seq(out_dir)  # ensure directory exists for append order
    filename = _suppress_filename(finding_id, decision.inputs_hash)
    artifact_path = "/".join((*_SUPPRESS_SUBPATH, filename))

    anchor = LineageSpine(lineage_root, run_id=_SUPPRESS_RUN_ID, hmac_key=hmac_key).record(
        artifact_path=artifact_path,
        content=decision.to_canonical_bytes(),
        actor=SUPPRESS_ACTOR,
        step_id=decision.inputs_hash,
        model=_SUPPRESS_MODEL,
        timestamp=timestamp,
    )

    anchored = GovernanceDecision(
        run_id=decision.run_id,
        subject=decision.subject,
        action=decision.action,
        verdict=decision.verdict,
        inputs_hash=decision.inputs_hash,
        timestamp=decision.timestamp,
        context=decision.context,
        journal_entry_hash=anchor,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(
        json.dumps(anchored.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )

    logger.info(
        "suppressed finding %s until %s (actor=%s, anchor=%s)",
        finding_id,
        expiry,
        SUPPRESS_ACTOR,
        anchor,
    )

    return anchored


__all__ = [
    "anchor_suppress_decision",
    "suppressions_dir",
]
