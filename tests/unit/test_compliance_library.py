"""Unit tests for the compliance-as-code policy library (GH-660)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.compliance_library import (
    ComplianceFramework,
    ComplianceReport,
    PolicyResult,
    PolicyRule,
    Severity,
    check_access_controls,
    check_audit_logging_enabled,
    check_auth_configured,
    check_backup_configured,
    check_change_management,
    check_consent_management,
    check_data_classification,
    check_data_retention,
    check_dependency_pinning,
    check_encryption_at_rest,
    check_incident_response_plan,
    check_logging_integrity,
    check_mfa_enabled,
    check_network_isolation,
    check_password_policy,
    check_phi_detection,
    check_privacy_policy,
    check_rate_limiting,
    check_sdd_state_directory,
    check_secrets_management,
    check_session_management,
    check_tls_enforced,
    check_vulnerability_scanning,
    get_all_rules,
    get_framework_rules,
    get_registered_check_names,
    get_rule_by_id,
    render_compliance_report,
    run_compliance_check,
)

# ---------------------------------------------------------------------------
# Framework enumeration
# ---------------------------------------------------------------------------


def test_compliance_framework_enum_values() -> None:
    assert ComplianceFramework.SOC2 == "soc2"
    assert ComplianceFramework.ISO27001 == "iso27001"
    assert ComplianceFramework.PCI_DSS == "pci_dss"
    assert ComplianceFramework.NIST_800_53 == "nist_800_53"
    assert ComplianceFramework.HIPAA == "hipaa"
    assert ComplianceFramework.GDPR == "gdpr"


def test_all_six_frameworks_exist() -> None:
    assert len(ComplianceFramework) == 6


# ---------------------------------------------------------------------------
# Severity enumeration
# ---------------------------------------------------------------------------


def test_severity_enum_values() -> None:
    assert Severity.CRITICAL == "critical"
    assert Severity.HIGH == "high"
    assert Severity.MEDIUM == "medium"
    assert Severity.LOW == "low"


# ---------------------------------------------------------------------------
# PolicyRule dataclass
# ---------------------------------------------------------------------------


def test_policy_rule_is_frozen() -> None:
    rule = PolicyRule(
        rule_id="test-01",
        framework=ComplianceFramework.SOC2,
        title="Test Rule",
        description="A test rule.",
        check_function_name="check_test",
        severity=Severity.HIGH,
    )
    with pytest.raises(AttributeError):
        rule.title = "Changed"  # type: ignore[misc]


def test_policy_rule_fields() -> None:
    rule = PolicyRule(
        rule_id="test-01",
        framework=ComplianceFramework.HIPAA,
        title="Test Title",
        description="Desc",
        check_function_name="check_something",
        severity=Severity.CRITICAL,
    )
    assert rule.rule_id == "test-01"
    assert rule.framework == ComplianceFramework.HIPAA
    assert rule.title == "Test Title"
    assert rule.check_function_name == "check_something"
    assert rule.severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# PolicyResult dataclass
# ---------------------------------------------------------------------------


def test_policy_result_is_frozen() -> None:
    rule = PolicyRule(
        rule_id="x",
        framework=ComplianceFramework.SOC2,
        title="X",
        description="X",
        check_function_name="x",
        severity=Severity.LOW,
    )
    result = PolicyResult(rule=rule, passed=True, evidence="ok", remediation="")
    with pytest.raises(AttributeError):
        result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ComplianceReport dataclass
# ---------------------------------------------------------------------------


def test_compliance_report_is_frozen() -> None:
    report = ComplianceReport(
        framework=ComplianceFramework.SOC2,
        rules_checked=0,
        rules_passed=0,
        rules_failed=0,
        results=(),
        score=0.0,
    )
    with pytest.raises(AttributeError):
        report.score = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------


def test_all_rules_count_at_least_20() -> None:
    rules = get_all_rules()
    assert len(rules) >= 20, f"Expected >= 20 rules, got {len(rules)}"


def test_no_duplicate_rule_ids() -> None:
    rules = get_all_rules()
    ids = [r.rule_id for r in rules]
    assert len(ids) == len(set(ids)), "Duplicate rule IDs found"


def test_all_frameworks_have_rules() -> None:
    for fw in ComplianceFramework:
        rules = get_framework_rules(fw)
        assert len(rules) > 0, f"Framework {fw} has no rules"


def test_get_framework_rules_returns_only_matching() -> None:
    for fw in ComplianceFramework:
        rules = get_framework_rules(fw)
        for rule in rules:
            assert rule.framework == fw


def test_get_rule_by_id_found() -> None:
    rule = get_rule_by_id("soc2-audit-01")
    assert rule is not None
    assert rule.rule_id == "soc2-audit-01"


def test_get_rule_by_id_not_found() -> None:
    assert get_rule_by_id("nonexistent-rule") is None


def test_all_rules_have_valid_check_function_name() -> None:
    registered = get_registered_check_names()
    for rule in get_all_rules():
        assert rule.check_function_name in registered, (
            f"Rule {rule.rule_id} references missing check function: {rule.check_function_name}"
        )


def test_all_rules_have_nonempty_fields() -> None:
    for rule in get_all_rules():
        assert rule.rule_id.strip(), "Rule has empty rule_id"
        assert rule.title.strip(), f"Rule {rule.rule_id} has empty title"
        assert rule.description.strip(), f"Rule {rule.rule_id} has empty description"
        assert rule.check_function_name.strip(), f"Rule {rule.rule_id} has empty check_function_name"


# ---------------------------------------------------------------------------
# Individual check functions (filesystem-based)
# ---------------------------------------------------------------------------


def test_check_audit_logging_enabled_pass(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    result = check_audit_logging_enabled(tmp_path)
    assert result.passed is True


def test_check_audit_logging_enabled_fail(tmp_path: Path) -> None:
    result = check_audit_logging_enabled(tmp_path)
    assert result.passed is False
    assert result.remediation != ""


def test_check_auth_configured_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"auth": {"provider": "jwt"}})
    result = check_auth_configured(tmp_path)
    assert result.passed is True


def test_check_auth_configured_fail(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"cli": "claude"})
    result = check_auth_configured(tmp_path)
    assert result.passed is False


def test_check_encryption_at_rest_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"state_encryption": {"enabled": True}})
    result = check_encryption_at_rest(tmp_path)
    assert result.passed is True


def test_check_encryption_at_rest_via_compliance_key(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"compliance": {"encrypt_state_at_rest": True}})
    result = check_encryption_at_rest(tmp_path)
    assert result.passed is True


def test_check_encryption_at_rest_fail(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"cli": "claude"})
    result = check_encryption_at_rest(tmp_path)
    assert result.passed is False


def test_check_access_controls_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"rbac": {"admin": ["*"]}})
    result = check_access_controls(tmp_path)
    assert result.passed is True


def test_check_access_controls_fail(tmp_path: Path) -> None:
    result = check_access_controls(tmp_path)
    assert result.passed is False


def test_check_data_retention_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"data_retention": {"days": 90}})
    result = check_data_retention(tmp_path)
    assert result.passed is True


def test_check_data_retention_fail(tmp_path: Path) -> None:
    result = check_data_retention(tmp_path)
    assert result.passed is False


def test_check_backup_configured_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"backup": {"schedule": "daily"}})
    result = check_backup_configured(tmp_path)
    assert result.passed is True


def test_check_tls_enforced_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"tls_enforced": True}})
    result = check_tls_enforced(tmp_path)
    assert result.passed is True


def test_check_tls_enforced_fail(tmp_path: Path) -> None:
    result = check_tls_enforced(tmp_path)
    assert result.passed is False


def test_check_incident_response_plan_pass(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "incident-response.md").write_text("# IR Plan")
    result = check_incident_response_plan(tmp_path)
    assert result.passed is True


def test_check_incident_response_plan_fail(tmp_path: Path) -> None:
    result = check_incident_response_plan(tmp_path)
    assert result.passed is False


def test_check_secrets_management_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"secrets": {"provider": "vault"}})
    result = check_secrets_management(tmp_path)
    assert result.passed is True


def test_check_change_management_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"quality_gates": {"enabled": True}})
    result = check_change_management(tmp_path)
    assert result.passed is True


def test_check_network_isolation_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"network_isolation": True}})
    result = check_network_isolation(tmp_path)
    assert result.passed is True


def test_check_logging_integrity_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"compliance": {"audit_hmac_chain": True}})
    result = check_logging_integrity(tmp_path)
    assert result.passed is True


def test_check_session_management_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"session_timeout_minutes": 30}})
    result = check_session_management(tmp_path)
    assert result.passed is True


def test_check_password_policy_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"password_min_length": 14}})
    result = check_password_policy(tmp_path)
    assert result.passed is True


def test_check_password_policy_fail_too_short(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"password_min_length": 6}})
    result = check_password_policy(tmp_path)
    assert result.passed is False


def test_check_mfa_enabled_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"mfa_enabled": True}})
    result = check_mfa_enabled(tmp_path)
    assert result.passed is True


def test_check_sdd_state_directory_pass(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    result = check_sdd_state_directory(tmp_path)
    assert result.passed is True


def test_check_sdd_state_directory_fail(tmp_path: Path) -> None:
    result = check_sdd_state_directory(tmp_path)
    assert result.passed is False


def test_check_rate_limiting_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"rate_limiting_enabled": True}})
    result = check_rate_limiting(tmp_path)
    assert result.passed is True


def test_check_dependency_pinning_pass(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("# lock")
    result = check_dependency_pinning(tmp_path)
    assert result.passed is True


def test_check_dependency_pinning_fail(tmp_path: Path) -> None:
    result = check_dependency_pinning(tmp_path)
    assert result.passed is False


def test_check_privacy_policy_pass(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "privacy-policy.md").write_text("# Privacy")
    result = check_privacy_policy(tmp_path)
    assert result.passed is True


def test_check_data_classification_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"data_classification": {"levels": ["public", "internal"]}})
    result = check_data_classification(tmp_path)
    assert result.passed is True


def test_check_phi_detection_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"compliance": {"phi_detection": True}})
    result = check_phi_detection(tmp_path)
    assert result.passed is True


def test_check_consent_management_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"consent": {"provider": "onetrust"}})
    result = check_consent_management(tmp_path)
    assert result.passed is True


def test_check_vulnerability_scanning_pass(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "bernstein.yaml", {"security": {"vulnerability_scanning": True}})
    result = check_vulnerability_scanning(tmp_path)
    assert result.passed is True


# ---------------------------------------------------------------------------
# run_compliance_check (end-to-end)
# ---------------------------------------------------------------------------


def test_run_compliance_check_all_fail(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.SOC2, tmp_path)
    assert report.framework == ComplianceFramework.SOC2
    assert report.rules_checked > 0
    assert report.rules_failed > 0
    assert report.score < 1.0


def test_run_compliance_check_score_calculation(tmp_path: Path) -> None:
    # Set up enough to pass some SOC2 rules
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    _write_yaml(tmp_path / "bernstein.yaml", {"quality_gates": {"enabled": True}})
    report = run_compliance_check(ComplianceFramework.SOC2, tmp_path)
    expected_score = report.rules_passed / report.rules_checked
    assert abs(report.score - expected_score) < 0.001


def test_run_compliance_check_results_count_matches(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.ISO27001, tmp_path)
    assert report.rules_checked == len(report.results)
    assert report.rules_passed + report.rules_failed == report.rules_checked


def test_run_compliance_check_all_frameworks(tmp_path: Path) -> None:
    for fw in ComplianceFramework:
        report = run_compliance_check(fw, tmp_path)
        assert report.framework == fw
        assert report.rules_checked > 0
        assert isinstance(report.results, tuple)


# ---------------------------------------------------------------------------
# render_compliance_report
# ---------------------------------------------------------------------------


def test_render_compliance_report_contains_framework_name(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.PCI_DSS, tmp_path)
    md = render_compliance_report(report)
    assert "PCI DSS" in md


def test_render_compliance_report_contains_score(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.SOC2, tmp_path)
    md = render_compliance_report(report)
    assert "Score:" in md
    assert "%" in md


def test_render_compliance_report_contains_table(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.NIST_800_53, tmp_path)
    md = render_compliance_report(report)
    assert "| Status |" in md
    assert "| Rule ID |" in md


def test_render_compliance_report_shows_failed_section(tmp_path: Path) -> None:
    report = run_compliance_check(ComplianceFramework.SOC2, tmp_path)
    md = render_compliance_report(report)
    if report.rules_failed > 0:
        assert "## Failed Rules" in md


def test_render_compliance_report_empty_results() -> None:
    report = ComplianceReport(
        framework=ComplianceFramework.GDPR,
        rules_checked=0,
        rules_passed=0,
        rules_failed=0,
        results=(),
        score=0.0,
    )
    md = render_compliance_report(report)
    assert "GDPR" in md
    assert "Score:" in md


# ---------------------------------------------------------------------------
# sdd/config.yaml fallback
# ---------------------------------------------------------------------------


def test_check_auth_via_sdd_config(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    _write_yaml(sdd / "config.yaml", {"auth": {"method": "token"}})
    result = check_auth_configured(tmp_path)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write a dict as YAML to *path*, creating parents as needed."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))
