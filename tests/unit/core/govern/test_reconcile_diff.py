"""Tests for ``govern reconcile --propose``: snapshot vs desired state (#5085).

The properties under test, in the order the issue lists them:

1. ``test_snapshot_covers_all_four_entity_kinds_with_observed_at``
2. ``test_propose_mutates_nothing_on_disk_or_process`` -- load-bearing
3. ``test_propose_writes_one_decision_record_per_run``
4. ``test_no_drift_exits_zero_and_writes_one_no_drift_record``
5. ``test_drift_exits_nonzero_with_one_verdict_line_per_entity``
6. ``test_present_but_undeclared_with_prune_false_is_held_not_removed``
7. ``test_second_run_reports_only_changes_unless_full``
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.governance_cmd import govern_group
from bernstein.core.govern.reconcile import snapshot_surface
from bernstein.core.govern.reconcile_models import (
    DiffAction,
    EntityKind,
    EntityStatus,
)
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.governance import read_decisions

RUN_ID = "govern-reconcile"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an isolated, repo-local audit key."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_schedule(project: Path, schedule_id: str, cron: str) -> None:
    """Plant a schedule record where ``ScheduleStore`` keeps them."""
    sched_dir = project / ".sdd" / "runtime" / "schedules"
    sched_dir.mkdir(parents=True, exist_ok=True)
    (sched_dir / f"{schedule_id}.json").write_text(
        json.dumps({"id": schedule_id, "cron": cron, "goal": "nightly", "misfire_policy": "skip"}),
        encoding="utf-8",
    )


def _desired_from_snapshot(
    project: Path,
    *,
    drop: int = 0,
    mutate: tuple[EntityKind, str] | None = None,
) -> Path:
    """Write a desired-state document that mirrors the live snapshot.

    ``drop`` removes the first N entities (making them present-but-undeclared);
    ``mutate`` rewrites one entity's declared value (making it changed).
    """
    snapshot = snapshot_surface(sdd_dir=project / ".sdd", observed_at=1000)
    entities = [
        {
            "kind": e.kind.value,
            "id": e.entity_id,
            "declared_value": e.observed_value,
            "prune": False,
            "self_heal": False,
        }
        for e in snapshot.entities
    ]
    entities = entities[drop:]
    if mutate is not None:
        kind, entity_id = mutate
        for entry in entities:
            if entry["kind"] == kind.value and entry["id"] == entity_id:
                entry["declared_value"] = "definitely-not-the-observed-value"
                break
        else:  # pragma: no cover - fixture misuse
            raise AssertionError(f"no snapshot entity {kind.value}:{entity_id}")
    path = project / "desired.json"
    path.write_text(json.dumps({"v": 1, "entities": entities}), encoding="utf-8")
    return path


def _tree_digest(root: Path, *, exclude: Path) -> dict[str, str]:
    """Return ``relative path -> sha256`` for every file under *root*."""
    digest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if exclude in path.parents or path == exclude:
            continue
        if path.is_file():
            digest[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            digest[str(path.relative_to(root)) + "/"] = "<dir>"
    return digest


def _invoke(project: Path, desired: Path, *extra: str) -> object:
    return CliRunner().invoke(
        govern_group,
        ["reconcile", "--propose", "--desired", str(desired), "--workdir", str(project), *extra],
    )


# ---------------------------------------------------------------------------
# 1. Snapshot shape
# ---------------------------------------------------------------------------


def test_snapshot_covers_all_four_entity_kinds_with_observed_at(project: Path) -> None:
    """Every entity kind is enumerated, keyed by a stable id, stamped observed_at."""
    _write_schedule(project, "sched-abc", "0 3 * * *")

    snapshot = snapshot_surface(sdd_dir=project / ".sdd", observed_at=1234)

    kinds = {e.kind for e in snapshot.entities}
    assert kinds == {
        EntityKind.ADAPTER,
        EntityKind.LANE,
        EntityKind.SCHEDULED_TASK,
        EntityKind.CAPABILITY,
    }
    assert all(e.observed_at == 1234 for e in snapshot.entities)
    assert all(e.entity_id for e in snapshot.entities)
    # Stable ids: unique per (kind, id), and one scheme per kind.
    keys = [(e.kind, e.entity_id) for e in snapshot.entities]
    assert len(keys) == len(set(keys))
    assert (EntityKind.SCHEDULED_TASK, "sched-abc") in keys
    assert all("/" in e.entity_id for e in snapshot.entities if e.kind is EntityKind.CAPABILITY), (
        "capability ids are scope/name"
    )
    # Two snapshots of an unchanged tree are byte-identical.
    assert snapshot.content_hash() == snapshot_surface(sdd_dir=project / ".sdd", observed_at=1234).content_hash()


# ---------------------------------------------------------------------------
# 2. Load-bearing: propose mutates nothing
# ---------------------------------------------------------------------------


def test_propose_mutates_nothing_on_disk_or_process(project: Path) -> None:
    """``--propose`` writes its decision record and touches nothing else."""
    # Bootstrap the audit key up front: creating it is chain infrastructure the
    # run needs, not a change to the governed surface under test.
    load_or_create_audit_key(project / "audit.key")
    _write_schedule(project, "sched-abc", "0 3 * * *")
    desired = _desired_from_snapshot(project)

    lineage_root = project / ".sdd" / "lineage"
    before = _tree_digest(project, exclude=lineage_root)
    env_before = dict(os.environ)
    cwd_before = os.getcwd()

    result = _invoke(project, desired)

    assert result.exit_code == 0, result.output
    assert _tree_digest(project, exclude=lineage_root) == before
    assert dict(os.environ) == env_before
    assert os.getcwd() == cwd_before


# ---------------------------------------------------------------------------
# 3. One decision record per run
# ---------------------------------------------------------------------------


def test_propose_writes_one_decision_record_per_run(project: Path) -> None:
    """One propose run appends exactly one anchored decision record."""
    desired = _desired_from_snapshot(project)

    result = _invoke(project, desired)
    assert result.exit_code == 0, result.output

    records = read_decisions(project / ".sdd" / "lineage", RUN_ID)
    assert len(records) == 1
    assert records[0].action == "reconcile"
    assert records[0].journal_entry_hash

    _invoke(project, desired)
    assert len(read_decisions(project / ".sdd" / "lineage", RUN_ID)) == 2


# ---------------------------------------------------------------------------
# 4. No drift
# ---------------------------------------------------------------------------


def test_no_drift_exits_zero_and_writes_one_no_drift_record(project: Path) -> None:
    """A snapshot that matches the desired state exits 0 with one no-drift record."""
    desired = _desired_from_snapshot(project)

    result = _invoke(project, desired)

    assert result.exit_code == 0, result.output
    assert "no drift" in result.output
    records = read_decisions(project / ".sdd" / "lineage", RUN_ID)
    assert len(records) == 1
    assert records[0].verdict == "no_drift"


# ---------------------------------------------------------------------------
# 5. Drift
# ---------------------------------------------------------------------------


def test_drift_exits_nonzero_with_one_verdict_line_per_entity(project: Path) -> None:
    """Each drifted entity gets exactly one verdict line; the run exits non-zero."""
    _write_schedule(project, "sched-abc", "0 3 * * *")
    snapshot = snapshot_surface(sdd_dir=project / ".sdd", observed_at=1000)
    lane_ids = [e.entity_id for e in snapshot.entities if e.kind is EntityKind.LANE]
    desired = _desired_from_snapshot(project, drop=2, mutate=(EntityKind.LANE, lane_ids[0]))

    result = _invoke(project, desired)

    assert result.exit_code == 2, result.output
    drift_lines = [line for line in result.output.splitlines() if line.startswith("drift ")]
    # 2 dropped entities (present-but-undeclared) + 1 mutated value (changed).
    assert len(drift_lines) == 3
    records = read_decisions(project / ".sdd" / "lineage", RUN_ID)
    assert records[-1].verdict == "drift"


# ---------------------------------------------------------------------------
# 6. Prune gate
# ---------------------------------------------------------------------------


def test_present_but_undeclared_with_prune_false_is_held_not_removed(project: Path) -> None:
    """An undesired entity under ``prune: false`` is held, never queued for removal."""
    from bernstein.core.govern.reconcile import compute_reconcile_diff
    from bernstein.core.govern.reconcile_models import DesiredState, Snapshot, SnapshotEntity

    snapshot = Snapshot(
        entities=(
            SnapshotEntity(
                kind=EntityKind.ADAPTER,
                entity_id="stray",
                observed_value="x",
                observed_at=10,
                evidence_ref="fixture",
            ),
        ),
        observed_at=10,
    )
    held = compute_reconcile_diff(
        snapshot=snapshot,
        desired=DesiredState.from_dict({"v": 1, "entities": []}),
        baseline={},
        run_id=RUN_ID,
        timestamp=10,
    )
    (entry,) = held.entries
    assert entry.status is EntityStatus.PRESENT_BUT_UNDECLARED
    assert entry.action is DiffAction.HOLD

    pruned = compute_reconcile_diff(
        snapshot=snapshot,
        desired=DesiredState.from_dict(
            {"v": 1, "entities": [], "defaults": {"adapter": {"prune": True, "self_heal": False}}}
        ),
        baseline={},
        run_id=RUN_ID,
        timestamp=10,
    )
    assert pruned.entries[0].action is DiffAction.REMOVE


# ---------------------------------------------------------------------------
# 7. Incremental reporting
# ---------------------------------------------------------------------------


def test_second_run_reports_only_changes_unless_full(project: Path) -> None:
    """A consecutive run prints only what moved; ``--full`` prints the whole state."""
    desired = _desired_from_snapshot(project)

    first = _invoke(project, desired)
    assert first.exit_code == 0, first.output
    assert not [line for line in first.output.splitlines() if line.startswith("state ")]

    full = _invoke(project, desired, "--full")
    assert full.exit_code == 0, full.output
    snapshot = snapshot_surface(sdd_dir=project / ".sdd", observed_at=1000)
    state_lines = [line for line in full.output.splitlines() if line.startswith("state ")]
    assert len(state_lines) == len(snapshot.entities)


# ---------------------------------------------------------------------------
# 8. "new" is relative to the previous report
# ---------------------------------------------------------------------------


def test_entity_absent_from_previous_report_classifies_as_new(project: Path) -> None:
    """An entity that appeared since the last run is NEW, even when it matches."""
    desired = _desired_from_snapshot(project)
    assert _invoke(project, desired).exit_code == 0

    _write_schedule(project, "sched-new", "0 4 * * *")
    desired = _desired_from_snapshot(project)

    result = _invoke(project, desired)

    assert result.exit_code == 2, result.output
    drift_lines = [line for line in result.output.splitlines() if line.startswith("drift ")]
    assert len(drift_lines) == 1
    assert EntityStatus.NEW.value in drift_lines[0]
    assert "scheduled_task:sched-new" in drift_lines[0]
