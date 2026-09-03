"""Snapshot the governed surface and diff it against a desired-state document.

Issue #5085. Three pure-ish pieces, in the order a ``govern reconcile
--propose`` run uses them:

``snapshot_surface``
    Enumerates every registered adapter, cost lane, scheduled task and
    capability entry into a :class:`Snapshot`. It only reads: registries are
    iterated, schedule records are read straight off disk rather than through
    ``ScheduleStore`` (whose constructor creates its directory), and nothing
    else on the filesystem or in the process is touched.

``compute_reconcile_diff``
    A pure projection of ``(snapshot, desired, baseline)`` onto one
    :class:`ReconcileEntry` per entity. The baseline is the previous run's
    observed state, which is what makes ``NEW`` mean "appeared since the last
    report" rather than "not in the desired document".

``propose_reconcile``
    Recovers the baseline from the run's own prior decision records, computes
    the diff, and writes exactly one anchored :class:`GovernanceDecision` for
    the run. The record carries the observed state it judged, so the next run's
    baseline comes from the chain rather than from a side file that can drift
    away from the record it is supposed to describe.

Stable-id conventions live in :mod:`bernstein.core.govern.reconcile_models`.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.govern.reconcile_models import (
    DesiredState,
    DiffAction,
    EntityKind,
    EntityStatus,
    ReconcileDiff,
    ReconcileEntry,
    Snapshot,
    SnapshotEntity,
)
from bernstein.core.security.governance import GovernanceDecision, anchor_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

#: The action every reconcile decision record carries, so a reader can pick
#: reconcile rows out of a run that also holds access and budget decisions.
RECONCILE_ACTION = "reconcile"

#: The decision subject: the surface being reconciled, not a person.
RECONCILE_SUBJECT = "govern:surface"

#: Where the schedule store keeps its records, relative to ``.sdd``. Read
#: directly because ``ScheduleStore.__init__`` creates the directory, and a
#: propose run must not.
_SCHEDULE_SUBPATH = ("runtime", "schedules")


def _canonical_number(value: float) -> str:
    """Render *value* as a canonical JSON number string."""
    return json.dumps(round(float(value), 6))


def _dotted_path(obj: Any) -> str:
    """Return the dotted import path of *obj* (or of its class)."""
    target = obj if isinstance(obj, type) else type(obj)
    return f"{target.__module__}.{target.__qualname__}"


def _adapter_entities(observed_at: int) -> list[SnapshotEntity]:
    """Enumerate the adapter registry. Id = the registry key."""
    from bernstein.adapters.registry import iter_adapter_specs

    return [
        SnapshotEntity(
            kind=EntityKind.ADAPTER,
            entity_id=name,
            observed_value=_dotted_path(spec),
            observed_at=observed_at,
            evidence_ref="bernstein.adapters.registry",
        )
        for name, spec in iter_adapter_specs()
    ]


def _lane_entities(observed_at: int) -> list[SnapshotEntity]:
    """Enumerate the cost-scheduling lanes. Id = the lane name."""
    from bernstein.core.cost.scheduling.knob_matrix import LANE_MULTIPLIERS

    return [
        SnapshotEntity(
            kind=EntityKind.LANE,
            entity_id=lane,
            observed_value=_canonical_number(multiplier),
            observed_at=observed_at,
            evidence_ref="bernstein.core.cost.scheduling.knob_matrix.LANE_MULTIPLIERS",
        )
        for lane, multiplier in sorted(LANE_MULTIPLIERS.items())
    ]


def _capability_entities(observed_at: int) -> list[SnapshotEntity]:
    """Enumerate declared capability axes. Id = ``<profile>/<axis>``."""
    from bernstein.adapters.capability_profile import BOOLEAN_CAPABILITIES, iter_profiles

    entities: list[SnapshotEntity] = []
    for name, profile in iter_profiles():
        for axis in sorted(BOOLEAN_CAPABILITIES):
            entities.append(
                SnapshotEntity(
                    kind=EntityKind.CAPABILITY,
                    entity_id=f"{name}/{axis}",
                    observed_value="true" if profile.capability_value(axis) else "false",
                    observed_at=observed_at,
                    evidence_ref=f"bernstein.adapters.capability_profile.PROFILES[{name!r}]",
                )
            )
    return entities


def _scheduled_task_entities(sdd_dir: Path, observed_at: int) -> list[SnapshotEntity]:
    """Enumerate registered schedules. Id = the schedule id.

    A record that cannot be parsed is skipped rather than reported as absent:
    the enumeration says what it read, and a malformed file is not a schedule.
    """
    schedule_dir = sdd_dir.joinpath(*_SCHEDULE_SUBPATH)
    if not schedule_dir.is_dir():
        return []
    entities: list[SnapshotEntity] = []
    for path in sorted(schedule_dir.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        record = cast("dict[str, Any]", raw)
        schedule_id = str(record.get("id") or "")
        if not schedule_id:
            continue
        cron = str(record.get("cron", ""))
        misfire = str(record.get("misfire_policy", "skip"))
        entities.append(
            SnapshotEntity(
                kind=EntityKind.SCHEDULED_TASK,
                entity_id=schedule_id,
                observed_value=f"{cron}|{misfire}",
                observed_at=observed_at,
                evidence_ref="/".join((*_SCHEDULE_SUBPATH, path.name)),
            )
        )
    return entities


def snapshot_surface(*, sdd_dir: Path, observed_at: int) -> Snapshot:
    """Enumerate the governed surface reachable from *sdd_dir*.

    Args:
        sdd_dir: The project's ``.sdd`` directory. Only read from.
        observed_at: Integer epoch stamped onto every enumerated entity, so a
            diff can name when the observation it judged was taken.

    Returns:
        A :class:`Snapshot` whose entities are ordered by ``(kind, id)``, so
        two enumerations of an unchanged environment hash identically.
    """
    entities = [
        *_adapter_entities(observed_at),
        *_capability_entities(observed_at),
        *_lane_entities(observed_at),
        *_scheduled_task_entities(sdd_dir, observed_at),
    ]
    entities.sort(key=lambda e: (e.kind.value, e.entity_id))
    return Snapshot(entities=tuple(entities), observed_at=observed_at)


def _classify(
    *,
    declared_value: str | None,
    observed_value: str | None,
    previously_observed: bool,
) -> EntityStatus:
    """Classify one entity against the desired state and the previous report.

    Precedence is fixed so the classification is a total function:
    presence against the desired state decides first, then a value mismatch,
    then whether the entity is a first sighting.
    """
    if declared_value is None:
        return EntityStatus.PRESENT_BUT_UNDECLARED
    if observed_value is None:
        return EntityStatus.DECLARED_BUT_ABSENT
    if observed_value != declared_value:
        return EntityStatus.CHANGED
    if not previously_observed:
        return EntityStatus.NEW
    return EntityStatus.UNCHANGED


def _action_for(status: EntityStatus, *, prune: bool, self_heal: bool) -> DiffAction:
    """Map a status plus its entity policy onto the proposed action.

    A drifted entity whose policy withholds the flag becomes a ``HOLD``: the
    finding stands, nothing is queued. In particular a present-but-undeclared
    entity under ``prune: false`` is never queued for removal.
    """
    if status is EntityStatus.PRESENT_BUT_UNDECLARED:
        return DiffAction.REMOVE if prune else DiffAction.HOLD
    if status is EntityStatus.DECLARED_BUT_ABSENT:
        return DiffAction.ADD if self_heal else DiffAction.HOLD
    if status is EntityStatus.CHANGED:
        return DiffAction.MUTATE if self_heal else DiffAction.HOLD
    return DiffAction.NONE


def _inputs_hash(*, snapshot: Snapshot, desired: DesiredState, baseline: dict[str, str] | None) -> str:
    """Return the content hash of the classification inputs.

    A verifier recomputes the classification from the same three inputs, so a
    tampered snapshot or a widened desired-state document changes the hash.
    """
    payload = {
        "kind": RECONCILE_ACTION,
        "snapshot_hash": snapshot.content_hash(),
        "desired_hash": desired.content_hash(),
        "baseline": None if baseline is None else dict(sorted(baseline.items())),
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_reconcile_diff(
    *,
    snapshot: Snapshot,
    desired: DesiredState,
    baseline: dict[str, str] | None,
    run_id: str,
    timestamp: int,
) -> ReconcileDiff:
    """Diff *snapshot* against *desired*, using *baseline* as the last report.

    Args:
        snapshot: The enumerated actual state.
        desired: The declared desired state, with per-entity and per-kind
            ``prune`` / ``self_heal`` policy.
        baseline: ``"<kind>:<id>" -> observed_value`` from the previous
            propose run, or ``None`` when the run has no previous report. With
            no previous report nothing is ``NEW``: there is no earlier state
            for an entity to be new against, so a first run over a matching
            environment is a clean "no drift" rather than a wall of findings.
        run_id: The run whose spine the resulting decision anchors to.
        timestamp: Integer timestamp; caller-chosen but stable.

    Returns:
        A :class:`ReconcileDiff` with one entry per entity, ordered by
        ``(kind, id)``.
    """
    observed = {(e.kind, e.entity_id): e for e in snapshot.entities}
    declared = desired.by_key()

    entries: list[ReconcileEntry] = []
    for key in sorted(set(observed) | set(declared), key=lambda k: (k[0].value, k[1])):
        kind, entity_id = key
        obs = observed.get(key)
        dec = declared.get(key)
        status = _classify(
            declared_value=None if dec is None else dec.declared_value,
            observed_value=None if obs is None else obs.observed_value,
            previously_observed=baseline is None or f"{kind.value}:{entity_id}" in baseline,
        )
        policy = dec.policy if dec is not None else desired.policy_for(kind)
        entries.append(
            ReconcileEntry(
                kind=kind,
                entity_id=entity_id,
                status=status,
                action=_action_for(status, prune=policy.prune, self_heal=policy.self_heal),
                declared_value=None if dec is None else dec.declared_value,
                observed_value=None if obs is None else obs.observed_value,
                observed_at=None if obs is None else obs.observed_at,
                evidence_ref="" if obs is None else obs.evidence_ref,
            )
        )

    return ReconcileDiff(
        run_id=run_id,
        entries=tuple(entries),
        inputs_hash=_inputs_hash(snapshot=snapshot, desired=desired, baseline=baseline),
        timestamp=timestamp,
    )


def baseline_from_decisions(lineage_root: Path, run_id: str) -> dict[str, str] | None:
    """Return the observed state the most recent propose run for *run_id* saw.

    The baseline is read back out of the run's own decision records, so the
    thing a later run compares against is the same artefact an auditor reads.
    ``None`` means no prior propose run -- distinct from a prior run that
    observed nothing, which returns an empty mapping.
    """
    for record in reversed(read_decisions(lineage_root, run_id)):
        if record.action != RECONCILE_ACTION:
            continue
        observed = record.context.get("observed_state")
        if isinstance(observed, dict):
            pairs = cast("dict[str, Any]", observed)
            return {str(key): str(value) for key, value in pairs.items()}
        return {}
    return None


def propose_reconcile(
    *,
    run_id: str,
    lineage_root: Path,
    hmac_key: bytes,
    snapshot: Snapshot,
    desired: DesiredState,
    now: int,
) -> tuple[ReconcileDiff, GovernanceDecision]:
    """Compute the diff and record it as one anchored decision.

    The only thing this writes is the decision record and its spine entry --
    the diff is a proposal, so nothing on the governed surface moves.

    Args:
        run_id: The run whose spine the record anchors to.
        lineage_root: Spine root (``.sdd/lineage``).
        hmac_key: Audit-chain HMAC key that tags spine entries.
        snapshot: The enumerated actual state.
        desired: The declared desired state.
        now: Integer timestamp; the decision and spine timestamp.

    Returns:
        The computed diff and the anchored decision record.
    """
    baseline = baseline_from_decisions(lineage_root, run_id)
    diff = compute_reconcile_diff(
        snapshot=snapshot,
        desired=desired,
        baseline=baseline,
        run_id=run_id,
        timestamp=now,
    )
    counts: dict[str, int] = {}
    for entry in diff.entries:
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1

    # Import here to avoid circular imports
    from bernstein.core.identity import grants
    from bernstein.core.identity.grant_sweep import sweep_grants

    # Perform grant sweep check
    grant_finding = None
    grant_records_path = lineage_root.parent / "audit" / "grants" / f"{run_id}.jsonl"
    if grant_records_path.is_file():
        # Read the grant records from the audit directory
        result = grants.verify_grant_chain(root=lineage_root.parent / "audit", run_id=run_id, key=hmac_key)
        if result.valid:
            grant_finding = sweep_grants(result, now=now)

    decision = GovernanceDecision(
        run_id=run_id,
        subject=RECONCILE_SUBJECT,
        action=RECONCILE_ACTION,
        verdict=diff.verdict,
        inputs_hash=diff.inputs_hash,
        timestamp=now,
        context={
            "counts": counts,
            "diff": diff.to_dict(),
            "observed_at": snapshot.observed_at,
            "observed_state": snapshot.as_state_map(),
            "grant_finding": grant_finding,
        },
    )
    anchored = anchor_decision(lineage_root=lineage_root, hmac_key=hmac_key, decision=decision)
    return diff, anchored


__all__ = [
    "RECONCILE_ACTION",
    "RECONCILE_SUBJECT",
    "baseline_from_decisions",
    "compute_reconcile_diff",
    "propose_reconcile",
    "snapshot_surface",
]
