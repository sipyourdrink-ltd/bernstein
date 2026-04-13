"""Tests for the compliance presets module.

Tests cover:
- CompliancePreset enum values
- ComplianceConfig.from_preset() for all three tiers
- ComplianceConfig.from_dict() for string and mapping inputs
- Feature escalation: DEVELOPMENT < STANDARD < REGULATED
- Prerequisite checks and warnings
- AI content labeling by file extension
- SBOM generation (CycloneDX format)
- Evidence bundle export
- Persistence (save/load round-trip)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.compliance import (
    ComplianceConfig,
    CompliancePreset,
    SBOMEntry,
    ai_label_for_file,
    export_evidence_bundle,
    generate_sbom,
    load_compliance_config,
    persist_compliance_config,
)

# ---------------------------------------------------------------------------
# CompliancePreset enum
# ---------------------------------------------------------------------------


class TestCompliancePreset:
    def test_enum_values(self) -> None:
        assert CompliancePreset.DEVELOPMENT.value == "development"
        assert CompliancePreset.STANDARD.value == "standard"
        assert CompliancePreset.REGULATED.value == "regulated"

    def test_from_string(self) -> None:
        assert CompliancePreset("development") == CompliancePreset.DEVELOPMENT
        assert CompliancePreset("standard") == CompliancePreset.STANDARD
        assert CompliancePreset("regulated") == CompliancePreset.REGULATED

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            CompliancePreset("invalid")


# ---------------------------------------------------------------------------
# ComplianceConfig.from_preset()
# ---------------------------------------------------------------------------


class TestFromPreset:
    def test_development_preset(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.DEVELOPMENT)
        assert cfg.preset == CompliancePreset.DEVELOPMENT
        assert cfg.audit_logging is True
        assert cfg.wal_enabled is True
        assert cfg.ai_content_labels is True
        # Features NOT in development:
        assert cfg.audit_hmac_chain is False
        assert cfg.governed_workflow is False
        assert cfg.approval_gates is False
        assert cfg.wal_signed is False
        assert cfg.data_residency is False
        assert cfg.sbom_enabled is False
        assert cfg.evidence_bundle is False
        assert cfg.mandatory_human_review is False
        assert cfg.execution_fingerprint is False

    def test_standard_preset(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.STANDARD)
        assert cfg.preset == CompliancePreset.STANDARD
        # All of development:
        assert cfg.audit_logging is True
        assert cfg.wal_enabled is True
        assert cfg.ai_content_labels is True
        # Plus standard features:
        assert cfg.audit_hmac_chain is True
        assert cfg.governed_workflow is True
        assert cfg.approval_gates is True
        assert cfg.execution_fingerprint is True
        # Not in standard:
        assert cfg.wal_signed is False
        assert cfg.data_residency is False
        assert cfg.sbom_enabled is False
        assert cfg.evidence_bundle is False
        assert cfg.mandatory_human_review is False

    def test_regulated_preset(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.REGULATED)
        assert cfg.preset == CompliancePreset.REGULATED
        # All of standard:
        assert cfg.audit_logging is True
        assert cfg.audit_hmac_chain is True
        assert cfg.wal_enabled is True
        assert cfg.ai_content_labels is True
        assert cfg.governed_workflow is True
        assert cfg.approval_gates is True
        assert cfg.execution_fingerprint is True
        # Plus regulated features:
        assert cfg.wal_signed is True
        assert cfg.data_residency is True
        assert cfg.data_residency_region == "eu"
        assert cfg.sbom_enabled is True
        assert cfg.evidence_bundle is True
        assert cfg.mandatory_human_review is True

    def test_escalation_order(self) -> None:
        """Each tier is a strict superset of the previous tier's True fields."""
        dev = ComplianceConfig.from_preset(CompliancePreset.DEVELOPMENT)
        std = ComplianceConfig.from_preset(CompliancePreset.STANDARD)
        reg = ComplianceConfig.from_preset(CompliancePreset.REGULATED)

        def _true_fields(cfg: ComplianceConfig) -> set[str]:
            return {k for k, v in cfg.to_dict().items() if v is True}

        dev_fields = _true_fields(dev)
        std_fields = _true_fields(std)
        reg_fields = _true_fields(reg)

        assert dev_fields < std_fields, "standard must be a superset of development"
        assert std_fields < reg_fields, "regulated must be a superset of standard"


# ---------------------------------------------------------------------------
# ComplianceConfig.from_dict()
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_from_string(self) -> None:
        cfg = ComplianceConfig.from_dict("standard")
        assert cfg.preset == CompliancePreset.STANDARD
        assert cfg.governed_workflow is True

    def test_from_dict_with_preset(self) -> None:
        cfg = ComplianceConfig.from_dict({"preset": "development", "sbom_enabled": True})
        assert cfg.preset == CompliancePreset.DEVELOPMENT
        assert cfg.audit_logging is True  # from preset
        assert cfg.sbom_enabled is True  # overridden

    def test_from_dict_without_preset(self) -> None:
        cfg = ComplianceConfig.from_dict({"audit_logging": True, "wal_enabled": True})
        assert cfg.preset is None
        assert cfg.audit_logging is True
        assert cfg.wal_enabled is True
        assert cfg.governed_workflow is False

    def test_from_dict_override_label_format(self) -> None:
        cfg = ComplianceConfig.from_dict(
            {
                "preset": "development",
                "ai_label_format": "# AI-generated (custom)",
            }
        )
        assert cfg.ai_label_format == "# AI-generated (custom)"


# ---------------------------------------------------------------------------
# Prerequisite checks
# ---------------------------------------------------------------------------


class TestPrerequisites:
    def test_no_warnings_for_valid_presets(self) -> None:
        for preset in CompliancePreset:
            cfg = ComplianceConfig.from_preset(preset)
            assert cfg.check_prerequisites() == [], f"preset {preset.value} should have no warnings"

    def test_hmac_without_audit_warns(self) -> None:
        cfg = ComplianceConfig(audit_hmac_chain=True, audit_logging=False)
        warnings = cfg.check_prerequisites()
        assert any("HMAC" in w for w in warnings)

    def test_signed_wal_without_wal_warns(self) -> None:
        cfg = ComplianceConfig(wal_signed=True, wal_enabled=False)
        warnings = cfg.check_prerequisites()
        assert any("Signed WAL" in w for w in warnings)

    def test_data_residency_without_region_warns(self) -> None:
        cfg = ComplianceConfig(data_residency=True, data_residency_region="")
        warnings = cfg.check_prerequisites()
        assert any("region" in w.lower() for w in warnings)

    def test_mandatory_review_without_approval_warns(self) -> None:
        cfg = ComplianceConfig(mandatory_human_review=True, approval_gates=False)
        warnings = cfg.check_prerequisites()
        assert any("approval_gates" in w for w in warnings)

    def test_evidence_without_wal_warns(self) -> None:
        cfg = ComplianceConfig(evidence_bundle=True, wal_enabled=False, audit_logging=False)
        warnings = cfg.check_prerequisites()
        assert any("WAL" in w for w in warnings)
        assert any("audit_logging" in w for w in warnings)


# ---------------------------------------------------------------------------
# to_dict / round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict_includes_all_fields(self) -> None:
        cfg = ComplianceConfig.from_preset(CompliancePreset.STANDARD)
        d = cfg.to_dict()
        assert d["preset"] == "standard"
        assert d["governed_workflow"] is True
        assert d["audit_hmac_chain"] is True
        assert isinstance(d["ai_label_format"], str)

    def test_to_dict_none_preset(self) -> None:
        cfg = ComplianceConfig()
        d = cfg.to_dict()
        assert d["preset"] is None


# ---------------------------------------------------------------------------
# AI content labeling
# ---------------------------------------------------------------------------


class TestAILabeling:
    def test_python_label(self) -> None:
        result = ai_label_for_file(Path("main.py"), "Generated by AI agent (Bernstein)")
        assert result == "# Generated by AI agent (Bernstein)"

    def test_javascript_label(self) -> None:
        result = ai_label_for_file(Path("app.js"), "Generated by AI agent (Bernstein)")
        assert result == "// Generated by AI agent (Bernstein)"

    def test_css_label(self) -> None:
        result = ai_label_for_file(Path("style.css"), "Generated by AI agent (Bernstein)")
        assert result == "/* Generated by AI agent (Bernstein) */"

    def test_html_label(self) -> None:
        result = ai_label_for_file(Path("index.html"), "Generated by AI agent (Bernstein)")
        assert result == "<!-- Generated by AI agent (Bernstein) -->"

    def test_sql_label(self) -> None:
        result = ai_label_for_file(Path("schema.sql"), "Generated by AI agent (Bernstein)")
        assert result == "-- Generated by AI agent (Bernstein)"

    def test_unknown_extension_returns_none(self) -> None:
        result = ai_label_for_file(Path("image.png"), "Generated by AI agent (Bernstein)")
        assert result is None

    def test_go_label(self) -> None:
        result = ai_label_for_file(Path("main.go"), "Generated by AI agent (Bernstein)")
        assert result == "// Generated by AI agent (Bernstein)"

    def test_rust_label(self) -> None:
        result = ai_label_for_file(Path("lib.rs"), "Generated by AI agent (Bernstein)")
        assert result == "// Generated by AI agent (Bernstein)"


# ---------------------------------------------------------------------------
# SBOM generation
# ---------------------------------------------------------------------------


class TestSBOM:
    def test_generates_cyclonedx_json(self, tmp_path: Path) -> None:
        components = [
            SBOMEntry(name="requests", version="2.31.0", purl="pkg:pypi/requests@2.31.0"),
            SBOMEntry(name="flask", version="3.0.0", purl="pkg:pypi/flask@3.0.0"),
        ]
        result = generate_sbom(components, "run-001", tmp_path)
        assert result.exists()
        data = json.loads(result.read_text())
        assert data["bomFormat"] == "CycloneDX"
        assert data["specVersion"] == "1.5"
        assert len(data["components"]) == 2
        assert data["components"][0]["name"] == "requests"
        assert data["components"][0]["purl"] == "pkg:pypi/requests@2.31.0"

    def test_empty_components(self, tmp_path: Path) -> None:
        result = generate_sbom([], "run-002", tmp_path)
        data = json.loads(result.read_text())
        assert data["components"] == []

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "sbom"
        generate_sbom([], "run-003", out)
        assert out.exists()


# ---------------------------------------------------------------------------
# Evidence bundle export
# ---------------------------------------------------------------------------


class TestEvidenceBundle:
    def test_export_creates_bundle_dir(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        (sdd / "audit").mkdir(parents=True)
        (sdd / "audit" / "events.jsonl").write_text('{"event": "test"}\n')
        (sdd / "runtime" / "wal").mkdir(parents=True)
        (sdd / "runtime" / "wal" / "run-001.wal.jsonl").write_text('{"seq": 0}\n')

        bundle = export_evidence_bundle("run-001", sdd)
        assert bundle.exists()
        assert (bundle / "manifest.json").exists()
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["run_id"] == "run-001"
        assert len(manifest["artifacts"]) >= 1

    def test_export_handles_missing_artifacts(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        bundle = export_evidence_bundle("run-002", sdd)
        assert bundle.exists()
        manifest = json.loads((bundle / "manifest.json").read_text())
        assert manifest["artifacts"] == []


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        cfg = ComplianceConfig.from_preset(CompliancePreset.STANDARD)
        persist_compliance_config(cfg, sdd)

        loaded = load_compliance_config(sdd)
        assert loaded is not None
        assert loaded.preset == CompliancePreset.STANDARD
        assert loaded.governed_workflow is True
        assert loaded.audit_hmac_chain is True

    def test_load_returns_none_when_missing(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        assert load_compliance_config(sdd) is None

    def test_load_returns_none_on_corrupt_file(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        config_dir = sdd / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "compliance.json").write_text("not valid json{{{")
        assert load_compliance_config(sdd) is None
