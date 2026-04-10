"""Vertical-specific agent packs for regulated industries.

Pre-built role and quality-gate configurations for common verticals
(FinTech, HealthTech, GovTech).  Each pack bundles compliance-relevant
roles, automated quality gates, and tagging metadata so that a single
``bernstein.yaml`` snippet can bootstrap an entire regulated pipeline.

Usage::

    from bernstein.core.vertical_packs import get_pack, generate_pack_config

    pack = get_pack("fintech")
    if pack:
        snippet = generate_pack_config(pack)
        print(snippet)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import yaml

# ---------------------------------------------------------------------------
# Spec dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityGateSpec:
    """A single quality gate bundled with a vertical pack.

    Attributes:
        name: Short identifier (e.g. ``pci-dss-scan``).
        command: Shell command to execute.
        description: Human-readable explanation of what the gate checks.
        severity: ``"error"`` hard-blocks the pipeline; ``"warning"`` is advisory.
    """

    name: str
    command: str
    description: str
    severity: Literal["error", "warning"]


@dataclass(frozen=True)
class RoleSpec:
    """A role definition bundled with a vertical pack.

    Attributes:
        name: Role identifier (e.g. ``pci-auditor``).
        model: Recommended model for the role.
        effort: Recommended effort level (``"low"``, ``"medium"``, ``"high"``).
        description: What this role does in the pipeline.
    """

    name: str
    model: str
    effort: str
    description: str


@dataclass(frozen=True)
class VerticalPack:
    """A complete vertical-specific agent pack.

    Attributes:
        pack_id: Unique identifier (e.g. ``fintech``).
        display_name: Human-readable name.
        description: Summary of the pack's purpose.
        industry: Industry vertical this pack targets.
        roles: Role definitions to inject into the pipeline.
        quality_gates: Quality gates to enforce.
        compliance_tags: Tags for compliance metadata (e.g. ``PCI-DSS``, ``SOX``).
    """

    pack_id: str
    display_name: str
    description: str
    industry: str
    roles: list[RoleSpec] = field(default_factory=list)
    quality_gates: list[QualityGateSpec] = field(default_factory=list)
    compliance_tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Built-in packs
# ---------------------------------------------------------------------------

BUILTIN_PACKS: dict[str, VerticalPack] = {
    "fintech": VerticalPack(
        pack_id="fintech",
        display_name="FinTech Compliance Pack",
        description="PCI-DSS scanning, SOX audit trails, and financial-services compliance roles.",
        industry="Financial Technology",
        roles=[
            RoleSpec(
                name="pci-auditor",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Audits code changes for PCI-DSS compliance violations.",
            ),
            RoleSpec(
                name="sox-compliance",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Enforces SOX change-management controls and audit trails.",
            ),
            RoleSpec(
                name="fraud-detection-reviewer",
                model="anthropic/claude-sonnet-4-20250514",
                effort="medium",
                description="Reviews transaction-processing code for fraud-detection gaps.",
            ),
        ],
        quality_gates=[
            QualityGateSpec(
                name="pci-dss-scan",
                command="bernstein gate pci-dss-scan",
                description="Scans for PCI-DSS violations in payment-processing code.",
                severity="error",
            ),
            QualityGateSpec(
                name="sox-audit-trail",
                command="bernstein gate sox-audit-trail",
                description="Verifies SOX-compliant audit trail generation.",
                severity="error",
            ),
            QualityGateSpec(
                name="secrets-scan",
                command="bernstein gate secrets-scan",
                description="Detects hardcoded API keys, tokens, and credentials.",
                severity="error",
            ),
        ],
        compliance_tags=["PCI-DSS", "SOX", "SOC2"],
    ),
    "healthtech": VerticalPack(
        pack_id="healthtech",
        display_name="HealthTech Compliance Pack",
        description="PHI detection, HIPAA audit controls, and healthcare-specific review roles.",
        industry="Health Technology",
        roles=[
            RoleSpec(
                name="hipaa-auditor",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Audits code for HIPAA compliance and PHI handling violations.",
            ),
            RoleSpec(
                name="phi-detector",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Scans outputs and diffs for Protected Health Information leakage.",
            ),
            RoleSpec(
                name="ehr-integration-reviewer",
                model="anthropic/claude-sonnet-4-20250514",
                effort="medium",
                description="Reviews EHR/EMR integration code for data-integrity issues.",
            ),
        ],
        quality_gates=[
            QualityGateSpec(
                name="phi-detection",
                command="bernstein gate phi-detection",
                description="Detects Protected Health Information in code and outputs.",
                severity="error",
            ),
            QualityGateSpec(
                name="hipaa-audit",
                command="bernstein gate hipaa-audit",
                description="Verifies HIPAA-compliant access controls and audit logging.",
                severity="error",
            ),
            QualityGateSpec(
                name="encryption-at-rest",
                command="bernstein gate encryption-at-rest",
                description="Ensures PHI data stores use AES-256 encryption at rest.",
                severity="warning",
            ),
        ],
        compliance_tags=["HIPAA", "HITECH", "SOC2"],
    ),
    "govtech": VerticalPack(
        pack_id="govtech",
        display_name="GovTech Compliance Pack",
        description="STIG hardening checks, FedRAMP controls, and government-sector review roles.",
        industry="Government Technology",
        roles=[
            RoleSpec(
                name="fedramp-auditor",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Audits infrastructure and application code for FedRAMP compliance.",
            ),
            RoleSpec(
                name="stig-reviewer",
                model="anthropic/claude-sonnet-4-20250514",
                effort="high",
                description="Reviews system configurations against DISA STIG benchmarks.",
            ),
            RoleSpec(
                name="supply-chain-auditor",
                model="anthropic/claude-sonnet-4-20250514",
                effort="medium",
                description="Validates software supply-chain integrity and SBOM completeness.",
            ),
        ],
        quality_gates=[
            QualityGateSpec(
                name="stig-check",
                command="bernstein gate stig-check",
                description="Validates configurations against DISA STIG benchmarks.",
                severity="error",
            ),
            QualityGateSpec(
                name="fedramp-controls",
                command="bernstein gate fedramp-controls",
                description="Checks FedRAMP Moderate/High baseline control implementation.",
                severity="error",
            ),
            QualityGateSpec(
                name="sbom-validation",
                command="bernstein gate sbom-validation",
                description="Validates Software Bill of Materials completeness and integrity.",
                severity="warning",
            ),
        ],
        compliance_tags=["FedRAMP", "FISMA", "NIST-800-53"],
    ),
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_pack(pack_id: str) -> VerticalPack | None:
    """Return a built-in vertical pack by ID, or ``None`` if unknown."""
    return BUILTIN_PACKS.get(pack_id)


def list_packs() -> list[str]:
    """Return sorted list of available pack IDs."""
    return sorted(BUILTIN_PACKS)


def generate_pack_config(pack: VerticalPack) -> str:
    """Generate a ``bernstein.yaml`` snippet for the given pack.

    The output is valid YAML that can be merged into an existing
    ``bernstein.yaml`` configuration file.

    Args:
        pack: The vertical pack to generate configuration for.

    Returns:
        A YAML string containing roles and quality_gates sections.
    """
    config: dict[str, object] = {
        "vertical_pack": pack.pack_id,
        "industry": pack.industry,
        "compliance_tags": pack.compliance_tags,
        "roles": [
            {
                "name": role.name,
                "model": role.model,
                "effort": role.effort,
                "description": role.description,
            }
            for role in pack.roles
        ],
        "quality_gates": [
            {
                "name": gate.name,
                "command": gate.command,
                "description": gate.description,
                "severity": gate.severity,
            }
            for gate in pack.quality_gates
        ],
    }
    return yaml.dump(config, default_flow_style=False, sort_keys=False)
