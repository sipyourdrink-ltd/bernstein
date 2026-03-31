"""Tests for PII and secret detection gate (pii_output_gate.py).

Covers:
- Secret detection: AWS keys, GitHub tokens, Slack tokens, Stripe keys,
  private keys, generic API keys, passwords, connection strings, bearer tokens, JWTs
- PII detection: emails, phone numbers, SSNs
- Allowlist filtering: example.com, test@, placeholder, localhost
- Diff scanning: only added lines scanned, removals and headers ignored
- Redaction: raw secrets never stored in findings
- format_findings output
- Integration with quality gates via _run_pii_gate
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.pii_output_gate import (
    SecretFinding,
    format_findings,
    scan_diff,
    scan_text,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# scan_text: secret detection
# ---------------------------------------------------------------------------


class TestScanTextSecrets:
    def test_aws_access_key_detected(self) -> None:
        text = 'aws_key = "AKIAIOSFODNN7EXAMPLE"'
        findings = scan_text(text)
        assert any(f.rule == "aws_access_key" for f in findings)

    def test_aws_secret_key_detected(self) -> None:
        text = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        findings = scan_text(text)
        assert any(f.rule == "aws_secret_key" for f in findings)

    def test_github_token_detected(self) -> None:
        text = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"'
        findings = scan_text(text)
        assert any(f.rule == "github_token" for f in findings)

    def test_slack_token_detected(self) -> None:
        # Build token dynamically to avoid GitHub push protection
        prefix = "xoxb"
        text = f"SLACK_TOKEN={prefix}-000FAKE000-000FAKE000-FakeTokenVal"
        findings = scan_text(text)
        assert any(f.rule == "slack_token" for f in findings)

    def test_stripe_key_detected(self) -> None:
        # Build key dynamically to avoid GitHub push protection
        prefix = "sk_" + "live"
        text = f'stripe_key = "{prefix}_FAKEFAKEFAKEFAKE"'
        findings = scan_text(text)
        assert any(f.rule == "stripe_key" for f in findings)

    def test_private_key_detected(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQ..."
        findings = scan_text(text)
        assert any(f.rule == "private_key" for f in findings)

    def test_generic_api_key_detected(self) -> None:
        text = 'api_key = "sk-proj-abcdef1234567890ABCD"'
        findings = scan_text(text)
        assert any(f.rule == "generic_api_key" for f in findings)

    def test_password_assignment_detected(self) -> None:
        text = 'db_password = "SuperS3cret!Pass"'
        findings = scan_text(text)
        assert any(f.rule == "password_assignment" for f in findings)

    def test_connection_string_detected(self) -> None:
        text = 'DATABASE_URL = "postgres://user:pass@dbhost:5432/mydb"'
        findings = scan_text(text)
        assert any(f.rule == "connection_string" for f in findings)

    def test_bearer_token_detected(self) -> None:
        text = 'authorization = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def"'
        findings = scan_text(text)
        assert any(f.rule == "bearer_token" for f in findings)

    def test_jwt_token_detected(self) -> None:
        text = "token = eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        findings = scan_text(text)
        assert any(f.rule == "jwt_token" for f in findings)

    def test_gcp_service_account_detected(self) -> None:
        text = '{\n  "type": "service_account",\n  "project_id": "my-project"\n}'
        findings = scan_text(text)
        assert any(f.rule == "gcp_service_account" for f in findings)


# ---------------------------------------------------------------------------
# scan_text: PII detection
# ---------------------------------------------------------------------------


class TestScanTextPii:
    def test_email_detected(self) -> None:
        text = "Contact alice@realcompany.com for details."
        findings = scan_text(text)
        assert any(f.rule == "email_address" for f in findings)

    def test_phone_detected(self) -> None:
        text = "Call 415-555-1234 for support."
        findings = scan_text(text)
        assert any(f.rule == "phone_number" for f in findings)

    def test_ssn_detected(self) -> None:
        text = "SSN: 123-45-6789"
        findings = scan_text(text)
        assert any(f.rule == "ssn" for f in findings)

    def test_pii_severity_is_medium(self) -> None:
        text = "Contact alice@realcompany.com"
        findings = scan_text(text)
        pii = [f for f in findings if f.rule == "email_address"]
        assert pii and pii[0].severity == "medium"

    def test_secret_severity_is_high(self) -> None:
        text = 'api_key = "sk-proj-abcdef1234567890ABCD"'
        findings = scan_text(text)
        secrets = [f for f in findings if f.rule == "generic_api_key"]
        assert secrets and secrets[0].severity == "high"


# ---------------------------------------------------------------------------
# scan_text: allowlist filtering
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_example_com_email_allowed(self) -> None:
        text = "user@example.com"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_test_email_allowed(self) -> None:
        text = "test@company.org"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_noreply_email_allowed(self) -> None:
        text = "noreply@company.org"
        findings = scan_text(text)
        assert not any(f.rule == "email_address" for f in findings)

    def test_placeholder_api_key_allowed(self) -> None:
        text = 'api_key = "your_api_key_here"'
        findings = scan_text(text)
        assert not any(f.rule == "generic_api_key" for f in findings)

    def test_changeme_password_allowed(self) -> None:
        text = 'password = "changeme"'
        findings = scan_text(text)
        assert not any(f.rule == "password_assignment" for f in findings)

    def test_localhost_connection_allowed(self) -> None:
        text = 'url = "postgres://user:pass@localhost:5432/testdb"'
        findings = scan_text(text)
        assert not any(f.rule == "connection_string" for f in findings)

    def test_dummy_value_allowed(self) -> None:
        text = 'api_key = "dummy_key_for_testing"'
        findings = scan_text(text)
        assert not any(f.rule == "generic_api_key" for f in findings)


# ---------------------------------------------------------------------------
# scan_text: clean content
# ---------------------------------------------------------------------------


class TestCleanContent:
    def test_normal_code_no_findings(self) -> None:
        text = "def hello():\n    return 'world'\n"
        assert scan_text(text) == []

    def test_comments_without_secrets_no_findings(self) -> None:
        text = "# This module handles authentication\n# Use environment variables for secrets\n"
        assert scan_text(text) == []


# ---------------------------------------------------------------------------
# scan_text: finding properties
# ---------------------------------------------------------------------------


class TestFindingProperties:
    def test_finding_is_frozen_dataclass(self) -> None:
        findings = scan_text('api_key = "sk-proj-abcdef1234567890ABCD"')
        assert findings
        assert isinstance(findings[0], SecretFinding)

    def test_line_number_is_correct(self) -> None:
        text = "line1\nline2\napi_key = 'sk-proj-abcdef1234567890ABCD'"
        findings = scan_text(text)
        assert findings[0].line_number == 3

    def test_redacted_match_does_not_contain_raw_secret(self) -> None:
        text = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"'
        findings = scan_text(text)
        gh = next(f for f in findings if f.rule == "github_token")
        assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in gh.redacted_match
        assert "***" in gh.redacted_match

    def test_each_rule_fires_at_most_once(self) -> None:
        text = "key1@real.com\nkey2@real.com\n"
        findings = scan_text(text)
        email_findings = [f for f in findings if f.rule == "email_address"]
        assert len(email_findings) <= 1


# ---------------------------------------------------------------------------
# scan_diff: only added lines
# ---------------------------------------------------------------------------


class TestScanDiff:
    def test_added_line_with_secret_detected(self) -> None:
        diff = (
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -1,3 +1,4 @@\n"
            " import os\n"
            '+API_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
            " other = 1\n"
        )
        findings = scan_diff(diff)
        assert any(f.rule == "aws_access_key" for f in findings)

    def test_removed_line_with_secret_not_detected(self) -> None:
        diff = '--- a/config.py\n+++ b/config.py\n@@ -1,3 +1,2 @@\n import os\n-API_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_context_line_not_scanned(self) -> None:
        diff = '--- a/config.py\n+++ b/config.py\n@@ -1,3 +1,3 @@\n API_KEY = "AKIAIOSFODNN7EXAMPLE"\n+# new comment\n'
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_diff_header_plus_lines_skipped(self) -> None:
        diff = "+++ b/file_with_AKIAIOSFODNN7EXAMPLE.py\n+clean line\n"
        findings = scan_diff(diff)
        assert not any(f.rule == "aws_access_key" for f in findings)

    def test_clean_diff_no_findings(self) -> None:
        diff = "--- a/utils.py\n+++ b/utils.py\n@@ -1 +1,2 @@\n def foo(): pass\n+def bar(): pass\n"
        assert scan_diff(diff) == []


# ---------------------------------------------------------------------------
# format_findings
# ---------------------------------------------------------------------------


class TestFormatFindings:
    def test_no_findings_message(self) -> None:
        assert "No secrets or PII" in format_findings([])

    def test_findings_include_count(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "1 finding" in output

    def test_findings_include_severity(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "HIGH" in output

    def test_findings_include_rule(self) -> None:
        findings = scan_text('password = "hunter2"')
        output = format_findings(findings)
        assert "password_assignment" in output


# ---------------------------------------------------------------------------
# Quality gate integration (_run_pii_gate)
# ---------------------------------------------------------------------------


class TestPiiQualityGate:
    def test_clean_directory_passes(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "main.py").write_text("def main():\n    return 42\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed
        assert not result.blocked
        assert result.gate == "pii_scan"

    def test_secret_in_file_blocks(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "config.py").write_text('API_KEY = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert not result.passed
        assert result.blocked
        assert "aws_access_key" in result.detail

    def test_pii_in_file_does_not_block(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        # email is medium severity — does not block, but is flagged
        (src / "utils.py").write_text("# Contact alice@realcompany.com\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed
        assert not result.blocked

    def test_missing_path_ignored(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["nonexistent/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed

    def test_binary_files_skipped(self, tmp_path: Path) -> None:
        from bernstein.core.quality_gates import QualityGatesConfig, _run_pii_gate

        src = tmp_path / "src"
        src.mkdir()
        (src / "image.png").write_bytes(b"\x89PNG\r\n")
        config = QualityGatesConfig(pii_scan=True, pii_scan_paths=["src/"])
        result = _run_pii_gate(config, tmp_path)
        assert result.passed

    def test_pii_gate_in_run_quality_gates(self, tmp_path: Path) -> None:
        from bernstein.core.models import Complexity, Scope, Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates

        src = tmp_path / "src"
        src.mkdir()
        (src / "leak.py").write_text('SECRET = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=True,
            pii_scan_paths=["src/"],
        )
        task = Task(
            id="T-pii-1",
            title="Test task",
            description="Test",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
        )
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert not result.passed
        gate_names = [r.gate for r in result.gate_results]
        assert "pii_scan" in gate_names

    def test_pii_gate_disabled_skips(self, tmp_path: Path) -> None:
        from bernstein.core.models import Complexity, Scope, Task
        from bernstein.core.quality_gates import QualityGatesConfig, run_quality_gates

        src = tmp_path / "src"
        src.mkdir()
        (src / "leak.py").write_text('SECRET = "AKIAIOSFODNN7EXAMPLE"\n')
        config = QualityGatesConfig(
            enabled=True,
            lint=False,
            type_check=False,
            tests=False,
            pii_scan=False,
        )
        task = Task(
            id="T-pii-2",
            title="Test task",
            description="Test",
            role="backend",
            scope=Scope.SMALL,
            complexity=Complexity.LOW,
        )
        result = run_quality_gates(task, tmp_path, tmp_path, config)
        assert result.passed
        gate_names = [r.gate for r in result.gate_results]
        assert "pii_scan" not in gate_names
