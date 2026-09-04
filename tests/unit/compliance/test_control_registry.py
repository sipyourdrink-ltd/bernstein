"""
Unit tests for the Compliance Control Registry (Issue #5455).

Acceptance criteria covered:
1. Registry contains >= 30 controls covering existing maps (EU AI Act, OWASP ASI, OWASP Skills, ISO 42001, FINOS).
2. Every suite declares >= 1 valid control ID; unmapped suite or unknown control ID fails validation.
3. CLI command `bernstein compliance controls` renders coverage table and JSON.
4. Generated doc table in docs/compliance/regulator-mapped-packs.md is kept in sync with the registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.compliance.controls import (
    get_control,
    list_controls,
    validate_control_id,
    validate_suite_controls,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.suite import BenchSuite, BenchTask


class TestControlRegistryPopulation:
    """AC-1: >= 30 controls covering the existing maps."""

    def test_registry_contains_at_least_30_controls(self) -> None:
        controls = list_controls()
        assert len(controls) >= 30, f"Expected at least 30 controls, got {len(controls)}"

    def test_controls_cover_major_frameworks(self) -> None:
        controls = list_controls()
        frameworks_found: set[str] = set()
        for c in controls:
            frameworks_found.update(c.references.keys())

        assert "eu_ai_act" in frameworks_found
        assert "owasp_asi" in frameworks_found
        assert "owasp_skills" in frameworks_found
        assert "iso42001" in frameworks_found
        assert "finos_aigf" in frameworks_found

    def test_get_control_by_id(self) -> None:
        ctrl = get_control("CTRL-AUDIT-TRAIL")
        assert ctrl is not None
        assert ctrl.control_id == "CTRL-AUDIT-TRAIL"
        assert "eu_ai_act" in ctrl.references
        assert "finos_aigf" in ctrl.references

    def test_unknown_control_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="NON_EXISTENT_CONTROL"):
            get_control("NON_EXISTENT_CONTROL")

    def test_validate_control_id(self) -> None:
        assert validate_control_id("CTRL-AUDIT-TRAIL") is True
        assert validate_control_id("ASI01") is True
        assert validate_control_id("AST01") is True
        assert validate_control_id("UNKNOWN_123") is False


class TestSuiteControlsValidation:
    """AC-2: Every suite declares >= 1 control; test enforces it."""

    def test_golden_suite_declares_valid_controls(self) -> None:
        suite = build_golden_suite_v1()
        assert len(suite.controls) >= 1
        for cid in suite.controls:
            assert validate_control_id(cid), f"Invalid control ID {cid} in golden suite"

    def test_validate_suite_controls_passes_on_valid_suite(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[BenchTask(id="t1", description="d", steps=(), assertions=())],
            controls=["CTRL-AUDIT-TRAIL", "ASI01"],
        )
        errors = validate_suite_controls(suite)
        assert errors == []

    def test_suite_with_empty_controls_fails_validation(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[BenchTask(id="t1", description="d", steps=(), assertions=())],
            controls=[],
        )
        errors = validate_suite_controls(suite)
        assert len(errors) > 0
        assert any("declares no controls" in e for e in errors)

    def test_suite_with_unknown_control_fails_validation(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[BenchTask(id="t1", description="d", steps=(), assertions=())],
            controls=["CTRL-AUDIT-TRAIL", "FAKE-CONTROL-999"],
        )
        errors = validate_suite_controls(suite)
        assert len(errors) > 0
        assert any("FAKE-CONTROL-999" in e for e in errors)

    def test_suite_serialization_preserves_controls(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[BenchTask(id="t1", description="d", steps=(), assertions=())],
            controls=["CTRL-AUDIT-TRAIL", "ASI02"],
        )
        d = suite.to_dict()
        assert d["controls"] == ["CTRL-AUDIT-TRAIL", "ASI02"]
        restored = BenchSuite.from_dict(d)
        assert restored.controls == ["CTRL-AUDIT-TRAIL", "ASI02"]


class TestComplianceControlsCLI:
    """AC-3: CLI coverage table."""

    def test_cli_controls_table_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls"])
        assert result.exit_code == 0, result.output
        assert "CTRL-AUDIT-TRAIL" in result.output
        assert "Control ID" in result.output

    def test_cli_controls_json_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--json-output"])
        assert result.exit_code == 0, result.output
        import json

        data = json.loads(result.output)
        assert "controls" in data
        assert len(data["controls"]) >= 30

    def test_cli_controls_filter_by_framework(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--framework", "owasp_asi"])
        assert result.exit_code == 0, result.output
        assert "ASI01" in result.output


class TestGeneratedDocsIntegrity:
    """AC-4: Generated doc table in CI."""

    def test_docs_contain_generated_controls_table(self) -> None:
        repo_root = Path(__file__).parents[3]
        doc_path = repo_root / "docs" / "compliance" / "regulator-mapped-packs.md"
        assert doc_path.exists(), f"Doc file missing: {doc_path}"
        content = doc_path.read_text(encoding="utf-8")
        assert "## Control registry" in content
        assert "CTRL-AUDIT-TRAIL" in content
