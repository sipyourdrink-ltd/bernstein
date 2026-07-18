"""Record and verify a recurring-goal fire as a deterministic projection.

Issue #2302. A recurring goal fire is a pure projection of
``(schedule_id, fire_time, last_state)`` onto a canonical task graph with
a deterministic ``graph_hash``. This module is the recording and
verification boundary that turns each fire into a replayable, verifiable
artifact:

- :func:`record_fire` runs the projection, writes ``{schedule_id,
  fire_time, last_state_hash, graph_hash}`` into the run **event journal**
  and seals the canonical graph bytes into the run **lineage spine**, then
  anchors the fire in the HMAC **audit chain**. The three stores share one
  fire so a verifier holding any of them can pin the exact graph the
  operator dispatched.
- :func:`verify_fire` re-runs the projection from the same inputs and
  confirms the recomputed ``graph_hash`` equals the one recorded in the
  journal - ``schedule verify`` replaying a past fire.

Determinism discipline: the fire-record run id, the journal payload, and
the spine timestamp are all pure functions of ``(schedule_id,
fire_time)``, so two operators with identical state produce byte-identical
journal and spine entries. No wall-clock leaks into any hashed field.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.schedule_projection import (
    ProjectionResult,
    project,
    project_schedule_fire,
)
from bernstein.core.replay.journal import EventJournal, contained_run_journal, load_events

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

#: Event type written into the per-run journal for each fire projection.
JOURNAL_EVENT = "schedule.fire_projection"

#: Actor recorded on the lineage-spine seal and the audit-chain anchor.
_ACTOR = "schedule_projection"


def fire_run_id(schedule_id: str, fire_time: int) -> str:
    """Return the deterministic run id keying a fire's journal and spine.

    Derived purely from ``(schedule_id, fire_time)`` so two operators land
    on the same run directory for the same fire, and so the id is
    filesystem-safe (no separators - :class:`LineageSpine` rejects those).
    """
    seed = json.dumps({"schedule_id": schedule_id, "fire_time": fire_time}, sort_keys=True).encode()
    return "sched-fire-" + hashlib.sha256(seed).hexdigest()[:16]


@dataclass(frozen=True)
class FireRecord:
    """The recorded outcome of one fire projection.

    Attributes:
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the fire instant.
        last_state_hash: Digest of the folded ``last_state`` (``"genesis"``
            for the first fire).
        graph_hash: Canonical task-graph hash the inputs project onto.
        journal_head: Merkle head of the fire's event journal after the
            projection row was appended.
        spine_entry_hash: Lineage-spine entry hash the graph bytes sealed
            into, or ``""`` when lineage recording is disabled.
        trigger_input_hash: Bound webhook / file-change event hash, empty
            for a plain scheduled fire.
        recurrence: Canonical recurrence rule, empty when none declared.
    """

    schedule_id: str
    fire_time: int
    last_state_hash: str
    graph_hash: str
    journal_head: str
    spine_entry_hash: str
    trigger_input_hash: str
    recurrence: str


def _projection_for(
    *,
    schedule_id: str,
    fire_time: int,
    last_state: Mapping[str, Any] | None,
    goal: str,
    scenario_id: str,
    recurrence: str,
    trigger_event: bytes | None,
) -> ProjectionResult:
    return project(
        schedule_id,
        fire_time,
        last_state,
        goal=goal,
        scenario_id=scenario_id,
        recurrence=recurrence,
        trigger_event=trigger_event,
    )


def record_fire(
    *,
    sdd_dir: Path,
    schedule_id: str,
    fire_time: int,
    last_state: Mapping[str, Any] | None = None,
    goal: str = "",
    scenario_id: str = "",
    recurrence: str = "",
    trigger_event: bytes | None = None,
    hmac_key: bytes | None = None,
    audit_chain: Any | None = None,
) -> FireRecord:
    """Record one fire projection into journal, lineage spine, and chain.

    Runs the deterministic projection, then:

    1. Appends a ``schedule.fire_projection`` row carrying ``{schedule_id,
       fire_time, last_state_hash, graph_hash}`` to the fire's event
       journal (AC3).
    2. Seals the canonical graph bytes into the run lineage spine with a
       deterministic ``timestamp=fire_time`` so replay is byte-identical.
    3. When *audit_chain* is supplied, anchors the fire in the HMAC chain
       via :func:`record_schedule_fire_projection`, binding the spine entry
       hash and (for a trigger fire) the trigger input hash.

    Args:
        sdd_dir: Project ``.sdd`` directory.
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the fire instant.
        last_state: Optional mapping folded into the projection digest.
        goal: Free-form goal text.
        scenario_id: Optional named scenario id.
        recurrence: Recurrence rule text (bare cron, ``cron:``, or
            ``RRULE:``); empty means none declared.
        trigger_event: For a webhook / file-change trigger, the raw event
            bytes bound into the projection; ``None`` for a scheduled fire.
        hmac_key: Audit-chain HMAC key for the lineage seal. Loaded via the
            canonical resolver when omitted.
        audit_chain: Optional :class:`AuditChainStore`; when supplied the
            fire is anchored in the HMAC chain.

    Returns:
        A :class:`FireRecord` with the graph hash, journal head, and spine
        entry hash.
    """
    projection = _projection_for(
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state=last_state,
        goal=goal,
        scenario_id=scenario_id,
        recurrence=recurrence,
        trigger_event=trigger_event,
    )
    run_id = fire_run_id(schedule_id, fire_time)

    journal = EventJournal(run_id, sdd_dir)
    journal.record(
        JOURNAL_EVENT,
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state_hash=projection.last_state_digest,
        graph_hash=projection.graph_hash,
        rev=projection.rev,
        recurrence=projection.recurrence,
        trigger_input_hash=projection.trigger_input_hash,
        # Persist the projection inputs so ``schedule verify`` re-derives
        # the fire from the journal row alone, independent of the live
        # ScheduleStore (which may have been edited or removed since).
        goal=goal,
        scenario_id=scenario_id,
    )

    if hmac_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        hmac_key = load_or_create_audit_key()

    from bernstein.adapters.base import record_artifact_write

    spine_entry_hash = record_artifact_write(
        artifact_path=f".sdd/runs/{run_id}/schedule-graph.json",
        content=projection.canonical_bytes,
        actor=_ACTOR,
        step_id=f"schedule-fire:{schedule_id}@{fire_time}",
        model="",
        lineage_root=sdd_dir / "lineage",
        run_id=run_id,
        hmac_key=hmac_key,
        timestamp=fire_time,
    )

    if audit_chain is not None:
        from bernstein.core.security.audit_chain import record_schedule_fire_projection

        record_schedule_fire_projection(
            chain=audit_chain,
            schedule_id=schedule_id,
            fire_time=fire_time,
            last_state_hash=projection.last_state_digest,
            graph_hash=projection.graph_hash,
            journal_entry_hash=spine_entry_hash or "",
            trigger_input_hash=projection.trigger_input_hash,
            recurrence=projection.recurrence,
        )

    return FireRecord(
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state_hash=projection.last_state_digest,
        graph_hash=projection.graph_hash,
        journal_head=journal.head(),
        spine_entry_hash=spine_entry_hash or "",
        trigger_input_hash=projection.trigger_input_hash,
        recurrence=projection.recurrence,
    )


@dataclass(frozen=True)
class FireVerification:
    """Outcome of replaying and re-deriving a past fire (``schedule verify``).

    Attributes:
        schedule_id: Schedule the fire belongs to.
        fire_time: Fire instant the record covers.
        recorded_graph_hash: The graph hash read back from the journal.
        recomputed_graph_hash: The graph hash re-derived from the same
            inputs.
        match: ``True`` iff recomputed equals recorded.
        reason: Human-readable failure reason (empty on a match).
    """

    schedule_id: str
    fire_time: int
    recorded_graph_hash: str
    recomputed_graph_hash: str
    match: bool
    reason: str = ""


def load_fire_records(sdd_dir: Path) -> list[dict[str, Any]]:
    """Load every recorded fire-projection journal row across all fires.

    Walks each ``.sdd/runs/sched-fire-*/journal.jsonl`` and returns the
    ``schedule.fire_projection`` rows in ``(fire_time, schedule_id)`` order
    so ``schedule verify`` iterates them deterministically.
    """
    runs_root = sdd_dir / "runs"
    if not runs_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(runs_root.glob("sched-fire-*")):
        journal_path = contained_run_journal(runs_root, run_dir.name)
        if journal_path is None:
            continue
        for event in load_events(journal_path):
            if event.get("event") == JOURNAL_EVENT:
                rows.append(event)
    rows.sort(key=lambda r: (int(r.get("fire_time", 0)), str(r.get("schedule_id", ""))))
    return rows


def verify_fire(
    *,
    schedule_id: str,
    fire_time: int,
    recorded_graph_hash: str,
    last_state: Mapping[str, Any] | None = None,
    goal: str = "",
    scenario_id: str = "",
    recurrence: str = "",
    trigger_input_hash: str = "",
) -> FireVerification:
    """Replay a past fire and confirm its graph hash still matches (AC4).

    Re-runs the deterministic projection from the recorded inputs and
    compares the recomputed ``graph_hash`` against the one persisted at
    fire time. A divergence names the fire, so ``schedule verify`` is safe
    as a CI gate.

    The recorded inputs are already in canonical form (the ``recurrence``
    and ``trigger_input_hash`` were canonicalised / hashed at fire time and
    persisted verbatim), so this re-derives via
    :func:`project_schedule_fire` directly rather than re-canonicalising -
    the raw trigger event bytes are not retained, only their bound hash.

    Args:
        schedule_id: Stable schedule identifier.
        fire_time: Integer Unix epoch of the fire instant.
        recorded_graph_hash: Graph hash read back from the journal.
        last_state: The ``last_state`` folded at fire time (``None`` for a
            genesis fire).
        goal: Goal text used at fire time.
        scenario_id: Scenario id used at fire time.
        recurrence: Canonical recurrence rule used at fire time.
        trigger_input_hash: Bound trigger event hash from fire time, if any.

    Returns:
        A :class:`FireVerification`; ``result.match`` is ``True`` only when
        the recomputed hash equals the recorded one.
    """
    projection = project_schedule_fire(
        schedule_id=schedule_id,
        fire_time=fire_time,
        last_state=last_state,
        goal=goal,
        scenario_id=scenario_id,
        recurrence=recurrence,
        trigger_input_hash=trigger_input_hash,
    )
    recomputed = projection.graph_hash
    match = recomputed == recorded_graph_hash
    reason = (
        "" if match else f"graph hash mismatch: recorded {recorded_graph_hash[:16]} != recomputed {recomputed[:16]}"
    )
    return FireVerification(
        schedule_id=schedule_id,
        fire_time=fire_time,
        recorded_graph_hash=recorded_graph_hash,
        recomputed_graph_hash=recomputed,
        match=match,
        reason=reason,
    )


def verify_all_fires(sdd_dir: Path) -> list[FireVerification]:
    """Replay every recorded fire and confirm each graph hash (AC4).

    Walks every ``schedule.fire_projection`` journal row via
    :func:`load_fire_records` and re-derives each fire from its persisted
    inputs. The list is ordered by ``(fire_time, schedule_id)`` so two
    operators walk their fires in the same order.
    """
    return [
        verify_fire(
            schedule_id=str(row.get("schedule_id", "")),
            fire_time=int(row.get("fire_time", 0)),
            recorded_graph_hash=str(row.get("graph_hash", "")),
            last_state=None,
            goal=str(row.get("goal", "")),
            scenario_id=str(row.get("scenario_id", "")),
            recurrence=str(row.get("recurrence", "")),
            trigger_input_hash=str(row.get("trigger_input_hash", "")),
        )
        for row in load_fire_records(sdd_dir)
    ]


__all__ = [
    "JOURNAL_EVENT",
    "FireRecord",
    "FireVerification",
    "fire_run_id",
    "load_fire_records",
    "record_fire",
    "verify_all_fires",
    "verify_fire",
]
