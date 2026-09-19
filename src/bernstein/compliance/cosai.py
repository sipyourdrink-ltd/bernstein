"""CoSAI Secure-by-Design patterns and Risk Map control map.

Operators running compliance-sensitive agentic workloads need to show,
from their own run evidence, which CoSAI (Coalition for Secure AI, OASIS
Open Project) Secure-by-Design principles and Risk Map controls a
Bernstein run covers. This module provides the CoSAI analogue of the EU
AI Act, OWASP ASI/AST, and ISO/IEC 42001 control maps in
``evidence_pack.py``.

The map is structured around the three core principles from the CoSAI
WS4 "Secure Design Patterns for Agentic Systems" specification:
* ``human-governed-accountable``: Human oversight, identity, and mandate governance.
* ``bounded-resilient``: Capability bounding, sandboxing, resource caps, and supply chain integrity.
* ``transparent-verifiable``: Tamper-evident audit logging, artifact lineage, and replayability.

Where the CoSAI Risk Map names a control, its ``controls.yaml``
identifier is associated with the requirement.

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

CoSAI documents are published under CC BY 4.0: principle names are quoted
with attribution and control descriptions are paraphrased.
"""

from __future__ import annotations

from typing import Any

#: Standard id used on the ``--standard`` flag and in manifests.
STANDARD_ID: str = "cosai"

#: Human-readable catalogue name emitted into ``controls.json``.
REGULATION: str = "CoSAI Secure-by-Design Patterns for Agentic Systems & Risk Map Controls"

# ---------------------------------------------------------------------------
# CoSAI WS4 principles & Risk Map control map
# ---------------------------------------------------------------------------
#
# Each entry maps a CoSAI WS4 principle identifier / Risk Map control to:
#   * ``control_id``  - WS4 principle sub-clause identifier.
#   * ``requirement`` - Paraphrased requirement text and Bernstein mechanism.
#   * ``artefact``    - Bundle file carrying the evidence.
#   * ``selector``    - Literal event_type tokens or attribute selectors.
#   * ``status``      - "mapped", "partial", or "todo".

CONTROLS: list[dict[str, Any]] = [
    # -----------------------------------------------------------------------
    # Principle 1: Human-Governed and Accountable
    # -----------------------------------------------------------------------
    {
        "control_id": "human-governed-accountable.oversight",
        "requirement": (
            "Human oversight and intervention (Risk Map: controlHumanApprovalAndIntervention): "
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
            "Dual authorization for critical operations (Risk Map: controlDualAuthorization): "
            "Multi-party approval policies enforce quorum requirements for high-risk actions "
            "and record separate resolution signatures."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "approval_pending,approval_resolved",
        "status": "mapped",
    },
    {
        "control_id": "human-governed-accountable.identity",
        "requirement": (
            "Agent identity and authentication (Risk Map: controlAgentIdentityAndSigning): "
            "Agents authenticate via cryptographic agent cards signed with detached JWS (Ed25519); "
            "delegation chains between parent and subagents are minted and verified."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "agent.transition,delegation_minted",
        "status": "mapped",
    },
    {
        "control_id": "human-governed-accountable.mandate",
        "requirement": (
            "Mandate and consent governance (Risk Map: controlMandateConsentGovernance): "
            "User mandate and consent boundaries are checked before task execution, and mandate "
            "consent or revocation events are chained."
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
            "Least privilege and capability bounding (Risk Map: controlLeastPrivilegeCapabilityBounding): "
            "Lethal-trifecta matrix bounds agent agency across private data, untrusted input, and external egress. "
            "Unauthorized capability expansion is denied and recorded."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal,command",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.sandboxing",
        "requirement": (
            "Execution sandboxing and isolation (Risk Map: controlExecutionSandboxing): "
            "Tool and command execution runs under restricted sandbox profiles with strict command "
            "allowlists; sandbox escape attempts are detected and recorded."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "sandbox_escape_attempt,command",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.resource-bounds",
        "requirement": (
            "Resource and cost bounding (Risk Map: controlResourceAndCostBounding): "
            "Run-level budget ceilings, token usage limits, and wall-clock execution deadlines bound "
            "consumption; cost ledger snapshots track per-task and per-model spend."
        ),
        "artefact": "costs/cost_history.jsonl",
        "selector": "budget.exhausted,budget.warning,task.deadline_exceeded,model,task_id,usd",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.supply-chain",
        "requirement": (
            "Supply chain and skill integrity (Risk Map: controlSupplyChainIntegrity): "
            "Skill packages and catalog entries must carry verified Ed25519 signatures prior to installation; "
            "fetch, install, upgrade, and uninstall operations are audited."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.fetch,skill.catalog.upgrade,skill.catalog.uninstall",
        "status": "mapped",
    },
    {
        "control_id": "bounded-resilient.codeguard",
        "requirement": (
            "CoSAI CodeGuard rule set preset (Risk Map: controlCodeGuardPreset): "
            "Automated guardrail preset loaded with CoSAI CodeGuard rules. Not currently loaded as a "
            "pre-configured guardrail preset in Bernstein."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "todo",
    },
    # -----------------------------------------------------------------------
    # Principle 3: Transparent and Verifiable
    # -----------------------------------------------------------------------
    {
        "control_id": "transparent-verifiable.audit-trail",
        "requirement": (
            "Tamper-evident audit logging (Risk Map: controlTamperEvidentAuditLogging): "
            "All task transitions, agent lifecycle state changes, and command invocations are logged into an "
            "RFC 2104 HMAC-chained audit log with offline verification."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition,command",
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
        "selector": "lineage_tamper_detected,content_hash,parent_hashes",
        "status": "mapped",
    },
    {
        "control_id": "transparent-verifiable.replay-reproducibility",
        "requirement": (
            "Trajectory reconstruction and replay (Risk Map: controlTrajectoryReconstructionAndReplay): "
            "Deterministic replay journaling records full execution steps and state transitions, enabling "
            "offline reconstruction and verification of agent trajectories."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "replay.step,replay.export",
        "status": "mapped",
    },
    {
        "control_id": "transparent-verifiable.context-integrity",
        "requirement": (
            "Context and prompt integrity (Risk Map: controlContextAndPromptIntegrity): "
            "Screening and audit recording of prompt injection and context capsules. Detection of "
            "semantic memory poisoning across multiple sessions remains partial."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "context.capsule_recorded,capability_matrix_refusal",
        "status": "partial",
    },
]

#: Controls whose full implementation or signal chaining is deferred.
DEFERRED: list[str] = [
    "bounded-resilient.codeguard: CoSAI CodeGuard rule set preset not yet bundled as a guardrail preset.",
    "transparent-verifiable.context-integrity: cross-session semantic memory poisoning detection is not yet chained.",
]


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
