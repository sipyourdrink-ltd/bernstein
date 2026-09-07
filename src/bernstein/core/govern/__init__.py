"""Govern plan models for posture diff artifacts."""

from __future__ import annotations

from typing import Any

from bernstein.core.govern.agent_presence import (
    AgentPresence,
    Enrollment,
    apply_presence,
    enrollment_gap,
)
from bernstein.core.govern.apply import (
    ApplyStatus,
    ChangeApplier,
    ChangeOutcome,
    ChangeResult,
    ChangeStatus,
    GovernApplyRecord,
    GovernApplyRefused,
    apply_plan,
    verify_govern_apply_projection,
)
from bernstein.core.govern.duplication_audit import (
    DuplicationFinding,
    DuplicationReport,
    Verdict,
    collect_duplication,
)
from bernstein.core.govern.findings import Finding, FindingsDocument
from bernstein.core.govern.freshness_gate import (
    FreshnessGate,
    FreshnessResult,
    ProducerState,
    freshness_gated_read,
)
from bernstein.core.govern.inventory_models import Inventory, Surface, SweepResult, Tombstone
from bernstein.core.govern.lanes import (
    Barrier,
    LaneAction,
    LaneError,
    LaneManifest,
    load_lane_set,
    reconcile_lanes,
)
from bernstein.core.govern.observation import ObservationEnvelope, ObservationLedger
from bernstein.core.govern.observation_store import (
    ObservationRecord,
    ObservationStore,
    ObservationStoreError,
    RecordState,
    observation_store_root,
)
from bernstein.core.govern.plan_models import (
    GovernPlan,
    PlanEntry,
    PlanEntryKind,
    compute_inputs_hash,
)
from bernstein.core.govern.playbook_models import (
    Playbook,
    PlaybookClause,
    PlaybookValidationError,
    RemediationAction,
)
from bernstein.core.govern.probe import (
    CollectionMethod,
    CostClass,
    Probe,
    ProbeError,
    ProbeSet,
    load_probe_set,
)
from bernstein.core.govern.proposal import DraftProposal, ProposalStatus
from bernstein.core.govern.reconcile import (
    compute_reconcile_diff,
    propose_reconcile,
    snapshot_surface,
)
from bernstein.core.govern.reconcile_models import (
    DesiredEntity,
    DesiredState,
    DiffAction,
    EntityKind,
    EntityPolicy,
    EntityStatus,
    ReconcileDiff,
    ReconcileEntry,
    Snapshot,
    SnapshotEntity,
)
from bernstein.core.govern.remediation import (
    RemediationProposal,
    RemediationStep,
    UnremediatedFinding,
    collect_remediation,
)
from bernstein.core.govern.restore import (
    RestoreEntry,
    RestorePlan,
    RestoreRefusal,
    build_restore_plan,
)


def compute_plan(
    *,
    playbook: dict[str, Any],
    inventory: dict[str, Any],
    run_id: str,
    timestamp: int,
) -> GovernPlan:
    """Compute the posture diff between *playbook* and *inventory*.

    The playbook declares the required state; the inventory enumerates the
    observed state. Each entry in the plan classifies one surface-level
    mismatch.

    Playbook schema::

        {
          "forbidden": [{"surface": "...", "clause": "..."}],
          "required": [{"surface": "...", "clause": "...", "declared_value": "..."}],
          "permitted": [{"surface": "...", "clause": "...", "declared_ceiling": "..."}]
        }

    Inventory schema::

        {
          "surfaces": [{"surface": "...", "observed_value": "...", "evidence_ref": "..."}]
        }

    Determinism: all fields are pure functions of the input data.
    """
    entries: list[PlanEntry] = []

    # Surfaces the inventory could not read. These are NOT observations: a
    # surface that was not read cannot be judged compliant, so it is held out
    # of the observed map entirely and reported as UNKNOWN below. An inventory
    # states this either per-record ("unreadable": true, when the surface was
    # known but could not be queried) or as a top-level list (when the
    # enumeration itself failed and produced no record at all).
    unreadable: dict[str, str] = {}
    for s in inventory.get("surfaces", []):
        if s.get("unreadable"):
            unreadable[str(s["surface"])] = str(s.get("evidence_ref", ""))
    for surface_id in inventory.get("unreadable", []):
        unreadable.setdefault(str(surface_id), "")

    # Build inventory lookup
    inventory_map: dict[str, dict[str, str]] = {}
    for s in inventory.get("surfaces", []):
        if str(s["surface"]) in unreadable:
            continue
        inventory_map[s["surface"]] = {
            "observed_value": str(s.get("observed_value", "")),
            "evidence_ref": str(s.get("evidence_ref", "")),
        }

    # FORBIDDEN: surfaces in inventory that the playbook forbids
    forbidden_map: dict[str, str] = {s["surface"]: s["clause"] for s in playbook.get("forbidden", [])}
    for surface, inv_data in inventory_map.items():
        if surface in forbidden_map:
            entries.append(
                PlanEntry(
                    kind=PlanEntryKind.FORBIDDEN,
                    surface=surface,
                    evidence_ref=inv_data["evidence_ref"],
                    playbook_clause=forbidden_map[surface],
                    observed_value=inv_data["observed_value"],
                    declared_value=None,
                    timestamp=timestamp,
                )
            )

    # WIDER_CEILING: permitted surfaces whose observed value exceeds declared ceiling
    permitted_map: dict[str, tuple[str, str]] = {}
    for s in playbook.get("permitted", []):
        if "declared_ceiling" in s:
            permitted_map[s["surface"]] = (str(s["declared_ceiling"]), s["clause"])

    for surface, inv_data in inventory_map.items():
        if surface in permitted_map:
            ceiling_str, clause = permitted_map[surface]
            obs_str = inv_data["observed_value"]
            if obs_str and _compare_values(obs_str, ceiling_str) > 0:
                entries.append(
                    PlanEntry(
                        kind=PlanEntryKind.WIDER_CEILING,
                        surface=surface,
                        evidence_ref=inv_data["evidence_ref"],
                        playbook_clause=clause,
                        observed_value=obs_str,
                        declared_value=ceiling_str,
                        timestamp=timestamp,
                    )
                )

    # ABSENT: surfaces the playbook requires but are missing from inventory
    required_clause: dict[str, str] = {}
    required_declared: dict[str, str] = {}
    for s in playbook.get("required", []):
        surface = s["surface"]
        required_clause[surface] = s["clause"]
        required_declared[surface] = str(s.get("declared_value", ""))

    for surface, clause in required_clause.items():
        if surface not in inventory_map and surface not in unreadable:
            entries.append(
                PlanEntry(
                    kind=PlanEntryKind.ABSENT,
                    surface=surface,
                    evidence_ref="",
                    playbook_clause=clause,
                    observed_value=None,
                    declared_value=required_declared.get(surface, ""),
                    timestamp=timestamp,
                )
            )

    # UNKNOWN: governed surfaces the inventory could not read. Sorted so that
    # two operators reach the same order regardless of how their inventory
    # serialized the two ways of declaring an unread surface.
    clause_for: dict[str, str] = {}
    clause_for.update(forbidden_map)
    clause_for.update({s: c for s, (_, c) in permitted_map.items()})
    clause_for.update(required_clause)
    for surface in sorted(unreadable):
        unknown_clause = clause_for.get(surface)
        if unknown_clause is None:
            # Not governed by any clause: nothing judges it, so there is
            # nothing to report.
            continue
        entries.append(
            PlanEntry(
                kind=PlanEntryKind.UNKNOWN,
                surface=surface,
                evidence_ref=unreadable[surface],
                playbook_clause=unknown_clause,
                observed_value=None,
                declared_value=required_declared.get(surface),
                timestamp=timestamp,
            )
        )

    inputs_hash = compute_inputs_hash(playbook=playbook, inventory=inventory)

    return GovernPlan(
        run_id=run_id,
        entries=tuple(entries),
        inputs_hash=inputs_hash,
        timestamp=timestamp,
    )


def _compare_values(observed: str, ceiling: str) -> int:
    """Compare *observed* to *ceiling*.

    Returns >0 if observed > ceiling, 0 if equal, <0 otherwise.
    """
    try:
        obs, ceil = float(observed), float(ceiling)
    except (ValueError, TypeError):
        pass
    else:
        # int() truncates toward zero, so any breach smaller than 1.0 read as
        # "equal" and the surface passed as compliant.
        return (obs > ceil) - (obs < ceil)
    return (observed > ceiling) - (observed < ceiling)


__all__ = [
    "AgentPresence",
    "ApplyStatus",
    "Barrier",
    "ChangeApplier",
    "ChangeOutcome",
    "ChangeResult",
    "ChangeStatus",
    "CollectionMethod",
    "CostClass",
    "DesiredEntity",
    "DesiredState",
    "DiffAction",
    "DraftProposal",
    "DuplicationFinding",
    "DuplicationReport",
    "Enrollment",
    "EntityKind",
    "EntityPolicy",
    "EntityStatus",
    "Finding",
    "FindingsDocument",
    "FreshnessGate",
    "FreshnessResult",
    "GovernApplyRecord",
    "GovernApplyRefused",
    "GovernPlan",
    "Inventory",
    "LaneAction",
    "LaneError",
    "LaneManifest",
    "ObservationEnvelope",
    "ObservationLedger",
    "ObservationRecord",
    "ObservationStore",
    "ObservationStoreError",
    "PlanEntry",
    "PlanEntryKind",
    "Playbook",
    "PlaybookClause",
    "PlaybookValidationError",
    "Probe",
    "ProbeError",
    "ProbeSet",
    "ProducerState",
    "ProposalStatus",
    "ReconcileDiff",
    "ReconcileEntry",
    "RecordState",
    "RemediationAction",
    "RemediationProposal",
    "RemediationStep",
    "RestoreEntry",
    "RestorePlan",
    "RestoreRefusal",
    "Snapshot",
    "SnapshotEntity",
    "Surface",
    "SweepResult",
    "Tombstone",
    "UnremediatedFinding",
    "Verdict",
    "apply_plan",
    "apply_presence",
    "build_restore_plan",
    "collect_duplication",
    "collect_remediation",
    "compute_inputs_hash",
    "compute_plan",
    "compute_reconcile_diff",
    "enrollment_gap",
    "freshness_gated_read",
    "load_lane_set",
    "load_probe_set",
    "observation_store_root",
    "propose_reconcile",
    "reconcile_lanes",
    "snapshot_surface",
    "verify_govern_apply_projection",
]
