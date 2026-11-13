"""Machine-readable governance audit output and CI exit codes (#5076)."""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from click.testing import CliRunner
from jsonschema import Draft7Validator, FormatChecker, ValidationError

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
        (Verdict.MEASURED, None, False, 1),
        (Verdict.MEASURED, None, True, 1),
        (Verdict.NOT_MEASURABLE, None, False, 1),
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


def test_failure_takes_precedence_over_strict_incomplete(registry: CheckRegistry) -> None:
    registry.clear()
    for index, (verdict, passed) in enumerate(
        [(Verdict.MEASURED, False), (Verdict.NOT_MEASURABLE, None), (Verdict.DECLARED, None)]
    ):
        registry.register(FixtureCheck(_finding(index, verdict, passed)))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--strict"])
    assert "No such option" not in result.output
    assert result.exit_code == 1, result.output


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
    assert result.exit_code == 1, result.output
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
        expected_message = finding.summary
        if finding.verdict == Verdict.NOT_MEASURABLE:
            expected_message = f"{expected_message}: {finding.reason}"
        assert row["message"]["text"] == expected_message
        assert row["level"] == "none" or row["kind"] == "fail"
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


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("output_format", ["text", "json", "sarif"])
def test_crashing_check_fails_closed_and_quiet_suppresses_warning(
    registry: CheckRegistry, caplog: pytest.LogCaptureFixture, strict: bool, quiet: bool, output_format: str
) -> None:
    # A real stderr handler catches warnings even when pytest captures logging.
    handler = logging.StreamHandler(sys.stderr)
    logger = logging.getLogger("bernstein.core.checks.registry")

    class CrashingCheck:
        check_id = "fixture:crash"

        def run(self, workdir: Path | None = None) -> Finding:
            handler.setStream(sys.stderr)
            raise RuntimeError("fixture crash")

    registry.register(CrashingCheck())
    args = ["--format", output_format]
    if strict:
        args.append("--strict")
    if quiet:
        args.append("--quiet")
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger=logger.name):
            result = CliRunner().invoke(audit.govern_audit_cmd, args)
    finally:
        logger.removeHandler(handler)
    assert result.exit_code == 1, result.output
    if quiet:
        assert result.stdout == result.stderr == ""
        assert "fixture crash" not in caplog.text
    else:
        assert "fixture crash" in caplog.text
        assert "fixture crash" in result.stderr
    if output_format == "json" and not quiet:
        rows = json.loads(result.stdout)
        assert len(rows) == 4
        assert rows[-1]["reason"] == "RuntimeError"


@pytest.mark.parametrize(
    "args",
    [
        ["--format", "xml"],
        ["--unknown"],
        ["--workdir", "/path/that/does/not/exist"],
        ["--json", "--format", "sarif"],
        ["--json-output", "--format", "sarif"],
        ["--list", "--format", "sarif"],
    ],
)
def test_usage_errors_share_strict_incomplete_exit_code(registry: CheckRegistry, args: list[str]) -> None:
    from bernstein.cli.main import cli

    result = CliRunner().invoke(cli, ["govern", "audit", *args])
    assert result.exit_code == 2, result.output
    assert "Error:" in result.stderr


def test_registry_reports_invalid_return_as_execution_error(registry: CheckRegistry) -> None:
    class InvalidCheck:
        check_id = "fixture:invalid"

        def run(self, workdir: Path | None = None) -> Finding:
            return {}  # type: ignore[return-value]

    registry.register(InvalidCheck())
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--strict", "--quiet"])
    assert result.exit_code == 1
    assert result.stdout == result.stderr == ""


def _validate_sarif(payload: dict) -> None:
    schema_path = Path(__file__).parents[2] / "fixtures/sarif/govern-audit-2.1.0.schema.json"
    schema = json.loads(schema_path.read_text())
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema, format_checker=FormatChecker()).validate(payload)


@pytest.mark.parametrize("empty", [False, True])
def test_sarif_validates_offline_and_links_rules_and_locations(
    registry: CheckRegistry, tmp_path: Path, empty: bool
) -> None:
    registry.clear()
    if not empty:
        for index, verdict in enumerate(Verdict):
            registry.register(FixtureCheck(_finding(index, verdict, False if verdict == Verdict.MEASURED else None)))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "sarif", "-w", str(tmp_path)])
    payload = json.loads(result.stdout)
    _validate_sarif(payload)
    run = payload["runs"][0]
    rules = run["tool"]["driver"]["rules"]
    assert len(rules) == len(list(Verdict)) if not empty else len(rules) == 0
    for row in run["results"]:
        # SARIF 2.1.0 section 3.27.9 constrains kind/level beyond the schema.
        assert row["level"] == "none" or row["kind"] == "fail"
        assert rules[row["ruleIndex"]]["id"] == row["ruleId"]
        assert row["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "bernstein.yaml"
        verdict = row["properties"]["verdict"]
        if verdict == "pass":
            assert (row["kind"], row["level"]) == ("pass", "none")
        elif verdict == "not_measurable":
            assert (row["kind"], row["level"]) == ("review", "none")
            assert "Missing permission" in row["message"]["text"]
        elif verdict in ("fail", "measured"):
            assert (row["kind"], row["level"]) == ("fail", "error")
        elif verdict == "declared":
            assert (row["kind"], row["level"]) == ("informational", "none")


@pytest.mark.parametrize("broken", ["missing_location", "invalid_level", "missing_rules", "invalid_message"])
def test_offline_sarif_validation_rejects_invalid_output(registry: CheckRegistry, broken: str) -> None:
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "sarif"])
    payload = json.loads(result.stdout)
    run = payload["runs"][0]
    if broken == "missing_location":
        run["results"][0]["locations"] = []
    elif broken == "invalid_level":
        run["results"][0]["level"] = "critical"
    elif broken == "missing_rules":
        del run["tool"]["driver"]["rules"]
    else:
        run["results"][0]["message"] = {}
    with pytest.raises(ValidationError):
        _validate_sarif(payload)


@pytest.mark.parametrize(
    "locator_kind",
    ["relative", "absolute", "uri", "opaque", "workspace", "outside", "outside_uri", "traversal", "symlink"],
)
def test_sarif_uses_reported_file_or_evaluated_config(
    registry: CheckRegistry, tmp_path: Path, locator_kind: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "bernstein.yml").write_text("{}")
    file = root / "src" / "a file.py"
    file.parent.mkdir()
    file.write_text("# Evidence\n")
    outside = tmp_path / "outside.py"
    outside.write_text("# External evidence\n")
    (root / "link.py").symlink_to(outside)
    locators = {
        "relative": "src/a file.py",
        "absolute": str(file),
        "uri": file.as_uri(),
        "opaque": "env://SETTING",
        "workspace": f"doctor:compliance:{root}",
        "outside": str(outside),
        "outside_uri": outside.as_uri(),
        "traversal": "../outside.py",
        "symlink": "link.py",
    }
    finding = Finding(
        check_id="fixture:location",
        verdict=Verdict.FAIL,
        evidence=(Evidence(locator=locators[locator_kind], sha256="a" * 64),),
    )
    registry.clear()
    registry.register(FixtureCheck(finding))
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "sarif", "-w", str(root)])
    payload = json.loads(result.stdout)
    _validate_sarif(payload)
    uri = payload["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    if locator_kind in ("opaque", "workspace", "outside", "outside_uri", "traversal", "symlink"):
        assert uri == "bernstein.yml"
    else:
        assert uri == "src/a%20file.py"
    assert str(tmp_path) not in result.stdout
    assert "file:///" not in result.stdout
    assert payload["runs"][0]["results"][0]["properties"]["evidence"] == [{"sha256": "a" * 64}]


@pytest.mark.parametrize(
    "local_path", ["/Users/owner/private.py", "file:///tmp/private.py", r"C:\Users\owner\private.py"]
)
def test_sarif_redacts_local_paths_in_diagnostics(registry: CheckRegistry, local_path: str) -> None:
    registry.clear()
    registry.register(
        FixtureCheck(
            Finding(
                check_id="fixture:private",
                verdict=Verdict.NOT_MEASURABLE,
                message=f"Cannot read {local_path}",
                reason=f"Permission denied: {local_path}",
                remediation=f"Grant access to {local_path}",
            )
        )
    )
    result = CliRunner().invoke(audit.govern_audit_cmd, ["--format", "sarif"])
    payload = json.loads(result.stdout)
    _validate_sarif(payload)
    assert local_path not in json.dumps(payload, ensure_ascii=False).replace("\\\\", "\\")
    assert "[local path]" in payload["runs"][0]["results"][0]["message"]["text"]
