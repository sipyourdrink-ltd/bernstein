"""Tests for the HIPAA compliance module.

Covers:
- PHI detection patterns (SSN, MRN, DOB, phone, email, ICD codes, etc.)
- PHIDetector health context filtering
- File access control / PHI path blocking
- AES-256-GCM encryption/decryption at rest
- BAA-ready compliance report generation
- HIPAAMode integration helper
- CompliancePreset.HIPAA integration with ComplianceConfig
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from bernstein.core.compliance import ComplianceConfig, CompliancePreset
from bernstein.core.hipaa import (
    HIPAAMode,
    PHICategory,
    PHIDetectionResult,
    PHIDetector,
    decrypt_file_aes256gcm,
    encrypt_file_aes256gcm,
    generate_hipaa_report,
    is_phi_file,
    load_or_create_hipaa_encryption_key,
    save_hipaa_report,
)

# ---------------------------------------------------------------------------
# PHIDetector — pattern matching
# ---------------------------------------------------------------------------


class TestPHIDetectorPatterns:
    def _detector(self) -> PHIDetector:
        return PHIDetector(require_health_context=False)

    def test_ssn_detected(self) -> None:
        result = self._detector().scan("Patient SSN: 123-45-6789")
        assert result.contains_phi
        cats = {f.category for f in result.findings}
        assert PHICategory.SSN in cats

    def test_ssn_000_not_detected(self) -> None:
        """SSNs starting with 000 are invalid and should not match."""
        result = self._detector().scan("Number: 000-45-6789")
        ssns = [f for f in result.findings if f.category == PHICategory.SSN]
        assert len(ssns) == 0

    def test_mrn_detected(self) -> None:
        result = self._detector().scan("MRN: 1234567")
        assert result.contains_phi
        cats = {f.category for f in result.findings}
        assert PHICategory.MRN in cats

    def test_email_detected(self) -> None:
        result = self._detector().scan("Contact: john.doe@hospital.org")
        assert result.contains_phi
        cats = {f.category for f in result.findings}
        assert PHICategory.EMAIL in cats

    def test_phone_detected(self) -> None:
        result = self._detector().scan("Call us at 555-867-5309")
        assert result.contains_phi
        cats = {f.category for f in result.findings}
        assert PHICategory.PHONE in cats

    def test_ip_address_detected(self) -> None:
        result = self._detector().scan("Server at 192.168.1.100")
        cats = {f.category for f in result.findings}
        assert PHICategory.IP_ADDRESS in cats

    def test_clean_text_no_phi(self) -> None:
        result = self._detector().scan("Deploy the new feature to production servers.")
        assert not result.contains_phi
        assert result.findings == []

    def test_redacted_text_replaces_phi(self) -> None:
        result = self._detector().scan("SSN: 123-45-6789")
        assert "123-45-6789" not in result.redacted_text
        assert "[REDACTED:" in result.redacted_text

    def test_multiple_phi_in_one_text(self) -> None:
        text = "Patient john@example.com, SSN 123-45-6789, phone 555-123-4567"
        result = self._detector().scan(text)
        assert result.contains_phi
        cats = {f.category for f in result.findings}
        assert PHICategory.EMAIL in cats
        assert PHICategory.SSN in cats
        assert PHICategory.PHONE in cats

    def test_findings_have_required_fields(self) -> None:
        result = self._detector().scan("SSN: 123-45-6789")
        for finding in result.findings:
            assert finding.category is not None
            assert finding.description
            assert finding.start >= 0
            assert finding.end > finding.start
            assert "[REDACTED" in finding.redacted
            assert isinstance(finding.context_window, str)

    def test_dob_detected(self) -> None:
        result = self._detector().scan("DOB: 01/15/1980")
        cats = {f.category for f in result.findings}
        assert PHICategory.DOB in cats

    def test_diagnosis_detected(self) -> None:
        result = self._detector().scan("Patient diagnosed with type 2 diabetes")
        cats = {f.category for f in result.findings}
        assert PHICategory.DIAGNOSIS in cats


class TestPHIDetectorHealthContext:
    def test_require_health_context_suppresses_false_positives(self) -> None:
        """With require_health_context=True, don't flag non-medical text."""
        detector = PHIDetector(require_health_context=True)
        # Phone-like pattern without health context
        result = detector.scan("Call 555-123-4567 for support")
        assert not result.contains_phi

    def test_require_health_context_allows_phi_in_medical_text(self) -> None:
        """With health context, still detect PHI."""
        detector = PHIDetector(require_health_context=True)
        result = detector.scan("Patient SSN: 123-45-6789 for insurance claim")
        assert result.contains_phi
        assert result.has_health_context

    def test_health_context_flag_set(self) -> None:
        detector = PHIDetector(require_health_context=False)
        result = detector.scan("Patient record for clinical review")
        assert result.has_health_context

    def test_no_health_context_flag_clean(self) -> None:
        detector = PHIDetector(require_health_context=False)
        result = detector.scan("Deploy the new microservice to production")
        assert not result.has_health_context


class TestPHIDetectionResult:
    def test_dataclass_immutable(self) -> None:
        result = PHIDetectionResult(
            contains_phi=False,
            findings=[],
            redacted_text="clean",
            has_health_context=False,
        )
        with pytest.raises((AttributeError, TypeError)):
            result.contains_phi = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# File access controls
# ---------------------------------------------------------------------------


class TestIsPhiFile:
    def test_phi_extension_blocked(self) -> None:
        assert is_phi_file("records.phi") is True

    def test_ehr_extension_blocked(self) -> None:
        assert is_phi_file("data.ehr") is True

    def test_patient_records_dir_blocked(self) -> None:
        assert is_phi_file("patient_records/2026/john_doe.json") is True

    def test_regular_file_allowed(self) -> None:
        assert is_phi_file("src/main.py") is False
        assert is_phi_file("README.md") is False
        assert is_phi_file("tests/unit/test_foo.py") is False

    def test_custom_patterns(self) -> None:
        assert is_phi_file("confidential/patient_data.csv", ["confidential/**"]) is True
        assert is_phi_file("data.csv", ["confidential/**"]) is False

    def test_ssn_in_filename_blocked(self) -> None:
        assert is_phi_file("export_ssn_2026.csv") is True

    def test_mrn_in_filename_blocked(self) -> None:
        assert is_phi_file("mrn_lookup.csv") is True


# ---------------------------------------------------------------------------
# AES-256-GCM encryption at rest
# ---------------------------------------------------------------------------


class TestEncryptionAtRest:
    def test_encrypt_decrypt_round_trip(self, tmp_path: Path) -> None:
        key = os.urandom(32)
        original = tmp_path / "state.json"
        original.write_text('{"tasks": []}')

        enc_path = encrypt_file_aes256gcm(original, key)
        assert enc_path.suffix == ".enc"
        assert not original.exists()

        dec_path = decrypt_file_aes256gcm(enc_path, key)
        assert dec_path == original
        assert dec_path.read_text() == '{"tasks": []}'

    def test_wrong_key_raises(self, tmp_path: Path) -> None:
        key = os.urandom(32)
        wrong_key = os.urandom(32)
        original = tmp_path / "data.json"
        original.write_text("sensitive")

        enc_path = encrypt_file_aes256gcm(original, key)

        from cryptography.exceptions import InvalidTag

        with pytest.raises(InvalidTag):
            decrypt_file_aes256gcm(enc_path, wrong_key)

    def test_invalid_key_length_raises(self, tmp_path: Path) -> None:
        bad_key = b"short"
        dummy = tmp_path / "x.json"
        dummy.write_text("x")
        with pytest.raises(ValueError, match="32 bytes"):
            encrypt_file_aes256gcm(dummy, bad_key)

    def test_load_or_create_key_generates_32_bytes(self, tmp_path: Path) -> None:
        key = load_or_create_hipaa_encryption_key(tmp_path)
        assert len(key) == 32
        assert (tmp_path / "config" / "hipaa-enc-key").exists()

    def test_load_or_create_key_idempotent(self, tmp_path: Path) -> None:
        key1 = load_or_create_hipaa_encryption_key(tmp_path)
        key2 = load_or_create_hipaa_encryption_key(tmp_path)
        assert key1 == key2

    def test_key_file_permissions(self, tmp_path: Path) -> None:
        load_or_create_hipaa_encryption_key(tmp_path)
        key_path = tmp_path / "config" / "hipaa-enc-key"
        stat = key_path.stat()
        # 0o600 = owner read/write only
        assert oct(stat.st_mode)[-3:] == "600"

    def test_ciphertext_differs_from_plaintext(self, tmp_path: Path) -> None:
        key = os.urandom(32)
        original = tmp_path / "plain.json"
        plaintext = '{"secret": "data"}'
        original.write_text(plaintext)

        enc_path = encrypt_file_aes256gcm(original, key)
        enc_bytes = enc_path.read_bytes()
        assert plaintext.encode() not in enc_bytes


# ---------------------------------------------------------------------------
# BAA-ready compliance report
# ---------------------------------------------------------------------------


class TestHIPAAComplianceReport:
    def test_generate_report_structure(self, tmp_path: Path) -> None:
        report = generate_hipaa_report(sdd_dir=tmp_path)
        assert report.generated_at
        assert isinstance(report.controls_active, dict)
        assert "phi_detection" in report.controls_active
        assert "encryption_at_rest" in report.controls_active

    def test_report_without_encryption_key_has_finding(self, tmp_path: Path) -> None:
        report = generate_hipaa_report(sdd_dir=tmp_path)
        assert report.encryption_at_rest is False
        assert any("HIPAA-OP-001" in f for f in report.findings)

    def test_report_with_encryption_key_no_enc_finding(self, tmp_path: Path) -> None:
        load_or_create_hipaa_encryption_key(tmp_path)
        report = generate_hipaa_report(sdd_dir=tmp_path)
        assert report.encryption_at_rest is True
        assert not any("HIPAA-OP-001" in f for f in report.findings)

    def test_report_phi_scan_summary(self, tmp_path: Path) -> None:
        events = [
            {"category": "ssn", "action": "detected"},
            {"category": "email", "action": "detected"},
            {"category": "file_access", "action": "blocked"},
        ]
        report = generate_hipaa_report(sdd_dir=tmp_path, phi_events_log=events)
        assert report.phi_scan_summary["ssn"] == 1
        assert report.phi_scan_summary["email"] == 1
        assert report.access_blocked_count == 1

    def test_report_to_dict(self, tmp_path: Path) -> None:
        report = generate_hipaa_report(sdd_dir=tmp_path, organization="Acme Health", baa_contact="ciso@acme.health")
        d = report.to_dict()
        assert d["report_type"] == "hipaa-compliance"
        assert d["organization"] == "Acme Health"
        assert d["baa_contact"] == "ciso@acme.health"
        assert isinstance(d["controls_active"], dict)

    def test_save_report_writes_json(self, tmp_path: Path) -> None:
        report = generate_hipaa_report(sdd_dir=tmp_path)
        path = save_hipaa_report(report, tmp_path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["report_type"] == "hipaa-compliance"

    def test_report_findings_list(self, tmp_path: Path) -> None:
        report = generate_hipaa_report(sdd_dir=tmp_path)
        assert isinstance(report.findings, list)


# ---------------------------------------------------------------------------
# HIPAAMode integration helper
# ---------------------------------------------------------------------------


class TestHIPAAMode:
    def test_scan_text_detects_phi(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path)
        result = mode.scan_text("SSN: 123-45-6789", source="test_input")
        assert result.contains_phi

    def test_scan_text_logs_events(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path)
        mode.scan_text("SSN: 123-45-6789", source="input")
        report = mode.generate_report()
        assert report.phi_scan_summary.get("ssn", 0) >= 1

    def test_check_file_access_allows_safe_file(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path)
        assert mode.check_file_access("src/main.py") is True

    def test_check_file_access_blocks_phi_file(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path)
        assert mode.check_file_access("patient_records/data.json") is False

    def test_check_file_access_logs_blocked_event(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path)
        mode.check_file_access("patient_records/data.json")
        report = mode.generate_report()
        assert report.access_blocked_count >= 1

    def test_custom_phi_patterns(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path, phi_file_patterns=["confidential/**"])
        assert mode.check_file_access("confidential/report.pdf") is False
        assert mode.check_file_access("docs/readme.md") is True

    def test_generate_report_organization(self, tmp_path: Path) -> None:
        mode = HIPAAMode(sdd_dir=tmp_path, organization="HealthCo", baa_contact="dpo@healthco.com")
        report = mode.generate_report()
        assert report.organization == "HealthCo"
        assert report.baa_contact == "dpo@healthco.com"


# ---------------------------------------------------------------------------
# CompliancePreset.HIPAA integration
# ---------------------------------------------------------------------------


class TestCompliancePresetHIPAA:
    def test_hipaa_preset_value(self) -> None:
        assert CompliancePreset.HIPAA.value == "hipaa"

    def test_from_preset_hipaa_enables_core_controls(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.HIPAA)
        assert cfg.hipaa_mode is True
        assert cfg.phi_detection is True
        assert cfg.encrypt_state_at_rest is True
        assert cfg.audit_logging is True
        assert cfg.audit_hmac_chain is True
        assert cfg.governed_workflow is True
        assert cfg.evidence_bundle is True

    def test_from_string_hipaa(self) -> None:
        cfg = ComplianceConfig.from_dict("hipaa")
        assert cfg.hipaa_mode is True
        assert cfg.phi_detection is True

    def test_hipaa_to_dict_includes_hipaa_fields(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.HIPAA)
        d = cfg.to_dict()
        assert d["hipaa_mode"] is True
        assert d["phi_detection"] is True
        assert d["encrypt_state_at_rest"] is True
        assert "phi_file_patterns" in d
        assert "baa_contact" in d

    def test_hipaa_check_prerequisites_passes(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.HIPAA)
        warnings = cfg.check_prerequisites()
        # Should have no warnings for the canonical preset
        assert warnings == []

    def test_hipaa_mode_without_phi_detection_warns(self) -> None:
        import dataclasses

        cfg = ComplianceConfig.from_preset(CompliancePreset.HIPAA)
        cfg = dataclasses.replace(cfg, phi_detection=False)
        warnings = cfg.check_prerequisites()
        assert any("phi_detection" in w for w in warnings)

    def test_from_dict_hipaa_with_baa_contact(self) -> None:
        cfg = ComplianceConfig.from_dict(
            {
                "preset": "hipaa",
                "baa_contact": "ciso@hospital.org",
            }
        )
        assert cfg.hipaa_mode is True
        assert cfg.baa_contact == "ciso@hospital.org"

    def test_phi_file_patterns_from_dict(self) -> None:
        cfg = ComplianceConfig.from_dict(
            {
                "preset": "hipaa",
                "phi_file_patterns": ["confidential/**", "*.ehr"],
            }
        )
        assert "confidential/**" in cfg.phi_file_patterns
        assert "*.ehr" in cfg.phi_file_patterns
