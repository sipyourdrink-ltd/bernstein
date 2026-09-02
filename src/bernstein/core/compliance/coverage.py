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
* :func:`assess_control_coverage` - evaluate coverage for all registered controls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.lineage.entry import LineageEntry

__all__ = [
    "CONTROL_EVENT_MAP",
    "ControlCoverageResult",
    "ControlCoverageStatus",
    "assess_control_coverage",
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
        evidence_refs: Content hashes of the chain entries that satisfied the
            required behaviour, sorted and de-duplicated. Empty when the
            control is not evidenced. A consumer that scores this result has to
            name the events it scored, and re-deriving the control-to-event
            match downstream would be a second, divergent judgement of the
            same chain.
    """

    policy_id: str
    control_id: str
    status: ControlCoverageStatus
    evidence_summary: str
    missing_inputs: list[str]
    reason: str
    evidence_refs: tuple[str, ...] = ()


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


# ---------------------------------------------------------------------------
# Coverage assessment
# ---------------------------------------------------------------------------

#: Suffix of the ``artefact_path`` that marks a lineage entry as an event of
#: the corresponding ``required_agent_behavior``. A new artefact-kind prefix
#: does not need new code branches here — it just needs a suffix in this map.
_BEHAVIOUR_SUFFIXES: dict[str, list[str]] = {
    "task-dispatch": ["task/"],
    "access-control": ["auth/", "access/", "login"],
    "change-management": ["task/", "config/", "change/", "merge/", "review/", "deploy/"],
    "approval-required": ["approval/", "receipt/"],
    "incident-report": ["incident/", "alert/"],
}


def _behaviour_from_entry(entry: LineageEntry) -> list[str]:
    """Return the behaviour labels an entry artefact_path satisfies."""
    path = entry.artefact_path.lower()
    matched: list[str] = []
    for behaviour, suffixes in _BEHAVIOUR_SUFFIXES.items():
        if any(path.startswith(s) for s in suffixes):
            matched.append(behaviour)
    return matched


def assess_control_coverage(
    entries: list[LineageEntry],
) -> list[ControlCoverageResult]:
    """Evaluate per-control coverage against the chain.

    Maps each ``policy_id`` in :data:`CONTROL_EVENT_MAP` to the chain events
    that satisfy it, reporting one of three statuses per control:

    * :attr:`ControlCoverageStatus.EVIDENCED` — at least one event in *entries*
      satisfies the ``required_agent_behavior``.
    * :attr:`ControlCoverageStatus.PARTIALLY_EVIDENCED` — the artefact kind
      is present but no entry matches the required behaviour suffix.
    * :attr:`ControlCoverageStatus.NOT_EVIDENCEABLE` — the control is not
      registered in :data:`CONTROL_EVENT_MAP` and the install cannot
      evidence it.

    Args:
        entries: Chain entries (lineage entries, audit events, etc.) to
            assess against.

    Returns:
        One :class:`ControlCoverageResult` per registered control, sorted by
        ``policy_id`` then ``control_id``.
    """
    observed_artefact_kinds: set[str] = set()
    #: behaviour -> content hashes of the entries that satisfy it. Built once so
    #: the numerator of any downstream score and the events it names come from
    #: the same match.
    entries_by_behaviour: dict[str, set[str]] = {}
    for entry in entries:
        observed_artefact_kinds.add(entry.artefact_kind)
        for behaviour in _behaviour_from_entry(entry):
            entries_by_behaviour.setdefault(behaviour, set()).add(entry.content_hash)
    observed_behaviours = set(entries_by_behaviour)

    results: list[ControlCoverageResult] = []
    for policy_id, spec in sorted(CONTROL_EVENT_MAP.items()):
        required = get_required_events(policy_id)
        artefact_kind = spec.get("required_artefact_kind", "")
        control_id = f"{policy_id}-control"

        if not required or not required.issubset(set(_BEHAVIOUR_SUFFIXES)):
            results.append(
                ControlCoverageResult(
                    policy_id=policy_id,
                    control_id=control_id,
                    status=ControlCoverageStatus.NOT_EVIDENCEABLE,
                    evidence_summary="No required behaviour mapped for this policy.",
                    missing_inputs=list(required),
                    reason=f"Policy '{policy_id}' has no required_agent_behavior in CONTROL_EVENT_MAP.",
                )
            )
            continue

        missing = [b for b in required if b not in observed_behaviours]

        if not missing:
            evidence_refs = sorted({h for b in required for h in entries_by_behaviour.get(b, set())})
            results.append(
                ControlCoverageResult(
                    policy_id=policy_id,
                    control_id=control_id,
                    status=ControlCoverageStatus.EVIDENCED,
                    evidence_summary=(
                        f"Chain contains {len(entries)} entries; required behaviour '{next(iter(required))}' observed."
                    ),
                    missing_inputs=[],
                    reason="Required chain events observed in the evidence set.",
                    evidence_refs=tuple(evidence_refs),
                )
            )
        elif artefact_kind and artefact_kind in observed_artefact_kinds:
            results.append(
                ControlCoverageResult(
                    policy_id=policy_id,
                    control_id=control_id,
                    status=ControlCoverageStatus.PARTIALLY_EVIDENCED,
                    evidence_summary=(
                        f"Entries of kind '{artefact_kind}' present but no event matching required behaviour "
                        f"'{next(iter(required))}'."
                    ),
                    missing_inputs=list(missing),
                    reason=f"No run in the period emitted the required '{next(iter(required))}' event.",
                )
            )
        else:
            results.append(
                ControlCoverageResult(
                    policy_id=policy_id,
                    control_id=control_id,
                    status=ControlCoverageStatus.PARTIALLY_EVIDENCED,
                    evidence_summary=f"No entries of kind '{artefact_kind}' observed in the chain.",
                    missing_inputs=list(missing),
                    reason=f"No run in the period emitted the required '{next(iter(required))}' event.",
                )
            )

    return results


def format_coverage_report(results: list[ControlCoverageResult]) -> str:
    """Render coverage results as a human-readable text table."""
    lines = [
        "=" * 72,
        "Compliance Coverage Report",
        "=" * 72,
        f"{'Policy ID':<24} {'Control':<24} {'Status':<22} Reason",
        "-" * 72,
    ]
    for r in results:
        status = r.status.value
        reason = r.reason if r.missing_inputs else r.evidence_summary
        lines.append(f"{r.policy_id:<24} {r.control_id:<24} {status:<22} {reason}")
    lines.append("=" * 72)
    return "\n".join(lines)
