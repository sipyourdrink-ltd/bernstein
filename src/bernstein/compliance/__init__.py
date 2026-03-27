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

__all__ = [
    "AnnexIIIDomain",
    "ClassificationResult",
    "ComplianceEngine",
    "ConformityAssessor",
    "ConformityCheck",
    "ConformityResult",
    "RiskCategory",
    "SystemDescriptor",
    "TechDoc",
    "TechDocGenerator",
]
