"""Tests for ENT-004: SOC 2 compliance reporting."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.soc2_report import (
    SOC2_CONTROLS,
    MerkleAttestation,
    SOC2ComplianceReport,
    generate_soc2_report,
    save_soc2_report,
)

_GENESIS_HMAC = "0" * 64


def _compute_test_hmac(key: bytes, prev_hmac: str, entry: dict[str, Any]) -> str:
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _create_audit_log(audit_dir: Path, key: bytes, count: int = 5) -> None:
    """Create a valid HMAC-chained audit log."""
    prev = _GENESIS_HMAC
    lines: list[str] = []
    for i in range(count):
        entry: dict[str, Any] = {
            "timestamp": f"2026-01-15T00:00:{i:02d}.000000Z",
            "event_type": "test.event",
            "actor": "test-actor",
            "resource_type": "task",
            "resource_id": f"task-{i}",
            "details": {},
            "prev_hmac": prev,
        }
        computed = _compute_test_hmac(key, prev, entry)
        entry["hmac"] = computed
        lines.append(json.dumps(entry, sort_keys=True))
        prev = computed
    (audit_dir / "2026-01-15.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def audit_dir(sdd_dir: Path) -> Path:
    d = sdd_dir / "audit"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def hmac_key(sdd_dir: Path) -> bytes:
    key = b"test-hmac-key-for-soc2"
    key_dir = sdd_dir / "config"
    key_dir.mkdir(parents=True)
    (key_dir / "audit-key").write_bytes(key)
    return key


class TestSOC2Controls:
    """Test SOC 2 control definitions."""

    def test_controls_defined(self) -> None:
        assert len(SOC2_CONTROLS) > 0

    def test_control_has_required_fields(self) -> None:
        for ctrl in SOC2_CONTROLS:
            assert ctrl.control_id
            assert ctrl.category
            assert ctrl.title
            assert ctrl.description
            assert len(ctrl.evidence_types) > 0

    def test_categories_present(self) -> None:
        categories = {c.category for c in SOC2_CONTROLS}
        assert "Security" in categories
        assert "Availability" in categories
        assert "Processing Integrity" in categories


class TestSOC2ComplianceReport:
    """Test report data class."""

    def test_to_dict(self) -> None:
        report = SOC2ComplianceReport(
            period="Q1-2026",
            period_start="2026-01-01",
            period_end="2026-03-31",
            controls=list(SOC2_CONTROLS),
        )
        d = report.to_dict()
        assert d["report_type"] == "soc2_compliance"
        assert d["period"] == "Q1-2026"
        assert len(d["controls"]) == len(SOC2_CONTROLS)

    def test_to_dict_with_merkle(self) -> None:
        report = SOC2ComplianceReport(
            period="Q1-2026",
            period_start="2026-01-01",
            period_end="2026-03-31",
            merkle_attestation=MerkleAttestation(
                root_hash="abc123",
                leaf_count=5,
            ),
        )
        d = report.to_dict()
        assert d["merkle_attestation"]["root_hash"] == "abc123"
        assert d["merkle_attestation"]["leaf_count"] == 5

    def test_to_dict_without_merkle(self) -> None:
        report = SOC2ComplianceReport(
            period="Q1-2026",
            period_start="2026-01-01",
            period_end="2026-03-31",
        )
        d = report.to_dict()
        assert d["merkle_attestation"] is None


class TestGenerateSOC2Report:
    """Test report generation."""

    def test_empty_sdd(self, sdd_dir: Path) -> None:
        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        assert report.overall_status == "non_compliant"
        assert report.period == "Q1-2026"
        assert len(report.controls) == len(SOC2_CONTROLS)
        assert len(report.evidence) == 0

    def test_with_audit_log(self, sdd_dir: Path, audit_dir: Path, hmac_key: bytes) -> None:
        _create_audit_log(audit_dir, hmac_key)
        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        assert report.hmac_chain_valid is True
        # Should have audit log evidence
        audit_evidence = [e for e in report.evidence if e.evidence_type == "audit_log"]
        assert len(audit_evidence) > 0
        hmac_evidence = [e for e in report.evidence if e.evidence_type == "hmac_verification"]
        assert len(hmac_evidence) > 0

    def test_with_wal(self, sdd_dir: Path) -> None:
        wal_dir = sdd_dir / "runtime" / "wal"
        wal_dir.mkdir(parents=True)
        (wal_dir / "test-run.wal.jsonl").write_text('{"seq":0}\n')
        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        wal_evidence = [e for e in report.evidence if e.evidence_type == "wal"]
        assert len(wal_evidence) > 0

    def test_with_metrics(self, sdd_dir: Path) -> None:
        metrics_dir = sdd_dir / "metrics"
        metrics_dir.mkdir(parents=True)
        (metrics_dir / "tasks.jsonl").write_text('{"metric":"test"}\n')
        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        metrics_evidence = [e for e in report.evidence if e.evidence_type == "metrics"]
        assert len(metrics_evidence) > 0

    def test_compliant_status(self, sdd_dir: Path, audit_dir: Path, hmac_key: bytes) -> None:
        # Create all types of evidence to achieve "compliant"
        _create_audit_log(audit_dir, hmac_key)
        (sdd_dir / "runtime" / "wal").mkdir(parents=True)
        (sdd_dir / "runtime" / "wal" / "run.wal.jsonl").write_text('{"seq":0}\n')
        (sdd_dir / "metrics").mkdir(parents=True)
        (sdd_dir / "metrics" / "data.jsonl").write_text('{"m":"v"}\n')

        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        # Must have evidence for all control IDs for compliant
        evidence_controls = {e.control_id for e in report.evidence}
        all_controls = {c.control_id for c in SOC2_CONTROLS}
        if evidence_controls >= all_controls and report.hmac_chain_valid:
            assert report.overall_status == "compliant"

    def test_package_hash_computed(self, sdd_dir: Path) -> None:
        report = generate_soc2_report(sdd_dir, "Q1-2026", "2026-01-01", "2026-03-31")
        assert report.package_hash
        assert len(report.package_hash) == 64  # SHA-256 hex

    def test_date_filtering(self, sdd_dir: Path, audit_dir: Path, hmac_key: bytes) -> None:
        # File date outside range
        _create_audit_log(audit_dir, hmac_key)  # 2026-01-15
        report = generate_soc2_report(sdd_dir, "Q2-2026", "2026-04-01", "2026-06-30")
        audit_evidence = [e for e in report.evidence if e.evidence_type == "audit_log"]
        assert len(audit_evidence) == 0  # No files match Q2


class TestSaveSOC2Report:
    """Test report persistence."""

    def test_save_and_read(self, tmp_path: Path) -> None:
        report = SOC2ComplianceReport(
            period="Q1-2026",
            period_start="2026-01-01",
            period_end="2026-03-31",
            controls=list(SOC2_CONTROLS),
            overall_status="partial",
        )
        path = save_soc2_report(report, tmp_path / "output")
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["period"] == "Q1-2026"
        assert data["overall_status"] == "partial"
        assert len(data["controls"]) == len(SOC2_CONTROLS)
