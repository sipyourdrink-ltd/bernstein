"""EU AI Act Compliance Engine & Control Registry for Bernstein.

Provides Annex III risk classification, technical documentation generation
(Annex IV), automated conformity assessment, and centralized compliance
control registry mapped across regulatory frameworks.
"""

from __future__ import annotations

from bernstein.compliance.controls import (
    DEFAULT_REGISTRY,
    STANDARD_CONTROLS,
    Control,
    ControlRegistry,
    get_default_registry,
)
from bernstein.compliance.eu_ai_act import (
    AnnexIIIDomain,
    ClassificationResult,
    ComplianceEngine,
    ConformityAssessor,
    ConformityCheck,
    ConformityResult,
    RiskCategory,
    SystemDescriptor,
    TechDoc,
    TechDocGenerator,
)
from bernstein.compliance.evidence_pack import (
    SCHEMA_VERSION as EVIDENCE_PACK_SCHEMA_VERSION,
)
from bernstein.compliance.evidence_pack import (
    SUPPORTED_STANDARDS,
    EvidencePack,
    build_evidence_pack,
    get_standard_map,
    verify_evidence_pack,
)
from bernstein.compliance.iso42001 import control_map as iso42001_control_map
from bernstein.compliance.oscal import (
    build_oscal_assessment_results,
    validate_oscal_assessment_results,
)
from bernstein.compliance.owasp_asi import control_map as owasp_asi_control_map
from bernstein.compliance.owasp_skills import control_map as owasp_skills_control_map

__all__ = [
    "DEFAULT_REGISTRY",
    "EVIDENCE_PACK_SCHEMA_VERSION",
    "STANDARD_CONTROLS",
    "SUPPORTED_STANDARDS",
    "AnnexIIIDomain",
    "ClassificationResult",
    "ComplianceEngine",
    "ConformityAssessor",
    "ConformityCheck",
    "ConformityResult",
    "Control",
    "ControlRegistry",
    "EvidencePack",
    "RiskCategory",
    "SystemDescriptor",
    "TechDoc",
    "TechDocGenerator",
    "build_evidence_pack",
    "build_oscal_assessment_results",
    "get_default_registry",
    "get_standard_map",
    "iso42001_control_map",
    "owasp_asi_control_map",
    "owasp_skills_control_map",
    "validate_oscal_assessment_results",
    "verify_evidence_pack",
]
