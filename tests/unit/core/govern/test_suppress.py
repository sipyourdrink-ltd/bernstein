"""Suppression of audit findings via GovernanceDecision anchoring (issue #5078).

Tests cover:

1. a suppression decision is created with the correct fields;
2. the decision is anchored in the govern-audit spine;
3. the artefact is persisted under the suppressions subdirectory;
4. the spine entry hash and artefact hash are consistent;
5. two suppressions of the same finding produce different artefacts (different timestamps).
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.govern.suppress import (
    SUPPRESS_ACTOR,
    anchor_suppress_decision,
    suppressions_dir,
)

_KEY = b"k" * 32


def _lineage_root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


# ---------------------------------------------------------------------------
# 1
# --------------------------------------------------------------------------


def test_suppression_decision_has_correct_fields(tmp_path: Path) -> None:
    anchored = anchor_suppress_decision(
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor EOL confirmed",
        expiry="2026-12-31",
        timestamp=1_700_000_000,
    )

    assert anchored.subject == "MDL-001"
    assert anchored.action == "suppress"
    assert anchored.verdict == "accepted"
    assert anchored.run_id == "govern-audit"
    assert anchored.context == {"reason": "Vendor EOL confirmed", "expiry": "2026-12-31"}
    assert anchored.journal_entry_hash.startswith("sha256:")
    assert anchored.inputs_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# 2
# --------------------------------------------------------------------------


def test_suppression_is_anchored_in_the_govern_audit_spine(tmp_path: Path) -> None:
    from bernstein.core.govern.audit_report import GOVERN_AUDIT_RUN_ID
    from bernstein.core.lineage.spine import LineageSpine

    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="OBS-004",
        reason="Risk accepted per security team",
        expiry="2027-01-01",
        timestamp=1_700_000_001,
    )

    spine = LineageSpine(lineage_root, run_id=GOVERN_AUDIT_RUN_ID, hmac_key=_KEY)
    entries = list(spine.iter_entries())

    hashes = {e.entry_hash for e in entries}
    assert anchored.journal_entry_hash in hashes

    # The last entry on the chain is the suppression.
    last = entries[-1]
    assert last.actor == SUPPRESS_ACTOR
    assert last.step_id == anchored.inputs_hash


# ---------------------------------------------------------------------------
# 3
# --------------------------------------------------------------------------


def test_suppression_artefact_is_persisted_under_suppressions_subdirectory(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="CHG-002",
        reason="Exception approved",
        expiry="2026-06-30",
        timestamp=1_700_000_002,
    )

    sdir = suppressions_dir(lineage_root)
    assert sdir.is_dir()

    files = list(sdir.glob("*.json"))
    assert len(files) == 1

    row = json.loads(files[0].read_text(encoding="utf-8"))
    assert row["subject"] == "CHG-002"
    assert row["verdict"] == "accepted"
    assert row["action"] == "suppress"
    assert row["context"]["reason"] == "Exception approved"
    assert row["context"]["expiry"] == "2026-06-30"
    assert row["journal_entry_hash"] == anchored.journal_entry_hash


# ---------------------------------------------------------------------------
# 4
# --------------------------------------------------------------------------


def test_spine_entry_content_hash_matches_decision_canonical_bytes(tmp_path: Path) -> None:
    from bernstein.core.lineage.spine import LineageSpine, content_hash_of

    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="SEC-007",
        reason="Temporary override for testing",
        expiry="2026-09-30",
        timestamp=1_700_000_003,
    )

    # The spine hashes the decision's canonical bytes (the binding without journal_entry_hash).
    spine = LineageSpine(lineage_root, run_id="govern-audit", hmac_key=_KEY)
    entries = list(spine.iter_entries())
    last = entries[-1]
    assert last.content_hash == content_hash_of(anchored.to_canonical_bytes())


# ---------------------------------------------------------------------------
# 5
# --------------------------------------------------------------------------


def test_two_suppressions_produce_different_artefacts(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    first = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="First suppression",
        expiry="2026-12-31",
        timestamp=1_700_000_004,
    )
    second = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Second suppression",
        expiry="2027-12-31",
        timestamp=1_700_000_005,
    )

    assert first.journal_entry_hash != second.journal_entry_hash
    assert first.inputs_hash != second.inputs_hash

    sdir = suppressions_dir(lineage_root)
    files = sorted(sdir.glob("*.json"))
    assert len(files) == 2

    first_bytes = files[0].read_bytes()
    second_bytes = files[1].read_bytes()
    assert first_bytes != second_bytes


def test_suppressions_dir(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    sd = suppressions_dir(lineage_root)
    assert sd == lineage_root / "govern-audit" / "suppressions"
