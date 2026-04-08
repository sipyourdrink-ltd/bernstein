"""EU AI Act Compliance Engine — Annex III risk classification, Annex IV tech docs, conformity assessment.

Implements the EU Artificial Intelligence Act (Regulation (EU) 2024/1689).
Mandatory for high-risk AI systems from August 2027 (Article 111(2)).

Key Articles implemented:
  - Article 5: Prohibited practices (unacceptable risk)
  - Article 6 + Annex III: High-risk classification
  - Article 43: Conformity assessment procedures
  - Article 50: Transparency obligations
  - Annex IV: Technical documentation requirements
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path  # noqa: TC003
from typing import Any

logger = logging.getLogger(__name__)

_NOT_DOCUMENTED = "Not documented"


# ---------------------------------------------------------------------------
# Risk taxonomy
# ---------------------------------------------------------------------------


class RiskCategory(StrEnum):
    """EU AI Act risk tiers per Articles 5, 6, 50."""

    UNACCEPTABLE = "unacceptable"  # Article 5 — prohibited
    HIGH = "high"  # Article 6 + Annex III — strict obligations
    LIMITED = "limited"  # Article 50 — transparency obligations only
    MINIMAL = "minimal"  # No mandatory obligations


class AnnexIIIDomain(StrEnum):
    """High-risk domains enumerated in Annex III of the EU AI Act (8 areas).

    Numbering follows the official Annex III structure.
    """

    BIOMETRIC = "1_biometric_identification"
    CRITICAL_INFRASTRUCTURE = "2_critical_infrastructure"
    EDUCATION = "3_education_vocational_training"
    EMPLOYMENT = "4_employment_workers_management"
    ESSENTIAL_SERVICES = "5_essential_private_public_services"
    LAW_ENFORCEMENT = "6_law_enforcement"
    MIGRATION = "7_migration_asylum_border_control"
    JUSTICE = "8_administration_of_justice_democratic_processes"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------


@dataclass
class SystemDescriptor:
    """Describes an AI system for compliance classification.

    Attributes:
        name: Human-readable system name.
        version: Semver or opaque version string.
        description: Plain-language description of purpose and capabilities.
        intended_use: Specific intended use cases.
        deployment_context: Where/how the system is deployed.
        processes_biometrics: Whether it processes biometric data.
        interacts_with_critical_infrastructure: Water, energy, transport, etc.
        used_in_education: Influences access/evaluation in education.
        used_in_employment: Hiring, promotion, performance monitoring.
        used_in_essential_services: Credit, insurance, public benefits.
        used_in_law_enforcement: Policing, crime prediction, evidence evaluation.
        used_in_migration: Visa decisions, border control, asylum.
        used_in_justice: Judicial decisions, democratic processes.
        real_time_biometric_public: Real-time remote biometric ID in public spaces.
        subliminal_techniques: Exploits subconscious/subliminal techniques.
        exploits_vulnerabilities: Targets protected-characteristic vulnerabilities.
        social_scoring_public: Social scoring by public authorities.
        manipulates_behavior: Materially distorts behavior against user interests.
        metadata: Optional free-form key/value pairs for audit trail.
    """

    name: str
    version: str
    description: str
    intended_use: str
    deployment_context: str

    # Annex III domain flags
    processes_biometrics: bool = False
    interacts_with_critical_infrastructure: bool = False
    used_in_education: bool = False
    used_in_employment: bool = False
    used_in_essential_services: bool = False
    used_in_law_enforcement: bool = False
    used_in_migration: bool = False
    used_in_justice: bool = False

    # Article 5 (unacceptable risk) indicators
    real_time_biometric_public: bool = False
    subliminal_techniques: bool = False
    exploits_vulnerabilities: bool = False
    social_scoring_public: bool = False
    manipulates_behavior: bool = False

    # Transparency triggers (Article 50)
    is_chatbot_or_conversational: bool = False
    generates_synthetic_media: bool = False
    uses_emotion_recognition: bool = False

    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass
class ClassificationResult:
    """Output of Annex III risk classification.

    Attributes:
        system_name: Name from the SystemDescriptor.
        risk_category: Assigned EU AI Act risk tier.
        annex_iii_domain: Which Annex III domain triggered high-risk, if any.
        article5_triggers: List of Article 5 prohibitions that apply.
        article50_triggers: List of Article 50 transparency triggers.
        justification: Narrative explanation of the classification.
        classified_at: ISO-8601 timestamp of classification.
        classification_hash: Deterministic SHA-256 of inputs (for audit).
    """

    system_name: str
    risk_category: RiskCategory
    annex_iii_domain: AnnexIIIDomain
    article5_triggers: list[str]
    article50_triggers: list[str]
    justification: str
    classified_at: str
    classification_hash: str


# ---------------------------------------------------------------------------
# Technical documentation (Annex IV)
# ---------------------------------------------------------------------------


@dataclass
class TechDoc:
    """Annex IV technical documentation package.

    Mirrors the 8 sections of Annex IV (Articles 11, 18).

    Attributes:
        system_name: AI system identifier.
        system_version: Version at time of documentation.
        doc_version: Documentation version (increment on every update).
        general_description: Section 1 — purpose, capabilities, intended use.
        design_and_development: Section 2 — model architecture, training data,
            training process, validation, performance metrics.
        monitoring_and_control: Section 3 — human oversight mechanisms,
            monitoring procedures, logging capabilities.
        robustness_and_security: Section 4 — adversarial robustness, security
            measures, accuracy metrics, bias mitigation.
        data_governance: Section 5 — data sources, preprocessing, data quality
            measures, personal data handling.
        transparency_and_information: Section 6 — instructions for use,
            disclosure obligations, capability limitations.
        post_market_monitoring: Section 7 — monitoring plan, incident reporting
            procedures, feedback loop.
        conformity_declaration: Section 8 — standards applied, notified body
            reference, CE marking declaration.
        generated_at: ISO-8601 generation timestamp.
        doc_hash: SHA-256 of all sections (tamper-evidence).
    """

    system_name: str
    system_version: str
    doc_version: str

    # Annex IV sections
    general_description: str
    design_and_development: str
    monitoring_and_control: str
    robustness_and_security: str
    data_governance: str
    transparency_and_information: str
    post_market_monitoring: str
    conformity_declaration: str

    generated_at: str
    doc_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for storage / audit export."""
        return {
            "system_name": self.system_name,
            "system_version": self.system_version,
            "doc_version": self.doc_version,
            "sections": {
                "1_general_description": self.general_description,
                "2_design_and_development": self.design_and_development,
                "3_monitoring_and_control": self.monitoring_and_control,
                "4_robustness_and_security": self.robustness_and_security,
                "5_data_governance": self.data_governance,
                "6_transparency_and_information": self.transparency_and_information,
                "7_post_market_monitoring": self.post_market_monitoring,
                "8_conformity_declaration": self.conformity_declaration,
            },
            "generated_at": self.generated_at,
            "doc_hash": self.doc_hash,
        }


# ---------------------------------------------------------------------------
# Conformity assessment
# ---------------------------------------------------------------------------


@dataclass
class ConformityCheck:
    """A single conformity requirement check.

    Attributes:
        article: EU AI Act article reference (e.g. "Article 9").
        requirement: Plain-language description of the requirement.
        status: "PASS", "FAIL", or "PARTIAL".
        evidence: Artefact or observation that supports the status.
        remediation: Remediation steps if status is not PASS.
    """

    article: str
    requirement: str
    status: str  # "PASS" | "FAIL" | "PARTIAL"
    evidence: str
    remediation: str = ""


@dataclass
class ConformityResult:
    """Aggregated conformity assessment outcome.

    Attributes:
        system_name: AI system assessed.
        overall_status: "CONFORMANT", "NON_CONFORMANT", or "PARTIAL".
        checks: All individual requirement checks.
        passed: Count of passing checks.
        failed: Count of failing checks.
        partial: Count of partially-satisfied checks.
        mandatory_gaps: Critical gaps (FAIL on any Article 9/10/11/12/13/14/15).
        assessed_at: ISO-8601 timestamp.
        assessor: Identifier of the entity that ran the assessment.
    """

    system_name: str
    overall_status: str
    checks: list[ConformityCheck]
    passed: int
    failed: int
    partial: int
    mandatory_gaps: list[str]
    assessed_at: str
    assessor: str = "bernstein-compliance-engine"


# ---------------------------------------------------------------------------
# Annex III Classifier
# ---------------------------------------------------------------------------


class _AnnexIIIClassifier:
    """Internal classifier — maps SystemDescriptor flags to RiskCategory + domain."""

    # Article 5 prohibition checks: (flag_attr, description)
    _ARTICLE5_CHECKS: list[tuple[str, str]] = [
        (
            "real_time_biometric_public",
            "Article 5(1)(h): Real-time remote biometric identification in publicly accessible spaces",
        ),
        (
            "subliminal_techniques",
            "Article 5(1)(a): Subliminal techniques that materially distort behaviour",
        ),
        (
            "exploits_vulnerabilities",
            "Article 5(1)(b): Exploits vulnerabilities of specific groups",
        ),
        (
            "social_scoring_public",
            "Article 5(1)(c)/(d): Social scoring by public authorities",
        ),
        (
            "manipulates_behavior",
            "Article 5(1)(a): Manipulates behaviour against users' interests",
        ),
    ]

    # Annex III high-risk domain checks: (flag_attr, domain, justification)
    _ANNEX_III_CHECKS: list[tuple[str, AnnexIIIDomain, str]] = [
        (
            "processes_biometrics",
            AnnexIIIDomain.BIOMETRIC,
            "Annex III §1: Biometric identification and categorisation of natural persons",
        ),
        (
            "interacts_with_critical_infrastructure",
            AnnexIIIDomain.CRITICAL_INFRASTRUCTURE,
            "Annex III §2: Safety components of critical infrastructure",
        ),
        (
            "used_in_education",
            AnnexIIIDomain.EDUCATION,
            "Annex III §3: AI in education/vocational training (access, evaluation, monitoring)",
        ),
        (
            "used_in_employment",
            AnnexIIIDomain.EMPLOYMENT,
            "Annex III §4: Employment and workers management (recruitment, performance, termination)",
        ),
        (
            "used_in_essential_services",
            AnnexIIIDomain.ESSENTIAL_SERVICES,
            "Annex III §5: Access to essential services (credit, insurance, public benefits)",
        ),
        (
            "used_in_law_enforcement",
            AnnexIIIDomain.LAW_ENFORCEMENT,
            "Annex III §6: Law enforcement use cases",
        ),
        (
            "used_in_migration",
            AnnexIIIDomain.MIGRATION,
            "Annex III §7: Migration, asylum, border control management",
        ),
        (
            "used_in_justice",
            AnnexIIIDomain.JUSTICE,
            "Annex III §8: Administration of justice and democratic processes",
        ),
    ]

    # Article 50 transparency triggers: (flag_attr, description)
    _ARTICLE50_CHECKS: list[tuple[str, str]] = [
        (
            "is_chatbot_or_conversational",
            "Article 50(1): Disclose AI nature to persons interacting with chatbot/conversational AI",
        ),
        (
            "generates_synthetic_media",
            "Article 50(2)/(4): Mark AI-generated/manipulated content (deepfakes, synthetic audio/video)",
        ),
        (
            "uses_emotion_recognition",
            "Article 50(3): Inform natural persons of emotion recognition system use",
        ),
    ]

    def classify(self, descriptor: SystemDescriptor) -> ClassificationResult:
        now = datetime.now(tz=UTC).isoformat()

        # Step 1: Check Article 5 prohibitions
        article5_triggers: list[str] = []
        for attr, description in self._ARTICLE5_CHECKS:
            if getattr(descriptor, attr, False):
                article5_triggers.append(description)

        # Step 2: Check Annex III high-risk domains
        annex_iii_domain = AnnexIIIDomain.NOT_APPLICABLE
        annex_iii_justification = ""
        for attr, domain, justification in self._ANNEX_III_CHECKS:
            if getattr(descriptor, attr, False):
                annex_iii_domain = domain
                annex_iii_justification = justification
                break  # First match determines domain (most critical first)

        # Step 3: Check Article 50 transparency obligations
        article50_triggers: list[str] = []
        for attr, description in self._ARTICLE50_CHECKS:
            if getattr(descriptor, attr, False):
                article50_triggers.append(description)

        # Step 4: Determine risk category
        if article5_triggers:
            risk_category = RiskCategory.UNACCEPTABLE
            justification = (
                f"System '{descriptor.name}' falls under PROHIBITED practices per Article 5. "
                f"Triggers: {'; '.join(article5_triggers)}. "
                "Deployment is not permitted under the EU AI Act."
            )
        elif annex_iii_domain != AnnexIIIDomain.NOT_APPLICABLE:
            risk_category = RiskCategory.HIGH
            justification = (
                f"System '{descriptor.name}' is HIGH RISK under Annex III. "
                f"{annex_iii_justification}. "
                "Full conformity assessment, technical documentation (Annex IV), "
                "CE marking, and EU database registration required before deployment."
            )
        elif article50_triggers:
            risk_category = RiskCategory.LIMITED
            justification = (
                f"System '{descriptor.name}' has LIMITED RISK transparency obligations under Article 50. "
                f"Triggers: {'; '.join(article50_triggers)}. "
                "No conformity assessment required, but disclosure obligations apply."
            )
        else:
            risk_category = RiskCategory.MINIMAL
            justification = (
                f"System '{descriptor.name}' presents MINIMAL RISK. "
                "No mandatory EU AI Act obligations apply. "
                "Voluntary adherence to codes of conduct is encouraged."
            )

        # Deterministic audit hash: SHA-256 of all classification inputs
        hash_payload = json.dumps(
            {
                "name": descriptor.name,
                "version": descriptor.version,
                "description": descriptor.description,
                "intended_use": descriptor.intended_use,
                "deployment_context": descriptor.deployment_context,
                "flags": {
                    attr: getattr(descriptor, attr, False)
                    for attr, *_ in (
                        self._ARTICLE5_CHECKS
                        + [(a, d, j) for a, d, j in self._ANNEX_III_CHECKS]
                        + self._ARTICLE50_CHECKS
                    )
                },
            },
            sort_keys=True,
        ).encode()
        classification_hash = hashlib.sha256(hash_payload).hexdigest()

        return ClassificationResult(
            system_name=descriptor.name,
            risk_category=risk_category,
            annex_iii_domain=annex_iii_domain,
            article5_triggers=article5_triggers,
            article50_triggers=article50_triggers,
            justification=justification,
            classified_at=now,
            classification_hash=classification_hash,
        )


# ---------------------------------------------------------------------------
# Annex IV Technical Documentation Generator
# ---------------------------------------------------------------------------


class TechDocGenerator:
    """Generates Annex IV technical documentation for a high-risk AI system.

    Usage::

        generator = TechDocGenerator()
        doc = generator.generate(descriptor, classification_result)
        print(json.dumps(doc.to_dict(), indent=2))
    """

    def generate(
        self,
        descriptor: SystemDescriptor,
        classification: ClassificationResult,
        doc_version: str = "1.0.0",
    ) -> TechDoc:
        """Generate Annex IV technical documentation.

        Args:
            descriptor: System descriptor containing metadata and flags.
            classification: Prior classification result for this system.
            doc_version: Documentation version string.

        Returns:
            TechDoc with all 8 Annex IV sections populated.
        """
        now = datetime.now(tz=UTC).isoformat()

        sec1 = self._section1_general(descriptor, classification)
        sec2 = self._section2_design(descriptor)
        sec3 = self._section3_monitoring(descriptor)
        sec4 = self._section4_robustness(descriptor)
        sec5 = self._section5_data(descriptor)
        sec6 = self._section6_transparency(descriptor, classification)
        sec7 = self._section7_post_market(descriptor)
        sec8 = self._section8_conformity(descriptor, classification)

        sections = [sec1, sec2, sec3, sec4, sec5, sec6, sec7, sec8]
        doc_hash = hashlib.sha256("\n".join(sections).encode()).hexdigest()

        return TechDoc(
            system_name=descriptor.name,
            system_version=descriptor.version,
            doc_version=doc_version,
            general_description=sec1,
            design_and_development=sec2,
            monitoring_and_control=sec3,
            robustness_and_security=sec4,
            data_governance=sec5,
            transparency_and_information=sec6,
            post_market_monitoring=sec7,
            conformity_declaration=sec8,
            generated_at=now,
            doc_hash=doc_hash,
        )

    # --- Section builders ---

    def _section1_general(
        self,
        d: SystemDescriptor,
        c: ClassificationResult,
    ) -> str:
        domain_label = c.annex_iii_domain.value if c.annex_iii_domain != AnnexIIIDomain.NOT_APPLICABLE else "N/A"
        return (
            f"System Name: {d.name} v{d.version}\n"
            f"Risk Category: {c.risk_category.value.upper()}\n"
            f"Annex III Domain: {domain_label}\n"
            f"Description: {d.description}\n"
            f"Intended Use: {d.intended_use}\n"
            f"Deployment Context: {d.deployment_context}\n"
            f"Classification Hash: {c.classification_hash}\n"
            f"Classified At: {c.classified_at}"
        )

    def _section2_design(self, d: SystemDescriptor) -> str:
        return (
            f"Provider/Developer: {d.metadata.get('provider', 'NOT SPECIFIED')}\n"
            f"Model Architecture: {d.metadata.get('model_architecture', 'NOT SPECIFIED')}\n"
            f"Training Data Sources: {d.metadata.get('training_data', 'NOT SPECIFIED')}\n"
            f"Training Process: {d.metadata.get('training_process', 'NOT SPECIFIED')}\n"
            f"Validation Methodology: {d.metadata.get('validation_methodology', 'NOT SPECIFIED')}\n"
            f"Performance Metrics: {d.metadata.get('performance_metrics', 'NOT SPECIFIED')}\n"
            f"Known Limitations: {d.metadata.get('known_limitations', 'NOT SPECIFIED')}\n"
            "NOTE: Sections marked NOT SPECIFIED must be completed before market placement "
            "(Article 11 + Annex IV §2)."
        )

    def _section3_monitoring(self, d: SystemDescriptor) -> str:
        return (
            f"Human Oversight Mechanisms: {d.metadata.get('human_oversight', 'NOT SPECIFIED')}\n"
            f"Override Capability: {d.metadata.get('override_capability', 'NOT SPECIFIED')}\n"
            f"Monitoring Procedures: {d.metadata.get('monitoring_procedures', 'NOT SPECIFIED')}\n"
            f"Logging Capabilities: {d.metadata.get('logging_capabilities', 'NOT SPECIFIED')}\n"
            f"Audit Trail: {d.metadata.get('audit_trail', 'NOT SPECIFIED')}\n"
            "REQUIREMENT: AI system must allow human oversight persons to understand outputs, "
            "intervene, interrupt, or override at any time (Article 14)."
        )

    def _section4_robustness(self, d: SystemDescriptor) -> str:
        return (
            f"Robustness Testing: {d.metadata.get('robustness_testing', 'NOT SPECIFIED')}\n"
            f"Security Measures: {d.metadata.get('security_measures', 'NOT SPECIFIED')}\n"
            f"Accuracy Metrics: {d.metadata.get('accuracy_metrics', 'NOT SPECIFIED')}\n"
            f"Bias Assessment: {d.metadata.get('bias_assessment', 'NOT SPECIFIED')}\n"
            f"Bias Mitigation: {d.metadata.get('bias_mitigation', 'NOT SPECIFIED')}\n"
            f"Adversarial Testing: {d.metadata.get('adversarial_testing', 'NOT SPECIFIED')}\n"
            "REQUIREMENT: High-risk AI must be resilient against errors, faults, and cyber threats "
            "throughout lifecycle (Article 15)."
        )

    def _section5_data(self, d: SystemDescriptor) -> str:
        pii_note = (
            "CAUTION: System processes biometric/personal data. "
            "GDPR Article 9 / EU AI Act Article 10 data governance requirements apply."
            if d.processes_biometrics
            else ""
        )
        return (
            f"Training Data Sources: {d.metadata.get('training_data_sources', 'NOT SPECIFIED')}\n"
            f"Data Preprocessing: {d.metadata.get('data_preprocessing', 'NOT SPECIFIED')}\n"
            f"Data Quality Measures: {d.metadata.get('data_quality_measures', 'NOT SPECIFIED')}\n"
            f"Personal Data Handling: {d.metadata.get('personal_data_handling', 'NOT SPECIFIED')}\n"
            f"Data Retention Policy: {d.metadata.get('data_retention', 'NOT SPECIFIED')}\n"
            f"Third-Party Data: {d.metadata.get('third_party_data', 'NOT SPECIFIED')}\n"
            + (f"\n{pii_note}" if pii_note else "")
        )

    def _section6_transparency(
        self,
        d: SystemDescriptor,
        c: ClassificationResult,
    ) -> str:
        disclosures = (
            "\n".join(f"  - {t}" for t in c.article50_triggers) if c.article50_triggers else "  None identified"
        )
        return (
            f"Instructions for Use: {d.metadata.get('instructions_for_use', 'NOT SPECIFIED')}\n"
            f"Capability Limitations: {d.metadata.get('capability_limitations', 'NOT SPECIFIED')}\n"
            f"User Disclosure Requirements:\n{disclosures}\n"
            f"Output Interpretability: {d.metadata.get('output_interpretability', 'NOT SPECIFIED')}\n"
            "REQUIREMENT: Deployers must receive sufficient information to comply with obligations "
            "(Article 13). Users must be informed where Article 50 applies."
        )

    def _section7_post_market(self, d: SystemDescriptor) -> str:
        return (
            f"Monitoring Plan: {d.metadata.get('monitoring_plan', 'NOT SPECIFIED')}\n"
            f"Incident Reporting: {d.metadata.get('incident_reporting', 'NOT SPECIFIED')}\n"
            f"Serious Incident Reporting Deadline: 15 days after becoming aware (Article 73)\n"
            f"Market Surveillance Authority: {d.metadata.get('market_surveillance_authority', 'NOT SPECIFIED')}\n"
            f"Feedback Loop: {d.metadata.get('feedback_loop', 'NOT SPECIFIED')}\n"
            "REQUIREMENT: Providers must establish post-market monitoring system (Article 72). "
            "Serious incidents and malfunctions must be reported to national authorities."
        )

    def _section8_conformity(
        self,
        d: SystemDescriptor,
        c: ClassificationResult,
    ) -> str:
        procedure = (
            "Conformity Assessment Procedure: Annex VI (Internal Control)\n"
            "OR Third-party assessment by notified body if biometric identification system "
            "(Annex VII/VIII as applicable)"
            if c.risk_category == RiskCategory.HIGH
            else "No conformity assessment required for this risk category."
        )
        return (
            f"{procedure}\n"
            f"Harmonised Standards Applied: {d.metadata.get('harmonised_standards', 'NOT SPECIFIED')}\n"
            f"Notified Body: {d.metadata.get('notified_body', 'NOT YET ASSIGNED')}\n"
            f"CE Marking: {d.metadata.get('ce_marking_status', 'PENDING')}\n"
            f"EU Database Registration: {d.metadata.get('eu_db_registration', 'PENDING — required before deployment (Article 49)')}\n"
            f"Declaration of Conformity: {d.metadata.get('declaration_of_conformity', 'NOT YET ISSUED')}\n"
            "DEADLINE: August 2027 (Article 111(2) transitional provision for high-risk AI systems)."
        )


# ---------------------------------------------------------------------------
# Conformity Assessor
# ---------------------------------------------------------------------------


class ConformityAssessor:
    """Runs automated conformity checks against EU AI Act requirements.

    Checks Articles 9-15 (high-risk obligations) + Article 50 (transparency).

    Usage::

        assessor = ConformityAssessor()
        result = assessor.assess(descriptor, classification)
    """

    def assess(
        self,
        descriptor: SystemDescriptor,
        classification: ClassificationResult,
        assessor_id: str = "bernstein-compliance-engine",
    ) -> ConformityResult:
        """Run conformity assessment for a system.

        Args:
            descriptor: System descriptor.
            classification: Prior risk classification result.
            assessor_id: Identifier of the assessing entity.

        Returns:
            ConformityResult with all checks and overall status.
        """
        now = datetime.now(tz=UTC).isoformat()
        checks: list[ConformityCheck] = []

        if classification.risk_category == RiskCategory.UNACCEPTABLE:
            checks.append(
                ConformityCheck(
                    article="Article 5",
                    requirement="System must not implement prohibited AI practices",
                    status="FAIL",
                    evidence="; ".join(classification.article5_triggers),
                    remediation=(
                        "Remove or fundamentally redesign prohibited functionality. "
                        "Deployment is not permitted under the EU AI Act."
                    ),
                )
            )
        elif classification.risk_category == RiskCategory.HIGH:
            checks.extend(self._high_risk_checks(descriptor))
        elif classification.risk_category == RiskCategory.LIMITED:
            checks.extend(self._transparency_checks(descriptor, classification))
        else:
            checks.append(
                ConformityCheck(
                    article="General",
                    requirement="Minimal-risk system — no mandatory obligations",
                    status="PASS",
                    evidence="No Annex III triggers, no Article 5 triggers, no Article 50 triggers.",
                    remediation="",
                )
            )

        passed = sum(1 for c in checks if c.status == "PASS")
        failed = sum(1 for c in checks if c.status == "FAIL")
        partial = sum(1 for c in checks if c.status == "PARTIAL")

        mandatory_gaps = [f"{c.article}: {c.requirement}" for c in checks if c.status == "FAIL"]

        if failed > 0:
            overall_status = "NON_CONFORMANT"
        elif partial > 0:
            overall_status = "PARTIAL"
        else:
            overall_status = "CONFORMANT"

        return ConformityResult(
            system_name=descriptor.name,
            overall_status=overall_status,
            checks=checks,
            passed=passed,
            failed=failed,
            partial=partial,
            mandatory_gaps=mandatory_gaps,
            assessed_at=now,
            assessor=assessor_id,
        )

    def _high_risk_checks(self, d: SystemDescriptor) -> list[ConformityCheck]:
        """Article 9-15 checks for high-risk AI systems."""
        checks: list[ConformityCheck] = []

        # Article 9: Risk management system
        has_risk_mgmt = bool(d.metadata.get("risk_management_system"))
        checks.append(
            ConformityCheck(
                article="Article 9",
                requirement="Establish and maintain a risk management system throughout lifecycle",
                status="PASS" if has_risk_mgmt else "FAIL",
                evidence=d.metadata.get("risk_management_system", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_risk_mgmt
                    else "Document risk management system: identification, estimation, evaluation, and mitigation measures."
                ),
            )
        )

        # Article 10: Data and data governance
        has_data_gov = bool(d.metadata.get("data_governance_practices"))
        checks.append(
            ConformityCheck(
                article="Article 10",
                requirement="Training/validation/test data must meet quality criteria and data governance practices",
                status="PASS" if has_data_gov else "FAIL",
                evidence=d.metadata.get("data_governance_practices", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_data_gov
                    else "Document data governance: sources, collection, processing, relevance, completeness, bias examination."
                ),
            )
        )

        # Article 11: Technical documentation
        has_tech_doc = bool(d.metadata.get("technical_documentation_reference"))
        checks.append(
            ConformityCheck(
                article="Article 11",
                requirement="Annex IV technical documentation must be drawn up and kept up to date",
                status="PASS" if has_tech_doc else "PARTIAL",
                evidence=d.metadata.get(
                    "technical_documentation_reference",
                    "Technical documentation generated by Bernstein compliance engine — manual review required",
                ),
                remediation=(
                    "" if has_tech_doc else "Complete all NOT SPECIFIED sections in generated Annex IV documentation."
                ),
            )
        )

        # Article 12: Record-keeping / automatic logging
        has_logging = bool(d.metadata.get("logging_capabilities"))
        checks.append(
            ConformityCheck(
                article="Article 12",
                requirement="System must enable automatic logging of events throughout operational lifetime",
                status="PASS" if has_logging else "FAIL",
                evidence=d.metadata.get("logging_capabilities", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_logging
                    else "Implement automatic event logging: timestamps, inputs, decisions, confidence scores."
                ),
            )
        )

        # Article 13: Transparency
        has_transparency = bool(d.metadata.get("instructions_for_use"))
        checks.append(
            ConformityCheck(
                article="Article 13",
                requirement="System must be transparent; deployers must receive adequate information",
                status="PASS" if has_transparency else "PARTIAL",
                evidence=d.metadata.get("instructions_for_use", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_transparency
                    else "Document instructions for use including system identity, capabilities, limitations, and deployer obligations."
                ),
            )
        )

        # Article 14: Human oversight
        has_oversight = bool(d.metadata.get("human_oversight"))
        checks.append(
            ConformityCheck(
                article="Article 14",
                requirement="System must enable effective human oversight and intervention capability",
                status="PASS" if has_oversight else "FAIL",
                evidence=d.metadata.get("human_oversight", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_oversight
                    else "Implement human oversight: ability to understand, monitor, pause, and override AI outputs."
                ),
            )
        )

        # Article 15: Accuracy, robustness, cybersecurity
        has_robustness = bool(d.metadata.get("robustness_testing"))
        checks.append(
            ConformityCheck(
                article="Article 15",
                requirement="System must achieve appropriate accuracy, robustness, and cybersecurity",
                status="PASS" if has_robustness else "PARTIAL",
                evidence=d.metadata.get("robustness_testing", _NOT_DOCUMENTED),
                remediation=(
                    ""
                    if has_robustness
                    else "Document accuracy benchmarks, robustness testing results, and security assessment."
                ),
            )
        )

        return checks

    def _transparency_checks(
        self,
        d: SystemDescriptor,
        c: ClassificationResult,
    ) -> list[ConformityCheck]:
        """Article 50 transparency checks for limited-risk systems."""
        checks: list[ConformityCheck] = []
        for trigger in c.article50_triggers:
            article_ref = trigger.split(":")[0].strip()
            has_disclosure = bool(d.metadata.get("disclosure_mechanism"))
            checks.append(
                ConformityCheck(
                    article=article_ref,
                    requirement=trigger,
                    status="PASS" if has_disclosure else "FAIL",
                    evidence=d.metadata.get("disclosure_mechanism", _NOT_DOCUMENTED),
                    remediation=("" if has_disclosure else f"Implement disclosure mechanism for: {trigger}"),
                )
            )
        return checks


# ---------------------------------------------------------------------------
# ComplianceEngine — top-level facade
# ---------------------------------------------------------------------------


class ComplianceEngine:
    """Façade for the full EU AI Act compliance workflow.

    Orchestrates:
      1. Annex III risk classification
      2. Annex IV technical documentation generation (if high-risk)
      3. Conformity assessment

    Usage::

        engine = ComplianceEngine()
        report = engine.run(descriptor)
        print(report["classification"]["risk_category"])
    """

    def __init__(self) -> None:
        self._classifier = _AnnexIIIClassifier()
        self._doc_generator = TechDocGenerator()
        self._assessor = ConformityAssessor()

    def classify(self, descriptor: SystemDescriptor) -> ClassificationResult:
        """Run Annex III risk classification only."""
        return self._classifier.classify(descriptor)

    def generate_tech_doc(
        self,
        descriptor: SystemDescriptor,
        classification: ClassificationResult | None = None,
        doc_version: str = "1.0.0",
    ) -> TechDoc:
        """Generate Annex IV technical documentation.

        Args:
            descriptor: AI system descriptor.
            classification: Pre-computed classification; if None, runs classifier.
            doc_version: Documentation version string.
        """
        if classification is None:
            classification = self._classifier.classify(descriptor)
        return self._doc_generator.generate(descriptor, classification, doc_version)

    def assess_conformity(
        self,
        descriptor: SystemDescriptor,
        classification: ClassificationResult | None = None,
    ) -> ConformityResult:
        """Run conformity assessment.

        Args:
            descriptor: AI system descriptor.
            classification: Pre-computed classification; if None, runs classifier.
        """
        if classification is None:
            classification = self._classifier.classify(descriptor)
        return self._assessor.assess(descriptor, classification)

    def run(
        self,
        descriptor: SystemDescriptor,
        doc_version: str = "1.0.0",
        include_tech_doc: bool = True,
    ) -> dict[str, Any]:
        """Run full compliance workflow: classify → document → assess.

        Args:
            descriptor: AI system descriptor.
            doc_version: Annex IV document version.
            include_tech_doc: Whether to include Annex IV doc in output
                (can be disabled for minimal-risk systems to reduce noise).

        Returns:
            Dict with keys: "classification", "tech_doc" (if high-risk),
            "conformity", "compliance_summary".
        """
        classification = self._classifier.classify(descriptor)
        logger.info(
            "EU AI Act classification: system=%s risk=%s domain=%s",
            descriptor.name,
            classification.risk_category,
            classification.annex_iii_domain,
        )

        result: dict[str, Any] = {
            "classification": {
                "system_name": classification.system_name,
                "risk_category": classification.risk_category.value,
                "annex_iii_domain": classification.annex_iii_domain.value,
                "article5_triggers": classification.article5_triggers,
                "article50_triggers": classification.article50_triggers,
                "justification": classification.justification,
                "classified_at": classification.classified_at,
                "classification_hash": classification.classification_hash,
            }
        }

        should_generate_doc = include_tech_doc and classification.risk_category in (
            RiskCategory.HIGH,
            RiskCategory.UNACCEPTABLE,
        )
        if should_generate_doc:
            tech_doc = self._doc_generator.generate(descriptor, classification, doc_version)
            result["tech_doc"] = tech_doc.to_dict()

        conformity = self._assessor.assess(descriptor, classification)
        result["conformity"] = {
            "overall_status": conformity.overall_status,
            "passed": conformity.passed,
            "failed": conformity.failed,
            "partial": conformity.partial,
            "mandatory_gaps": conformity.mandatory_gaps,
            "assessed_at": conformity.assessed_at,
            "assessor": conformity.assessor,
            "checks": [
                {
                    "article": c.article,
                    "requirement": c.requirement,
                    "status": c.status,
                    "evidence": c.evidence,
                    "remediation": c.remediation,
                }
                for c in conformity.checks
            ],
        }

        result["compliance_summary"] = self._build_summary(classification, conformity)
        return result

    def _build_summary(
        self,
        c: ClassificationResult,
        r: ConformityResult,
    ) -> dict[str, Any]:
        deadline = "August 2027 (Article 111(2))" if c.risk_category == RiskCategory.HIGH else "N/A"
        return {
            "risk_category": c.risk_category.value,
            "overall_conformity": r.overall_status,
            "mandatory_gaps_count": r.failed,
            "deadline": deadline,
            "action_required": r.failed > 0 or c.risk_category == RiskCategory.UNACCEPTABLE,
            "next_steps": self._next_steps(c, r),
        }

    def _next_steps(
        self,
        c: ClassificationResult,
        r: ConformityResult,
    ) -> list[str]:
        if c.risk_category == RiskCategory.UNACCEPTABLE:
            return [
                "STOP: Remove or fundamentally redesign prohibited functionality.",
                "System cannot be deployed under the EU AI Act.",
                "Consult legal counsel before any further development.",
            ]
        if c.risk_category == RiskCategory.HIGH:
            steps = []
            if r.failed > 0:
                steps.append(f"Address {r.failed} mandatory gap(s) listed in conformity checks.")
            if r.partial > 0:
                steps.append(f"Complete {r.partial} partial requirement(s) in technical documentation.")
            steps += [
                "Register system in EU AI Act database (Article 49) before deployment.",
                "Obtain CE marking after successful conformity assessment.",
                "Establish post-market monitoring system (Article 72).",
                "Deadline: August 2027 (Article 111(2)).",
            ]
            return steps
        if c.risk_category == RiskCategory.LIMITED:
            return [
                "Implement transparency/disclosure obligations per Article 50.",
                "No conformity assessment or CE marking required.",
            ]
        return ["No mandatory action required. Consider voluntary code of conduct adherence."]

    def export_evidence_package(
        self,
        descriptor: SystemDescriptor,
        output_dir: Path,
        doc_version: str = "1.0.0",
    ) -> Path:
        """Run the full compliance workflow and write the evidence package to disk.

        Writes three files to *output_dir*:
          - ``classification.json`` — Annex III classification result
          - ``tech_doc.json`` — Annex IV technical documentation (high-risk only)
          - ``conformity.json`` — conformity assessment results
          - ``evidence_package.json`` — combined package for audit submission

        Args:
            descriptor: AI system descriptor.
            output_dir: Directory where evidence files are written.
            doc_version: Annex IV document version string.

        Returns:
            Path to the combined ``evidence_package.json`` file.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        report = self.run(descriptor, doc_version=doc_version, include_tech_doc=True)

        # Write individual artefacts
        _write_json(output_dir / "classification.json", report["classification"])
        if "tech_doc" in report:
            _write_json(output_dir / "tech_doc.json", report["tech_doc"])
        _write_json(output_dir / "conformity.json", report["conformity"])

        # Combined evidence package with metadata envelope
        package = {
            "schema_version": "1.0",
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "regulation": "EU AI Act (Regulation (EU) 2024/1689)",
            "system_name": descriptor.name,
            "system_version": descriptor.version,
            "report": report,
        }
        package_path = output_dir / "evidence_package.json"
        _write_json(package_path, package)
        logger.info(
            "EU AI Act evidence package written: system=%s path=%s",
            descriptor.name,
            package_path,
        )
        return package_path


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Bernstein system descriptor factory
# ---------------------------------------------------------------------------


def bernstein_descriptor(
    version: str = "1.0.0",
    *,
    deployment_context: str = "Self-hosted multi-agent orchestration platform",
    metadata: dict[str, Any] | None = None,
) -> SystemDescriptor:
    """Return a pre-configured SystemDescriptor for the Bernstein orchestration system.

    Bernstein is an orchestration platform for short-lived CLI coding agents.
    It is not itself a decision-making AI in an Annex III high-risk domain, but
    it may orchestrate agents that perform high-risk tasks.  The descriptor
    captures those nuances so operators can generate a meaningful evidence package.

    Args:
        version: Bernstein release version.
        deployment_context: Where/how Bernstein is deployed.
        metadata: Optional extra metadata to merge into the descriptor.

    Returns:
        SystemDescriptor configured for Bernstein.
    """
    base_metadata: dict[str, Any] = {
        "provider": "Bernstein open-source project (chernistry/bernstein)",
        "model_architecture": (
            "Multi-agent orchestration layer; underlying LLMs are third-party "
            "(Anthropic Claude, OpenAI Codex, Google Gemini, etc.)"
        ),
        "training_data": "Not applicable — Bernstein is orchestration software, not a trained model",
        "training_process": "Not applicable",
        "validation_methodology": "Automated test suite (pytest), ruff lint, Pyright strict type checking",
        "performance_metrics": "Task throughput, agent success rate, wall-clock time per task",
        "known_limitations": (
            "LLM agent outputs are non-deterministic; Bernstein does not validate semantic "
            "correctness of agent-generated code.  Human review is required."
        ),
        "human_oversight": (
            "Operators review task results via the Bernstein dashboard; all agent actions are "
            "logged to .sdd/runtime/; agents can be paused or killed via SHUTDOWN signal files."
        ),
        "override_capability": "SHUTDOWN signal file immediately terminates any running agent process",
        "monitoring_procedures": "Heartbeat files updated every 15 s; orchestrator monitors agent liveness",
        "logging_capabilities": (
            "Structured JSONL logs: access.jsonl, task store persistence, agent heartbeats, "
            "EU AI Act assessment log (eu_ai_act_assessments.jsonl)"
        ),
        "audit_trail": "All task state transitions persisted in .sdd/runtime/tasks.jsonl",
        "robustness_testing": "Unit and integration tests covering core orchestration paths",
        "security_measures": (
            "JWT cluster authentication, tenant isolation, rate limiting, "
            "secrets manager integration (Vault / AWS / 1Password)"
        ),
        "accuracy_metrics": "Not applicable — Bernstein orchestrates; accuracy is assessed per-agent",
        "bias_assessment": "Not applicable — orchestration layer; bias risk is in the underlying LLM agents",
        "bias_mitigation": "Not applicable",
        "adversarial_testing": "Pending; planned for post-v1.0 security audit",
        "training_data_sources": "Not applicable",
        "data_preprocessing": "Not applicable",
        "data_quality_measures": "Not applicable",
        "personal_data_handling": (
            "Bernstein logs task metadata only. API keys are managed via secrets vault integration "
            "(not stored in plaintext). No PII is intentionally collected."
        ),
        "data_retention": "Task logs retained until manual purge; no automatic expiry by default",
        "third_party_data": "None collected",
        "instructions_for_use": (
            "See README.md and CLAUDE.md.  Bernstein is not intended for use in "
            "EU AI Act Annex III high-risk domains without additional compliance measures."
        ),
        "capability_limitations": (
            "Bernstein relies on third-party LLMs; outputs are not validated for correctness, "
            "safety, or legal compliance.  Human review is mandatory for high-stakes decisions."
        ),
        "monitoring_plan": "Heartbeat monitoring + orchestrator watchdog; metrics exposed at /status",
        "incident_reporting": "Log incidents to GitHub Issues; serious incidents reported per Article 73",
        "market_surveillance_authority": "Determined by operator's EU member state",
        "feedback_loop": "GitHub Issues and community contributions",
        "risk_management_system": (
            "EU AI Act compliance engine (this module) classifies tasks and generates "
            "conformity evidence packages.  Human operators review high-risk findings."
        ),
        "data_governance_practices": (
            "Bernstein does not train models; it only routes tasks.  "
            "Data governance obligations fall to the underlying LLM providers."
        ),
        "technical_documentation_reference": (
            "Generated by Bernstein compliance engine — see evidence_package.json"
        ),
        "disclosure_mechanism": "Not applicable — Bernstein is a developer tool, not a consumer-facing AI",
        "harmonised_standards": "Pending adoption of harmonised EU AI Act standards (CEN/CENELEC)",
        "notified_body": "Not yet engaged",
        "ce_marking_status": "Not applicable for current risk category",
        "eu_db_registration": "Not required for current risk category",
        "declaration_of_conformity": "Not required for current risk category",
        "output_interpretability": (
            "Agent outputs are plain text / code diffs; all intermediate steps logged to .sdd/"
        ),
    }
    if metadata:
        base_metadata.update(metadata)

    return SystemDescriptor(
        name="Bernstein",
        version=version,
        description=(
            "Multi-agent orchestration platform for CLI coding agents (Claude Code, Codex, Gemini CLI, etc.). "
            "Bernstein spawns short-lived agents, assigns tasks from a shared backlog, "
            "and merges results back into a git repository.  "
            "It is orchestration middleware, not a decision-making AI system."
        ),
        intended_use=(
            "Software development automation: code generation, refactoring, testing, "
            "documentation, and security review — under human developer supervision."
        ),
        deployment_context=deployment_context,
        # Bernstein itself is not in any Annex III high-risk domain
        processes_biometrics=False,
        interacts_with_critical_infrastructure=False,
        used_in_education=False,
        used_in_employment=False,
        used_in_essential_services=False,
        used_in_law_enforcement=False,
        used_in_migration=False,
        used_in_justice=False,
        # Not Article 5 prohibited
        real_time_biometric_public=False,
        subliminal_techniques=False,
        exploits_vulnerabilities=False,
        social_scoring_public=False,
        manipulates_behavior=False,
        # Article 50 transparency
        is_chatbot_or_conversational=False,
        generates_synthetic_media=False,
        uses_emotion_recognition=False,
        metadata=base_metadata,
    )
