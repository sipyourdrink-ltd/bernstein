"""
Control registry: unified mapping from benchmark suites and evidence packs to compliance controls.

Provides a single registry of compliance controls spanning:
- FINOS AI Governance Framework (AIGF)
- EU AI Act (Regulation (EU) 2024/1689)
- OWASP Top 10 for Agentic Applications (ASI01-ASI10)
- OWASP Agentic Skills Top 10 (AST01-AST10)
- ISO/IEC 42001:2023 (AI Management System)

Benchmark suites declare `controls: list[str]` referencing these IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.eval.bench.suite import BenchSuite


@dataclass(frozen=True)
class ControlEntry:
    """A compliance control definition."""

    control_id: str
    title: str
    description: str
    references: dict[str, str]
    evidence_kinds: tuple[str, ...] = ("audit_chain",)
    status: str = "mapped"

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "description": self.description,
            "references": dict(self.references),
            "evidence_kinds": list(self.evidence_kinds),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ControlEntry:
        return cls(
            control_id=str(raw["control_id"]),
            title=str(raw["title"]),
            description=str(raw.get("description", "")),
            references=dict(raw.get("references", {})),
            evidence_kinds=tuple(raw.get("evidence_kinds", ("audit_chain",))),
            status=str(raw.get("status", "mapped")),
        )


_CANONICAL_CONTROLS: tuple[ControlEntry, ...] = (
    # --- FINOS AIGF & Core Governance Controls ---
    ControlEntry(
        control_id="CTRL-AUDIT-TRAIL",
        title="Audit Trail & Tamper-Evident Logging",
        description="HMAC-chained immutable event log recording all agent actions, transitions, and decisions.",
        references={
            "finos_aigf": "CTRL-AUDIT-TRAIL",
            "eu_ai_act": "Article 12",
            "iso42001": "A.6.2.8",
            "owasp_asi": "ASI01",
        },
        evidence_kinds=("audit_chain", "receipt"),
    ),
    ControlEntry(
        control_id="CTRL-DATA-LINEAGE",
        title="Data & Artifact Provenance Lineage",
        description="Per-artifact lineage log tracking parent-child hashes, signatures, and transformation stages.",
        references={
            "finos_aigf": "CTRL-DATA-LINEAGE",
            "eu_ai_act": "Article 10",
            "iso42001": "A.7.2",
            "owasp_skills": "AST02",
        },
        evidence_kinds=("lineage_log", "receipt"),
    ),
    ControlEntry(
        control_id="CTRL-MODEL-SUPPLY-CHAIN",
        title="Model Supply Chain & Artifact Attestation",
        description="Signed attestations, Ed25519 identity verification, and SLSA provenance for runtime artifacts.",
        references={
            "finos_aigf": "CTRL-MODEL-SUPPLY-CHAIN",
            "iso42001": "A.8.2",
            "owasp_skills": "AST01",
        },
        evidence_kinds=("signature", "attestation"),
    ),
    ControlEntry(
        control_id="CTRL-TOOL-INVENTORY",
        title="Tool & Adapter Capability Inventory",
        description="Capability matrix screening tool combinations (e.g. lethal trifecta prevention).",
        references={
            "finos_aigf": "CTRL-TOOL-INVENTORY",
            "owasp_asi": "ASI02",
            "owasp_skills": "AST04",
        },
        evidence_kinds=("audit_chain", "capability_matrix"),
    ),
    ControlEntry(
        control_id="CTRL-HUMAN-OVERSIGHT",
        title="Human Oversight & Approval Gates",
        description="Pre-execution authorization and dual-approval enforcement for privileged tasks.",
        references={
            "finos_aigf": "CTRL-HUMAN-OVERSIGHT",
            "eu_ai_act": "Article 14",
            "iso42001": "A.6.2.7",
            "owasp_asi": "ASI03",
        },
        evidence_kinds=("approval_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-ACCESS-CONTROL",
        title="Access Control & Privilege Scoping",
        description="Role-based access control, tool deny-lists, and execution permission graphs.",
        references={
            "finos_aigf": "CTRL-ACCESS-CONTROL",
            "iso42001": "A.6.2.2",
            "owasp_asi": "ASI03",
        },
        evidence_kinds=("audit_chain", "rbac_policy"),
    ),
    ControlEntry(
        control_id="CTRL-DATA-RESIDENCY",
        title="Data Residency & Sovereign Boundaries",
        description="Region policy gating and sovereign boundary enforcement on agent workflows.",
        references={
            "finos_aigf": "CTRL-DATA-RESIDENCY",
            "eu_ai_act": "Article 10",
        },
        evidence_kinds=("audit_chain", "policy_receipt"),
    ),
    ControlEntry(
        control_id="CTRL-PII-PROTECTION",
        title="PII & Sensitive Data Protection",
        description="DLP scanning, secret filtering, and differential privacy checks on inputs and outputs.",
        references={
            "finos_aigf": "CTRL-PII-PROTECTION",
            "eu_ai_act": "Article 10",
            "iso42001": "A.7.4",
            "owasp_skills": "AST08",
        },
        evidence_kinds=("dlp_scan", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-PROMPT-INJECTION-DEFENCE",
        title="Prompt Injection & Instruction Defense",
        description="Screening against prompt injection, jailbreaking, and goal deviation attacks.",
        references={
            "finos_aigf": "CTRL-PROMPT-INJECTION-DEFENCE",
            "owasp_asi": "ASI01",
            "eu_ai_act": "Article 15",
        },
        evidence_kinds=("guardrail_event", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-INCIDENT-RESPONSE",
        title="Incident Response & Quarantine",
        description="Automated denial tracking, anomaly correlation, and agent workspace isolation upon breach.",
        references={
            "finos_aigf": "CTRL-INCIDENT-RESPONSE",
            "eu_ai_act": "Article 73",
            "iso42001": "A.6.2.6",
            "owasp_asi": "ASI08",
        },
        evidence_kinds=("incident_timeline", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-SEGREGATION-OF-DUTIES",
        title="Segregation of Duties & Isolation",
        description="Per-role tool isolation, separation of developer and reviewer agent identities.",
        references={
            "finos_aigf": "CTRL-SEGREGATION-OF-DUTIES",
            "iso42001": "A.6.2.3",
        },
        evidence_kinds=("role_policy", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-RETENTION",
        title="Evidence Retention & Immutability",
        description="Configurable retention periods (e.g. 10 years for EU high-risk) with boundary hash verification.",
        references={
            "finos_aigf": "CTRL-RETENTION",
            "eu_ai_act": "Article 12(3)",
            "iso42001": "A.6.2.8",
        },
        evidence_kinds=("retention_manifest", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-ENCRYPTION-AT-REST",
        title="Encryption at Rest & Secret Storage",
        description="State file encryption and OS keychain integration for agent credentials.",
        references={
            "finos_aigf": "CTRL-ENCRYPTION-AT-REST",
            "iso42001": "A.6.2.4",
        },
        evidence_kinds=("key_store", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-ENCRYPTION-IN-TRANSIT",
        title="Encryption in Transit & mTLS",
        description="mTLS cluster communication and TLS connection pinning for agent API requests.",
        references={
            "finos_aigf": "CTRL-ENCRYPTION-IN-TRANSIT",
            "iso42001": "A.6.2.4",
            "owasp_asi": "ASI07",
        },
        evidence_kinds=("tls_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-DEPENDENCY-INTEGRITY",
        title="Dependency Integrity & Software BOM",
        description="Software Bill of Materials generation, license compliance, and CVE vulnerability scanning.",
        references={
            "finos_aigf": "CTRL-DEPENDENCY-INTEGRITY",
            "iso42001": "A.8.4",
            "owasp_skills": "AST06",
        },
        evidence_kinds=("sbom", "audit_chain"),
    ),
    ControlEntry(
        control_id="CTRL-CHANGE-MANAGEMENT",
        title="Change Management & Commit Signing",
        description="Write-ahead logging, git commit signatures, and deterministic reproducible builds.",
        references={
            "finos_aigf": "CTRL-CHANGE-MANAGEMENT",
            "iso42001": "A.9.2",
        },
        evidence_kinds=("wal", "audit_chain"),
    ),
    # --- OWASP ASI Top 10 Controls ---
    ControlEntry(
        control_id="ASI01",
        title="Agent Goal & Instruction Hijack",
        description="Protection against direct and indirect prompt injection compromising agent objectives.",
        references={"owasp_asi": "ASI01", "finos_aigf": "CTRL-PROMPT-INJECTION-DEFENCE"},
        evidence_kinds=("audit_chain", "guardrail_event"),
    ),
    ControlEntry(
        control_id="ASI02",
        title="Tool Misuse & Excessive Agency",
        description="Prevention of lethal tool combinations and unbounded autonomous agent action.",
        references={"owasp_asi": "ASI02", "finos_aigf": "CTRL-TOOL-INVENTORY"},
        evidence_kinds=("capability_matrix_refusal", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI03",
        title="Identity & Privilege Abuse",
        description="Guards against unauthorized elevation of privileges and unverified agent identity.",
        references={"owasp_asi": "ASI03", "finos_aigf": "CTRL-ACCESS-CONTROL"},
        evidence_kinds=("approval_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI04",
        title="Supply Chain & Skill Provenance",
        description="Verification of cryptographic signatures on third-party skill code before execution.",
        references={"owasp_asi": "ASI04", "owasp_skills": "AST01", "finos_aigf": "CTRL-MODEL-SUPPLY-CHAIN"},
        evidence_kinds=("signature", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI05",
        title="Unexpected Code Execution",
        description="Sandboxed command isolation and strict allow-listing of executable binary paths.",
        references={"owasp_asi": "ASI05", "owasp_skills": "AST05"},
        evidence_kinds=("sandbox_event", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI06",
        title="Memory & Context Poisoning",
        description="Isolation and validation of long-term memory records and agent context inputs.",
        references={"owasp_asi": "ASI06"},
        evidence_kinds=("lineage_log", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI07",
        title="Insecure Inter-Agent Communication",
        description="Cryptographic authentication and tamper checks on message buses between agents.",
        references={"owasp_asi": "ASI07", "finos_aigf": "CTRL-ENCRYPTION-IN-TRANSIT"},
        evidence_kinds=("tls_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI08",
        title="Cascading Agent Failure",
        description="Failure isolation, circuit breakers, and bounded recovery in multi-agent workflows.",
        references={"owasp_asi": "ASI08", "finos_aigf": "CTRL-INCIDENT-RESPONSE"},
        evidence_kinds=("denial_tracker", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI09",
        title="Agent Impersonation",
        description="Strict verification of agent cards, cryptographic keys, and ephemeral sessions.",
        references={"owasp_asi": "ASI09", "finos_aigf": "CTRL-ACCESS-CONTROL"},
        evidence_kinds=("agent_card", "audit_chain"),
    ),
    ControlEntry(
        control_id="ASI10",
        title="Rogue Agent Emergence",
        description="Deterministic orchestrator scheduling without LLMs in the state machine loop.",
        references={"owasp_asi": "ASI10", "finos_aigf": "CTRL-HUMAN-OVERSIGHT"},
        evidence_kinds=("deterministic_replay", "audit_chain"),
    ),
    # --- OWASP Skills Top 10 Controls ---
    ControlEntry(
        control_id="AST01",
        title="Untrusted Skill Installation",
        description="Signature checking against trusted catalog keys before skill registration.",
        references={"owasp_skills": "AST01", "finos_aigf": "CTRL-MODEL-SUPPLY-CHAIN"},
        evidence_kinds=("signature", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST02",
        title="Post-Publish Skill Tampering",
        description="Content hash verification and lineage tracking to detect modified skill files.",
        references={"owasp_skills": "AST02", "finos_aigf": "CTRL-DATA-LINEAGE"},
        evidence_kinds=("lineage_log", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST03",
        title="Skill Provenance & Pinning",
        description="Version pinning and immutable history for skill installs and upgrades.",
        references={"owasp_skills": "AST03"},
        evidence_kinds=("audit_chain",),
    ),
    ControlEntry(
        control_id="AST04",
        title="Excessive Skill Capability",
        description="Screening skill capability declarations against the lethal trifecta matrix.",
        references={"owasp_skills": "AST04", "owasp_asi": "ASI02"},
        evidence_kinds=("capability_matrix_refusal", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST05",
        title="Unsafe Skill Execution",
        description="Sandboxed environment enforcement for commands invoked by agent skills.",
        references={"owasp_skills": "AST05", "owasp_asi": "ASI05"},
        evidence_kinds=("sandbox_event", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST06",
        title="Skill Dependency Compromise",
        description="Vulnerability and dependency scanning of packages imported by skills.",
        references={"owasp_skills": "AST06", "finos_aigf": "CTRL-DEPENDENCY-INTEGRITY"},
        evidence_kinds=("sbom", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST07",
        title="Skill Permission Creep",
        description="Granular permission grants and periodic validation of skill tool access.",
        references={"owasp_skills": "AST07", "finos_aigf": "CTRL-ACCESS-CONTROL"},
        evidence_kinds=("rbac_policy", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST08",
        title="Skill Data Exfiltration",
        description="Egress gateway screening to prevent skill-driven exfiltration of sensitive data.",
        references={"owasp_skills": "AST08", "finos_aigf": "CTRL-PII-PROTECTION"},
        evidence_kinds=("dlp_scan", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST09",
        title="Skill Confused Deputy",
        description="Enforcing principal identity propagation across multi-tier skill delegations.",
        references={"owasp_skills": "AST09", "owasp_asi": "ASI03"},
        evidence_kinds=("approval_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="AST10",
        title="Deprecated & Abandoned Skills",
        description="Lifecycle tracking and automated sunsetting of unmaintained skills.",
        references={"owasp_skills": "AST10"},
        evidence_kinds=("audit_chain",),
    ),
    # --- EU AI Act Controls ---
    ControlEntry(
        control_id="EU-AI-ACT-ART05",
        title="Prohibited AI Practices Screening",
        description="Automated pre-flight verification that system use-case avoids prohibited practices.",
        references={"eu_ai_act": "Article 5"},
        evidence_kinds=("assessment_record", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART09",
        title="Risk Management System",
        description="Continuous identification and mitigation of agentic operational risks.",
        references={"eu_ai_act": "Article 9", "iso42001": "A.5.2"},
        evidence_kinds=("risk_assessment", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART10",
        title="Data and Data Governance",
        description="Data provenance, bias screening, and governance over training/context datasets.",
        references={"eu_ai_act": "Article 10", "iso42001": "A.7.2"},
        evidence_kinds=("lineage_log", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART11",
        title="Technical Documentation",
        description="Automated compilation of Annex IV compliant technical documentation packs.",
        references={"eu_ai_act": "Article 11", "iso42001": "A.6.2.5"},
        evidence_kinds=("evidence_pack",),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART12",
        title="Automatic Event Recording (Article 12)",
        description="Tamper-evident logs generated over the lifetime of the high-risk AI system.",
        references={
            "eu_ai_act": "Article 12",
            "finos_aigf": "CTRL-AUDIT-TRAIL",
            "iso42001": "A.6.2.8",
        },
        evidence_kinds=("audit_chain", "article12_bundle"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART13",
        title="Transparency and User Information",
        description="Clear operational visibility and capability transparency for AI system users.",
        references={"eu_ai_act": "Article 13"},
        evidence_kinds=("system_description", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART14",
        title="Human Oversight (Article 14)",
        description="Design enabling natural persons to oversee, intervene, and stop AI executions.",
        references={"eu_ai_act": "Article 14", "finos_aigf": "CTRL-HUMAN-OVERSIGHT"},
        evidence_kinds=("approval_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART15",
        title="Accuracy, Robustness and Cybersecurity",
        description="Resilience against attacks, reproducibility guarantees, and fail-safe operation.",
        references={"eu_ai_act": "Article 15", "iso42001": "A.6.2.6"},
        evidence_kinds=("benchmark_bundle", "audit_chain"),
    ),
    ControlEntry(
        control_id="EU-AI-ACT-ART73",
        title="Serious Incident Notification",
        description="Automated capture and collation of incident evidence for regulatory reporting.",
        references={"eu_ai_act": "Article 73", "finos_aigf": "CTRL-INCIDENT-RESPONSE"},
        evidence_kinds=("incident_pack", "audit_chain"),
    ),
    # --- ISO/IEC 42001 Annex A Controls ---
    ControlEntry(
        control_id="ISO-42001-A628",
        title="AI System Event Logging (A.6.2.8)",
        description="Recording of system events, agent lifecycle transitions, and executed tools.",
        references={"iso42001": "A.6.2.8", "finos_aigf": "CTRL-AUDIT-TRAIL"},
        evidence_kinds=("audit_chain",),
    ),
    ControlEntry(
        control_id="ISO-42001-A626",
        title="AI System Operation & Monitoring (A.6.2.6)",
        description="Continuous monitoring of operational metrics and performance thresholds.",
        references={"iso42001": "A.6.2.6", "finos_aigf": "CTRL-INCIDENT-RESPONSE"},
        evidence_kinds=("audit_chain", "sla_report"),
    ),
    ControlEntry(
        control_id="ISO-42001-A627",
        title="Human Oversight of AI Systems (A.6.2.7)",
        description="Mechanisms for human review and intervention in autonomous task sequences.",
        references={"iso42001": "A.6.2.7", "finos_aigf": "CTRL-HUMAN-OVERSIGHT"},
        evidence_kinds=("approval_receipt", "audit_chain"),
    ),
    ControlEntry(
        control_id="ISO-42001-A72",
        title="Data for AI Systems & Lineage (A.7.2)",
        description="Management and provenance tracking of datasets and intermediate artifacts.",
        references={"iso42001": "A.7.2", "finos_aigf": "CTRL-DATA-LINEAGE"},
        evidence_kinds=("lineage_log", "audit_chain"),
    ),
    ControlEntry(
        control_id="ISO-42001-A82",
        title="Third-Party AI Components & Suppliers (A.8.2)",
        description="Verification and provenance tracking of third-party models and skills.",
        references={"iso42001": "A.8.2", "finos_aigf": "CTRL-MODEL-SUPPLY-CHAIN"},
        evidence_kinds=("attestation", "audit_chain"),
    ),
)


class ControlRegistry:
    """Registry of compliance controls."""

    def __init__(self, controls: tuple[ControlEntry, ...] = _CANONICAL_CONTROLS) -> None:
        self._controls: dict[str, ControlEntry] = {c.control_id: c for c in controls}

    def get(self, control_id: str) -> ControlEntry:
        """Fetch a control by ID. Raises KeyError if unknown."""
        if control_id not in self._controls:
            raise KeyError(f"Unknown control ID: {control_id!r}")
        return self._controls[control_id]

    def has(self, control_id: str) -> bool:
        """Check if a control ID is registered."""
        return control_id in self._controls

    def list_all(self, framework: str | None = None) -> list[ControlEntry]:
        """List all controls, optionally filtered by framework."""
        if framework is None:
            return list(self._controls.values())
        return [c for c in self._controls.values() if framework in c.references]

    def validate_suite(self, suite: BenchSuite) -> list[str]:
        """Validate that a suite declares at least one valid control.

        Returns a list of validation error strings (empty if valid).
        """
        errors: list[str] = []
        if not getattr(suite, "controls", None):
            errors.append(f"Suite {suite.version!r} ({suite.suite_hash[:12]}) declares no controls.")
            return errors

        for cid in suite.controls:
            if not self.has(cid):
                errors.append(
                    f"Suite {suite.version!r} references unknown control {cid!r}. "
                    f"Must be registered in compliance/controls.py."
                )

        return errors


_GLOBAL_REGISTRY = ControlRegistry()


def get_control_registry() -> ControlRegistry:
    return _GLOBAL_REGISTRY


def get_control(control_id: str) -> ControlEntry:
    return _GLOBAL_REGISTRY.get(control_id)


def list_controls(framework: str | None = None) -> list[ControlEntry]:
    return _GLOBAL_REGISTRY.list_all(framework=framework)


def validate_control_id(control_id: str) -> bool:
    return _GLOBAL_REGISTRY.has(control_id)


def validate_suite_controls(suite: BenchSuite) -> list[str]:
    return _GLOBAL_REGISTRY.validate_suite(suite)


def generate_controls_markdown_table() -> str:
    """Generate a Markdown summary table of all registered compliance controls."""
    lines = [
        "| Control ID | Title | Framework References | Evidence Kinds | Status |",
        "|---|---|---|---|---|",
    ]
    for c in list_controls():
        refs = ", ".join(f"`{k}: {v}`" for k, v in sorted(c.references.items()))
        evidence = ", ".join(c.evidence_kinds)
        lines.append(f"| `{c.control_id}` | {c.title} | {refs} | {evidence} | {c.status} |")
    return "\n".join(lines)
