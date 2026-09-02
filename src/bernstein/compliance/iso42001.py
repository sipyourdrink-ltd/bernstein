"""ISO/IEC 42001:2023 (AI management system) Annex A control map.

Everything under ``core/compliance/`` and this package already projects the
HMAC-chained audit log into regulator-shaped documents (EU AI Act Article 12,
OWASP ASI, OWASP Skills). ISO/IEC 42001 is the management-system standard for
AI - organisations adopting it have to show evidence, per Annex A control,
that their AI systems are operated the way their policy says. This module is
the ISO/IEC 42001 analogue of ``owasp_asi.py``: it maps a subset of Annex A
control ids to the concrete Bernstein mechanism that addresses them and to
the audit-chain artefact that serves as evidence.

Only controls where a chain record can actually speak to the requirement are
mapped here - resource and lifecycle records, impact and event logging,
third-party and data provenance, and human oversight of individual
decisions. A large part of Annex A (policy content, staffing, training,
external stakeholder engagement, most of clause 4-10 of the main body) is
about an organisation's governance, not about what a tool can log, and no
control in ``CONTROLS`` claims otherwise.

Three-state honesty rule (stricter than the two-state ``mapped``/``partial``
vocabulary used elsewhere in this package, because Annex A control mixes
tool-evidenceable and pure-governance items in the same table):

* ``"mapped"``        - the chain contains records that satisfy the control;
                         the ``selector`` names the concrete evidence.
* ``"partial"``       - the chain covers part of the control; ``requirement``
                         states what is missing.
* ``"organisational"``- the control is about the operator's policy, training,
                         or governance and no tool can evidence it. Named
                         explicitly so the operator knows it is theirs to
                         answer, rather than silently absent from the pack.

A pack that marked a governance control ``"mapped"`` because the code cannot
tell the difference would be worse than no pack: it fails an audit and takes
the operator's credibility with it. ``"organisational"`` is not a gap in this
module - it is the module declining to overstate what a log can prove.

The map is anchored on the HMAC audit chain and the lineage log the same way
``owasp_asi.py`` is: every ``event_type`` token cited below is a literal
string a Bernstein run actually writes. ``docs/compliance/finos-aigf-mapping.md``
already cross-walks ISO/IEC 42001 clause 7.5.3 and clause 9 (main-body
clauses, not Annex A) onto ``CTRL-AUDIT-TRAIL`` / ``CTRL-RETENTION`` /
``CTRL-INCIDENT-RESPONSE`` informally; the Annex A rows below use the same
underlying mechanisms for the overlapping ground so the two documents do not
disagree about what is covered.

This is a first slice (issue #3238): a selectable standard, its control map,
and the docs page. The offline re-derivation path in ``bernstein audit
verify`` (re-deriving each ``"mapped"`` claim from the chain and failing on a
missing or mismatched record) and the ``regulator_renderers.py`` extension
are follow-up work, not implemented here.

Consumed by ``evidence_pack.build_evidence_pack`` under the ``iso-42001``
standard. Mirrors the ``_STANDARD_MAPS`` shape exactly:

    {
        "regulation": <str>,
        "controls": [ {control_id, requirement, artefact, selector, status}, ... ],
        "deferred": [ <str>, ... ],
    }
"""

from __future__ import annotations

from typing import Any

#: Standard id used on the ``--standard`` flag and in manifests.
STANDARD_ID: str = "iso-42001"

#: Human-readable catalogue name emitted into ``controls.json``.
REGULATION: str = "ISO/IEC 42001:2023, AI management system - Annex A controls"

# ---------------------------------------------------------------------------
# Annex A control map (subset - see module docstring for selection rule)
# ---------------------------------------------------------------------------
#
# Control ids follow the published ISO/IEC 42001:2023 Annex A numbering
# (theme.subclause, e.g. ``A.6.2.8``). ``selector`` lists literal
# ``event_type`` values (confirmed present in the codebase) or a lineage /
# cost-ledger attribute name; ``"n/a"`` for the controls marked
# ``"organisational"``, mirroring the retention row in the ``ai-act`` map.

CONTROLS: list[dict[str, Any]] = [
    {
        "control_id": "A.6.2.8",
        "requirement": (
            "AI system event logging: events across the AI system life cycle are "
            "recorded to support identification, monitoring and analysis. The "
            "HMAC-chained audit log records task and agent state transitions and "
            "every command a run executes."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition,command",
        "status": "mapped",
    },
    {
        "control_id": "A.6.2.6",
        "requirement": (
            "AI system operation and monitoring: the AI system's operation is "
            "monitored against defined criteria while in use. Stuck or "
            "misbehaving workers raise a signed supervisor escalation onto the "
            "same chain."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "task.transition,agent.transition,supervisor.escalated",
        "status": "mapped",
    },
    {
        "control_id": "A.7.5",
        "requirement": (
            "Data provenance: the origin and history of data consumed or "
            "produced by the AI system is traceable. The content-addressed "
            "lineage log records a hash chain from every artefact to its "
            "parents."
        ),
        "artefact": "lineage/log.jsonl",
        "selector": "content_hash,parent_hashes",
        "status": "mapped",
    },
    {
        "control_id": "A.4.5",
        "requirement": (
            "System and computing resources: compute resources consumed to "
            "operate the AI system are tracked. The cost ledger carries "
            "per-task, per-model spend for every run."
        ),
        "artefact": "costs/cost_history.jsonl",
        "selector": "model,task_id,usd",
        "status": "mapped",
    },
    {
        "control_id": "A.10.3",
        "requirement": (
            "Suppliers: risk from third-party components supplied into the AI "
            "system is identified and managed. The skill catalog verifies an "
            "Ed25519 signature over each entry before install and chains "
            "fetch/install/upgrade/uninstall; the operator's supplier risk "
            "assessment and contractual terms are not."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "skill.catalog.install,skill.catalog.upgrade,skill.catalog.fetch,skill.catalog.uninstall",
        "status": "partial",
    },
    {
        "control_id": "A.4.3",
        "requirement": (
            "Data resources: the data resources used to build and operate the "
            "AI system are identified. The audit-derived data catalog "
            "aggregates per-resource activity counts from the chain; a "
            "documented dataset inventory with classification is not derived "
            "from it."
        ),
        "artefact": "audit-chain/data_catalog.json",
        "selector": "resource_type,resource_id",
        "status": "partial",
    },
    {
        "control_id": "A.4.4",
        "requirement": (
            "Tooling resources: tools used to build, deploy and operate the AI "
            "system are identified and access to them is controlled. Tool "
            "access decisions (capability-matrix refusals, executed commands) "
            "are chained; the tool inventory itself lives in the "
            "capability-matrix configuration, not in a chain record."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "capability_matrix_refusal,command",
        "status": "partial",
    },
    {
        "control_id": "A.8.4",
        "requirement": (
            "Communication of incidents: AI-related incidents are recorded so "
            "they can be communicated to interested parties. Sandbox-escape "
            "attempts and capability-matrix refusals land on the chain; "
            "notifying regulators or customers is an operator process this "
            "does not automate."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "sandbox_escape_attempt,capability_matrix_refusal",
        "status": "partial",
    },
    {
        "control_id": "A.9.2",
        "requirement": (
            "Processes for responsible use: use of the AI system is subject to "
            "human oversight where the operator's policy requires it. "
            "Individual approval and auto-approve decisions are chained; the "
            "operator's broader responsible-use process and criteria are not."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "approval_pending,approval_resolved,auto_approve_decision",
        "status": "partial",
    },
    {
        "control_id": "A.6.2.4",
        "requirement": (
            "AI system verification and validation: the AI system is verified "
            "against its requirements before and during deployment. "
            "Review-pipeline stage and completion decisions are chained; the "
            "acceptance criteria and test evidence behind each decision live "
            "outside the chain."
        ),
        "artefact": "audit-chain/events.jsonl",
        "selector": "review_pipeline.stage,review_pipeline.complete",
        "status": "partial",
    },
    {
        "control_id": "A.5.2",
        "requirement": (
            "AI system impact assessment process: the organisation follows a "
            "documented process to assess the impacts of an AI system before "
            "and during its use. This is a policy and process question no "
            "chain record can answer; the operator owns it."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "organisational",
    },
    {
        "control_id": "A.2.2",
        "requirement": (
            "AI policy: the organisation defines and communicates a policy for "
            "the responsible development, provision or use of AI systems. "
            "Policy content and its communication are organisational, not "
            "something a run log evidences."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "organisational",
    },
    {
        "control_id": "A.3.2",
        "requirement": (
            "AI roles and responsibilities: roles and responsibilities "
            "relevant to the AI management system are defined and assigned. "
            "RBAC enforces access at runtime, but that a role was defined and "
            "assigned by the organisation is not the same claim, and this "
            "control is not marked as if it were."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "organisational",
    },
    {
        "control_id": "A.10.2",
        "requirement": (
            "Allocating responsibilities: responsibilities between the "
            "organisation and its AI system suppliers or customers are "
            "allocated and documented. This is a contractual and policy "
            "matter, not a chain record."
        ),
        "artefact": "n/a",
        "selector": "n/a",
        "status": "organisational",
    },
]

#: Annex A themes and subclauses intentionally out of scope for this slice -
#: either no chained signal exists yet, or the control is squarely
#: organisational and adding it would pad the map without adding honesty.
DEFERRED: list[str] = [
    "A.7.2-A.7.4, A.7.6 data acquisition, quality and preparation controls "
    "beyond provenance (no chained data-quality signal yet).",
    "A.6.2.7 AI system technical documentation completeness (organisational authoring, not chain-derivable).",
    "A.8.2-A.8.3 system documentation for users and external reporting (operator-authored content).",
    "A.9.3-A.9.4 responsible-use objectives and intended-use documentation (organisational).",
    "A.10.4 customer relationship allocation (organisational).",
    "Full Annex A has roughly forty controls across ten themes; this slice "
    "covers the records-derivable subset named in issue #3238. Extending "
    "the map to the remaining themes is follow-up work, not silently "
    "claimed here.",
]


def control_map() -> dict[str, Any]:
    """Return the ISO/IEC 42001 control-map block in ``_STANDARD_MAPS`` shape.

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
