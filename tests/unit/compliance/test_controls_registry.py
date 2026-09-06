"""
TDD tests for compliance control registry and suite control declarations (Issue #5455).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.compliance.controls import (
    get_default_registry,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.suite import BenchSuite, BenchTask


class TestControlRegistry:
    def test_registry_contains_at_least_30_controls(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        assert len(controls) >= 30

    def test_controls_cover_all_mandated_frameworks(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        frameworks_present = set()
        for c in controls:
            assert c.control_id.startswith("CTL-")
            assert len(c.title) > 0
            assert len(c.description) > 0
            assert len(c.evidence_kinds) > 0
            for fw in c.references:
                frameworks_present.add(fw)

        expected_frameworks = {
            "eu_ai_act",
            "owasp_asi",
            "owasp_skills",
            "nist_ai_rmf",
            "iso_42001",
            "finos_aigf",
        }
        for fw in expected_frameworks:
            assert fw in frameworks_present, f"Framework {fw} missing from registry"

    def test_control_lookup(self) -> None:
        registry = get_default_registry()
        c = registry.get("CTL-GOV-01")
        assert c is not None
        assert c.control_id == "CTL-GOV-01"
        assert "eu_ai_act" in c.references
        assert "audit_chain" in c.evidence_kinds or "policy" in c.evidence_kinds or len(c.evidence_kinds) > 0

    def test_filter_by_framework(self) -> None:
        registry = get_default_registry()
        eu_controls = registry.list_controls(framework="eu_ai_act")
        assert len(eu_controls) > 0
        for c in eu_controls:
            assert "eu_ai_act" in c.references

    def test_validate_control_ids(self) -> None:
        registry = get_default_registry()
        assert registry.validate_control_ids(["CTL-GOV-01", "CTL-ROB-01"]) == []
        invalid = registry.validate_control_ids(["CTL-GOV-01", "INVALID-99", "NONEXISTENT"])
        assert invalid == ["INVALID-99", "NONEXISTENT"]

    def test_markdown_table_generation(self) -> None:
        registry = get_default_registry()
        md = registry.to_markdown_table()
        assert "| Control ID | Title | Frameworks | Evidence Kinds |" in md
        assert "CTL-GOV-01" in md


class TestBenchSuiteControlEnforcement:
    def test_golden_suite_declares_valid_controls(self) -> None:
        suite = build_golden_suite_v1()
        assert len(suite.controls) > 0
        suite.validate_controls()

    def test_suite_without_controls_fails_validation(self) -> None:
        suite = BenchSuite(
            version="unmapped-v1",
            tasks=[BenchTask(id="t1", description="t1", steps=("s1",), assertions=())],
            controls=[],
        )
        with pytest.raises(ValueError, match="must declare at least one control ID"):
            suite.validate_controls()

    def test_suite_with_unregistered_control_fails_validation(self) -> None:
        suite = BenchSuite(
            version="bad-control-v1",
            tasks=[BenchTask(id="t1", description="t1", steps=("s1",), assertions=())],
            controls=["CTL-NON-EXISTENT-XYZ"],
        )
        with pytest.raises(ValueError, match="unregistered control IDs"):
            suite.validate_controls()

    def test_suite_hash_includes_controls(self) -> None:
        t = BenchTask(id="t1", description="t1", steps=("s1",), assertions=())
        suite1 = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01"])
        suite2 = BenchSuite(version="v1", tasks=[t], controls=["CTL-ROB-01"])
        suite3 = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01"])

        assert suite1.suite_hash == suite3.suite_hash
        assert suite1.suite_hash != suite2.suite_hash

    def test_suite_save_and_load_roundtrip_with_controls(self, tmp_path: Path) -> None:
        t = BenchTask(id="t1", description="t1", steps=("s1",), assertions=())
        suite = BenchSuite(version="v1", tasks=[t], controls=["CTL-GOV-01", "CTL-ROB-01"])
        suite_file = tmp_path / "suite.json"
        suite.save(suite_file)

        loaded = BenchSuite.load(suite_file)
        assert loaded.controls == ["CTL-GOV-01", "CTL-ROB-01"]
        assert loaded.suite_hash == suite.suite_hash


class TestComplianceControlsCLI:
    def test_compliance_controls_text(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls"])
        assert result.exit_code == 0
        assert "CTL-GOV-01" in result.output
        assert "Control ID" in result.output or "Title" in result.output

    def test_compliance_controls_json(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 30
        assert any(c["control_id"] == "CTL-GOV-01" for c in data)

    def test_compliance_controls_framework_filter(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--framework", "eu_ai_act", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0
        for c in data:
            assert "eu_ai_act" in c["references"]

    def test_compliance_controls_coverage(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--coverage"])
        assert result.exit_code == 0
        assert "golden-v1" in result.output or "Coverage" in result.output
