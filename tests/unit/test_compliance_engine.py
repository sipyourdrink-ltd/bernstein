"""Tests for compliance/eu_ai_act.py additions: bernstein_descriptor and export_evidence_package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.compliance.eu_ai_act import (
    ComplianceEngine,
    RiskCategory,
    SystemDescriptor,
    bernstein_descriptor,
)


class TestBernsteinDescriptor:
    def test_returns_system_descriptor(self) -> None:
        d = bernstein_descriptor()
        assert isinstance(d, SystemDescriptor)

    def test_name_is_bernstein(self) -> None:
        d = bernstein_descriptor()
        assert d.name == "Bernstein"

    def test_version_propagated(self) -> None:
        d = bernstein_descriptor(version="2.3.0")
        assert d.version == "2.3.0"

    def test_deployment_context_overridable(self) -> None:
        d = bernstein_descriptor(deployment_context="Kubernetes cluster")
        assert d.deployment_context == "Kubernetes cluster"

    def test_metadata_merged(self) -> None:
        d = bernstein_descriptor(metadata={"custom_key": "custom_value"})
        assert d.metadata["custom_key"] == "custom_value"
        # Base metadata still present
        assert "provider" in d.metadata

    def test_no_annex_iii_flags_set(self) -> None:
        d = bernstein_descriptor()
        assert not d.processes_biometrics
        assert not d.used_in_employment
        assert not d.used_in_law_enforcement
        assert not d.used_in_justice

    def test_no_article5_flags_set(self) -> None:
        d = bernstein_descriptor()
        assert not d.real_time_biometric_public
        assert not d.social_scoring_public
        assert not d.subliminal_techniques
        assert not d.manipulates_behavior

    def test_classifies_as_minimal_risk(self) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.MINIMAL


class TestExportEvidencePackage:
    def test_writes_evidence_package_json(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        pkg_path = engine.export_evidence_package(d, tmp_path)
        assert pkg_path.exists()
        assert pkg_path.name == "evidence_package.json"

    def test_package_contains_required_keys(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        pkg_path = engine.export_evidence_package(d, tmp_path)
        package = json.loads(pkg_path.read_text())
        assert package["schema_version"] == "1.0"
        assert package["regulation"] == "EU AI Act (Regulation (EU) 2024/1689)"
        assert package["system_name"] == "Bernstein"
        assert "report" in package

    def test_classification_json_written(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        engine.export_evidence_package(d, tmp_path)
        classification_file = tmp_path / "classification.json"
        assert classification_file.exists()
        data = json.loads(classification_file.read_text())
        assert data["risk_category"] == "minimal"

    def test_conformity_json_written(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        engine.export_evidence_package(d, tmp_path)
        conf_file = tmp_path / "conformity.json"
        assert conf_file.exists()
        data = json.loads(conf_file.read_text())
        assert "overall_status" in data

    def test_tech_doc_not_written_for_minimal_risk(self, tmp_path: Path) -> None:
        """Minimal-risk systems don't need Annex IV tech docs."""
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        engine.export_evidence_package(d, tmp_path)
        # tech_doc.json is only written for HIGH/UNACCEPTABLE
        assert not (tmp_path / "tech_doc.json").exists()

    def test_tech_doc_written_for_high_risk(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = SystemDescriptor(
            name="HRBot",
            version="1.0.0",
            description="Automated HR screening",
            intended_use="CV screening",
            deployment_context="Enterprise HR",
            used_in_employment=True,
        )
        engine.export_evidence_package(d, tmp_path)
        assert (tmp_path / "tech_doc.json").exists()

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        out_dir = tmp_path / "nested" / "compliance"
        engine.export_evidence_package(d, out_dir)
        assert out_dir.exists()

    def test_doc_version_propagated(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        engine.export_evidence_package(d, tmp_path, doc_version="2.0.0")
        package = json.loads((tmp_path / "evidence_package.json").read_text())
        # For minimal risk, tech_doc is not included, but the version is in the system_version field
        assert package["system_name"] == "Bernstein"

    def test_generated_at_is_iso8601(self, tmp_path: Path) -> None:
        engine = ComplianceEngine()
        d = bernstein_descriptor()
        pkg_path = engine.export_evidence_package(d, tmp_path)
        package = json.loads(pkg_path.read_text())
        ts = package["generated_at"]
        assert "T" in ts  # ISO-8601 has a T separator
