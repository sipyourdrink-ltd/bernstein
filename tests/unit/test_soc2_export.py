"""Tests for SOC 2 evidence export package.

Tests cover:
- Period parsing (quarters, months, years, invalid input)
- Evidence package assembly (audit logs, config, WAL, SBOM, Merkle seals)
- Period-based audit log filtering
- Zip and directory output formats
- Manifest generation with checksums
- HMAC chain verification inclusion
- Handling of missing artifacts gracefully
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from bernstein.core.compliance import export_soc2_package, parse_period

# ---------------------------------------------------------------------------
# parse_period
# ---------------------------------------------------------------------------


class TestParsePeriod:
    def test_q1(self) -> None:
        start, end = parse_period("Q1-2026")
        assert start == "2026-01-01"
        assert end == "2026-03-31"

    def test_q2(self) -> None:
        start, end = parse_period("Q2-2026")
        assert start == "2026-04-01"
        assert end == "2026-06-30"

    def test_q3(self) -> None:
        start, end = parse_period("Q3-2026")
        assert start == "2026-07-01"
        assert end == "2026-09-30"

    def test_q4(self) -> None:
        start, end = parse_period("Q4-2026")
        assert start == "2026-10-01"
        assert end == "2026-12-31"

    def test_quarter_case_insensitive(self) -> None:
        start, end = parse_period("q1-2026")
        assert start == "2026-01-01"
        assert end == "2026-03-31"

    def test_month(self) -> None:
        start, end = parse_period("2026-03")
        assert start == "2026-03-01"
        assert end == "2026-03-31"

    def test_month_february_non_leap(self) -> None:
        start, end = parse_period("2025-02")
        assert start == "2025-02-01"
        assert end == "2025-02-28"

    def test_month_february_leap(self) -> None:
        start, end = parse_period("2024-02")
        assert start == "2024-02-01"
        assert end == "2024-02-29"

    def test_year(self) -> None:
        start, end = parse_period("2026")
        assert start == "2026-01-01"
        assert end == "2026-12-31"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse period"):
            parse_period("not-a-period")

    def test_invalid_quarter_number(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse period"):
            parse_period("Q5-2026")

    def test_invalid_month_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid month"):
            parse_period("2026-13")


# ---------------------------------------------------------------------------
# Helpers to build a fake .sdd directory
# ---------------------------------------------------------------------------


def _make_sdd(tmp_path: Path) -> Path:
    """Create a minimal .sdd directory with audit data for Q1-2026."""
    sdd = tmp_path / ".sdd"

    # Audit logs — one in range, one out of range
    audit = sdd / "audit"
    audit.mkdir(parents=True)
    (audit / "2026-01-15.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-01-15T10:00:00.000000Z",
                "event_type": "task.created",
                "actor": "orchestrator",
                "resource_type": "task",
                "resource_id": "t-001",
                "details": {},
                "prev_hmac": "0" * 64,
                "hmac": "a" * 64,
            }
        )
        + "\n"
    )
    (audit / "2026-04-01.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-01T10:00:00.000000Z",
                "event_type": "task.created",
                "actor": "orchestrator",
                "resource_type": "task",
                "resource_id": "t-002",
                "details": {},
                "prev_hmac": "a" * 64,
                "hmac": "b" * 64,
            }
        )
        + "\n"
    )

    # Merkle seals
    merkle = audit / "merkle"
    merkle.mkdir()
    (merkle / "seal-2026-01-31.json").write_text(
        json.dumps({"root_hash": "abc123", "sealed_at_iso": "2026-01-31T23:59:59Z"})
    )

    # Compliance config (not the audit-key — that should be excluded)
    config = sdd / "config"
    config.mkdir(parents=True)
    (config / "compliance.json").write_text(json.dumps({"preset": "standard"}))
    (config / "policies.yaml").write_text("policies: []\n")
    (config / "audit-key").write_text("secret-key-should-not-be-exported")

    # WAL
    wal = sdd / "runtime" / "wal"
    wal.mkdir(parents=True)
    (wal / "run-001.wal.jsonl").write_text('{"seq": 0}\n')

    # SBOM
    sbom = sdd / "sbom"
    sbom.mkdir(parents=True)
    (sbom / "sbom-run-001.cdx.json").write_text(json.dumps({"bomFormat": "CycloneDX", "components": []}))

    return sdd


# ---------------------------------------------------------------------------
# export_soc2_package — directory format
# ---------------------------------------------------------------------------


class TestExportDir:
    def test_creates_bundle_directory(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        assert result.is_dir()
        assert result.name == "soc2-Q1-2026"

    def test_manifest_has_required_fields(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        manifest = json.loads((result / "manifest.json").read_text())
        assert manifest["package_type"] == "soc2-evidence"
        assert manifest["period"] == "Q1-2026"
        assert manifest["period_start"] == "2026-01-01"
        assert manifest["period_end"] == "2026-03-31"
        assert "exported_at" in manifest
        assert "artifacts" in manifest
        assert "verification" in manifest
        assert "file_checksums" in manifest

    def test_filters_audit_logs_by_period(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        audit_logs = list((result / "audit_logs").iterdir())
        names = [f.name for f in audit_logs]
        assert "2026-01-15.jsonl" in names
        assert "2026-04-01.jsonl" not in names

    def test_includes_merkle_seals(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        seals = list((result / "merkle_seals").iterdir())
        assert len(seals) == 1
        assert seals[0].name == "seal-2026-01-31.json"

    def test_includes_compliance_config(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        config_dir = result / "compliance_config"
        names = [f.name for f in config_dir.iterdir()]
        assert "compliance.json" in names
        assert "policies.yaml" in names

    def test_excludes_audit_key(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        config_dir = result / "compliance_config"
        names = [f.name for f in config_dir.iterdir()]
        assert "audit-key" not in names

    def test_includes_wal(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        wal_files = list((result / "wal").iterdir())
        assert len(wal_files) == 1

    def test_includes_sbom(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        sbom_files = list((result / "sbom").iterdir())
        assert len(sbom_files) == 1

    def test_includes_verification_results(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        verification = json.loads((result / "verification.json").read_text())
        assert "hmac_chain" in verification

    def test_file_checksums_in_manifest(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        manifest = json.loads((result / "manifest.json").read_text())
        checksums = manifest["file_checksums"]
        assert len(checksums) > 0
        # All checksums should be 64-char hex strings (SHA-256)
        for path, checksum in checksums.items():
            assert len(checksum) == 64, f"Bad checksum for {path}"

    def test_custom_output_path(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        custom_out = tmp_path / "custom_output"
        result = export_soc2_package(sdd, "Q1-2026", output_path=custom_out, fmt="dir")
        assert result.parent == custom_out
        assert result.is_dir()


# ---------------------------------------------------------------------------
# export_soc2_package — zip format
# ---------------------------------------------------------------------------


class TestExportZip:
    def test_creates_zip_file(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="zip")
        assert result.suffix == ".zip"
        assert result.exists()

    def test_zip_contains_manifest(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="zip")
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert any("manifest.json" in n for n in names)

    def test_zip_contains_audit_logs(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="zip")
        with zipfile.ZipFile(result) as zf:
            names = zf.namelist()
            assert any("2026-01-15.jsonl" in n for n in names)
            assert not any("2026-04-01.jsonl" in n for n in names)

    def test_zip_removes_temp_directory(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "Q1-2026", fmt="zip")
        bundle_dir = result.parent / "soc2-Q1-2026"
        assert not bundle_dir.exists(), "Temp directory should be cleaned up"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_sdd_produces_minimal_package(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        result = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        assert result.is_dir()
        manifest = json.loads((result / "manifest.json").read_text())
        assert manifest["artifacts"] == []

    def test_overwrites_existing_bundle(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result1 = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        assert result1.is_dir()
        result2 = export_soc2_package(sdd, "Q1-2026", fmt="dir")
        assert result2.is_dir()
        assert result1 == result2

    def test_month_period_filters_correctly(self, tmp_path: Path) -> None:
        sdd = _make_sdd(tmp_path)
        result = export_soc2_package(sdd, "2026-01", fmt="dir")
        audit_dir = result / "audit_logs"
        if audit_dir.exists():
            names = [f.name for f in audit_dir.iterdir()]
            assert "2026-01-15.jsonl" in names
            assert "2026-04-01.jsonl" not in names
