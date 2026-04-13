"""Tests for the enhanced DLP scanner v2 (dlp_scanner_v2.py).

Covers:
- DLPCategory StrEnum membership
- Credit card detection with Luhn validation
- SSN pattern detection
- Health record identifiers (MRN, patient ID, health plan ID)
- Internal URL patterns (*.internal, *.corp)
- Customer ID patterns with configurable prefixes
- License header detection (SPDX, copyright, GPL, MIT, Apache)
- Credentials detection (API keys, passwords, tokens, private keys)
- PII detection (email addresses)
- Allowlist suppression
- Policy configuration (enabled_categories, block_on_critical, custom_patterns)
- scan_text, scan_file, scan_agent_output
- render_dlp_report
- DLPMatch / DLPScanResult frozen dataclass invariants
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from bernstein.core.security.dlp_scanner_v2 import (
    DLPCategory,
    DLPMatch,
    DLPPolicy,
    DLPScanResult,
    render_dlp_report,
    scan_agent_output,
    scan_file,
    scan_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_pattern(result: DLPScanResult, name: str) -> bool:
    return any(m.pattern_name == name for m in result.matches)


# ---------------------------------------------------------------------------
# DLPCategory StrEnum
# ---------------------------------------------------------------------------


class TestDLPCategory:
    def test_all_members_present(self) -> None:
        names = {c.value for c in DLPCategory}
        assert names == {
            "source_code",
            "proprietary_data",
            "regulated_data",
            "credentials",
            "pii",
        }

    def test_strenum_is_string(self) -> None:
        assert isinstance(DLPCategory.PII, str)
        assert DLPCategory.PII == "pii"


# ---------------------------------------------------------------------------
# Credit card detection (Luhn-validated)
# ---------------------------------------------------------------------------


class TestCreditCard:
    def test_valid_visa_detected(self) -> None:
        # 4111111111111111 passes Luhn
        result = scan_text("card: 4111111111111111")
        assert _has_pattern(result, "credit_card")

    def test_valid_visa_with_dashes_detected(self) -> None:
        result = scan_text("cc: 4111-1111-1111-1111")
        assert _has_pattern(result, "credit_card")

    def test_valid_visa_with_spaces_detected(self) -> None:
        result = scan_text("pan: 4111 1111 1111 1111")
        assert _has_pattern(result, "credit_card")

    def test_invalid_luhn_not_detected(self) -> None:
        # 4111111111111112 does NOT pass Luhn
        result = scan_text("card: 4111111111111112")
        assert not _has_pattern(result, "credit_card")

    def test_credit_card_severity_is_critical(self) -> None:
        result = scan_text("card: 4111111111111111")
        cc_matches = [m for m in result.matches if m.pattern_name == "credit_card"]
        assert cc_matches
        assert cc_matches[0].severity == "critical"

    def test_credit_card_blocks_by_default(self) -> None:
        result = scan_text("cc 4111111111111111")
        assert result.blocked is True


# ---------------------------------------------------------------------------
# SSN detection
# ---------------------------------------------------------------------------


class TestSSN:
    def test_ssn_pattern_detected(self) -> None:
        result = scan_text("ssn: 123-45-6789")
        assert _has_pattern(result, "ssn")

    def test_ssn_severity_is_critical(self) -> None:
        result = scan_text("ssn: 123-45-6789")
        ssn_matches = [m for m in result.matches if m.pattern_name == "ssn"]
        assert ssn_matches
        assert ssn_matches[0].severity == "critical"

    def test_ssn_not_triggered_by_phone(self) -> None:
        # Phone numbers like 555-123-4567 should not trigger (wrong format)
        result = scan_text("phone: 555-123-4567")
        assert not _has_pattern(result, "ssn")


# ---------------------------------------------------------------------------
# Health record identifiers
# ---------------------------------------------------------------------------


class TestHealthRecords:
    def test_mrn_detected(self) -> None:
        result = scan_text('mrn = "12345678"')
        assert _has_pattern(result, "mrn")

    def test_medical_record_number_detected(self) -> None:
        result = scan_text('medical_record_number: "MRN001234"')
        assert _has_pattern(result, "mrn")

    def test_patient_id_detected(self) -> None:
        result = scan_text("patient_id: 987654321")
        assert _has_pattern(result, "patient_id")

    def test_health_plan_id_detected(self) -> None:
        result = scan_text('member_id = "ABCD12345678"')
        assert _has_pattern(result, "health_plan_id")

    def test_insurance_id_detected(self) -> None:
        result = scan_text('insurance_id: "INS987654"')
        assert _has_pattern(result, "health_plan_id")


# ---------------------------------------------------------------------------
# Internal URL patterns
# ---------------------------------------------------------------------------


class TestInternalURLs:
    def test_internal_suffix_detected(self) -> None:
        result = scan_text("host = db.internal")
        assert _has_pattern(result, "internal_url_internal")

    def test_corp_suffix_detected(self) -> None:
        result = scan_text("url = https://api.mycompany.corp/v1")
        assert _has_pattern(result, "internal_url_corp")

    def test_custom_suffix_detected(self) -> None:
        policy = DLPPolicy(internal_url_suffixes=(".intranet",))
        result = scan_text("server = portal.intranet", policy)
        assert _has_pattern(result, "internal_url_intranet")

    def test_public_url_not_detected(self) -> None:
        result = scan_text("url = https://api.github.com/v3")
        assert not _has_pattern(result, "internal_url_internal")
        assert not _has_pattern(result, "internal_url_corp")


# ---------------------------------------------------------------------------
# Customer ID patterns
# ---------------------------------------------------------------------------


class TestCustomerID:
    def test_cust_prefix_detected(self) -> None:
        result = scan_text("ref: CUST-abc12345")
        assert _has_pattern(result, "customer_id")

    def test_org_prefix_detected(self) -> None:
        result = scan_text("org: ORG-789xyz")
        assert _has_pattern(result, "customer_id")

    def test_acct_prefix_detected(self) -> None:
        result = scan_text("account: ACCT-ABCDE123")
        assert _has_pattern(result, "customer_id")

    def test_custom_prefix_detected(self) -> None:
        policy = DLPPolicy(customer_id_prefixes=("TENANT",))
        result = scan_text("id: TENANT-9876abcd", policy)
        assert _has_pattern(result, "customer_id")

    def test_no_prefixes_disables_detection(self) -> None:
        policy = DLPPolicy(customer_id_prefixes=())
        result = scan_text("ref: CUST-abc12345", policy)
        assert not _has_pattern(result, "customer_id")


# ---------------------------------------------------------------------------
# License header detection
# ---------------------------------------------------------------------------


class TestLicenseHeaders:
    def test_spdx_detected(self) -> None:
        result = scan_text("// SPDX-License-Identifier: Apache-2.0")
        assert _has_pattern(result, "spdx_license")

    def test_copyright_header_detected(self) -> None:
        result = scan_text("# Copyright (c) 2023 Acme Corp")
        assert _has_pattern(result, "copyright_header")

    def test_all_rights_reserved_detected(self) -> None:
        result = scan_text("All rights reserved.")
        assert _has_pattern(result, "all_rights_reserved")

    def test_gpl_text_detected(self) -> None:
        result = scan_text("Under the GNU General Public License v3")
        assert _has_pattern(result, "gpl_license_text")

    def test_mit_boilerplate_detected(self) -> None:
        result = scan_text("Permission is hereby granted, free of charge")
        assert _has_pattern(result, "mit_license_block")

    def test_apache_boilerplate_detected(self) -> None:
        result = scan_text("Licensed under the Apache License, Version 2.0")
        assert _has_pattern(result, "apache_license_block")


# ---------------------------------------------------------------------------
# Credentials detection
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_api_key_detected(self) -> None:
        result = scan_text('api_key = "sk_live_abcdef1234567890abcd"')
        assert _has_pattern(result, "api_key")

    def test_password_assignment_detected(self) -> None:
        result = scan_text("password = 'hunter2secret'")
        assert _has_pattern(result, "password_assignment")

    def test_private_key_detected(self) -> None:
        result = scan_text("-----BEGIN RSA PRIVATE KEY-----")
        assert _has_pattern(result, "private_key_block")

    def test_bearer_token_detected(self) -> None:
        result = scan_text('auth_token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"')
        assert _has_pattern(result, "bearer_token")


# ---------------------------------------------------------------------------
# Allowlist suppression
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_example_com_suppressed(self) -> None:
        result = scan_text("host = api.example.com")
        assert not _has_pattern(result, "internal_url_internal")

    def test_localhost_suppressed(self) -> None:
        result = scan_text("db = localhost")
        assert not result.matches

    def test_fake_prefix_suppressed(self) -> None:
        result = scan_text("FAKE ref: CUST-abc12345")
        assert not _has_pattern(result, "customer_id")

    def test_test_prefix_suppressed(self) -> None:
        result = scan_text('TEST mrn = "12345678"')
        assert not _has_pattern(result, "mrn")


# ---------------------------------------------------------------------------
# Policy configuration
# ---------------------------------------------------------------------------


class TestPolicyConfig:
    def test_disabled_category_skipped(self) -> None:
        policy = DLPPolicy(enabled_categories=frozenset({DLPCategory.PII}))
        result = scan_text("card: 4111111111111111", policy)
        assert not _has_pattern(result, "credit_card")

    def test_block_on_critical_false(self) -> None:
        policy = DLPPolicy(block_on_critical=False)
        result = scan_text("card: 4111111111111111", policy)
        assert result.blocked is False

    def test_custom_patterns_applied(self) -> None:
        policy = DLPPolicy(custom_patterns=(("secret_project", r"PROJECT-ALPHA-\d+"),))
        result = scan_text("ref: PROJECT-ALPHA-42", policy)
        assert _has_pattern(result, "custom_secret_project")

    def test_invalid_custom_pattern_ignored(self) -> None:
        policy = DLPPolicy(custom_patterns=(("bad", r"[invalid"),))
        # Should not raise
        result = scan_text("some text", policy)
        assert result is not None


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------


class TestScanFile:
    def test_scan_file_with_violations(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('ssn = "123-45-6789"\n')
            f.flush()
            result = scan_file(f.name)
        assert _has_pattern(result, "ssn")
        assert result.file_path == f.name

    def test_scan_file_nonexistent(self) -> None:
        result = scan_file("/nonexistent/path/file.txt")
        assert not result.matches
        assert result.blocked is False

    def test_scan_file_records_time(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("hello world\n")
            f.flush()
            result = scan_file(f.name)
        assert result.scan_time_ms >= 0


# ---------------------------------------------------------------------------
# scan_agent_output
# ---------------------------------------------------------------------------


class TestScanAgentOutput:
    def test_scans_directory_files(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.py").write_text('ssn = "123-45-6789"\n')
            (Path(d) / "b.py").write_text("clean code\n")
            results = scan_agent_output(d)
        assert len(results) == 2
        has_ssn = any(_has_pattern(r, "ssn") for r in results)
        assert has_ssn

    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            results = scan_agent_output(d)
        assert results == []

    def test_nonexistent_directory(self) -> None:
        results = scan_agent_output("/nonexistent/dir")
        assert results == []

    def test_recursive_scan(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            (sub / "deep.py").write_text('mrn = "12345678"\n')
            results = scan_agent_output(d)
        assert len(results) == 1
        assert _has_pattern(results[0], "mrn")


# ---------------------------------------------------------------------------
# render_dlp_report
# ---------------------------------------------------------------------------


class TestRenderReport:
    def test_no_findings_report(self) -> None:
        report = render_dlp_report([])
        assert "No violations detected" in report

    def test_report_with_findings(self) -> None:
        result = scan_text("ssn: 123-45-6789")
        report = render_dlp_report([result])
        assert "# DLP Scan Report" in report
        assert "CRITICAL" in report
        assert "ssn" in report

    def test_severity_grouped(self) -> None:
        text = "// SPDX-License-Identifier: MIT\nssn: 123-45-6789"
        result = scan_text(text)
        report = render_dlp_report([result])
        # CRITICAL should appear before HIGH
        crit_pos = report.find("CRITICAL")
        high_pos = report.find("HIGH")
        assert crit_pos < high_pos

    def test_report_includes_file_path(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('ssn = "123-45-6789"\n')
            f.flush()
            result = scan_file(f.name)
        report = render_dlp_report([result])
        assert f.name in report

    def test_report_blocked_count(self) -> None:
        result = scan_text("ssn: 123-45-6789")
        report = render_dlp_report([result])
        assert "Files blocked" in report


# ---------------------------------------------------------------------------
# Frozen dataclass invariants
# ---------------------------------------------------------------------------


class TestFrozenDataclasses:
    def test_dlp_match_frozen(self) -> None:
        match = DLPMatch(
            category=DLPCategory.PII,
            pattern_name="test",
            matched_text="xxx",
            line_number=1,
            confidence=0.9,
            severity="high",
        )
        try:
            match.line_number = 2  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised

    def test_dlp_scan_result_frozen(self) -> None:
        result = DLPScanResult(
            file_path="",
            matches=(),
            blocked=False,
            scan_time_ms=0.0,
        )
        try:
            result.blocked = True  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised

    def test_dlp_policy_frozen(self) -> None:
        policy = DLPPolicy()
        try:
            policy.block_on_critical = False  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_text_no_findings(self) -> None:
        result = scan_text("")
        assert not result.matches
        assert result.blocked is False

    def test_scan_time_recorded(self) -> None:
        result = scan_text("hello world")
        assert result.scan_time_ms >= 0

    def test_matched_text_truncated(self) -> None:
        long_key = "a" * 100
        result = scan_text(f'api_key = "{long_key}"')
        for m in result.matches:
            assert len(m.matched_text) <= 40

    def test_confidence_in_range(self) -> None:
        result = scan_text("ssn: 123-45-6789\ncard: 4111111111111111")
        for m in result.matches:
            assert 0.0 <= m.confidence <= 1.0

    def test_line_number_accurate(self) -> None:
        text = "line1\nline2\nssn: 123-45-6789"
        result = scan_text(text)
        ssn_matches = [m for m in result.matches if m.pattern_name == "ssn"]
        assert ssn_matches
        assert ssn_matches[0].line_number == 3
