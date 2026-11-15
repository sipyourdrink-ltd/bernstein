"""CoSAI Principles for Secure-by-Design Agentic Systems & Risk Map control map.

Operators running compliance-sensitive agentic workloads need to show,
from their own run evidence, which CoSAI (Coalition for Secure AI, OASIS
Open Project) Principles for Secure-by-Design Agentic Systems and Risk Map controls
a Bernstein run covers. This module provides the CoSAI analogue of the EU
AI Act, OWASP ASI/AST, and ISO/IEC 42001 control maps in
``evidence_pack.py``.

The map is structured around the three core principles published by the
CoSAI Technical Steering Committee (TSC) in *CoSAI Principles for Secure-by-Design
Agentic Systems* (``cosai-oasis/cosai-tsc``, ``security-principles-for-agentic-systems.md``):
* ``human-governed-accountable``: Human-governed and Accountable.
* ``bounded-resilient``: Bounded and Resilient.
* ``transparent-verifiable``: Transparent and Verifiable.

Sub-clause identifiers (e.g. ``human-governed-accountable.oversight``) are
Bernstein's internal control IDs organized under the TSC's three top-level
principles. Where the CoSAI Risk Map (``cosai-oasis/secure-ai-tooling``,
``risk-map/yaml/controls.yaml``) defines a corresponding control, its
upstream identifier (e.g. ``controlAgentPluginUserControl``) is cited. Where
no direct Risk Map control exists, no Risk Map ID is asserted.

Three-state honesty rule:
* ``"mapped"``  - the chain contains records that satisfy the control;
                  the ``selector`` names the concrete audit evidence.
* ``"partial"`` - the chain covers part of the control; ``requirement``
                  states what is missing.
* ``"todo"``    - the control is recognized in the catalogue but not yet
                  enforced or chained by the orchestrator today.

The block is consumed by ``evidence_pack.build_evidence_pack`` under the
``cosai`` standard. It mirrors the ``_STANDARD_MAPS`` shape exactly:

    {
        "regulation": <str>,
        "controls": [ {control_id, requirement, artefact, selector, status}, ... ],
        "deferred": [ <str>, ... ],
    }

CoSAI TSC guidance documents (cosai-oasis/cosai-tsc) are published under CC BY 4.0;
Risk Map controls (cosai-oasis/secure-ai-tooling) are published under Apache-2.0.
Principle titles are quoted with attribution to the CoSAI TSC and control descriptions
are paraphrased.
"""

from __future__ import annotations

from typing import Any

#: Standard id used on the ``--standard`` flag and in manifests.
STANDARD_ID: str = "cosai"

#: Human-readable catalogue name emitted into ``controls.json``.
REGULATION: str = "CoSAI Principles for Secure-by-Design Agentic Systems & Risk Map Controls"

# ---------------------------------------------------------------------------
# CoSAI TSC principles & Risk Map control map
# ---------------------------------------------------------------------------
#
# Each entry maps a Bernstein control identifier under the CoSAI TSC principles to:
#   * ``control_id``  - Internal principle sub-clause identifier.
#   * ``requirement`` - Paraphrased requirement text, Risk Map ID (if any), and mechanism.
#   * ``artefact``    - Bundle file carrying the evidence.
#   * ``selector``    - Literal event_type tokens or attribute selectors.
#   * ``status``      - "mapped", "partial", or "todo".

CONTROLS: list[dict[str, Any]] = [
    # -----------------------------------------------------------------------
    # Principle 1: Human-governed and Accountable
    # -----------------------------------------------------------------------
    {
        "control_id": "human-governed-accountable.oversight",
        "requirement": (
            "Human oversight and user control (Risk Map: controlAgentPluginUserControl): "
            "Sensitive agent actions require human approval; approval states, resolver identities, "
            "and auto-approval decisions are cryptographically recorded in the audit chain."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "approval_pending,approval_resolved,auto_approve_decision,human_approval_decision",
        "status": "mapped",
    },
    {
        "control_id": "human-governed-accountable.dual-control",
        "requirement": (
            "Dual authorization for critical operations: Multi-party approval policies enforce "
            "quorum requirements for high-risk actions. Multi-party resolution signatures and "
            "quorum validation are not yet emitted to the audit chain."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "todo",
    },
    {
        "control_id": "human-governed-accountable.identity",
        "requirement": (
            "Agent identity and integrity management (Risk Map: controlAgentIntegrityManagement): "
            "Delegation minting between parent and subagents is recorded in the audit chain; "
            "agent card signing (detached JWS, Ed25519) is not chained."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "delegation_minted",
        "status": "partial",
    },
    {
        "control_id": "human-governed-accountable.mandate",
        "requirement": (
            "Payment mandate and consent governance: User spending mandates and consent boundaries "
            "are checked before task execution, and mandate consent or revocation events are chained."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "mandate.consent_receipt,mandate.revocation",
        "status": "mapped",
    },
    # -----------------------------------------------------------------------
    # Principle 2: Bounded and Resilient
    # -----------------------------------------------------------------------
    {
        "control_id": "bounded-resilient.least-privilege",
        "requirement": (
            "Least privilege and plugin permissions (Risk Map: controlAgentPluginPermissions): "
            "Lethal-trifecta matrix bounds agent agency across private data, untrusted input, and external egress. "
            "Unauthorized capability expansion is denied and recorded."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.sandboxing",
        "requirement": (
            "Execution sandboxing and bounds: Tool and command execution runs under restricted sandbox "
            "profiles with strict command allowlists; runtime enforcement occurs at the policy layer "
            "without chained escape event records."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "partial",
    },
    {
        "control_id": "bounded-resilient.resource-bounds",
        "requirement": (
            "Resource and cost bounding (Risk Map: controlAgentExecutionBounds): "
            "Recorded spend vs budget: cost ledger snapshots track spent and budget amounts."
        ),
        "artefact": "costs/cost_history.jsonl",
        "selector": "spent_usd,budget_usd",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.codeguard",
        "requirement": (
            "CoSAI CodeGuard rule set preset: Automated guardrail preset loaded with CoSAI CodeGuard rules. "
            "CodeGuard (cosai-oasis/project-codeguard) is not currently loaded as a pre-configured guardrail "
            "preset in Bernstein."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "todo",
    },
    # -----------------------------------------------------------------------
    # Principle 3: Transparent and Verifiable
    # -----------------------------------------------------------------------
    {
        "control_id": "transparent-verifiable.supply-chain",
        "requirement": (
            "Supply chain component verification: Skill packages and catalog entries carry "
            "verified Ed25519 signatures prior to installation; fetch, install, upgrade, "
            "and uninstall operations are audited."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.fetch,skill.catalog.upgrade,skill.catalog.uninstall",
        "status": "mapped",
    },
    {
        "control_id": "transparent-verifiable.audit-trail",
        "requirement": (
            "Agent observability and tamper-evident audit logging (Risk Map: controlAgentObservability): "
            "All task transitions and agent lifecycle state changes are logged into an "
            "RFC 2104 HMAC-chained audit log with offline verification."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition",
        "status": "mapped",
    },
    {
        "control_id": "transparent-verifiable.lineage-provenance",
        "requirement": (
            "Model and data integrity management (Risk Map: controlModelAndDataIntegrityManagement): "
            "Content-addressed lineage log records SHA-256 digests and parent provenance for all produced artifacts, "
            "detecting tampering against recorded hashes."
        ),
        "artefact": "lineage/log.jsonl",
        "selector": "content_hash,parent_hashes",
        "status": "mapped",
    },
    {
        "control_id": "transparent-verifiable.replay-reproducibility",
        "requirement": (
            "Deterministic trajectory reconstruction: Task and agent lifecycle transitions in the HMAC "
            "audit chain record coarse execution flow (task.transition, agent.transition). Fine-grained "
            "step-level deterministic replay execution is tracked in local runtime journals (.sdd/runtime/) "
            "rather than emitted directly as audit-chain events."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition",
        "status": "partial",
    },
    {
        "control_id": "transparent-verifiable.context-integrity",
        "requirement": (
            "Input validation, sanitization, and context integrity (Risk Map: controlInputValidationAndSanitization): "
            "Screening and audit recording of prompt injection and context capsules. Detection of "
            "semantic memory poisoning across multiple sessions remains partial."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "context.capsule,capability_matrix_refusal",
        "status": "partial",
    },
]

# Owner-selected CoSAI MCP paper revision, section 6.2 (#6217). These are
# upstream threat IDs, separate from Bernstein's thirteen principle rows.
_MCP_PAPER_TITLE = "Model Context Protocol (MCP) Security"
_MCP_SOURCE_REVISION = "7ec1306f2f55563f6eeef9d36a6bb2b531491ceb"
_MCP_SOURCE_DATE = "2026-01-08"
_MCP_THREATS = (
    (
        "MCP-T1",
        "Improper Authentication and Identity Management",
        "The bundle scanner checks OAuth redirect callback patterns and records findings",
        "This is not runtime user authentication, credential storage or identity verification",
        "mcp.bundle_scan",
    ),
    (
        "MCP-T2",
        "Missing or Improper Access Control",
        "The bundle scanner checks scope-expansion patterns; the load policy records admission",
        "Static findings and server admission do not establish per-object or per-tool authorization",
        "mcp.bundle_scan,mcp.load_decision",
    ),
    (
        "MCP-T3",
        "Input Validation/Sanitization Failures",
        "The bundle scanner records path-traversal and shell-injection pattern findings",
        "The finite static rules do not validate every runtime input or injection context",
        "mcp.bundle_scan",
    ),
    (
        "MCP-T4",
        "Input/Instruction Boundary Distinction Failure",
        "Manifest verification can bind supplied bundle bytes to a trusted publisher's signed hash",
        "Content integrity does not distinguish trusted instructions from adversarial tool or resource content",
        "mcp.signature_verification",
    ),
    (
        "MCP-T5",
        "Inadequate Data Protection and Confidentiality Controls",
        "The bundle scanner records path-traversal findings relevant to file exposure",
        "This does not establish data classification, encryption or runtime confidentiality controls",
        "mcp.bundle_scan",
    ),
    (
        "MCP-T6",
        "Missing Integrity/Verification Controls",
        "Manifest signature, trusted-publisher and optional supplied bundle-hash verification are recorded",
        "Pre-load verification is not continuous behavioral attestation or authentication of every protocol message",
        "mcp.signature_verification,mcp.load_decision",
    ),
    (
        "MCP-T7",
        "Session and Transport Security Failures",
        "The pre-load signing gate supplies no session or transport security evidence",
        "Session binding, transport encryption and session termination need separate evidence",
        "",
    ),
    (
        "MCP-T8",
        "Network Binding/Isolation Failures",
        "The pre-load signing gate supplies no network binding or isolation evidence",
        "Interface binding, DNS rebinding defenses and network segmentation need separate evidence",
        "",
    ),
    (
        "MCP-T9",
        "Trust Boundary and Privilege Design Failures",
        "Trusted-publisher policy and strict or warning admission decisions are recorded",
        "Server admission does not prove transitive trust boundaries or runtime privilege separation",
        "mcp.signature_verification,mcp.load_decision",
    ),
    (
        "MCP-T10",
        "Resource Management/Rate Limiting Absence",
        "The pre-load signing gate supplies no runtime resource or rate-limit evidence",
        "Token, request and payload quotas need separate runtime evidence",
        "",
    ),
    (
        "MCP-T11",
        "Supply Chain and Lifecycle Security Failures",
        "Publisher and optional supplied bundle-hash verification, static scanning and admission are recorded",
        "Pre-load checks do not prove a complete acquisition, inventory, update or retirement lifecycle",
        "mcp.signature_verification,mcp.bundle_scan,mcp.load_decision",
    ),
    (
        "MCP-T12",
        "Insufficient Logging, Monitoring, and Auditability",
        "The enabled gate chains verification, scan status and linked admission decisions with server identity",
        "These load events do not establish comprehensive tool-call, data-access or anomaly monitoring coverage",
        "mcp.signature_verification,mcp.bundle_scan,mcp.load_decision",
    ),
)

CONTROLS.extend(
    {
        "control_id": control_id,
        "requirement": (
            f"{title} (CoSAI {_MCP_PAPER_TITLE}; revision {_MCP_SOURCE_REVISION}; "
            f"{_MCP_SOURCE_DATE}; section 6.2). {mechanism}. Gap: {gap}."
        ),
        "artefact": "audit-chain/events.jsonl" if selector else "n/a",
        "selector": selector or "n/a",
        "status": "partial" if selector else "todo",
    }
    for control_id, title, mechanism, gap, selector in _MCP_THREATS
)

#: Controls whose full implementation or signal chaining is deferred.
DEFERRED: list[str] = [
    "human-governed-accountable.dual-control: multi-party quorum signatures and verification are not chained.",
    "bounded-resilient.sandboxing: sandbox execution is enforced at runtime without chained escape events.",
    "bounded-resilient.codeguard: CoSAI CodeGuard rule set preset not yet bundled as a guardrail preset.",
    (
        "transparent-verifiable.replay-reproducibility: step-level replay journal is local to .sdd/runtime/ "
        "rather than audit-chained."
    ),
    "transparent-verifiable.context-integrity: cross-session semantic memory poisoning detection is not yet chained.",
]
DEFERRED.extend(f"{control_id}: {gap}." for control_id, _title, _mechanism, gap, _selector in _MCP_THREATS)


def control_map() -> dict[str, Any]:
    """Return the CoSAI control-map block in ``_STANDARD_MAPS`` shape.

    The list is copied so a caller mutating the returned dict cannot
    corrupt the module-level catalogue.
    """
    return {
        "regulation": REGULATION,
        "controls": [c.copy() for c in CONTROLS],
        "deferred": DEFERRED.copy(),
    }


__all__ = [
    "CONTROLS",
    "DEFERRED",
    "REGULATION",
    "STANDARD_ID",
    "control_map",
]
