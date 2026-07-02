"""EU AI Act Compliance Engine for Bernstein.

Provides Annex III risk classification, technical documentation generation
(Annex IV), and automated conformity assessment per EU AI Act requirements.
Mandatory by August 2027.
"""

from __future__ import annotations

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
)
from bernstein.compliance.owasp_asi import control_map as owasp_asi_control_map
from bernstein.compliance.owasp_skills import control_map as owasp_skills_control_map

__all__ = [
    "EVIDENCE_PACK_SCHEMA_VERSION",
    "SUPPORTED_STANDARDS",
    "AnnexIIIDomain",
    "ClassificationResult",
    "ComplianceEngine",
    "ConformityAssessor",
    "ConformityCheck",
    "ConformityResult",
    "EvidencePack",
    "RiskCategory",
    "SystemDescriptor",
    "TechDoc",
    "TechDocGenerator",
    "build_evidence_pack",
    "get_standard_map",
    "owasp_asi_control_map",
    "owasp_skills_control_map",
]
