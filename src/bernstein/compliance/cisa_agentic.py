"""CISA / Five Eyes agentic AI risk crosswalk for evidence packs.

Maps the May 2026 joint guidance on careful adoption of agentic AI
services to Bernstein's existing security and governance evidence.

The mapping is intentionally conservative: a risk is only marked
``mapped`` when Bernstein has an existing mechanism and an audit-chain
selector that can evidence it. Gaps remain ``partial`` or ``todo``.
"""

from __future__ import annotations

from typing import Any

STANDARD_ID = "cisa-agentic"

REGULATION = "CISA / Five Eyes, Careful Adoption of Agentic AI Services (May 2026)"

CONTROLS: list[dict[str, Any]] = [
    {
        "control_id": "CISA-01",
        "category": "broader",
        "risk": "Inherited risks of LLMs",
        "mechanism": "Input refusal and runtime evaluation guardrails",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "input.refusal_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-02",
        "category": "broader",
        "risk": "Increased attack surface",
        "mechanism": "Capability selection and MCP capability-drift evidence",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "mcp.capability_drift",
        "status": "partial",
    },
    {
        "control_id": "CISA-03",
        "category": "broader",
        "risk": "Increased complexity",
        "mechanism": "Sealed run graph and subagent delegation evidence",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "run_graph.sealed",
        "status": "partial",
    },
    {
        "control_id": "CISA-04",
        "category": "broader",
        "risk": "Evolving security as technology matures",
        "mechanism": "Model drift and update-advisory evidence",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "model.drift_observation",
        "status": "partial",
    },
    {
        "control_id": "CISA-05",
        "category": "privilege",
        "risk": "Privilege compromise and scope creep",
        "mechanism": "Capability deltas detect widening grants and record authorization",
        "module": "bernstein.core.security.capability_delta",
        "event_type": "capability.delta_recorded,capability.authorization",
        "status": "mapped",
    },
    {
        "control_id": "CISA-06",
        "category": "privilege",
        "risk": "Identity spoofing and agent impersonation",
        "mechanism": "SPIFFE/SVID binding and signed agent identity anchoring",
        "module": "bernstein.core.identity.spiffe.binding",
        "event_type": "spiffe.svid_binding,identity.spawn_attestation",
        "status": "mapped",
    },
    {
        "control_id": "CISA-07",
        "category": "design_configuration",
        "risk": "Unvetted third-party components",
        "mechanism": "Adapter/plugin admission and conformance receipts",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "adapter.admission_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-08",
        "category": "design_configuration",
        "risk": "Static role or permission checks",
        "mechanism": "Runtime capability authorization and enforced tool dispatch",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "toolcall.enforced_dispatch",
        "status": "partial",
    },
    {
        "control_id": "CISA-09",
        "category": "design_configuration",
        "risk": "Poor segmentation",
        "mechanism": "Per-task sandbox host isolation",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "sandbox.host_isolation_declared",
        "status": "mapped",
    },
    {
        "control_id": "CISA-10",
        "category": "design_configuration",
        "risk": "Incomplete or outdated allow lists",
        "mechanism": "Capability manifests and capability-drift detection",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "mcp.capability_drift",
        "status": "partial",
    },
    {
        "control_id": "CISA-11",
        "category": "behaviour",
        "risk": "Goal misalignment and unintended behaviour",
        "mechanism": "Intent capsules, intent-drift evidence and evaluation gates",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "intent.drift",
        "status": "partial",
    },
    {
        "control_id": "CISA-12",
        "category": "behaviour",
        "risk": "Deceptive behaviour",
        "mechanism": "Evaluation gate verdicts and clean-run attestations",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "eval.gate_verdict",
        "status": "partial",
    },
    {
        "control_id": "CISA-13",
        "category": "behaviour",
        "risk": "Emergent capabilities and unpredictable behaviour",
        "mechanism": "Capability evaluation and capability-delta evidence",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "capability.delta_recorded",
        "status": "partial",
    },
    {
        "control_id": "CISA-14",
        "category": "behaviour",
        "risk": "Malicious exploitation and behaviour",
        "mechanism": "Input refusal and guarded tool dispatch",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "input.refusal_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-15",
        "category": "structural",
        "risk": "Orchestration and resources",
        "mechanism": "Task lifecycle, resource release and cost budget controls",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "task.suspend_resource_release",
        "status": "partial",
    },
    {
        "control_id": "CISA-16",
        "category": "structural",
        "risk": "Tool use",
        "mechanism": "Tool-call attestation and enforced dispatch",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "toolcall.attestation,toolcall.enforced_dispatch",
        "status": "mapped",
    },
    {
        "control_id": "CISA-17",
        "category": "structural",
        "risk": "Third-party components",
        "mechanism": "Adapter admission, version posture and capability selection",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "adapter.admission_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-18",
        "category": "structural",
        "risk": "Data",
        "mechanism": "Provenance decisions and quarantine evidence",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "provenance.taint_decision,provenance.quarantine",
        "status": "partial",
    },
    {
        "control_id": "CISA-19",
        "category": "structural",
        "risk": "Rogue agents",
        "mechanism": "Identity anchoring, delegation receipts and revocation",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "subagent.delegation,identity.revoked",
        "status": "partial",
    },
    {
        "control_id": "CISA-20",
        "category": "structural",
        "risk": "Communication",
        "mechanism": "Agent-to-agent message receipts and identity binding",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "a2a.message_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-21",
        "category": "accountability",
        "risk": "Actions and processes",
        "mechanism": "HMAC audit chain records agent actions and process receipts",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "run.lifecycle",
        "status": "mapped",
    },
    {
        "control_id": "CISA-22",
        "category": "accountability",
        "risk": "Accuracy",
        "mechanism": "Evaluation gates and trajectory receipts",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "eval.trajectory_receipt",
        "status": "partial",
    },
    {
        "control_id": "CISA-23",
        "category": "accountability",
        "risk": "Visibility",
        "mechanism": "Audit receipts and observability projections",
        "module": "bernstein.core.security.audit_chain",
        "event_type": "otel.projection",
        "status": "mapped",
    },
]

DEFERRED: list[str] = []


def control_map() -> dict[str, Any]:
    """Return an independent CISA agentic-AI control map."""
    return {
        "regulation": REGULATION,
        "controls": [dict(control) for control in CONTROLS],
        "deferred": list(DEFERRED),
    }
