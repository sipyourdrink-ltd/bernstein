"""OWASP Top 10 for Agentic Applications (ASI01-ASI10) control map.

Operators running agentic workloads in security-sensitive environments
need to show, from their own run evidence, which agentic-security
controls a Bernstein run already covers. This module is the ASI analogue
of the EU AI Act Article 12 clause map in ``evidence_pack.py``: it maps
each OWASP ASI control id to the concrete Bernstein mechanism that
addresses it and to the HMAC-chained audit event type(s) that serve as
evidence.

The map is anchored on the HMAC audit chain: every ``event_type`` cited
below is a literal string a Bernstein run actually writes into
``.sdd/audit/*.jsonl``. Strip the audit chain and this map has nothing
to point at, so the coverage claim is only as strong as the tamper-
evident chain that backs it. That coupling is deliberate: the evidence
is the chain, not a side document that merely describes the chain.

Honesty rule (same as ``owasp_asi_detectors.py``): where Bernstein only
partially addresses a control, the entry is marked ``"partial"`` and the
gap is stated in ``requirement``. An honest partial map is the
deliverable; over-claiming coverage would mislead an auditor.

The block is consumed by ``evidence_pack.build_evidence_pack`` under the
``owasp-asi`` standard. It mirrors the ``_STANDARD_MAPS`` shape exactly:

    {
        "regulation": <str>,
        "controls": [ {control_id, requirement, artefact, selector, status}, ... ],
        "deferred": [ <str>, ... ],
    }

``selector`` names the audit event attribute (or literal ``event_type``
values) carrying the primary evidence. It is informational, mirroring
the EU AI Act block; it is not enforced at build time.
"""

from __future__ import annotations

from typing import Any

#: Standard id used on the ``--standard`` flag and in manifests.
STANDARD_ID: str = "owasp-asi"

#: Human-readable catalogue name emitted into ``controls.json``.
REGULATION: str = "OWASP Top 10 for Agentic Applications (ASI01-ASI10, Dec 2025)"

# ---------------------------------------------------------------------------
# ASI01-ASI10 control map
# ---------------------------------------------------------------------------
#
# Each ``selector`` string lists literal ``event_type`` values (confirmed
# present in the codebase) or an audit attribute name. The ``artefact``
# points at the same bundle files the EU AI Act pack emits, so an auditor
# switching standards sees a consistent layout.

CONTROLS: list[dict[str, Any]] = [
    {
        "control_id": "ASI01",
        "requirement": (
            "Agent goal / instruction hijack: prompt-injection and goal "
            "manipulation are screened by the on-by-default OWASP ASI "
            "guardrail pack (ASI01 goal-hijack detector) and promptware "
            "scanner; refusals land in the audit chain."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal,command",
        "status": "partial",
    },
    {
        "control_id": "ASI02",
        "requirement": (
            "Tool misuse / excessive agency: the lethal-trifecta capability "
            "matrix denies any tool chain that simultaneously holds private "
            "data access, untrusted content, and external egress; the "
            "deterministic scheduler bounds agency (no LLM in the "
            "coordination loop). Refusals are recorded."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal",
        "status": "mapped",
    },
    {
        "control_id": "ASI03",
        "requirement": (
            "Identity and privilege abuse: per-adapter environment isolation "
            "and the Ed25519-signed install identity scope what an agent may "
            "act as; approval gates record who authorised a privileged step."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "approval_pending,approval_resolved,auto_approve_decision",
        "status": "partial",
    },
    {
        "control_id": "ASI04",
        "requirement": (
            "Supply-chain / skill provenance: the skill catalog verifies an "
            "Ed25519 detached signature over each entry before install; "
            "fetch, install, upgrade and uninstall are all chained."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.upgrade,skill.catalog.fetch,skill.catalog.uninstall",
        "status": "mapped",
    },
    {
        "control_id": "ASI05",
        "requirement": (
            "Unsafe code / command execution: commands run under a sandbox "
            "profile and a command allowlist; a sandbox-escape attempt is "
            "detected and recorded as a security incident."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "sandbox_escape_attempt,command",
        "status": "mapped",
    },
    {
        "control_id": "ASI06",
        "requirement": (
            "Memory / context poisoning: lineage tampering on a persisted "
            "artefact is detected against the content-addressed lineage "
            "chain and recorded. Detection of poisoned model context beyond "
            "artefact tampering is not yet covered."
        ),
        "artefact": "lineage/log.jsonl",
        "selector": "lineage_tamper_detected,content_hash,parent_hashes",
        "status": "partial",
    },
    {
        "control_id": "ASI07",
        "requirement": (
            "Insecure agent-to-agent (A2A) trust: peer capability cards are "
            "verified as detached JWS over JCS-canonical JSON with Ed25519 "
            "and gated on the operator trusted-issuer set and policy floor "
            "before any delegation. See 'bernstein interop a2a conformance'."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "agent.transition",
        "status": "mapped",
    },
    {
        "control_id": "ASI08",
        "requirement": (
            "Unbounded consumption: per-run cost caps, budget-exhaustion and "
            "budget-warning events, and task-deadline events bound spend and "
            "wall-clock; the cost ledger carries per-task, per-model spend."
        ),
        "artefact": "costs/cost_history.jsonl",
        "selector": "budget.exhausted,budget.warning,task.deadline_exceeded,model,task_id,usd",
        "status": "mapped",
    },
    {
        "control_id": "ASI09",
        "requirement": (
            "Observability / traceability gaps: every step is written to the "
            "HMAC-chained audit log and the content-addressed lineage log, "
            "so run activity is reconstructable offline; a stuck worker "
            "raises a signed supervisor escalation."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition,supervisor.escalated",
        "status": "mapped",
    },
    {
        "control_id": "ASI10",
        "requirement": (
            "Misalignment / behavioural drift: review-pipeline stages and "
            "capability-matrix refusals are recorded, giving an audit trail "
            "of gated decisions. Automated drift scoring across runs is a "
            "heuristic in the ASI detector pack and not yet a first-class "
            "chained metric."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "review_pipeline.stage,review_pipeline.complete,capability_matrix_refusal",
        "status": "partial",
    },
]

#: Controls whose evidence is out of scope for the read-only evidence pack
#: (they require signals Bernstein does not yet chain).
DEFERRED: list[str] = [
    "ASI06 model-context poisoning beyond artefact-hash tampering (no chained signal yet).",
    "ASI10 cross-run behavioural-drift scoring as a first-class chained metric (heuristic only today).",
]


def control_map() -> dict[str, Any]:
    """Return the OWASP ASI control-map block in ``_STANDARD_MAPS`` shape.

    The list is copied so a caller mutating the returned dict cannot
    corrupt the module-level catalogue.
    """
    return {
        "regulation": REGULATION,
        "controls": [dict(c) for c in CONTROLS],
        "deferred": list(DEFERRED),
    }


__all__ = [
    "CONTROLS",
    "DEFERRED",
    "REGULATION",
    "STANDARD_ID",
    "control_map",
]
