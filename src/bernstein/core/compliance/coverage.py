"""Control-to-chain-event mapping for compliance coverage assessment.

Provides a data-driven mapping from regulatory policy identifiers to the
chain events that constitute evidence for those policies, replacing
hard-coded conditional branches with declarative row additions to
``CONTROL_EVENT_MAP``.

Public surface:

* :data:`CONTROL_EVENT_MAP` - declarative policy-to-event mapping.
* :class:`ControlCoverageStatus` - per-control coverage status.
* :class:`ControlCoverageResult` - structured coverage assessment result.
* :func:`get_required_events` - derive required event indicators for a policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CONTROL_EVENT_MAP",
    "ControlCoverageResult",
    "ControlCoverageStatus",
    "get_required_events",
]


class ControlCoverageStatus(Enum):
    """Coverage status for a single control within a policy."""

    EVIDENCED = "evidenced"
    PARTIALLY_EVIDENCED = "partially_evidenced"
    NOT_EVIDENCEABLE = "not_evidenceable"


@dataclass(frozen=True)
class ControlCoverageResult:
    """Structured result of a control coverage assessment.

    Attributes:
        policy_id: Regulatory policy identifier (e.g. ``eu-ai-act-art-12``).
        control_id: Specific control within the policy (e.g. ``CC6.1``).
        status: Coverage status for this control.
        evidence_summary: Human-readable summary of evidence found.
        missing_inputs: List of required inputs not satisfied by the chain.
        reason: Brief explanation of the status determination.
    """

    policy_id: str
    control_id: str
    status: ControlCoverageStatus
    evidence_summary: str
    missing_inputs: list[str]
    reason: str


# ---------------------------------------------------------------------
# Control-event map
#
# Maps ``policy_id`` to a dict with three keys:
#
#   required_artefact_kind
#       The kind of chain artefact (e.g. ``lineage-entry``,
#       ``audit-event``, ``approval-receipt``) that satisfies this control.
#
#   required_agent_behavior
#       A short label describing the agent behaviour the control expects
#       (e.g. ``task-dispatch``, ``tool-execution``, ``approval-required``).
#
#   partial_evidence_hint
#       A hint for the coverage assessor describing what counts as
#       partial evidence when the full requirement cannot be met (e.g.
#       ``"log exists but is unsigned"``).
#
# New regulatory requirements are added as new dict entries here; no new
# code branches are required.
# ---------------------------------------------------------------------

CONTROL_EVENT_MAP: dict[str, dict[str, str]] = {
    "eu-ai-act-art-12": {
        "required_artefact_kind": "lineage-entry",
        "required_agent_behavior": "task-dispatch",
        "partial_evidence_hint": "lineage log exists for period but entries lack complete parent linkage",
    },
    "eu-ai-act-art-14": {
        "required_artefact_kind": "approval-receipt",
        "required_agent_behavior": "approval-required",
        "partial_evidence_hint": "approval receipt exists but displayed-action differs from executed-action",
    },
    "eu-ai-act-art-73": {
        "required_artefact_kind": "audit-event",
        "required_agent_behavior": "incident-report",
        "partial_evidence_hint": "incident timeline exists but audit-slice is incomplete or gaps.json is non-empty",
    },
    "soc2-cc6-1": {
        "required_artefact_kind": "audit-event",
        "required_agent_behavior": "access-control",
        "partial_evidence_hint": "auth events present but no evidence of access revocation",
    },
    "soc2-cc8-1": {
        "required_artefact_kind": "lineage-entry",
        "required_agent_behavior": "change-management",
        "partial_evidence_hint": "task entries present but review/approval record absent",
    },
}


def get_required_events(policy_id: str) -> set[str]:
    """Return the set of required event indicators for *policy_id*.

    Derived directly from ``CONTROL_EVENT_MAP``: the ``required_agent_behavior``
    field is returned as a single-element set when the policy is registered,
    or an empty set when the policy is not found.

    Args:
        policy_id: Regulatory policy identifier (e.g. ``eu-ai-act-art-12``).

    Returns:
        Set of required event indicator strings; empty if *policy_id* is unknown.
    """
    entry = CONTROL_EVENT_MAP.get(policy_id, {})
    behavior = entry.get("required_agent_behavior", "")
    return {behavior} if behavior else set()
