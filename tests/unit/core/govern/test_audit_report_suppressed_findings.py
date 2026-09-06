"""Suppressed findings appear in reports re-labeled with the decision anchor.

Issue #5078 (AC2). When a report is rendered, every finding that has a current
suppression decision must still appear in the output -- never dropped -- but
its label is the suppression's ``accepted`` verdict plus the suppression
anchor. A suppression whose ``expiry`` is in the past no longer applies, so
the finding's normal verdict is shown instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.core.govern.audit_report import AuditReport
from bernstein.core.govern.suppress import (
    anchor_suppress_decision,
    read_suppressions,
)
from bernstein.core.security.governance import GovernanceDecision

_KEY = b"k" * 32


def _lineage_root(tmp_path: Path) -> Path:
    return tmp_path / ".sdd" / "lineage"


def _finding(finding_id: str, *, verdict: str = "measured", passed: bool = True) -> dict[str, Any]:
    return {
        "id": finding_id,
        "area": finding_id.split("-")[0].lower(),
        "verdict": verdict,
        "passed": passed,
        "summary": f"summary for {finding_id}",
        "remediation": f"remediate {finding_id}",
        "evidence": [{"locator": f"config/{finding_id}.yaml", "sha256": "a" * 64}],
    }


def _report(findings: list[dict[str, Any]], *, timestamp: int = 1_700_000_000) -> AuditReport:
    return AuditReport(findings=tuple(findings), timestamp=timestamp)


def test_suppressed_finding_is_relabeled_with_decision_anchor(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor EOL confirmed",
        expiry="2099-12-31",
        timestamp=1_700_000_000,
    )

    suppressions = tuple(read_suppressions(lineage_root))
    report = _report([_finding("MDL-001", verdict="measured", passed=True)])

    labels = report.suppressed_findings(suppressions, now=1_800_000_000)

    assert "MDL-001" in labels
    assert labels["MDL-001"]["anchor"] == anchored.journal_entry_hash
    assert labels["MDL-001"]["reason"] == "Vendor EOL confirmed"
    assert labels["MDL-001"]["expiry"] == "2099-12-31"


def test_unsuppressed_finding_is_not_in_labels(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Vendor EOL confirmed",
        expiry="2099-12-31",
        timestamp=1_700_000_000,
    )

    suppressions = tuple(read_suppressions(lineage_root))
    report = _report(
        [
            _finding("MDL-001", verdict="measured", passed=True),
            _finding("OBS-004", verdict="declared", passed=False),
        ]
    )

    labels = report.suppressed_findings(suppressions, now=1_800_000_000)
    assert set(labels) == {"MDL-001"}


def test_lapsed_suppression_is_ignored(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Old reason",
        expiry="2000-01-01",
        timestamp=1_700_000_000,
    )

    suppressions = tuple(read_suppressions(lineage_root))
    report = _report([_finding("MDL-001", verdict="measured", passed=True)])

    labels = report.suppressed_findings(suppressions, now=1_800_000_000)
    assert labels == {}


def test_unparseable_expiry_treated_as_lapsed(tmp_path: Path) -> None:
    lineage_root = _lineage_root(tmp_path)
    anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="MDL-001",
        reason="Bad expiry",
        expiry="not-a-date",
        timestamp=1_700_000_000,
    )

    suppressions = tuple(read_suppressions(lineage_root))
    report = _report([_finding("MDL-001", verdict="measured", passed=True)])

    labels = report.suppressed_findings(suppressions, now=1_800_000_000)
    assert labels == {}


def test_non_suppress_decision_is_ignored(tmp_path: Path) -> None:
    """A non-suppress GovernanceDecision must never re-label a finding."""
    decision = GovernanceDecision(
        run_id="govern-audit",
        subject="MDL-001",
        action="acknowledge",
        verdict="accepted",
        inputs_hash="sha256:" + "f" * 64,
        timestamp=1_700_000_000,
        context={"reason": "irrelevant", "expiry": "2099-12-31"},
        journal_entry_hash="sha256:" + "a" * 64,
    )
    report = _report([_finding("MDL-001", verdict="measured", passed=True)])

    labels = report.suppressed_findings((decision,), now=1_800_000_000)
    assert labels == {}


def test_finding_ids_present_in_suppressed_labels_remain_in_report(tmp_path: Path) -> None:
    """AC2: a suppressed finding is never removed from the report -- only re-labeled."""
    lineage_root = _lineage_root(tmp_path)
    anchored = anchor_suppress_decision(
        lineage_root=lineage_root,
        hmac_key=_KEY,
        finding_id="OBS-004",
        reason="Risk accepted",
        expiry="2099-12-31",
        timestamp=1_700_000_000,
    )

    suppressions = tuple(read_suppressions(lineage_root))
    report = _report([_finding("OBS-004", verdict="declared", passed=False)])

    labels = report.suppressed_findings(suppressions, now=1_800_000_000)
    assert "OBS-004" in labels
    assert labels["OBS-004"]["anchor"] == anchored.journal_entry_hash

    # The finding itself is still in the report -- never removed.
    assert report.finding_by_id("OBS-004") is not None
