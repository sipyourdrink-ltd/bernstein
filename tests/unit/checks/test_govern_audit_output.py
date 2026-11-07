"""Machine-readable governance audit output and CI exit codes (#5076)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands import govern_audit_cmd as audit
from bernstein.core.checks.contract import Evidence, Finding, Verdict
from bernstein.core.checks.registry import CheckRegistry


class FixtureCheck:
    title = "Fixture check"
    description = "Returns an explicit finding without reading the workspace."

    def __init__(self, finding: Finding) -> None:
        self.finding = finding
        self.check_id = finding.id

    def run(self, workdir: Path | None = None) -> Finding:
        return self.finding


def _finding(index: int, verdict: Verdict, passed: bool | None = None) -> Finding:
    return Finding(
        check_id=f"fixture:{index}",
        verdict=verdict,
        passed=passed,
        evidence=(Evidence(locator=f"fixture://{index}", sha256="a" * 64),),
        message=f"Finding {index}",
        summary=f"Summary {index}",
        reason="Missing permission" if verdict == Verdict.NOT_MEASURABLE else None,
        remediation="Grant permission",
        what_would_make_it_measurable="Allow fixture access" if verdict == Verdict.NOT_MEASURABLE else None,
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> CheckRegistry:
    """Exactly three checks; each test can replace their outcomes."""
    reg = CheckRegistry()
    for index in range(3):
        reg.register(FixtureCheck(_finding(index, Verdict.MEASURED, True)))
    monkeypatch.setattr(audit, "DEFAULT_REGISTRY", reg)
    monkeypatch.setattr(audit, "populate_default_checks", lambda reg: None)
    return reg


@pytest.mark.parametrize(
    ("verdict", "passed", "strict", "expected"),
    [
        (Verdict.MEASURED, True, False, 0),
        (Verdict.MEASURED, True, True, 0),
        (Verdict.MEASURED, False, False, 1),
        (Verdict.MEASURED, False, True, 1),
        (Verdict.NOT_MEASURABLE, None, False, 0),
        (Verdict.NOT_MEASURABLE, None, True, 2),
        (Verdict.DECLARED, None, False, 0),
        (Verdict.DECLARED, False, True, 0),
        (Verdict.DECLARED, True, True, 0),
        (Verdict.PASS, None, True, 0),
        (Verdict.FAIL, None, False, 1),
    ],
)
@pytest.mark.parametrize("output_format", ["text", "json", "sarif"])
def test_exit_truth_table(registry, verdict, passed, strict, expected, output_format) -> None:
    registry.unregister("fixture:1")
    registry.register(FixtureCheck(_finding(1, verdict, passed)))
    args = ["--format", output_format]
    if strict:
        args.append("--strict")
    result = CliRunner().invoke(audit.govern_audit_cmd, args)
    assert "No such option" not in result.output
    assert result.exit_code == expected, result.output


def test_strict_incomplete_takes_precedence_over_failure(registry: CheckRegistry) -> None:
    registry.clear()
    for index, (verdict, passed) in enumerate(
        [(Verdict.MEASURED, False), (Verdict.NOT_MEASURABLE, None), (Verdict.DECLARED, None)]
    ):
        registry.register(FixtureCheck(_finding(index, verdict, passed)))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--strict"])
    assert "No such option" not in result.output
    assert result.exit_code == 2, result.output


def test_json_preserves_every_contract_field(registry: CheckRegistry) -> None:
    findings = [
        _finding(0, Verdict.MEASURED, False),
        _finding(1, Verdict.DECLARED),
        _finding(2, Verdict.NOT_MEASURABLE),
    ]
    registry.clear()
    for finding in findings:
        registry.register(FixtureCheck(finding))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "json"])
    assert result.exit_code == 1, result.output
    expected = [{**asdict(finding), "id": finding.id} for finding in findings]
    assert json.loads(result.output) == json.loads(json.dumps(expected))


@pytest.mark.parametrize("measured_verdict", [Verdict.MEASURED, Verdict.PASS, Verdict.FAIL])
def test_sarif_carries_finding_ids_and_three_state_kind(registry, measured_verdict) -> None:
    findings = [
        _finding(0, measured_verdict, measured_verdict != Verdict.FAIL),
        _finding(1, Verdict.DECLARED),
        _finding(2, Verdict.NOT_MEASURABLE),
    ]
    registry.clear()
    for finding in findings:
        registry.register(FixtureCheck(finding))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "sarif"])
    assert result.exit_code == (1 if measured_verdict == Verdict.FAIL else 0), result.output
    payload = json.loads(result.output)
    assert payload["version"] == "2.1.0"
    assert payload["$schema"].endswith("sarif-schema-2.1.0.json")
    run = payload["runs"][0]
    assert run["tool"]["driver"]["name"] == "bernstein govern audit"
    assert [row["ruleId"] for row in run["results"]] == [f.id for f in findings]
    assert [row["properties"]["kind"] for row in run["results"]] == [
        "measured",
        "declared",
        "not_measurable",
    ]
    for row, finding in zip(run["results"], findings, strict=True):
        assert row["message"]["text"] == finding.summary
        assert row["properties"]["passed"] == finding.passed


@pytest.mark.parametrize("output_format", ["text", "json", "sarif"])
@pytest.mark.parametrize(
    "verdict,passed,expected",
    [
        (Verdict.MEASURED, True, 0),
        (Verdict.MEASURED, False, 1),
        (Verdict.NOT_MEASURABLE, None, 2),
    ],
)
def test_quiet_prints_nothing(registry, output_format, verdict, passed, expected) -> None:
    registry.unregister("fixture:1")
    registry.register(FixtureCheck(_finding(1, verdict, passed)))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", output_format, "--quiet", "--strict"])
    assert result.output == ""
    assert result.stderr == ""
    assert result.exit_code == expected


def test_quiet_list_prints_nothing(registry: CheckRegistry) -> None:
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--list", "--quiet"])
    assert result.output == ""
    assert result.exit_code == 0


def test_quiet_empty_selection_prints_nothing(registry: CheckRegistry) -> None:
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--only", "absent", "--quiet"])
    assert result.output == ""
    assert result.exit_code == 1
