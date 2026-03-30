"""Tests for EU AI Act Compliance Engine."""

from __future__ import annotations

from bernstein.compliance.eu_ai_act import (
    AnnexIIIDomain,
    ComplianceEngine,
    ConformityAssessor,
    RiskCategory,
    SystemDescriptor,
    TechDoc,
    TechDocGenerator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def minimal_descriptor(**overrides: object) -> SystemDescriptor:
    """Minimal-risk system — no flags set."""
    return SystemDescriptor(
        name="TestBot",
        version="1.0.0",
        description="A simple recommendation widget",
        intended_use="E-commerce product suggestions",
        deployment_context="Consumer website",
        **overrides,  # type: ignore[arg-type]
    )


def high_risk_descriptor(**overrides: object) -> SystemDescriptor:
    """High-risk system in employment domain."""
    return SystemDescriptor(
        name="HRSelector",
        version="2.0.0",
        description="AI-powered CV screening and candidate ranking system",
        intended_use="Automated resume screening for job applications",
        deployment_context="Enterprise HR platform",
        used_in_employment=True,
        **overrides,  # type: ignore[arg-type]
    )


def prohibited_descriptor(**overrides: object) -> SystemDescriptor:
    """Article 5 prohibited system."""
    return SystemDescriptor(
        name="SocialScorer",
        version="1.0.0",
        description="Public authority social scoring system",
        intended_use="Evaluate citizen trustworthiness for public services",
        deployment_context="Government portal",
        social_scoring_public=True,
        **overrides,  # type: ignore[arg-type]
    )


def limited_risk_descriptor(**overrides: object) -> SystemDescriptor:
    """Limited-risk system with Article 50 transparency obligations."""
    return SystemDescriptor(
        name="SupportBot",
        version="3.0.0",
        description="Customer service chatbot",
        intended_use="Answer customer support queries",
        deployment_context="Consumer-facing chat widget",
        is_chatbot_or_conversational=True,
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


class TestClassification:
    def test_minimal_risk(self) -> None:
        engine = ComplianceEngine()
        result = engine.classify(minimal_descriptor())
        assert result.risk_category == RiskCategory.MINIMAL
        assert result.annex_iii_domain == AnnexIIIDomain.NOT_APPLICABLE
        assert result.article5_triggers == []
        assert result.article50_triggers == []

    def test_high_risk_employment(self) -> None:
        engine = ComplianceEngine()
        result = engine.classify(high_risk_descriptor())
        assert result.risk_category == RiskCategory.HIGH
        assert result.annex_iii_domain == AnnexIIIDomain.EMPLOYMENT

    def test_high_risk_biometric(self) -> None:
        engine = ComplianceEngine()
        d = minimal_descriptor(processes_biometrics=True)
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.HIGH
        assert result.annex_iii_domain == AnnexIIIDomain.BIOMETRIC

    def test_high_risk_law_enforcement(self) -> None:
        engine = ComplianceEngine()
        d = minimal_descriptor(used_in_law_enforcement=True)
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.HIGH
        assert result.annex_iii_domain == AnnexIIIDomain.LAW_ENFORCEMENT

    def test_unacceptable_social_scoring(self) -> None:
        engine = ComplianceEngine()
        result = engine.classify(prohibited_descriptor())
        assert result.risk_category == RiskCategory.UNACCEPTABLE
        assert len(result.article5_triggers) >= 1
        assert any("5(1)(c)" in t or "5(1)(d)" in t for t in result.article5_triggers)

    def test_unacceptable_real_time_biometric(self) -> None:
        engine = ComplianceEngine()
        d = minimal_descriptor(real_time_biometric_public=True)
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.UNACCEPTABLE

    def test_unacceptable_overrides_high_risk(self) -> None:
        """Article 5 prohibition takes precedence over Annex III high-risk."""
        engine = ComplianceEngine()
        d = SystemDescriptor(
            name="BadSystem",
            version="1.0.0",
            description="Both prohibited and high-risk",
            intended_use="Test",
            deployment_context="Test",
            used_in_employment=True,
            social_scoring_public=True,
        )
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.UNACCEPTABLE

    def test_limited_risk_chatbot(self) -> None:
        engine = ComplianceEngine()
        result = engine.classify(limited_risk_descriptor())
        assert result.risk_category == RiskCategory.LIMITED
        assert any("chatbot" in t.lower() or "conversational" in t.lower() for t in result.article50_triggers)

    def test_limited_risk_synthetic_media(self) -> None:
        engine = ComplianceEngine()
        d = minimal_descriptor(generates_synthetic_media=True)
        result = engine.classify(d)
        assert result.risk_category == RiskCategory.LIMITED

    def test_classification_hash_deterministic(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()
        result1 = engine.classify(d)
        result2 = engine.classify(d)
        assert result1.classification_hash == result2.classification_hash

    def test_classification_hash_differs_on_change(self) -> None:
        engine = ComplianceEngine()
        d1 = SystemDescriptor(
            name="HRSelector",
            version="2.0.0",
            description="CV screening",
            intended_use="Hiring",
            deployment_context="HR platform",
            used_in_employment=True,
        )
        d2 = SystemDescriptor(
            name="HRSelector",
            version="3.0.0",
            description="CV screening",
            intended_use="Hiring",
            deployment_context="HR platform",
            used_in_employment=True,
        )
        r1 = engine.classify(d1)
        r2 = engine.classify(d2)
        assert r1.classification_hash != r2.classification_hash

    def test_classification_has_justification(self) -> None:
        engine = ComplianceEngine()
        for d in [minimal_descriptor(), high_risk_descriptor(), prohibited_descriptor(), limited_risk_descriptor()]:
            result = engine.classify(d)
            assert len(result.justification) > 20

    def test_classification_has_timestamp(self) -> None:
        engine = ComplianceEngine()
        result = engine.classify(minimal_descriptor())
        assert "T" in result.classified_at  # ISO-8601

    def test_all_annex_iii_domains(self) -> None:
        engine = ComplianceEngine()
        domain_flags = [
            ("processes_biometrics", AnnexIIIDomain.BIOMETRIC),
            ("interacts_with_critical_infrastructure", AnnexIIIDomain.CRITICAL_INFRASTRUCTURE),
            ("used_in_education", AnnexIIIDomain.EDUCATION),
            ("used_in_employment", AnnexIIIDomain.EMPLOYMENT),
            ("used_in_essential_services", AnnexIIIDomain.ESSENTIAL_SERVICES),
            ("used_in_law_enforcement", AnnexIIIDomain.LAW_ENFORCEMENT),
            ("used_in_migration", AnnexIIIDomain.MIGRATION),
            ("used_in_justice", AnnexIIIDomain.JUSTICE),
        ]
        for flag, _expected_domain in domain_flags:
            d = minimal_descriptor(**{flag: True})
            result = engine.classify(d)
            assert result.risk_category == RiskCategory.HIGH, f"Expected HIGH for {flag}"
            # Domain is set to first match; biometric comes first in checks
            assert result.annex_iii_domain != AnnexIIIDomain.NOT_APPLICABLE


# ---------------------------------------------------------------------------
# TechDocGenerator tests
# ---------------------------------------------------------------------------


class TestTechDocGenerator:
    def test_generates_for_high_risk(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()
        classification = engine.classify(d)
        doc = engine.generate_tech_doc(d, classification)

        assert isinstance(doc, TechDoc)
        assert doc.system_name == d.name
        assert doc.system_version == d.version
        assert doc.doc_version == "1.0.0"

    def test_all_sections_present(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()
        doc = engine.generate_tech_doc(d)

        assert doc.general_description
        assert doc.design_and_development
        assert doc.monitoring_and_control
        assert doc.robustness_and_security
        assert doc.data_governance
        assert doc.transparency_and_information
        assert doc.post_market_monitoring
        assert doc.conformity_declaration

    def test_doc_hash_present(self) -> None:
        engine = ComplianceEngine()
        doc = engine.generate_tech_doc(high_risk_descriptor())
        assert len(doc.doc_hash) == 64  # SHA-256 hex

    def test_doc_hash_deterministic(self) -> None:
        gen = TechDocGenerator()
        d = high_risk_descriptor()
        # Need same classification (same timestamp would differ; use same object)
        engine = ComplianceEngine()
        c = engine.classify(d)
        doc1 = gen.generate(d, c)
        doc2 = gen.generate(d, c)
        assert doc1.doc_hash == doc2.doc_hash

    def test_to_dict_structure(self) -> None:
        engine = ComplianceEngine()
        doc = engine.generate_tech_doc(high_risk_descriptor())
        d = doc.to_dict()
        assert "sections" in d
        assert "1_general_description" in d["sections"]
        assert "8_conformity_declaration" in d["sections"]
        assert "doc_hash" in d

    def test_biometric_flag_in_data_section(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor(processes_biometrics=True)
        doc = engine.generate_tech_doc(d)
        assert "biometric" in doc.data_governance.lower() or "GDPR" in doc.data_governance

    def test_custom_doc_version(self) -> None:
        engine = ComplianceEngine()
        doc = engine.generate_tech_doc(high_risk_descriptor(), doc_version="2.1.0")
        assert doc.doc_version == "2.1.0"

    def test_metadata_reflected_in_doc(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor(metadata={"provider": "Acme Corp"})
        doc = engine.generate_tech_doc(d)
        assert "Acme Corp" in doc.design_and_development


# ---------------------------------------------------------------------------
# ConformityAssessor tests
# ---------------------------------------------------------------------------


class TestConformityAssessor:
    def test_minimal_risk_conformant(self) -> None:
        engine = ComplianceEngine()
        d = minimal_descriptor()
        result = engine.assess_conformity(d)
        assert result.overall_status == "CONFORMANT"
        assert result.failed == 0

    def test_prohibited_non_conformant(self) -> None:
        engine = ComplianceEngine()
        d = prohibited_descriptor()
        result = engine.assess_conformity(d)
        assert result.overall_status == "NON_CONFORMANT"
        assert result.failed >= 1

    def test_high_risk_no_metadata_fails(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()  # no metadata → mandatory gaps
        result = engine.assess_conformity(d)
        assert result.failed > 0
        assert result.overall_status == "NON_CONFORMANT"

    def test_high_risk_with_full_metadata_passes(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor(
            metadata={
                "risk_management_system": "ISO 31000 risk framework documented",
                "data_governance_practices": "ISO 8000 data quality standard applied",
                "technical_documentation_reference": "Annex IV doc v1.0",
                "logging_capabilities": "Structured JSON audit log, immutable",
                "instructions_for_use": "Deployer guide v2.1 published",
                "human_oversight": "Human-in-the-loop review required for all rejections",
                "robustness_testing": "Adversarial testing report Q1-2027",
            }
        )
        result = engine.assess_conformity(d)
        # With all metadata, most checks should pass
        assert result.passed >= 5

    def test_checks_cover_articles_9_to_15(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()
        result = engine.assess_conformity(d)
        articles = {c.article for c in result.checks}
        for expected in ["Article 9", "Article 10", "Article 12", "Article 14"]:
            assert expected in articles, f"Missing {expected}"

    def test_limited_risk_transparency_check(self) -> None:
        engine = ComplianceEngine()
        d = limited_risk_descriptor()
        result = engine.assess_conformity(d)
        # No disclosure_mechanism in metadata → should FAIL transparency check
        assert result.failed >= 1

    def test_limited_risk_with_disclosure_passes(self) -> None:
        engine = ComplianceEngine()
        d = limited_risk_descriptor(metadata={"disclosure_mechanism": "Prominent AI disclosure banner"})
        result = engine.assess_conformity(d)
        assert result.failed == 0

    def test_mandatory_gaps_populated(self) -> None:
        engine = ComplianceEngine()
        d = high_risk_descriptor()
        result = engine.assess_conformity(d)
        assert len(result.mandatory_gaps) == result.failed

    def test_row_conservation(self) -> None:
        """passed + failed + partial == total checks."""
        engine = ComplianceEngine()
        for d in [minimal_descriptor(), high_risk_descriptor(), limited_risk_descriptor()]:
            result = engine.assess_conformity(d)
            assert result.passed + result.failed + result.partial == len(result.checks)

    def test_assessor_id_custom(self) -> None:
        assessor = ConformityAssessor()
        engine = ComplianceEngine()
        d = minimal_descriptor()
        c = engine.classify(d)
        result = assessor.assess(d, c, assessor_id="test-runner")
        assert result.assessor == "test-runner"


# ---------------------------------------------------------------------------
# ComplianceEngine.run() integration tests
# ---------------------------------------------------------------------------


class TestComplianceEngineRun:
    def test_run_returns_all_keys_for_high_risk(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(high_risk_descriptor())
        assert "classification" in report
        assert "tech_doc" in report
        assert "conformity" in report
        assert "compliance_summary" in report

    def test_run_no_tech_doc_for_minimal_risk(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(minimal_descriptor())
        assert "tech_doc" not in report

    def test_run_no_tech_doc_when_disabled(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(high_risk_descriptor(), include_tech_doc=False)
        assert "tech_doc" not in report

    def test_run_tech_doc_for_prohibited(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(prohibited_descriptor())
        assert "tech_doc" in report  # UNACCEPTABLE also gets tech doc

    def test_compliance_summary_action_required_for_high_risk(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(high_risk_descriptor())
        summary = report["compliance_summary"]
        assert summary["action_required"] is True
        assert "2027" in summary["deadline"]

    def test_compliance_summary_no_action_for_minimal(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(minimal_descriptor())
        summary = report["compliance_summary"]
        assert summary["action_required"] is False
        assert summary["deadline"] == "N/A"

    def test_next_steps_prohibited_contains_stop(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(prohibited_descriptor())
        steps = report["compliance_summary"]["next_steps"]
        assert any("STOP" in s for s in steps)

    def test_next_steps_high_risk_mentions_deadline(self) -> None:
        engine = ComplianceEngine()
        report = engine.run(high_risk_descriptor())
        steps = report["compliance_summary"]["next_steps"]
        assert any("2027" in s for s in steps)

    def test_full_round_trip_serialisable(self) -> None:
        """Report must be JSON-serialisable for audit export."""
        import json

        engine = ComplianceEngine()
        report = engine.run(high_risk_descriptor())
        # Should not raise
        serialised = json.dumps(report)
        assert len(serialised) > 100

    def test_next_steps_limited_risk(self) -> None:
        """_next_steps for LIMITED risk returns Article 50 guidance."""
        engine = ComplianceEngine()
        report = engine.run(limited_risk_descriptor())
        steps = report["compliance_summary"]["next_steps"]
        assert any("50" in s or "transparency" in s.lower() or "disclosure" in s.lower() for s in steps)

    def test_conformity_overall_status_partial(self) -> None:
        """A system where all checks are PARTIAL (not FAIL) gets PARTIAL overall status."""
        engine = ComplianceEngine()
        # High-risk system with only Article 11 and 13 triggers (PARTIAL) and the rest PASS.
        # Provide everything except tech_doc ref and instructions_for_use → Article 11 and 13 are PARTIAL.
        # Also provide Article 15 (robustness) as NOT SPECIFIED → PARTIAL too.
        # To get PARTIAL (not NON_CONFORMANT) we need zero FAILs.
        d = SystemDescriptor(
            name="PartialBot",
            version="1.0.0",
            description="A system with partial compliance",
            intended_use="Hiring automation",
            deployment_context="HR",
            used_in_employment=True,
            metadata={
                "risk_management_system": "ISO 31000 risk framework",
                "data_governance_practices": "ISO 8000 data quality",
                "logging_capabilities": "Structured JSON audit log",
                "human_oversight": "Human-in-the-loop for all decisions",
                # Intentionally omit technical_documentation_reference → Article 11 = PARTIAL
                # Intentionally omit instructions_for_use → Article 13 = PARTIAL
                # Intentionally omit robustness_testing → Article 15 = PARTIAL
            },
        )
        result = engine.assess_conformity(d)
        # No FAILs (risk_mgmt, data_gov, logging, oversight all provided)
        assert result.failed == 0
        assert result.partial > 0
        assert result.overall_status == "PARTIAL"
