"""Tests for the DLP scanner (dlp_scanner.py).

Covers:
- License violation detection: SPDX identifiers, copyright headers,
  "all rights reserved" notices, GPL boilerplate
- Regulated data detection: NPI numbers, ICD-10 codes, MRNs, DEA numbers,
  health plan IDs, dates of birth
- Proprietary data detection: RFC-1918 addresses, internal hostnames,
  customer ID UUIDs, production DB connection patterns
- Allowlist / false-positive suppression
- Diff mode: only added lines are inspected
- Config flags: block_license_violations, block_regulated_data, etc.
- Module-level helpers: scan_text_for_dlp, scan_diff_for_dlp
- DLPScanResult.format_report
"""

from __future__ import annotations

from bernstein.core.dlp_scanner import (
    DLPConfig,
    DLPScanner,
    DLPScanResult,
    scan_diff_for_dlp,
    scan_text_for_dlp,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scanner(config: DLPConfig | None = None) -> DLPScanner:
    return DLPScanner(config or DLPConfig())


def _has_rule(result: DLPScanResult, rule: str) -> bool:
    return any(f.rule == rule for f in result.findings)


# ---------------------------------------------------------------------------
# License violation detection
# ---------------------------------------------------------------------------


class TestLicenseViolations:
    def test_spdx_identifier_detected(self) -> None:
        text = "// SPDX-License-Identifier: Apache-2.0"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "spdx_identifier")

    def test_spdx_identifier_gpl_detected(self) -> None:
        text = "# SPDX-License-Identifier: GPL-3.0-or-later"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "spdx_identifier")

    def test_copyright_header_detected(self) -> None:
        text = "# Copyright (c) 2023 Acme Corp"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "copyright_header")

    def test_copyright_year_range_detected(self) -> None:
        text = "// Copyright 2020-2024 ExternalProject authors"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "copyright_header")

    def test_all_rights_reserved_detected(self) -> None:
        text = "All rights reserved."
        result = _scanner().scan_text(text)
        assert _has_rule(result, "all_rights_reserved")

    def test_gpl_notice_detected(self) -> None:
        text = "This file is released under the GNU General Public License."
        result = _scanner().scan_text(text)
        assert _has_rule(result, "gpl_notice")

    def test_agpl_notice_detected(self) -> None:
        text = "Licensed under the GNU Affero General Public License, version 3."
        result = _scanner().scan_text(text)
        assert _has_rule(result, "gpl_notice")

    def test_license_violations_block_merge_by_default(self) -> None:
        text = "# SPDX-License-Identifier: MIT"
        result = _scanner().scan_text(text)
        assert result.has_blocks is True
        block_findings = [f for f in result.findings if f.block_merge]
        assert block_findings

    def test_license_violations_no_block_when_disabled(self) -> None:
        config = DLPConfig(block_license_violations=False)
        text = "# SPDX-License-Identifier: MIT"
        result = DLPScanner(config).scan_text(text)
        assert result.has_blocks is False

    def test_license_check_disabled(self) -> None:
        config = DLPConfig(check_license_violations=False)
        text = "# SPDX-License-Identifier: MIT"
        result = DLPScanner(config).scan_text(text)
        assert not _has_rule(result, "spdx_identifier")


# ---------------------------------------------------------------------------
# Regulated data (PHI) detection
# ---------------------------------------------------------------------------


class TestRegulatedData:
    def test_npi_number_detected(self) -> None:
        text = "npi = '1234567890'"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "npi_number")

    def test_npi_explicit_label_detected(self) -> None:
        text = 'national_provider_identifier: "9876543210"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "npi_number")

    def test_icd10_code_detected(self) -> None:
        text = 'diagnosis_code = "J18.9"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "icd10_code")

    def test_icd10_plain_label_detected(self) -> None:
        text = "icd_10: A01.0"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "icd10_code")

    def test_mrn_detected(self) -> None:
        text = 'mrn = "12345678"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "mrn")

    def test_patient_id_detected(self) -> None:
        text = "patient_id: 987654321"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "mrn")

    def test_dea_number_detected(self) -> None:
        text = 'dea_number = "AB1234567"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "dea_number")

    def test_health_plan_id_detected(self) -> None:
        text = 'member_id = "ABCD12345678"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "health_plan_id")

    def test_date_of_birth_detected(self) -> None:
        text = 'dob = "1985-06-15"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "date_of_birth")

    def test_regulated_data_blocks_merge_by_default(self) -> None:
        text = 'mrn = "12345678"'
        result = _scanner().scan_text(text)
        assert result.has_blocks is True

    def test_regulated_data_no_block_when_disabled(self) -> None:
        config = DLPConfig(block_regulated_data=False)
        text = 'mrn = "12345678"'
        result = DLPScanner(config).scan_text(text)
        assert result.has_blocks is False

    def test_regulated_data_check_disabled(self) -> None:
        config = DLPConfig(check_regulated_data=False)
        text = 'mrn = "12345678"'
        result = DLPScanner(config).scan_text(text)
        assert not _has_rule(result, "mrn")


# ---------------------------------------------------------------------------
# Proprietary data detection
# ---------------------------------------------------------------------------


class TestProprietaryData:
    def test_private_ip_10_detected(self) -> None:
        text = "host = 10.0.1.5"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "private_ip_address")

    def test_private_ip_172_detected(self) -> None:
        text = "endpoint = 172.16.0.1"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "private_ip_address")

    def test_private_ip_192_168_detected(self) -> None:
        text = "server = 192.168.0.100"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "private_ip_address")

    def test_public_ip_not_detected(self) -> None:
        text = "endpoint = 203.0.113.1"
        result = _scanner().scan_text(text)
        assert not _has_rule(result, "private_ip_address")

    def test_internal_hostname_detected(self) -> None:
        text = "db_host = mydb.internal"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "internal_hostname")

    def test_corp_hostname_detected(self) -> None:
        text = "api_url = https://api.mycompany.corp/v1"
        result = _scanner().scan_text(text)
        assert _has_rule(result, "internal_hostname")

    def test_customer_id_uuid_detected(self) -> None:
        text = 'customer_id = "550e8400-e29b-41d4-a716-446655440000"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "customer_id")

    def test_account_id_uuid_detected(self) -> None:
        text = 'account_id: "123e4567-e89b-12d3-a456-426614174000"'
        result = _scanner().scan_text(text)
        assert _has_rule(result, "customer_id")

    def test_proprietary_data_no_block_by_default(self) -> None:
        # Proprietary data findings should warn but NOT block by default.
        text = "host = 10.0.1.5"
        result = _scanner().scan_text(text)
        prop_findings = [f for f in result.findings if f.category == "proprietary_data"]
        assert prop_findings
        assert all(not f.block_merge for f in prop_findings)

    def test_proprietary_data_blocks_when_config_set(self) -> None:
        config = DLPConfig(block_proprietary_data=True)
        text = "host = 10.0.1.5"
        result = DLPScanner(config).scan_text(text)
        assert result.has_blocks is True

    def test_proprietary_check_disabled(self) -> None:
        config = DLPConfig(check_proprietary_data=False)
        text = "host = 10.0.1.5"
        result = DLPScanner(config).scan_text(text)
        assert not _has_rule(result, "private_ip_address")

    def test_custom_internal_url_pattern(self) -> None:
        config = DLPConfig(internal_url_patterns=["*.acmecorp.io"])
        text = "url = https://svc.acmecorp.io/api"
        result = DLPScanner(config).scan_text(text)
        assert any("custom_internal_url" in f.rule for f in result.findings)


# ---------------------------------------------------------------------------
# Allowlist / false-positive suppression
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_example_com_suppressed(self) -> None:
        text = "host = api.example.com"
        result = _scanner().scan_text(text)
        # internal_hostname should NOT trigger for example.com (allowlisted)
        assert not _has_rule(result, "internal_hostname")

    def test_localhost_suppressed(self) -> None:
        text = "db_host = localhost"
        result = _scanner().scan_text(text)
        # Should not trigger internal hostname
        assert not _has_rule(result, "internal_hostname")

    def test_fake_prefix_suppresses_customer_id(self) -> None:
        text = 'customer_id = "FAKE-550e8400-e29b-41d4-a716-446655440000"'
        result = _scanner().scan_text(text)
        assert not _has_rule(result, "customer_id")

    def test_test_prefix_suppresses_finding(self) -> None:
        text = 'mrn = "TEST12345678"'
        result = _scanner().scan_text(text)
        assert not _has_rule(result, "mrn")

    def test_custom_allowlist_pattern(self) -> None:
        config = DLPConfig(allowlist_patterns=[r"(?i)synthetic_data"])
        text = "# synthetic_data: mrn = 12345678"
        result = DLPScanner(config).scan_text(text)
        assert not _has_rule(result, "mrn")


# ---------------------------------------------------------------------------
# Diff mode scanning
# ---------------------------------------------------------------------------


class TestDiffMode:
    def test_only_added_lines_scanned(self) -> None:
        diff = """\
 context line
-removed: mrn = '12345678'
+added: npi = '1234567890'
"""
        result = _scanner().scan_diff(diff)
        # npi on added line should trigger, not mrn (removed)
        assert _has_rule(result, "npi_number")
        assert not _has_rule(result, "mrn")

    def test_diff_header_plus_plus_plus_skipped(self) -> None:
        diff = """\
+++ b/src/service.py
+npi = '1234567890'
"""
        result = _scanner().scan_diff(diff)
        assert _has_rule(result, "npi_number")

    def test_context_lines_not_scanned(self) -> None:
        diff = " npi = '1234567890'\n"
        result = _scanner().scan_diff(diff)
        assert not _has_rule(result, "npi_number")

    def test_no_findings_clean_diff(self) -> None:
        diff = """\
+def greet(name: str) -> str:
+    return f"Hello {name}"
"""
        result = _scanner().scan_diff(diff)
        assert not result.findings


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    def test_scan_text_for_dlp(self) -> None:
        result = scan_text_for_dlp('mrn = "12345678"')
        assert _has_rule(result, "mrn")

    def test_scan_diff_for_dlp(self) -> None:
        result = scan_diff_for_dlp('+npi = "1234567890"')
        assert _has_rule(result, "npi_number")

    def test_scan_text_for_dlp_with_config(self) -> None:
        config = DLPConfig(check_regulated_data=False)
        result = scan_text_for_dlp('mrn = "12345678"', config=config)
        assert not _has_rule(result, "mrn")


# ---------------------------------------------------------------------------
# DLPScanResult helpers
# ---------------------------------------------------------------------------


class TestDLPScanResult:
    def test_empty_result(self) -> None:
        result = DLPScanResult.empty()
        assert not result.findings
        assert not result.has_blocks
        assert not result.categories_hit

    def test_format_report_no_findings(self) -> None:
        result = DLPScanResult.empty()
        assert "no violations" in result.format_report()

    def test_format_report_with_findings(self) -> None:
        text = '# SPDX-License-Identifier: GPL-3.0\nnpi = "1234567890"'
        result = _scanner().scan_text(text)
        report = result.format_report()
        assert "BLOCK" in report or "WARN" in report
        assert "license_violation" in report or "regulated_data" in report

    def test_categories_hit_populated(self) -> None:
        text = '# SPDX-License-Identifier: MIT\nnpi = "1234567890"'
        result = _scanner().scan_text(text)
        assert "license_violation" in result.categories_hit
        assert "regulated_data" in result.categories_hit


# ---------------------------------------------------------------------------
# Global disable
# ---------------------------------------------------------------------------


class TestGlobalDisable:
    def test_disabled_scanner_returns_empty(self) -> None:
        config = DLPConfig(enabled=False)
        result = DLPScanner(config).scan_text('mrn = "12345678"')
        assert not result.findings
        assert not result.has_blocks
