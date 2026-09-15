"""Tests for govern audit JSON and SARIF output, and CI exit code gating (#5076).

Acceptance criteria:
- `--format json` emits one object per finding with every contract field;
- `--format sarif` emits SARIF 2.1.0 with ruleId = finding.id and a kind property
  carrying the three-state verdict.
- Exit code: 0 when every required check is measured and passed; 1 when any
  required check is measured and failed; 2 when any required check is
  not_measurable and --strict is set. Declared findings never change the exit code.
- `--quiet` prints nothing; the exit code is the whole answer.
- Tests cover each exit path with a fixture registry of three checks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import govern_group
from bernstein.core.checks.contract import Evidence, Finding, Verdict
from bernstein.core.checks.formatters import (
    compute_audit_exit_code,
    findings_to_json,
    findings_to_sarif,
    verdict_to_kind,
)
from bernstein.core.checks.registry import CheckRegistry


class _DummyCheck:
    """Fixture check returning a pre-configured Finding."""

    def __init__(self, finding: Finding) -> None:
        self._finding = finding

    @property
    def check_id(self) -> str:
        return self._finding.check_id

    @property
    def title(self) -> str:
        return f"Dummy {self._finding.check_id}"

    @property
    def description(self) -> str:
        return f"Check {self._finding.check_id}"

    def run(self, workdir: Path | None = None) -> Finding:
        return self._finding

    def __call__(self, workdir: Path | None = None) -> Finding:
        return self.run(workdir)


@pytest.fixture
def evidence_sample() -> tuple[Evidence, ...]:
    return (Evidence.from_bytes("file:///etc/bernstein/conf.json", b'{"configured": true}'),)


@pytest.fixture
def pass_finding(evidence_sample: tuple[Evidence, ...]) -> Finding:
    return Finding(
        check_id="sec:encryption_at_rest",
        verdict=Verdict.PASS,
        evidence=evidence_sample,
        summary="Storage encryption is enabled",
        remediation="Ensure disks are encrypted",
        passed=True,
    )


@pytest.fixture
def fail_finding(evidence_sample: tuple[Evidence, ...]) -> Finding:
    return Finding(
        check_id="sec:access_control",
        verdict=Verdict.FAIL,
        evidence=evidence_sample,
        summary="Access control violation detected",
        remediation="Revoke unauthorized principal access",
        passed=False,
    )


@pytest.fixture
def not_measurable_finding() -> Finding:
    return Finding(
        check_id="obs:audit_pipeline",
        verdict=Verdict.NOT_MEASURABLE,
        what_would_make_it_measurable="Configure audit log ingestion endpoint",
        reason="ConfigurationMissing",
        summary="Audit log pipeline not reachable",
        remediation="Set AUDIT_LOG_ENDPOINT env var",
    )


@pytest.fixture
def declared_finding() -> Finding:
    return Finding(
        check_id="gov:training_policy",
        verdict=Verdict.DECLARED,
        summary="Security training completed on declaration",
        remediation="File declaration with compliance team",
    )


@pytest.fixture
def three_checks_registry(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
) -> CheckRegistry:
    """Fixture registry of three checks: pass, fail, not_measurable."""
    registry = CheckRegistry()
    registry.register(_DummyCheck(pass_finding))
    registry.register(_DummyCheck(fail_finding))
    registry.register(_DummyCheck(not_measurable_finding))
    return registry


# ---------------------------------------------------------------------------
# 1. JSON output formatting tests
# ---------------------------------------------------------------------------


def test_finding_to_dict_contains_all_contract_fields(pass_finding: Finding) -> None:
    """Finding.to_dict() emits every single contract field."""
    d = pass_finding.to_dict()
    assert d["id"] == "sec:encryption_at_rest"
    assert d["check_id"] == "sec:encryption_at_rest"
    assert d["area"] == "sec"
    assert d["verdict"] == "pass"
    assert len(d["evidence"]) == 1
    assert d["evidence"][0]["locator"] == "file:///etc/bernstein/conf.json"
    assert d["evidence"][0]["sha256"].startswith("sha256:")
    assert d["summary"] == "Storage encryption is enabled"
    assert d["message"] == "Storage encryption is enabled"
    assert d["remediation"] == "Ensure disks are encrypted"
    assert d["passed"] is True
    assert "what_would_make_it_measurable" in d
    assert "reason" in d


def test_finding_from_dict_roundtrip(
    pass_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """Finding round-trips through to_dict and from_dict."""
    d_pass = pass_finding.to_dict()
    restored_pass = Finding.from_dict(d_pass)
    assert restored_pass.id == pass_finding.id
    assert restored_pass.verdict == pass_finding.verdict
    assert restored_pass.passed == pass_finding.passed
    assert restored_pass.evidence == pass_finding.evidence

    d_nm = not_measurable_finding.to_dict()
    restored_nm = Finding.from_dict(d_nm)
    assert restored_nm.id == not_measurable_finding.id
    assert restored_nm.verdict == Verdict.NOT_MEASURABLE
    assert restored_nm.what_would_make_it_measurable == not_measurable_finding.what_would_make_it_measurable


def test_cli_format_json_emits_one_object_per_finding_with_every_contract_field(
    three_checks_registry: CheckRegistry,
) -> None:
    """`--format json` emits one object per finding with every contract field."""
    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["audit", "--format", "json"],
        obj={"check_registry": three_checks_registry},
    )
    # Registry has a failing check, so exit code is 1
    assert result.exit_code == 1

    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert len(payload) == 3

    contract_fields = {
        "id",
        "check_id",
        "area",
        "verdict",
        "evidence",
        "what_would_make_it_measurable",
        "reason",
        "message",
        "summary",
        "remediation",
        "passed",
    }
    for item in payload:
        assert contract_fields.issubset(item.keys()), f"Missing fields in {item}"

    ids = [item["id"] for item in payload]
    assert ids == [
        "sec:encryption_at_rest",
        "sec:access_control",
        "obs:audit_pipeline",
    ]


# ---------------------------------------------------------------------------
# 2. SARIF 2.1.0 output formatting tests
# ---------------------------------------------------------------------------


def test_cli_format_sarif_emits_sarif_210_with_rule_id_and_kind(
    three_checks_registry: CheckRegistry,
) -> None:
    """`--format sarif` emits SARIF 2.1.0 with ruleId = finding.id and kind property."""
    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["audit", "--format", "sarif"],
        obj={"check_registry": three_checks_registry},
    )
    assert result.exit_code == 1

    sarif = json.loads(result.output)
    assert sarif["version"] == "2.1.0"
    assert "sarif-schema-2.1.0" in sarif["$schema"]

    runs = sarif["runs"]
    assert len(runs) == 1
    run = runs[0]

    rules = run["tool"]["driver"]["rules"]
    rule_ids = [r["id"] for r in rules]
    assert rule_ids == [
        "sec:encryption_at_rest",
        "sec:access_control",
        "obs:audit_pipeline",
    ]

    results = run["results"]
    assert len(results) == 3

    # Check 1: Pass
    res0 = results[0]
    assert res0["ruleId"] == "sec:encryption_at_rest"
    assert res0["kind"] == "pass"
    assert res0["properties"]["kind"] == "pass"
    assert res0["level"] == "none"

    # Check 2: Fail
    res1 = results[1]
    assert res1["ruleId"] == "sec:access_control"
    assert res1["kind"] == "fail"
    assert res1["properties"]["kind"] == "fail"
    assert res1["level"] == "error"

    # Check 3: Not Measurable
    res2 = results[2]
    assert res2["ruleId"] == "obs:audit_pipeline"
    assert res2["kind"] == "not_measurable"
    assert res2["properties"]["kind"] == "not_measurable"
    assert res2["level"] == "warning"
    assert res2["properties"]["what_would_make_it_measurable"] == ("Configure audit log ingestion endpoint")


# ---------------------------------------------------------------------------
# 3. Exit code truth table tests with fixture registry
# ---------------------------------------------------------------------------


def test_exit_code_0_when_every_required_check_measured_and_passed(
    pass_finding: Finding,
) -> None:
    """Exit 0 when every required check is measured and passed."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"], obj={"check_registry": reg})
    assert result.exit_code == 0
    assert "sec:encryption_at_rest" in result.output


def test_exit_code_1_when_any_required_check_measured_and_failed(
    pass_finding: Finding,
    fail_finding: Finding,
) -> None:
    """Exit 1 when any required check is measured and failed."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(fail_finding))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"], obj={"check_registry": reg})
    assert result.exit_code == 1


def test_exit_code_0_when_not_measurable_without_strict(
    pass_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """Exit 0 when a check is not_measurable and --strict is NOT set."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(not_measurable_finding))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"], obj={"check_registry": reg})
    assert result.exit_code == 0


def test_exit_code_2_when_any_required_check_not_measurable_and_strict_set(
    pass_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """Exit 2 when any required check is not_measurable and --strict is set."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(not_measurable_finding))

    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["audit", "--strict"],
        obj={"check_registry": reg},
    )
    assert result.exit_code == 2


def test_exit_code_1_takes_precedence_over_strict_not_measurable(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """Failure (1) takes precedence over strict not_measurable (2)."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(fail_finding))
    reg.register(_DummyCheck(not_measurable_finding))

    runner = CliRunner()
    result = runner.invoke(
        govern_group,
        ["audit", "--strict"],
        obj={"check_registry": reg},
    )
    assert result.exit_code == 1


def test_declared_findings_never_change_exit_code(
    pass_finding: Finding,
    declared_finding: Finding,
) -> None:
    """Declared findings never change the exit code."""
    # Pass + Declared -> 0
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(declared_finding))

    runner = CliRunner()
    result = runner.invoke(govern_group, ["audit"], obj={"check_registry": reg})
    assert result.exit_code == 0

    # Only Declared -> 0
    reg_only_declared = CheckRegistry()
    reg_only_declared.register(_DummyCheck(declared_finding))
    res_decl = runner.invoke(govern_group, ["audit"], obj={"check_registry": reg_only_declared})
    assert res_decl.exit_code == 0

    # Only Declared with --strict -> 0 (strict only affects not_measurable)
    res_decl_strict = runner.invoke(
        govern_group,
        ["audit", "--strict"],
        obj={"check_registry": reg_only_declared},
    )
    assert res_decl_strict.exit_code == 0


def test_selective_required_checks_gate_exit_code(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """`--required` selectively gates exit code on the named check IDs."""
    reg = CheckRegistry()
    reg.register(_DummyCheck(pass_finding))
    reg.register(_DummyCheck(fail_finding))
    reg.register(_DummyCheck(not_measurable_finding))

    runner = CliRunner()
    # When only the passing check is required, exit code is 0
    res_pass_only = runner.invoke(
        govern_group,
        ["audit", "--required", "sec:encryption_at_rest"],
        obj={"check_registry": reg},
    )
    assert res_pass_only.exit_code == 0

    # When the failing check is required, exit code is 1
    res_fail_req = runner.invoke(
        govern_group,
        ["audit", "--required", "sec:access_control"],
        obj={"check_registry": reg},
    )
    assert res_fail_req.exit_code == 1

    # When not_measurable is required with --strict, exit code is 2
    res_nm_strict = runner.invoke(
        govern_group,
        ["audit", "--strict", "--required", "obs:audit_pipeline"],
        obj={"check_registry": reg},
    )
    assert res_nm_strict.exit_code == 2


# ---------------------------------------------------------------------------
# 4. --quiet prints nothing; exit code is the whole answer
# ---------------------------------------------------------------------------


def test_quiet_suppresses_all_output_and_preserves_exit_code(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
) -> None:
    """`--quiet` prints nothing; the exit code is the whole answer."""
    runner = CliRunner()

    # Pass case -> exit 0, empty stdout
    reg_pass = CheckRegistry()
    reg_pass.register(_DummyCheck(pass_finding))
    res_pass = runner.invoke(
        govern_group,
        ["audit", "--quiet"],
        obj={"check_registry": reg_pass},
    )
    assert res_pass.exit_code == 0
    assert res_pass.output == ""

    # Fail case -> exit 1, empty stdout
    reg_fail = CheckRegistry()
    reg_fail.register(_DummyCheck(fail_finding))
    res_fail = runner.invoke(
        govern_group,
        ["audit", "-q"],
        obj={"check_registry": reg_fail},
    )
    assert res_fail.exit_code == 1
    assert res_fail.output == ""

    # Strict not_measurable case -> exit 2, empty stdout
    reg_nm = CheckRegistry()
    reg_nm.register(_DummyCheck(not_measurable_finding))
    res_nm = runner.invoke(
        govern_group,
        ["audit", "--strict", "--quiet"],
        obj={"check_registry": reg_nm},
    )
    assert res_nm.exit_code == 2
    assert res_nm.output == ""


# ---------------------------------------------------------------------------
# 5. Direct unit tests for compute_audit_exit_code
# ---------------------------------------------------------------------------


def test_compute_audit_exit_code_truth_table(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
    declared_finding: Finding,
) -> None:
    """Exhaustive truth table test on compute_audit_exit_code."""
    assert compute_audit_exit_code([]) == 0
    assert compute_audit_exit_code([pass_finding]) == 0
    assert compute_audit_exit_code([pass_finding, declared_finding]) == 0
    assert compute_audit_exit_code([declared_finding]) == 0
    assert compute_audit_exit_code([pass_finding, fail_finding]) == 1
    assert compute_audit_exit_code([pass_finding, not_measurable_finding], strict=False) == 0
    assert compute_audit_exit_code([pass_finding, not_measurable_finding], strict=True) == 2
    assert compute_audit_exit_code([fail_finding, not_measurable_finding], strict=True) == 1
    assert compute_audit_exit_code([declared_finding], strict=True) == 0


def test_verdict_to_kind_mapping(
    pass_finding: Finding,
    fail_finding: Finding,
    not_measurable_finding: Finding,
    declared_finding: Finding,
) -> None:
    """verdict_to_kind maps verdicts to three-state strings or declared."""
    assert verdict_to_kind(pass_finding) == "pass"
    assert verdict_to_kind(fail_finding) == "fail"
    assert verdict_to_kind(not_measurable_finding) == "not_measurable"
    assert verdict_to_kind(declared_finding) == "declared"


def test_findings_to_json_and_sarif_direct(
    pass_finding: Finding,
    fail_finding: Finding,
) -> None:
    """Direct invocation of findings_to_json and findings_to_sarif."""
    raw_json = findings_to_json([pass_finding, fail_finding])
    parsed_json = json.loads(raw_json)
    assert len(parsed_json) == 2
    assert parsed_json[0]["id"] == pass_finding.id

    sarif_dict = findings_to_sarif([pass_finding, fail_finding])
    assert sarif_dict["version"] == "2.1.0"
    assert len(sarif_dict["runs"][0]["results"]) == 2
