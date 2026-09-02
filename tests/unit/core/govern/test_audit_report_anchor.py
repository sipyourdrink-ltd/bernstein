"""The govern audit report is a chain-anchored artefact (issue #5077).

The report is not a printout: its canonical bytes are deterministic, its
sha256 identifies a posture, it is anchored in the lineage spine over exactly
those bytes the way ``GovernanceDecision`` is, and drift between two audits is
a comparison of two anchored artefacts rather than a re-run.

Each test names the property it protects:

1. two audits over an unchanged install produce byte-identical reports;
2. a changed verdict changes the report hash;
3. the producer's finding order does not change the report bytes;
4. an anchored report verifies offline against the spine;
5. a byte-flipped stored report is unverifiable, not merely different;
6. a forged ``journal_entry_hash`` fails verification;
7. drift lists only findings whose verdict or evidence changed;
8. drift names an appeared and a disappeared finding;
9. a report references the anchor of the inventory it audited;
10. duplicate finding ids are rejected, so the diff stays well-defined;
11. ``bernstein audit verify`` fails when a stored report is tampered.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.govern.audit_report import (
    GOVERN_AUDIT_RUN_ID,
    AuditReport,
    anchor_audit_report,
    diff_reports,
    read_audit_report,
    reports_dir,
    verify_audit_report,
)

_KEY = b"k" * 32


def _lineage_root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _finding(
    finding_id: str,
    *,
    verdict: str = "measured",
    passed: bool = True,
    evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one serialised finding record as a check producer would emit it."""
    return {
        "id": finding_id,
        "area": finding_id.split("-")[0].lower(),
        "verdict": verdict,
        "passed": passed,
        "summary": f"summary for {finding_id}",
        "remediation": f"remediate {finding_id}",
        "evidence": evidence
        if evidence is not None
        else [{"locator": f"config/{finding_id}.yaml", "sha256": "a" * 64}],
    }


def _report(findings: list[dict[str, Any]], *, timestamp: int = 1_700_000_000, **kwargs: Any) -> AuditReport:
    return AuditReport(findings=tuple(findings), timestamp=timestamp, **kwargs)


# --------------------------------------------------------------------------- 1


def test_two_audits_over_an_unchanged_install_produce_identical_report_bytes() -> None:
    first = _report([_finding("MDL-001"), _finding("OBS-004", verdict="declared", passed=False)])
    second = _report([_finding("MDL-001"), _finding("OBS-004", verdict="declared", passed=False)])

    assert first.to_canonical_bytes() == second.to_canonical_bytes()
    assert first.report_hash() == second.report_hash()


# --------------------------------------------------------------------------- 2


def test_report_hash_changes_when_a_finding_verdict_changes() -> None:
    before = _report([_finding("MDL-001", verdict="measured")])
    after = _report([_finding("MDL-001", verdict="not_measurable")])

    assert before.report_hash() != after.report_hash()


# --------------------------------------------------------------------------- 3


def test_producer_finding_order_does_not_change_the_report_bytes() -> None:
    forward = _report([_finding("MDL-001"), _finding("OBS-004"), _finding("CHG-002")])
    shuffled = _report([_finding("CHG-002"), _finding("MDL-001"), _finding("OBS-004")])

    assert forward.to_canonical_bytes() == shuffled.to_canonical_bytes()


# --------------------------------------------------------------------------- 4


def test_anchored_report_verifies_offline_against_the_spine(tmp_path: Path) -> None:
    root = _lineage_root(tmp_path)
    anchored = anchor_audit_report(
        lineage_root=root,
        hmac_key=_KEY,
        report=_report([_finding("MDL-001"), _finding("OBS-004")]),
    )

    assert anchored.journal_entry_hash
    result = verify_audit_report(lineage_root=root, hmac_key=_KEY, report=anchored)
    assert result.ok, result.reason

    # The stored copy round-trips to the same artefact, and its sha256 is the
    # posture identity used to address it later.
    stored = read_audit_report(root, anchored.report_hash())
    assert stored is not None
    assert stored.to_canonical_bytes() == anchored.to_canonical_bytes()
    assert stored.journal_entry_hash == anchored.journal_entry_hash


# --------------------------------------------------------------------------- 5


def test_byte_flipped_stored_report_is_unverifiable_not_merely_different(tmp_path: Path) -> None:
    root = _lineage_root(tmp_path)
    anchored = anchor_audit_report(
        lineage_root=root,
        hmac_key=_KEY,
        report=_report([_finding("MDL-001", verdict="measured", passed=False)]),
    )

    path = next(iter(sorted(reports_dir(root).glob("*.json"))))
    row = json.loads(path.read_text(encoding="utf-8"))
    row["findings"][0]["passed"] = True
    path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    tampered = read_audit_report(root, anchored.report_hash())
    # The stored bytes no longer address the posture they claim to be.
    assert tampered is None

    stored_rows = [AuditReport.from_dict(json.loads(p.read_bytes())) for p in sorted(reports_dir(root).glob("*.json"))]
    assert len(stored_rows) == 1
    result = verify_audit_report(lineage_root=root, hmac_key=_KEY, report=stored_rows[0])
    assert not result.ok
    assert "not anchored" in result.reason


# --------------------------------------------------------------------------- 6


def test_report_with_a_forged_journal_entry_hash_fails_verification(tmp_path: Path) -> None:
    root = _lineage_root(tmp_path)
    anchored = anchor_audit_report(
        lineage_root=root,
        hmac_key=_KEY,
        report=_report([_finding("MDL-001")]),
    )
    forged = AuditReport(
        findings=anchored.findings,
        timestamp=anchored.timestamp,
        inventory_hash=anchored.inventory_hash,
        inventory_anchor=anchored.inventory_anchor,
        journal_entry_hash="sha256:" + "0" * 64,
    )

    result = verify_audit_report(lineage_root=root, hmac_key=_KEY, report=forged)
    assert not result.ok
    assert "journal_entry_hash" in result.reason


# --------------------------------------------------------------------------- 7


def test_drift_lists_only_findings_whose_verdict_or_evidence_changed() -> None:
    before = _report(
        [
            _finding("MDL-001"),
            _finding("OBS-004"),
            _finding("CHG-002"),
        ]
    )
    after = _report(
        [
            # unchanged
            _finding("MDL-001"),
            # verdict changed
            _finding("OBS-004", verdict="not_measurable", passed=False),
            # evidence changed, verdict identical
            _finding("CHG-002", evidence=[{"locator": "config/CHG-002.yaml", "sha256": "b" * 64}]),
        ],
        timestamp=1_700_009_999,
    )

    drift = diff_reports(before, after)
    assert [d.finding_id for d in drift] == ["CHG-002", "OBS-004"]
    assert [d.change for d in drift] == ["evidence", "verdict"]
    # A changed timestamp alone is not drift: MDL-001 read the same evidence and
    # returned the same verdict, so it does not appear.
    assert "MDL-001" not in {d.finding_id for d in drift}


# --------------------------------------------------------------------------- 8


def test_drift_names_an_appeared_and_a_disappeared_finding() -> None:
    before = _report([_finding("MDL-001"), _finding("OBS-004")])
    after = _report([_finding("MDL-001"), _finding("CHG-002")])

    drift = {d.finding_id: d.change for d in diff_reports(before, after)}
    assert drift == {"CHG-002": "appeared", "OBS-004": "disappeared"}


# --------------------------------------------------------------------------- 9


def test_report_references_the_anchor_of_the_inventory_it_audited(tmp_path: Path) -> None:
    root = _lineage_root(tmp_path)
    from bernstein.core.govern.inventory_models import Inventory, Surface
    from bernstein.core.lineage.spine import LineageSpine

    inventory = Inventory(
        surfaces=(Surface(surface="agent:claude-code", observed_value="2.1.0", evidence_ref="fs:~/.claude"),)
    )
    inventory_bytes = json.dumps(inventory.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    inventory_anchor = LineageSpine(root, run_id=GOVERN_AUDIT_RUN_ID, hmac_key=_KEY).record(
        artifact_path="inventory.json",
        content=inventory_bytes,
        actor="bernstein.govern",
        step_id=inventory.content_hash(),
        model="none",
        timestamp=1_700_000_000,
    )

    anchored = anchor_audit_report(
        lineage_root=root,
        hmac_key=_KEY,
        report=_report(
            [_finding("MDL-001")],
            inventory_hash=inventory.content_hash(),
            inventory_anchor=inventory_anchor,
        ),
    )

    # One chain walk answers "audited what was enumerated on that date": the
    # report's own anchor, and the inventory anchor it names, are entries in
    # the same spine.
    result = verify_audit_report(lineage_root=root, hmac_key=_KEY, report=anchored)
    assert result.ok, result.reason

    entries = {e.entry_hash for e in LineageSpine(root, run_id=GOVERN_AUDIT_RUN_ID, hmac_key=_KEY).iter_entries()}
    assert anchored.inventory_anchor in entries
    assert anchored.journal_entry_hash in entries


def test_report_naming_an_inventory_anchor_absent_from_the_spine_fails_verification(tmp_path: Path) -> None:
    root = _lineage_root(tmp_path)
    anchored = anchor_audit_report(
        lineage_root=root,
        hmac_key=_KEY,
        report=_report(
            [_finding("MDL-001")],
            inventory_hash="sha256:" + "c" * 64,
            inventory_anchor="sha256:" + "d" * 64,
        ),
    )

    result = verify_audit_report(lineage_root=root, hmac_key=_KEY, report=anchored)
    assert not result.ok
    assert "inventory" in result.reason


# --------------------------------------------------------------------------- 10


def test_duplicate_finding_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate finding id"):
        _report([_finding("MDL-001"), _finding("MDL-001", verdict="declared")]).to_canonical_bytes()


def test_finding_without_an_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="finding id"):
        _report([{"verdict": "measured", "evidence": []}]).to_canonical_bytes()


# --------------------------------------------------------------------------- 11


def test_audit_verify_fails_when_a_stored_report_is_tampered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.commands import audit_cmd

    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path / ".sdd" / "audit")
    monkeypatch.setattr("bernstein.core.security.audit.load_audit_key", lambda *a, **k: _KEY)

    root = _lineage_root(tmp_path)
    anchor_audit_report(lineage_root=root, hmac_key=_KEY, report=_report([_finding("MDL-001")]))
    assert audit_cmd._verify_audit_reports() is True

    path = next(iter(sorted(reports_dir(root).glob("*.json"))))
    row = json.loads(path.read_text(encoding="utf-8"))
    row["findings"][0]["summary"] = "rewritten after the fact"
    path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    assert audit_cmd._verify_audit_reports() is False


def test_audit_verify_is_a_silent_no_op_without_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.commands import audit_cmd

    monkeypatch.setattr(audit_cmd, "AUDIT_DIR", tmp_path / ".sdd" / "audit")
    monkeypatch.setattr("bernstein.core.security.audit.load_audit_key", lambda *a, **k: _KEY)

    assert audit_cmd._verify_audit_reports() is True
