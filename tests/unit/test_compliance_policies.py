"""Unit tests for the compliance-as-code policy library."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.compliance_policies import (
    ALL_POLICIES,
    ComplianceFramework,
    CompliancePolicyLibrary,
    PolicyInput,
    PolicySeverity,
    evaluate_all,
    evaluate_framework,
    evaluate_policy,
)

# ---------------------------------------------------------------------------
# Policy library completeness
# ---------------------------------------------------------------------------


def test_at_least_50_policies() -> None:
    assert len(ALL_POLICIES) >= 50, f"Expected ≥50 policies, got {len(ALL_POLICIES)}"


def test_all_frameworks_represented() -> None:
    framework_ids = {p.framework for p in ALL_POLICIES}
    assert ComplianceFramework.SOC2 in framework_ids
    assert ComplianceFramework.ISO27001 in framework_ids
    assert ComplianceFramework.PCI_DSS in framework_ids
    assert ComplianceFramework.NIST_800_53 in framework_ids


def test_no_duplicate_policy_ids() -> None:
    ids = [p.policy_id for p in ALL_POLICIES]
    assert len(ids) == len(set(ids)), "Duplicate policy IDs found"


def test_all_policies_have_rego_rule() -> None:
    for p in ALL_POLICIES:
        assert p.rego_rule.strip(), f"Policy {p.policy_id} has empty rego_rule"


def test_all_policies_have_remediation() -> None:
    for p in ALL_POLICIES:
        assert p.remediation.strip(), f"Policy {p.policy_id} has empty remediation"


def test_all_policies_have_valid_severity() -> None:
    valid = set(PolicySeverity)
    for p in ALL_POLICIES:
        assert p.severity in valid, f"Policy {p.policy_id} has invalid severity: {p.severity}"


# ---------------------------------------------------------------------------
# evaluate_policy
# ---------------------------------------------------------------------------


def test_passing_policy_returns_no_finding() -> None:
    inp = PolicyInput(audit_logging=True)
    from bernstein.core.compliance_policies import _BY_ID

    policy = _BY_ID["soc2-cc7-01"]
    result = evaluate_policy(policy, inp)
    assert result.passed is True
    assert result.finding == ""
    assert result.remediation == ""


def test_failing_policy_returns_finding() -> None:
    inp = PolicyInput(audit_logging=False)
    from bernstein.core.compliance_policies import _BY_ID

    policy = _BY_ID["soc2-cc7-01"]
    result = evaluate_policy(policy, inp)
    assert result.passed is False
    assert result.policy_id in result.finding
    assert result.remediation != ""


def test_evaluate_policy_exception_returns_fail() -> None:
    """A check that raises should be treated as a failure, not a crash."""
    from bernstein.core.compliance_policies import ComplianceFramework, PolicySeverity, _p

    bad = _p(
        "test-boom",
        "boom",
        ComplianceFramework.SOC2,
        "CC0.0",
        "Raises",
        PolicySeverity.LOW,
        "",
        lambda _: 1 / 0,
        "fix it",
    )
    inp = PolicyInput()
    result = evaluate_policy(bad, inp)
    assert result.passed is False


# ---------------------------------------------------------------------------
# evaluate_framework
# ---------------------------------------------------------------------------


def test_evaluate_framework_returns_results_for_all_policies() -> None:
    inp = PolicyInput()
    soc2_results = evaluate_framework(ComplianceFramework.SOC2, inp)
    from bernstein.core.compliance_policies import _BY_FRAMEWORK

    assert len(soc2_results) == len(_BY_FRAMEWORK[ComplianceFramework.SOC2])


def test_all_fail_with_default_input() -> None:
    """Default PolicyInput has worst-case values; most policies should fail."""
    inp = PolicyInput()
    results = evaluate_all(inp)
    failing = [r for r in results if not r.passed]
    # At least 80% should fail with default (all-off) input
    assert len(failing) / len(results) > 0.8


def test_high_compliance_input_mostly_passes() -> None:
    """A well-configured deployment should pass most policies."""
    inp = PolicyInput(
        audit_logging=True,
        audit_hmac_chain=True,
        audit_retention_days=365,
        sandbox_enabled=True,
        seccomp_enabled=True,
        network_isolation=True,
        read_only_rootfs=True,
        tls_enforced=True,
        secrets_rotation_days=60,
        mfa_enabled=True,
        rbac_enabled=True,
        least_privilege_caps=True,
        vulnerability_scanning=True,
        sbom_enabled=True,
        change_approval_gates=True,
        incident_response_plan=True,
        data_classification=True,
        encrypt_at_rest=True,
        encrypt_in_transit=True,
        log_integrity=True,
        access_review_days=30,
        password_min_length=16,
        session_timeout_minutes=30,
        agent_token_expiry_hours=8,
        rate_limiting_enabled=True,
        waf_enabled=True,
        backup_enabled=True,
        backup_encryption=True,
        dr_rto_hours=2,
        code_signing=True,
        dependency_pinning=True,
        sast_in_ci=True,
    )
    results = evaluate_all(inp)
    passing = [r for r in results if r.passed]
    assert len(passing) / len(results) > 0.8


# ---------------------------------------------------------------------------
# PolicyResult.to_dict
# ---------------------------------------------------------------------------


def test_policy_result_to_dict_structure() -> None:
    inp = PolicyInput(audit_logging=True)
    from bernstein.core.compliance_policies import _BY_ID

    result = evaluate_policy(_BY_ID["soc2-cc7-01"], inp)
    d = result.to_dict()
    assert d["policy_id"] == "soc2-cc7-01"
    assert d["framework"] == "soc2"
    assert isinstance(d["passed"], bool)
    assert "remediation" in d


# ---------------------------------------------------------------------------
# CompliancePolicyLibrary
# ---------------------------------------------------------------------------


def test_library_enable_persists_marker_file(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    lib.enable(ComplianceFramework.SOC2, config_dir=tmp_path)
    marker = tmp_path / "enabled" / "soc2.yaml"
    assert marker.exists()
    content = marker.read_text()
    assert "framework: soc2" in content
    assert "enabled: true" in content


def test_library_enable_without_config_dir_does_not_write(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    lib.enable(ComplianceFramework.ISO27001)
    assert not (tmp_path / "enabled" / "iso27001.yaml").exists()
    assert ComplianceFramework.ISO27001 in lib.enabled_frameworks


def test_library_disable_removes_marker(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    lib.enable(ComplianceFramework.PCI_DSS, config_dir=tmp_path)
    lib.disable(ComplianceFramework.PCI_DSS, config_dir=tmp_path)
    marker = tmp_path / "enabled" / "pci_dss.yaml"
    assert not marker.exists()
    assert ComplianceFramework.PCI_DSS not in lib.enabled_frameworks


def test_library_load_enabled_from_disk(tmp_path: Path) -> None:
    lib1 = CompliancePolicyLibrary()
    lib1.enable(ComplianceFramework.NIST_800_53, config_dir=tmp_path)
    lib1.enable(ComplianceFramework.SOC2, config_dir=tmp_path)

    lib2 = CompliancePolicyLibrary()
    lib2.load_enabled(tmp_path)
    assert ComplianceFramework.NIST_800_53 in lib2.enabled_frameworks
    assert ComplianceFramework.SOC2 in lib2.enabled_frameworks


def test_library_evaluate_only_enabled_frameworks(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    lib.enable(ComplianceFramework.SOC2)

    inp = PolicyInput()
    results = lib.evaluate(inp)
    assert all(r.framework == ComplianceFramework.SOC2 for r in results)


def test_library_policy_count(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    soc2_count = lib.policy_count(ComplianceFramework.SOC2)
    assert soc2_count > 0
    total = lib.policy_count()
    assert total >= 50


def test_library_get_policy_by_id() -> None:
    lib = CompliancePolicyLibrary()
    policy = lib.get_policy("pci-req3-01")
    assert policy is not None
    assert policy.framework == ComplianceFramework.PCI_DSS


def test_library_get_policy_unknown_returns_none() -> None:
    lib = CompliancePolicyLibrary()
    assert lib.get_policy("no-such-policy") is None


def test_library_export_rego(tmp_path: Path) -> None:
    lib = CompliancePolicyLibrary()
    paths = lib.export_rego(ComplianceFramework.SOC2, dest_dir=tmp_path)
    assert len(paths) > 0
    for p in paths:
        assert p.suffix == ".rego"
        content = p.read_text()
        assert "package bernstein" in content
