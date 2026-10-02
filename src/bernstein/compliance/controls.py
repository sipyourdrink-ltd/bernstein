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

A control is defined here and nowhere else. Anything that claims to measure
one -- an evaluation suite, an evidence pack, an assessment export -- names
it by registry id, and ``validate_control_ids`` is how a caller refuses a
name the registry does not know.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from bernstein.compliance.finos_aigf import reference_label as _finos_ref
from bernstein.compliance.owasp_asi import reference_label as _asi_ref
from bernstein.compliance.owasp_skills import reference_label as _ast_ref

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Control:
    """A compliance or security control with cross-framework mapping.

    ``frozen=True`` stops the attributes being rebound; it does nothing about
    mutating the containers they point at. A registered control is
    process-wide shared state through :data:`DEFAULT_REGISTRY`, so
    ``control.references["eu_ai_act"] = ...`` on a value handed out by
    :meth:`ControlRegistry.get` would silently change the mapping every later
    reader sees -- for a catalogue whose whole purpose is to be the
    authoritative statement of what a control means, that is a tampering
    surface rather than an inconvenience.

    ``__post_init__`` therefore *copies* what it is given (so the caller's own
    dict or list cannot reach in afterwards) and then exposes the copy
    immutably: a ``MappingProxyType`` refuses item assignment, and a tuple has
    no mutators. ``to_dict`` still hands back plain ``dict``/``list``, so
    serialisation and every existing consumer are unchanged.
    """

    control_id: str
    title: str
    description: str
    references: Mapping[str, str] = field(default_factory=dict)
    evidence_kinds: Sequence[str] = field(default_factory=tuple)
    category: str = "governance"

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", MappingProxyType(dict(self.references)))
        object.__setattr__(self, "evidence_kinds", tuple(self.evidence_kinds))

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "title": self.title,
            "description": self.description,
            "references": dict(self.references),
            "evidence_kinds": list(self.evidence_kinds),
            "category": self.category,
        }


#: Framework keys a control may be cross-referenced against.
#:
#: ``register()`` refuses anything else. A control defined against
#: ``"iso_42O01"`` (capital O) would otherwise register cleanly and then be
#: invisible to ``list_controls(framework="iso_42001")`` and to the
#: ``--framework`` flag: the reference is silently dropped rather than
#: reported. The CLI already guards the query side against exactly that
#: typo, and a definition-side typo is the half that cannot be noticed.
KNOWN_FRAMEWORKS: frozenset[str] = frozenset(
    {"eu_ai_act", "owasp_asi", "owasp_skills", "nist_ai_rmf", "iso_42001", "finos_aigf"}
)


# ---------------------------------------------------------------------------
# Standard Control Definitions (>= 30 pre-populated controls)
# ---------------------------------------------------------------------------
#
# The ``owasp_asi``, ``owasp_skills`` and ``finos_aigf`` values below are not
# written by hand. Each is built by the helper that owns that framework's
# catalogue -- ``owasp_asi.reference_label``, ``owasp_skills.reference_label``,
# ``finos_aigf.reference_label`` -- so this file cites an external control id
# but never states what that id means. A label typed here could disagree with
# the map that drives the evidence pack, and did: ``ASI08`` was labelled
# "Human-in-the-Loop Bypass" while ``owasp_asi.py`` defines it as unbounded
# consumption. Citing an id the helper does not know now fails at import.
#
# Where a Bernstein control has no counterpart in a framework's published
# catalogue, it carries no key for that framework. That is deliberate: the
# OWASP ASI list has no sensitive-data-exposure entry and the FINOS AIGF
# mitigation list has no pre-execution human-approval entry, so a reference
# to one would be a claim an auditor could not resolve.

STANDARD_CONTROLS: tuple[Control, ...] = (
    Control(
        control_id="CTL-GOV-01",
        title="Policy as Code & Governance Boundary",
        description="Deterministic enforcement of organizational policies before and during agent task execution.",
        references={
            "eu_ai_act": "Article 13(1) - Transparency and provision of instructions",
            "nist_ai_rmf": "GOVERN-1.1",
            "iso_42001": "A.5.2 AI Policy",
            "finos_aigf": _finos_ref("mi-18"),
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
            "owasp_asi": _asi_ref("ASI09"),
            "nist_ai_rmf": "GOVERN-4.1",
            "iso_42001": "A.8.4 Logging and Monitoring",
            "finos_aigf": _finos_ref("mi-21"),
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
            "finos_aigf": _finos_ref("mi-4"),
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
            "owasp_asi": _asi_ref("ASI06"),
            "nist_ai_rmf": "MAP-1.5",
            "iso_42001": "A.7.2 AI Data Lifecycle",
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
            "nist_ai_rmf": "MEASURE-2.5",
            "iso_42001": "A.9.2 Human Oversight",
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
            "owasp_asi": _asi_ref("ASI01"),
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": _finos_ref("mi-17"),
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
            "owasp_asi": _asi_ref("ASI05"),
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": _finos_ref("mi-19"),
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
            "nist_ai_rmf": "MANAGE-2.2",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": _finos_ref("mi-1"),
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
            "nist_ai_rmf": "MEASURE-2.11",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": _finos_ref("mi-5"),
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
            "owasp_asi": _asi_ref("ASI02"),
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": _finos_ref("mi-3"),
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
            "finos_aigf": _finos_ref("mi-6"),
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
            "nist_ai_rmf": "GOVERN-1.2",
            "iso_42001": "A.7.2 AI Data Lifecycle",
            "finos_aigf": _finos_ref("mi-1"),
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
            "finos_aigf": _finos_ref("mi-4"),
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
            "owasp_asi": _asi_ref("ASI08"),
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.5.2 Resource Management",
            "finos_aigf": _finos_ref("mi-9"),
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
            "finos_aigf": _finos_ref("mi-5"),
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
            "finos_aigf": _finos_ref("mi-5"),
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
            "finos_aigf": _finos_ref("mi-5"),
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
            "finos_aigf": _finos_ref("mi-5"),
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
            "owasp_skills": _ast_ref("AST01"),
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
        },
        evidence_kinds=["skill_manifest", "audit_chain"],
        category="skills",
    ),
    Control(
        control_id="CTL-SKILL-02",
        title="Skill Execution Boundaries & Permissions",
        description="Fine-grained permission boundaries and scope restrictions for skill pack execution.",
        references={
            "owasp_skills": _ast_ref("AST04"),
            "nist_ai_rmf": "MANAGE-1.3",
            "iso_42001": "A.8.2 Security Architecture",
            "finos_aigf": _finos_ref("mi-18"),
        },
        evidence_kinds=["audit_chain", "policy"],
        category="skills",
    ),
    Control(
        control_id="CTL-SKILL-03",
        title="Untrusted Skill Quarantine & Code Review",
        description="Quarantine and explicit operator review for imported or community skill packs before activation.",
        references={
            "owasp_skills": _ast_ref("AST01"),
            "nist_ai_rmf": "MANAGE-2.4",
            "iso_42001": "A.8.2 Security Architecture",
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
            "finos_aigf": _finos_ref("mi-4"),
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
            "finos_aigf": _finos_ref("mi-4"),
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
        },
        evidence_kinds=["verifier_receipt"],
        category="deployment",
    ),
)


# ---------------------------------------------------------------------------
# Control Registry Class
# ---------------------------------------------------------------------------


class ControlRegistry:
    """Registry of standard and custom compliance controls.

    ``ControlRegistry()`` starts from :data:`STANDARD_CONTROLS`;
    ``ControlRegistry(controls=[...])`` starts from exactly those controls
    and nothing else, which is what an isolated test registry wants.
    """

    def __init__(self, controls: Iterable[Control] | None = None) -> None:
        self._controls: dict[str, Control] = {}
        if controls is not None:
            for c in controls:
                self.register(c)
        else:
            for c in STANDARD_CONTROLS:
                self.register(c)

    def register(self, control: Control) -> None:
        """Register a control; a control is defined here and nowhere else.

        Refuses an ID that is already registered: a plugin or an import-order
        accident re-registering ``CTL-SEC-02`` would otherwise replace the
        canonical definition process-wide, and every compliance claim made
        against that ID afterwards would mean something else. A blank ID is
        refused for the same reason -- it can never be cited.

        The remaining checks refuse a control that would register cleanly and
        then be unusable as evidence: a blank title or description leaves an
        assessor nothing to assess against, an empty ``evidence_kinds`` names
        a control no run can ever produce evidence for, and a framework key
        outside :data:`KNOWN_FRAMEWORKS` is a typo that silently hides the
        reference from ``list_controls(framework=...)``.
        """
        control_id = control.control_id
        if not control_id or control_id != control_id.strip():
            raise ValueError(f"control id must be non-empty with no surrounding whitespace, got {control_id!r}")
        if control_id in self._controls:
            raise ValueError(f"control {control_id!r} is already registered; unregister it first to redefine it")
        if not control.title.strip():
            raise ValueError(f"control {control_id!r} must have a non-empty title")
        if not control.description.strip():
            raise ValueError(f"control {control_id!r} must have a non-empty description")
        if not control.evidence_kinds:
            raise ValueError(
                f"control {control_id!r} declares no evidence kinds; nothing could ever be filed against it"
            )
        unknown = sorted(set(control.references) - KNOWN_FRAMEWORKS)
        if unknown:
            raise ValueError(
                f"control {control_id!r} references unknown framework(s) {', '.join(unknown)}; "
                f"known frameworks: {', '.join(sorted(KNOWN_FRAMEWORKS))}"
            )
        self._controls[control_id] = control

    def unregister(self, control_id: str) -> Control:
        """Remove and return a control, so an extension can be undone leak-free."""
        try:
            return self._controls.pop(control_id)
        except KeyError:
            raise ValueError(f"control {control_id!r} is not registered") from None

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

    def to_markdown_table(self, framework: str | None = None) -> str:
        """Generate a Markdown table of controls and their framework mappings.

        ``framework`` filters exactly as :meth:`list_controls` does. Without
        it the Markdown format rendered the whole catalogue while ``json``
        and ``text`` rendered the filtered subset, so the same command with
        the same ``--framework`` produced a different answer per format.
        """
        headers = ["Control ID", "Title", "Frameworks", "Evidence Kinds"]
        lines = [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
        ]
        for c in self.list_controls(framework=framework):
            fw_str = ", ".join(f"{k.upper()}" for k in sorted(c.references.keys()))
            ev_str = ", ".join(c.evidence_kinds)
            lines.append("| " + " | ".join([c.control_id, c.title, fw_str, ev_str]) + " |")
        return "\n".join(lines)


# Singleton default registry populated with standard controls. It is the
# extension point: a plugin or an organisation registers a custom control here
# and ``validate_controls`` -- which builds from this registry -- admits it.
# The cost is that ``register()`` is process-wide; a test that extends it
# should do so on its own ``ControlRegistry()`` instance instead.
DEFAULT_REGISTRY = ControlRegistry()


def get_default_registry() -> ControlRegistry:
    """Return the singleton default control registry."""
    return DEFAULT_REGISTRY
