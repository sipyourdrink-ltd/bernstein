"""The policy library reaches both public audit surfaces (#5075)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.govern.compliance_checks import (
    ComplianceFramework,
    iter_compliance_checks,
    required_check_ids,
)


def test_main_audit_catalogue_contains_every_cmp_check_once() -> None:
    result = CliRunner().invoke(cli, ["govern", "audit", "--list", "--only", "CMP", "--json"])
    assert result.exit_code == 0, result.output
    ids = [item["check_id"] for item in json.loads(result.output)]
    assert ids == [spec.check_id for spec in iter_compliance_checks()]


def test_compliance_and_main_audit_report_identical_workspace_findings(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text("state_encryption: true\nauth: {}\n", encoding="utf-8")
    runner = CliRunner()
    audit = runner.invoke(cli, ["govern", "audit", "--only", "CMP", "--workdir", str(tmp_path), "--json"])
    compliance = runner.invoke(
        cli, ["compliance", "check", "--workdir", str(tmp_path), "--fail-on", "none", "--json-output"]
    )
    assert audit.exit_code in (0, 1), audit.output
    assert compliance.exit_code == 0, compliance.output
    findings = json.loads(audit.output)
    assert len(findings) == len(iter_compliance_checks())
    assert json.loads(compliance.output)["checks"] == findings
    encryption = next(f for f in findings if f["check_id"] == "CMP-003")
    assert encryption["verdict"] == "declared"
    assert encryption["passed"] is None


def test_policy_snapshot_reads_workspace_and_explicit_flags_override_it(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    args = [
        "compliance",
        "check",
        "--framework",
        "soc2",
        "--workdir",
        str(tmp_path),
        "--fail-on",
        "none",
        "--json-output",
    ]
    runner = CliRunner()
    observed = runner.invoke(cli, args)
    overridden = runner.invoke(cli, [*args, "--no-audit-logging"])
    assert observed.exit_code == overridden.exit_code == 0
    baseline = json.loads(observed.output)
    explicit = json.loads(overridden.output)
    policy = next(r for r in baseline["results"] if r["policy_id"] == "soc2-cc7-01")
    override = next(r for r in explicit["results"] if r["policy_id"] == "soc2-cc7-01")
    assert policy["passed"] is True
    assert override["passed"] is False
    assert policy["evidence_status"] == override["evidence_status"] == "operator_asserted"
    assert baseline["checks"] == explicit["checks"]


def test_audit_profile_marks_required_ids_without_changing_findings(tmp_path: Path) -> None:
    runner = CliRunner()
    args = ["govern", "audit", "--only", "CMP", "--workdir", str(tmp_path), "--json"]
    ordinary = runner.invoke(cli, args)
    selected = runner.invoke(cli, [*args, "--profile", "soc2"])
    assert ordinary.exit_code in (0, 1), ordinary.output
    assert selected.exit_code in (0, 1), selected.output
    base = json.loads(ordinary.output)
    profiled = json.loads(selected.output)
    required = required_check_ids(ComplianceFramework.SOC2)
    for finding in profiled:
        assert finding.pop("required") == (finding["check_id"] in required)
    assert profiled == base


def test_audit_rejects_unknown_profile_before_executing_checks() -> None:
    result = CliRunner().invoke(cli, ["govern", "audit", "--only", "CMP", "--profile", "unknown"])
    assert result.exit_code == 2
    assert "profile" in result.output.lower()


@pytest.mark.parametrize("workdir_args", [[], ["--workdir", "."], ["--workdir", "../workspace"]])
def test_default_and_relative_workdirs_produce_identical_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workdir_args: list[str]
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.chdir(root)
    runner = CliRunner()
    audit = runner.invoke(cli, ["govern", "audit", "--only", "CMP", "--json", *workdir_args])
    compliance = runner.invoke(cli, ["compliance", "check", "--fail-on", "none", "--json-output", *workdir_args])
    assert audit.exit_code == 1, audit.output
    assert compliance.exit_code == 0, compliance.output
    assert json.loads(compliance.output)["checks"] == json.loads(audit.output)


def test_nullable_config_keeps_unreadable_controls_in_the_report(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text("quality_gates: null\n", encoding="utf-8")
    (tmp_path / ".sdd" / "audit").mkdir(parents=True)
    runner = CliRunner()
    compliance = runner.invoke(
        cli, ["compliance", "check", "--workdir", str(tmp_path), "--fail-on", "none", "--json-output"]
    )
    audit = runner.invoke(cli, ["govern", "audit", "--only", "CMP", "--workdir", str(tmp_path), "--json"])
    assert compliance.exit_code == 0, compliance.output
    assert audit.exit_code == 1, audit.output
    payload = json.loads(compliance.output)
    assert payload["checks"] == json.loads(audit.output)
    findings = {finding["check_id"]: finding for finding in payload["checks"]}
    for cid in ("CMP-010", "CMP-011"):
        assert findings[cid]["area"] == "CMP"
        assert findings[cid]["verdict"] == "not_measurable"
        assert findings[cid]["passed"] is None
        assert findings[cid]["what_would_make_it_measurable"]
    assert findings["CMP-001"]["passed"] is True
    assert next(r for r in payload["results"] if r["policy_id"] == "soc2-cc7-01")["passed"] is True


@pytest.mark.parametrize("output_args", [["--json"], ["--format", "sarif"], ["--quiet"]])
def test_cmp_crash_takes_precedence_over_strict_incompleteness(tmp_path: Path, output_args: list[str]) -> None:
    (tmp_path / "bernstein.yaml").write_text("quality_gates: null\n", encoding="utf-8")
    skipped = [
        arg for spec in iter_compliance_checks() if spec.check_id != "CMP-010" for arg in ("--skip", spec.check_id)
    ]
    result = CliRunner().invoke(
        cli, ["govern", "audit", "--only", "CMP", "--strict", "--workdir", str(tmp_path), *skipped, *output_args]
    )
    assert result.exit_code == 1, result.output
    if output_args == ["--quiet"]:
        assert result.stdout == result.stderr == ""
