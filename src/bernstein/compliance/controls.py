"""
Compliance Control Registry for Bernstein.

Maintains the central taxonomy of governance, security, and verification
controls mapped across regulatory frameworks:
- EU AI Act (Regulation (EU) 2024/1689)
- OWASP Top 10 for Agentic Applications (ASI01-ASI10)
- OWASP Top 10 for Agentic Skills (AST01-AST10)
- NIST AI Risk Management Framework (AI RMF 1.0)
- ISO/IEC 42001:2023 (Artificial Intelligence Management System)
- FINOS AI Governance Framework (AIGF)

Every benchmark suite declares the control IDs it measures; unmapped suites
or invalid control declarations fail build-time validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.eval.bench.suite import BenchSuite


@dataclass(frozen=True)
class Control:
    """A compliance or security control with cross-framework mapping."""

    control_id: str
    title: str
    description: str
    references: dict[str, str] = field(default_factory=dict)
    evidence_kinds: list[str] = field(default_factory=list)
    category: str = "governance"

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "description": self.description,
            "references": dict(self.references),
            "evidence_kinds": list(self.evidence_kinds),
            "category": self.category,
        }


# ---------------------------------------------------------------------------
# Standard Control Definitions (>= 30 pre-populated controls)
# ---------------------------------------------------------------------------

STANDARD_CONTROLS: tuple[Control, ...] = (
    Control(
        control_id="CTL-GOV-01",
        title="Policy as Code & Governance Boundary",
        description="Deterministic enforcement of organizational policies before and during agent task execution.",
        references={
            "eu_ai_act": "Article 13(1) - Transparency and provision of instructions",
            "nist_ai_rmf": "GOVERN-1.1",
            "iso_42001": "A.5.2 AI Policy",
            "finos_aigf": "AIGF-GOV-01 Organizational Governance",
        },
        evidence_kinds=["audit_chain", "policy", "lineage_log"],
        category="governance",
    ),
    Control(
        control_id="CTL-GOV-02",
        title="Agent Identity & System Card Declaration",
        description=(
            "Every autonomous agent declares its identity, capabilities, model, "
            "and operating boundaries in a verified Agent Card."
        ),
        references={
            "eu_ai_act": "Article 13(2) - Technical capabilities and characteristics declaration",
            "nist_ai_rmf": "MAP-1.1",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-GOV-02 Model Identity & Registry",
        },
        evidence_kinds=["agent_card", "lineage_log"],
        category="governance",
    ),
    Control(
        control_id="CTL-AUD-01",
        title="Tamper-Evident HMAC Audit Logging",
        description=(
            "Every tool call, policy check, model query, and verdict is recorded in an "
            "RFC 2104 HMAC-chained append-only log."
        ),
        references={
            "eu_ai_act": "Article 12(1) - Automatic recording of events (logging)",
            "nist_ai_rmf": "GOVERN-4.1",
            "iso_42001": "A.8.4 Logging and Monitoring",
            "finos_aigf": "AIGF-AUD-01 Immutable Audit Logging",
        },
        evidence_kinds=["audit_chain"],
        category="audit",
    ),
    Control(
        control_id="CTL-AUD-02",
        title="Audit Chain Continuity & Retention Verification",
        description=(
            "Cryptographic verification of audit chain boundaries, continuity across segments, "
            "and retention compliance without truncation."
        ),
        references={
            "eu_ai_act": "Article 12(3) - Logging retention over high-risk AI system lifetime",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.4 Logging and Monitoring",
            "finos_aigf": "AIGF-AUD-02 Log Retention & Continuity",
        },
        evidence_kinds=["audit_chain", "retention_evidence"],
        category="audit",
    ),
    Control(
        control_id="CTL-LIN-01",
        title="Artifact Lineage & Provenance Tracking",
        description=(
            "Sigstore-style transparency log recording per-artifact creation, mutation, "
            "input hashes, and cryptographic signatures."
        ),
        references={
            "eu_ai_act": "Article 11 & Annex IV - Technical documentation & traceability",
            "nist_ai_rmf": "MAP-1.5",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": "AIGF-DAT-01 Artifact Provenance",
        },
        evidence_kinds=["lineage_log", "signatures"],
        category="lineage",
    ),
    Control(
        control_id="CTL-OVS-01",
        title="Human Oversight & Approval Gating",
        description=(
            "High-blast-radius and security-sensitive agent operations require explicit "
            "human approval with recorded identity."
        ),
        references={
            "eu_ai_act": "Article 14 - Human oversight of high-risk AI systems",
            "nist_ai_rmf": "GOVERN-3.2",
            "iso_42001": "A.9.2 Human Oversight",
            "finos_aigf": "AIGF-HUM-01 Human-in-the-Loop Controls",
        },
        evidence_kinds=["approval_receipt", "audit_chain"],
        category="oversight",
    ),
    Control(
        control_id="CTL-OVS-02",
        title="Displayed vs Executed Action Equivalence",
        description=(
            "Attested verification that the exact action shown to the human approver "
            "matches the action executed on the target system."
        ),
        references={
            "eu_ai_act": "Article 14(4) - Verification of system intervention and execution",
            "owasp_asi": "ASI08 - Human-in-the-Loop Bypass / Failure",
            "nist_ai_rmf": "MEASURE-2.5",
            "iso_42001": "A.9.2 Human Oversight",
            "finos_aigf": "AIGF-HUM-02 Action Intent Binding",
        },
        evidence_kinds=["approval_receipt", "oversight_evidence"],
        category="oversight",
    ),
    Control(
        control_id="CTL-SEC-01",
        title="Prompt Injection & Goal Hijack Defense",
        description=(
            "Detection and containment of prompt injection, indirect instructions, "
            "and goal hijacking attempts in agent contexts."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Cybersecurity & adversarial robustness",
            "owasp_asi": "ASI01 - Agent Goal / Instruction Hijack",
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SEC-01 Prompt Injection Defense",
        },
        evidence_kinds=["bench_bundle", "audit_chain"],
        category="security",
    ),
    Control(
        control_id="CTL-SEC-02",
        title="Tool Execution Sandboxing & Authorization",
        description=(
            "Strict permission gating, sandboxing, and seccomp/process isolation for all agent tool invocations."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Cybersecurity & technical robustness",
            "owasp_asi": "ASI02 - Excessive Agency & Privilege Escalation",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SEC-02 Tool Sandboxing & Least Privilege",
        },
        evidence_kinds=["audit_chain", "bench_bundle"],
        category="security",
    ),
    Control(
        control_id="CTL-SEC-03",
        title="Canary Token & Secret Leakage Prevention",
        description=(
            "All-surface scanning for canary tokens, API keys, credentials, and confidential "
            "artifacts across outputs and payloads."
        ),
        references={
            "eu_ai_act": "Article 10(5) - Data governance and confidentiality",
            "owasp_asi": "ASI06 - Sensitive Data Exposure",
            "nist_ai_rmf": "MANAGE-2.2",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": "AIGF-SEC-03 Secret & Data Leakage Prevention",
        },
        evidence_kinds=["bench_bundle", "audit_chain"],
        category="security",
    ),
    Control(
        control_id="CTL-SEC-04",
        title="Gate Evasion & Adversarial Bypass Resistance",
        description=(
            "Benchmarked resilience against adversarial evasions of policy gates, "
            "security filters, and content moderation."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Adversarial robustness & resilience",
            "owasp_asi": "ASI05 - Untrusted Environment Exploitation",
            "nist_ai_rmf": "MEASURE-2.11",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SEC-04 Evasion Resistance",
        },
        evidence_kinds=["bench_bundle"],
        category="security",
    ),
    Control(
        control_id="CTL-SEC-05",
        title="Outbound Model Egress & Policy Boundary Checks",
        description="Deterministic pre-flight checks recorded at the outbound model-call boundary before token egress.",
        references={
            "eu_ai_act": "Article 15(1) - Technical robustness and egress safety",
            "owasp_asi": "ASI04 - Insecure Inter-Agent / External Communication",
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SEC-05 Model Call Boundary Enforcement",
        },
        evidence_kinds=["audit_chain", "check_record"],
        category="security",
    ),
    Control(
        control_id="CTL-ROB-01",
        title="Deterministic Execution & Offline Replay Verification",
        description=(
            "Deterministic scheduling and replay verifier capable of confirming "
            "agent task verdicts offline without external services."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Technical accuracy and repeatability",
            "nist_ai_rmf": "MEASURE-2.1",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-ROB-01 Deterministic Execution & Replay",
        },
        evidence_kinds=["bench_bundle", "verifier_receipt"],
        category="robustness",
    ),
    Control(
        control_id="CTL-ROB-02",
        title="Model Drift & Degradation Detection",
        description=(
            "Continuous statistical tracking of agent task pass rates and execution variance "
            "to detect model degradation across versions."
        ),
        references={
            "eu_ai_act": "Article 15(2) - Post-market monitoring & performance consistency",
            "nist_ai_rmf": "MEASURE-2.6",
            "iso_42001": "A.10.1 Monitoring and Evaluation",
            "finos_aigf": "AIGF-ROB-02 Drift Detection & Performance Monitoring",
        },
        evidence_kinds=["bench_bundle", "drift_report"],
        category="robustness",
    ),
    Control(
        control_id="CTL-ROB-03",
        title="Error Handling & Graceful Degradation",
        description=(
            "Systematic containment of runtime exceptions, retry backoffs, "
            "and deterministic recovery without corrupting state."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Error resilience and fail-safe operation",
            "nist_ai_rmf": "MANAGE-1.2",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-ROB-03 Fault Tolerance & Recovery",
        },
        evidence_kinds=["audit_chain", "bench_bundle"],
        category="robustness",
    ),
    Control(
        control_id="CTL-DATA-01",
        title="Data Governance & Lineage Integrity",
        description=(
            "Verification of input training and evaluation datasets, ensuring integrity, "
            "non-contamination, and strict provenance."
        ),
        references={
            "eu_ai_act": "Article 10 - Data and data governance",
            "nist_ai_rmf": "MAP-2.1",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": "AIGF-DAT-02 Data Governance & Quality",
        },
        evidence_kinds=["lineage_log", "dataset_manifest"],
        category="data",
    ),
    Control(
        control_id="CTL-DATA-02",
        title="Confidential Information & PII Redaction",
        description=(
            "Automated redaction of PII, API tokens, and confidential user data before logging or external egress."
        ),
        references={
            "eu_ai_act": "Article 10(5) - Privacy and personal data protection",
            "owasp_asi": "ASI06 - Sensitive Data Exposure",
            "nist_ai_rmf": "GOVERN-1.2",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": "AIGF-DAT-03 PII & Confidentiality Controls",
        },
        evidence_kinds=["audit_chain", "redaction_log"],
        category="data",
    ),
    Control(
        control_id="CTL-INC-01",
        title="Serious Incident Recording & Timeline Reconstruction",
        description=(
            "Structured capture and cryptographic packaging of serious incident timelines, "
            "audit slices, and causal evidence."
        ),
        references={
            "eu_ai_act": "Article 73 - Reporting of serious incidents",
            "nist_ai_rmf": "MANAGE-4.1",
            "iso_42001": "A.8.4 Incident Management",
            "finos_aigf": "AIGF-INC-01 Serious Incident Management",
        },
        evidence_kinds=["incident_pack", "audit_chain"],
        category="incident",
    ),
    Control(
        control_id="CTL-COST-01",
        title="Token Budget & Cost Allocation Controls",
        description="Enforcement of per-verdict cost tracking, spawn-time token budgets, and CI cost ceiling gates.",
        references={
            "eu_ai_act": "Article 13(1) - Resource utilization transparency",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.5.2 Resource Management",
            "finos_aigf": "AIGF-FIN-01 Financial & Token Budget Controls",
        },
        evidence_kinds=["bench_bundle", "cost_ledger"],
        category="cost",
    ),
    Control(
        control_id="CTL-EVAL-01",
        title="Content-Addressed Benchmark Reproducibility",
        description=(
            "Evaluation task suites are content-addressed by task sequence hash; "
            "identical inputs guarantee verifiable reproducible runs."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Verification and testing standards",
            "nist_ai_rmf": "MEASURE-2.1",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-EVL-01 Benchmark Reproducibility",
        },
        evidence_kinds=["bench_bundle", "suite_hash"],
        category="evaluation",
    ),
    Control(
        control_id="CTL-EVAL-02",
        title="Multi-Run Empirical Determinism Scoring",
        description=(
            "Scoring multi-run reliability across runs k > 1 to detect "
            "non-deterministic agent behavior and flaky executions."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Accuracy and consistency assessment",
            "nist_ai_rmf": "MEASURE-2.2",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-EVL-02 Determinism Scoring",
        },
        evidence_kinds=["bench_bundle", "reliability_report"],
        category="evaluation",
    ),
    Control(
        control_id="CTL-EVAL-03",
        title="Quality Gate & Verification Adjudication",
        description=(
            "Automated adjudication gates recording independent verdicts "
            "and preventing unverified code or artifacts from landing."
        ),
        references={
            "eu_ai_act": "Article 14 - Automated and human quality adjudication",
            "nist_ai_rmf": "MEASURE-1.1",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-EVL-03 Quality Gate Enforcement",
        },
        evidence_kinds=["adjudication_record", "bench_bundle"],
        category="evaluation",
    ),
    Control(
        control_id="CTL-QUAL-01",
        title="Producing Identity & Independence Class Tracking",
        description=(
            "Recording producing agent identity, model, and independence classification "
            "in all verification and adjudication records."
        ),
        references={
            "eu_ai_act": "Article 13(2) & 14 - Provenance of automated decisions",
            "nist_ai_rmf": "GOVERN-3.1",
            "iso_42001": "A.6.2 AI System Assessment",
            "finos_aigf": "AIGF-GOV-03 Separation of Duties & Independence",
        },
        evidence_kinds=["adjudication_record", "audit_chain"],
        category="quality",
    ),
    Control(
        control_id="CTL-QUAL-02",
        title="Automated Test Coverage & Static Verification",
        description=(
            "Mandatory unit test verification, static type checking, "
            "and linter enforcement before build artifact release."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Code quality and static verification",
            "nist_ai_rmf": "MANAGE-1.1",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-DEV-01 Continuous Integration & Verification",
        },
        evidence_kinds=["ci_run", "sarif_report"],
        category="quality",
    ),
    Control(
        control_id="CTL-SKILL-01",
        title="Agentic Skill Discovery & Verification",
        description=(
            "Dynamic discovery and verification of agent skills against authorized catalogs and integrity signatures."
        ),
        references={
            "owasp_skills": "AST01 - Untrusted Skill Execution",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SKL-01 Skill Verification",
        },
        evidence_kinds=["skill_manifest", "audit_chain"],
        category="skills",
    ),
    Control(
        control_id="CTL-SKILL-02",
        title="Skill Execution Boundaries & Permissions",
        description="Fine-grained permission boundaries and scope restrictions for skill pack execution.",
        references={
            "owasp_skills": "AST02 - Excessive Skill Permissions",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SKL-02 Skill Privilege Isolation",
        },
        evidence_kinds=["audit_chain", "policy"],
        category="skills",
    ),
    Control(
        control_id="CTL-SKILL-03",
        title="Untrusted Skill Quarantine & Code Review",
        description="Quarantine and explicit operator review for imported or community skill packs before activation.",
        references={
            "owasp_skills": "AST04 - Malicious Skill Ingestion",
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SKL-03 Skill Ingestion & Quarantine",
        },
        evidence_kinds=["audit_chain", "approval_receipt"],
        category="skills",
    ),
    Control(
        control_id="CTL-MON-01",
        title="Operational Health & Status Dashboarding",
        description="Real-time status monitoring, agent liveness tracking, and operational metrics reporting.",
        references={
            "eu_ai_act": "Article 13(1) - Operational transparency",
            "nist_ai_rmf": "MANAGE-3.1",
            "iso_42001": "A.10.1 Monitoring and Evaluation",
            "finos_aigf": "AIGF-OPS-01 Operational Telemetry & Health",
        },
        evidence_kinds=["status_dashboard", "metrics"],
        category="monitoring",
    ),
    Control(
        control_id="CTL-MON-02",
        title="Anomaly Detection & Behavioral Alerts",
        description="Detection of anomalous agent execution patterns, token spikes, or abnormal error frequencies.",
        references={
            "eu_ai_act": "Article 15(2) - Performance monitoring and anomaly detection",
            "nist_ai_rmf": "MANAGE-3.2",
            "iso_42001": "A.10.1 Monitoring and Evaluation",
            "finos_aigf": "AIGF-OPS-02 Anomaly Detection & Alerts",
        },
        evidence_kinds=["audit_chain", "alert_record"],
        category="monitoring",
    ),
    Control(
        control_id="CTL-DOC-01",
        title="Technical Documentation & Compliance Evidence Packs",
        description=(
            "Automated compilation of Annex IV technical documentation and signed evidence "
            "packs mapped to standard clauses."
        ),
        references={
            "eu_ai_act": "Article 11 & Annex IV - Technical documentation",
            "nist_ai_rmf": "GOVERN-4.2",
            "iso_42001": "A.5.2 Documented Information",
            "finos_aigf": "AIGF-DOC-01 Compliance Evidence Packaging",
        },
        evidence_kinds=["evidence_pack", "tech_doc"],
        category="documentation",
    ),
    Control(
        control_id="CTL-DOC-02",
        title="Agent Capability & Limitation Declaration",
        description=(
            "Documented instructions, intended purpose, operational constraints, "
            "and known limitations of the agent system."
        ),
        references={
            "eu_ai_act": "Article 13(3) - Instructions for use & limitation notice",
            "nist_ai_rmf": "MAP-1.2",
            "iso_42001": "A.5.2 Documented Information",
            "finos_aigf": "AIGF-DOC-02 Capability & Limitation Notice",
        },
        evidence_kinds=["agent_card", "system_descriptor"],
        category="documentation",
    ),
    Control(
        control_id="CTL-DEP-01",
        title="Air-Gapped & Offline Verification Support",
        description=(
            "All verification tooling, compliance checks, and cryptographic validation "
            "operate hermetically in air-gapped environments."
        ),
        references={
            "eu_ai_act": "Article 15(1) - Resilient offline verification",
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": "AIGF-SEC-06 Air-Gap & Isolation Support",
        },
        evidence_kinds=["verifier_receipt"],
        category="deployment",
    ),
)


# ---------------------------------------------------------------------------
# Control Registry Class
# ---------------------------------------------------------------------------


class ControlRegistry:
    """Registry of standard and custom compliance controls."""

    def __init__(self, controls: Iterable[Control] | None = None) -> None:
        self._controls: dict[str, Control] = {}
        if controls is not None:
            for c in controls:
                self.register(c)
        else:
            for c in STANDARD_CONTROLS:
                self.register(c)

    def register(self, control: Control) -> None:
        """Register a control in the registry."""
        self._controls[control.control_id] = control

    def get(self, control_id: str) -> Control | None:
        """Look up a control by ID."""
        return self._controls.get(control_id)

    def list_controls(self, framework: str | None = None) -> list[Control]:
        """List all controls, optionally filtered by framework ID."""
        if framework:
            fw = framework.lower().replace("-", "_")
            return [c for c in self._controls.values() if fw in c.references]
        return list(self._controls.values())

    def validate_control_ids(self, control_ids: Iterable[str]) -> list[str]:
        """Return a list of any control IDs that are not present in the registry."""
        return [cid for cid in control_ids if cid not in self._controls]

    def coverage(self, suites: Iterable[BenchSuite]) -> dict[str, list[str]]:
        """Map each control_id to the list of suite versions that measure it."""
        mapping: dict[str, list[str]] = {cid: [] for cid in self._controls}
        for suite in suites:
            for cid in suite.controls:
                if cid in mapping:
                    mapping[cid].append(suite.version)
                else:
                    mapping[cid] = [suite.version]
        return mapping

    def to_markdown_table(self, suites: Iterable[BenchSuite] | None = None) -> str:
        """Generate a Markdown table of controls and their framework mappings."""
        cov = self.coverage(suites) if suites is not None else {}
        headers = ["Control ID", "Title", "Frameworks", "Evidence Kinds"]
        if suites is not None:
            headers.append("Suites Covering")

        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]

        for c in self.list_controls():
            fw_str = ", ".join(f"{k.upper()}" for k in sorted(c.references.keys()))
            ev_str = ", ".join(c.evidence_kinds)
            row = [c.control_id, c.title, fw_str, ev_str]
            if suites is not None:
                covering = cov.get(c.control_id, [])
                row.append(", ".join(covering) if covering else "*(uncovered)*")
            lines.append("| " + " | ".join(row) + " |")

        return "\n".join(lines)


# Singleton default registry populated with standard controls
DEFAULT_REGISTRY = ControlRegistry()


def get_default_registry() -> ControlRegistry:
    """Return the singleton default control registry."""
    return DEFAULT_REGISTRY
