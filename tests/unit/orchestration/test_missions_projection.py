"""Ledger-projected mission tests (#2509).

A mission is a declared decomposition of a goal into phases whose status is a
pure deterministic projection over the work-ledger chain plus the evidence
bundle records the phase receipts reference. Nothing about mission status is
stored: it is recomputed from the ledger prefix every time.

Each test maps to an acceptance criterion from the issue:

* AC1 -- a mission defined from a spec produces ledger entries and no stored
  status: recomputing yields the same MissionStatus.
* AC2 -- determinism: two hosts holding byte-identical ledgers compute
  byte-identical status bytes and an equal ``mission_status_hash``.
* AC3 -- verifiability: a phase cannot advance without a mission phase
  receipt; the receipt binds evidence bundle hashes; deleting or altering a
  referenced evidence bundle marks the phase unverified.
* AC4 -- tampering with any ledger entry surfaces at the exact chain position
  and the projection renders unverified rather than best-effort.
* AC5 -- exhausting a phase envelope halts new dispatch for that phase with a
  receipt while other phases continue; per-phase spend rollup matches the
  envelope report.
* AC6 -- killing the orchestrator mid-phase and resuming against a fresh copy
  of the same ledger reproduces the identical status hash.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from bernstein.core.cost.cost_tracker import TokenUsage
from bernstein.core.cost.spend_ledger import LedgerEntry as SpendLedgerEntry
from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    bundle_path,
    load_or_create_evidence_identity,
    read_evidence_bundle,
)
from bernstein.core.orchestration.missions import (
    KIND_MISSION_DEFINED,
    KIND_MISSION_PHASE_PASSED,
    MISSION_ACTIVE,
    MISSION_COMPLETE,
    MISSION_HALTED,
    MISSION_PENDING,
    MISSION_UNVERIFIED,
    PHASE_ACTIVE,
    PHASE_HALTED,
    PHASE_PASSED,
    PHASE_PENDING,
    PHASE_UNVERIFIED,
    MissionSpec,
    MissionSpecError,
    PhaseReceipt,
    PhaseSpec,
    PhaseStatus,
    define_mission,
    enforce_phase_dispatch,
    enter_phase,
    gather_evidence_hashes,
    halt_phase,
    mission_ledger_dir,
    pass_phase,
    phase_spend_report,
    project_mission,
    project_mission_from_ledger,
)
from bernstein.core.persistence.work_ledger import (
    LedgerReader,
    WorkLedger,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec() -> MissionSpec:
    """A two-phase mission: phase 2 gates on the evidence of task ``b``."""
    return MissionSpec(
        mission_id="m-1",
        goal="ship the multi-day migration",
        phases=(
            PhaseSpec(
                phase_id="p1",
                name="prepare",
                gate=("task-a",),
                envelope="mission-m-1-p1",
                budget_usd=40.0,
            ),
            PhaseSpec(
                phase_id="p2",
                name="migrate",
                gate=("task-b",),
                envelope="mission-m-1-p2",
                budget_usd=25.0,
            ),
        ),
    )


def _seal_evidence(workdir: Path, task_id: str, *, timestamp: int = 1000) -> str:
    """Seal a passing evidence bundle for ``task_id`` and return its hash."""
    priv, pub = load_or_create_evidence_identity(workdir / ".sdd" / "identity")
    outcome = ProducerOutcome(
        producer=EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        exit_code=0,
        output=f"ok {task_id}\n".encode(),
    )
    bundle = build_evidence_bundle(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id=task_id,
        outcomes=(outcome,),
        timestamp=timestamp,
    )
    return bundle.bundle_hash()


def _build_full_mission(sdd_dir: Path, workdir: Path) -> MissionSpec:
    """Define a mission and drive both phases to passed with sealed evidence."""
    spec = _spec()
    ledger_dir = mission_ledger_dir(sdd_dir, spec.mission_id)
    ledger = WorkLedger.open(ledger_dir)
    define_mission(ledger=ledger, spec=spec)

    _seal_evidence(workdir, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(workdir, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=12.0)

    _seal_evidence(workdir, "task-b")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    ev = gather_evidence_hashes(workdir, ("task-b",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p2", evidence_hashes=ev, spend_usd=9.0)
    ledger.close()
    return spec


# ---------------------------------------------------------------------------
# Spec validation at the boundary
# ---------------------------------------------------------------------------


def test_spec_round_trips_through_dict() -> None:
    spec = _spec()
    assert MissionSpec.from_dict(spec.to_dict()) == spec
    assert spec.phase_ids() == ("p1", "p2")


def test_spec_rejects_empty_phase_list() -> None:
    with pytest.raises(MissionSpecError, match="at least one phase"):
        MissionSpec(mission_id="m", goal="g", phases=()).validate()


def test_spec_rejects_duplicate_phase_ids() -> None:
    with pytest.raises(MissionSpecError, match="duplicate"):
        MissionSpec(
            mission_id="m",
            goal="g",
            phases=(
                PhaseSpec(phase_id="p", name="a", gate=("t",), envelope="e1", budget_usd=1.0),
                PhaseSpec(phase_id="p", name="b", gate=("t",), envelope="e2", budget_usd=1.0),
            ),
        ).validate()


def test_spec_rejects_negative_budget() -> None:
    with pytest.raises(MissionSpecError, match="budget"):
        MissionSpec(
            mission_id="m",
            goal="g",
            phases=(PhaseSpec(phase_id="p", name="a", gate=("t",), envelope="e", budget_usd=-1.0),),
        ).validate()


def test_spec_rejects_blank_mission_id() -> None:
    with pytest.raises(MissionSpecError, match="mission_id"):
        MissionSpec(
            mission_id="",
            goal="g",
            phases=(PhaseSpec(phase_id="p", name="a", gate=("t",), envelope="e", budget_usd=1.0),),
        ).validate()


def test_spec_hash_is_stable_and_goal_is_never_stored_verbatim() -> None:
    spec = _spec()
    # The ledger payload binds the goal by digest, never the raw text.
    assert spec.goal_digest() != spec.goal
    assert len(spec.goal_digest()) == 64
    assert spec.spec_hash() == _spec().spec_hash()


# ---------------------------------------------------------------------------
# AC1 -- defined mission produces ledger entries, no stored status
# ---------------------------------------------------------------------------


def test_define_mission_writes_ledger_entry_and_no_status_file(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger_dir = mission_ledger_dir(sdd_dir, spec.mission_id)
    ledger = WorkLedger.open(ledger_dir)
    entry = define_mission(ledger=ledger, spec=spec)
    ledger.close()

    assert entry.kind == KIND_MISSION_DEFINED
    assert entry.payload["spec_hash"] == spec.spec_hash()
    assert "goal_digest" in entry.payload
    # No status row: the only on-disk artifact is the append-only chain bucket.
    files = sorted(p.name for p in ledger_dir.iterdir())
    assert files == ["000000.jsonl"]


def test_recompute_after_deleting_derived_state_is_identical(tmp_path: Path) -> None:
    """AC1: deleting any derived cache and recomputing yields the same status."""
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)

    first = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id=spec.mission_id)
    # There is no derived cache to delete -- status is never stored. Recompute
    # from the same ledger prefix and prove byte-identity.
    second = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id=spec.mission_id)

    assert first.status.canonical_bytes() == second.status.canonical_bytes()
    assert first.status_hash == second.status_hash
    assert first.status.overall == MISSION_COMPLETE


# ---------------------------------------------------------------------------
# Phase state machine
# ---------------------------------------------------------------------------


def test_phase_states_track_the_ledger(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger_dir = mission_ledger_dir(sdd_dir, spec.mission_id)
    ledger = WorkLedger.open(ledger_dir)
    define_mission(ledger=ledger, spec=spec)

    # Nothing entered yet: both pending, mission pending.
    status = project_mission(list(LedgerReader(ledger_dir).entries()), {})
    assert [p.state for p in status.phases] == [PHASE_PENDING, PHASE_PENDING]
    assert status.overall == MISSION_PENDING

    # Enter phase 1: it goes active, mission active.
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    status = project_mission(list(LedgerReader(ledger_dir).entries()), {})
    assert status.phases[0].state == PHASE_ACTIVE
    assert status.phases[1].state == PHASE_PENDING
    assert status.overall == MISSION_ACTIVE
    assert status.active_phase == "p1"

    # Pass phase 1 with sealed evidence: it goes passed, active phase advances.
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=5.0)
    ledger.close()
    status = project_mission(list(LedgerReader(ledger_dir).entries()), ev)
    assert status.phases[0].state == PHASE_PASSED
    assert status.active_phase == "p2"


def test_pass_phase_refuses_without_gate_evidence(tmp_path: Path) -> None:
    """A phase gate cannot be satisfied when its evidence is absent (AC3)."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    with pytest.raises(MissionSpecError, match="gate"):
        # task-a evidence was never sealed.
        pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes={}, spend_usd=1.0)
    ledger.close()


# ---------------------------------------------------------------------------
# AC2 -- determinism across hosts (golden)
# ---------------------------------------------------------------------------


def test_two_hosts_compute_identical_status_bytes(tmp_path: Path) -> None:
    host_a = tmp_path / "host-a"
    host_b = tmp_path / "host-b"
    host_a.mkdir()
    host_b.mkdir()
    spec_a = _build_full_mission(host_a / ".sdd", host_a)
    spec_b = _build_full_mission(host_b / ".sdd", host_b)
    assert spec_a.spec_hash() == spec_b.spec_hash()

    proj_a = project_mission_from_ledger(sdd_dir=host_a / ".sdd", workdir=host_a, mission_id="m-1")
    proj_b = project_mission_from_ledger(sdd_dir=host_b / ".sdd", workdir=host_b, mission_id="m-1")

    assert proj_a.status.canonical_bytes() == proj_b.status.canonical_bytes()
    assert proj_a.status_hash == proj_b.status_hash


def test_status_hash_is_pinned_for_a_canonical_ledger(tmp_path: Path) -> None:
    """Golden: a fixed ledger + evidence produces a pinned status hash.

    The projection reads no clock and no host state, so the hash is a pure
    function of the chain payloads and the evidence bundle hashes it binds.
    A pinned digest catches any accidental change to the canonical shape.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    # Evidence hashes are content addresses of the sealed test bundles, which
    # are byte-stable for a fixed producer output + timestamp.
    ev = gather_evidence_hashes(tmp_path, ("task-a", "task-b"))

    status = project_mission(entries, ev)
    assert status.overall == MISSION_COMPLETE
    # Pinned canonical status hash under the current (v2) projection. Regenerate
    # deliberately if the mission status schema changes; a silent drift here
    # means the projection is no longer byte-stable across hosts.
    assert status.status_hash() == _PINNED_STATUS_HASH


def test_v1_projection_reproduces_the_archived_status_hash(tmp_path: Path) -> None:
    """#2683: an archived ledger still reproduces its v1 status hash byte for byte.

    The #2683 fold change moved mission_status_hash for every ledger (the
    schema version is hashed in). A ledger recorded under v1 stays verifiable:
    projecting it under schema_version=1 reproduces the exact hash it was
    recorded with, so a verifier tells an honest older projection apart from
    tampering. The canonical two-phase fixture has no halt, so only the schema
    version distinguishes its v1 and v2 hashes.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    ev = gather_evidence_hashes(tmp_path, ("task-a", "task-b"))

    assert project_mission(entries, ev, schema_version=1).status_hash() == _PINNED_STATUS_HASH_V1
    # v2 is the default, and the two hashes differ (the version is hashed in).
    assert project_mission(entries, ev).status_hash() == _PINNED_STATUS_HASH
    assert _PINNED_STATUS_HASH != _PINNED_STATUS_HASH_V1


def test_project_mission_rejects_an_unsupported_projection_version(tmp_path: Path) -> None:
    """An unrenderable projection version is a caller error, not a fold verdict."""
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    with pytest.raises(ValueError, match="unsupported mission status schema_version"):
        project_mission(entries, {}, schema_version=99)


#: Pinned mission status hash for the canonical two-phase fixture. ``_V1`` is
#: the pre-#2683 projection (retained so archived ledgers stay reproducible);
#: the unsuffixed constant is the current (v2) projection. Anchors the
#: cross-host determinism guarantee in CI.
_PINNED_STATUS_HASH_V1 = "bb00506ccf411e5a329514bcc32cd0299d472396cc2dc405d56438bc435ac839"
_PINNED_STATUS_HASH = "ea1d74c86cba9d74bd27f1eba7b19de1e74e4441a94ce3c8081afa6ad929e846"


# ---------------------------------------------------------------------------
# AC3 -- verifiability: evidence binding
# ---------------------------------------------------------------------------


def test_phase_receipt_binds_evidence_bundle_hashes(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    receipts = [e for e in entries if e.kind == KIND_MISSION_PHASE_PASSED]
    assert len(receipts) == 2
    bound = receipts[0].payload["evidence_bundle_hashes"]
    assert bound and all(h.startswith("sha256:") for h in bound)


def test_deleting_referenced_evidence_marks_phase_unverified(tmp_path: Path) -> None:
    """AC3: deleting a referenced evidence bundle => phase unverified."""
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)

    # Sanity: complete before tampering.
    good = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    assert good.status.overall == MISSION_COMPLETE
    assert good.evidence_verified is True

    # Delete phase 2's evidence bundle file (content-addressed filename).
    bundle_path(tmp_path, "task-b").unlink()

    bad = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    p2 = next(p for p in bad.status.phases if p.phase_id == "p2")
    assert p2.state == PHASE_UNVERIFIED
    assert bad.status.overall == MISSION_UNVERIFIED
    assert bad.evidence_verified is False
    assert bad.status_hash != good.status_hash


def test_altering_referenced_evidence_marks_phase_unverified(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    _build_full_mission(sdd_dir, tmp_path)

    # Re-seal task-b with different bytes -> different bundle hash than the one
    # the receipt bound.
    bundle_before = read_evidence_bundle(tmp_path, "task-b")
    assert bundle_before is not None
    outcome = ProducerOutcome(
        producer=EvidenceProducer(name="tests", kind="test", command=("run",), required=True),
        exit_code=0,
        output=b"tampered bytes\n",
    )
    priv, pub = load_or_create_evidence_identity(tmp_path / ".sdd" / "identity")
    build_evidence_bundle(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        task_id="task-b",
        outcomes=(outcome,),
        timestamp=2000,
    )
    bundle_after = read_evidence_bundle(tmp_path, "task-b")
    assert bundle_after is not None
    assert bundle_after.bundle_hash() != bundle_before.bundle_hash()

    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    p2 = next(p for p in proj.status.phases if p.phase_id == "p2")
    assert p2.state == PHASE_UNVERIFIED
    assert proj.status.overall == MISSION_UNVERIFIED


# ---------------------------------------------------------------------------
# AC4 -- tampering with a ledger entry
# ---------------------------------------------------------------------------


def test_tampering_ledger_entry_renders_unverified(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _build_full_mission(sdd_dir, tmp_path)
    ledger_dir = mission_ledger_dir(sdd_dir, spec.mission_id)
    bucket = ledger_dir / "000000.jsonl"

    lines = bucket.read_text(encoding="utf-8").splitlines()
    # Corrupt the payload of an interior entry without fixing its hash.
    import json

    row = json.loads(lines[1])
    row["payload"]["tampered"] = True
    lines[1] = json.dumps(row, separators=(",", ":"))
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")
    assert proj.ledger_verified is False
    assert proj.status.overall == MISSION_UNVERIFIED


# ---------------------------------------------------------------------------
# AC5 -- per-phase envelope enforcement + rollup
# ---------------------------------------------------------------------------


def _spend_entry(envelope: str, run_key: str, cost: float, ts: float) -> SpendLedgerEntry:
    return SpendLedgerEntry(
        ts=ts,
        ts_iso="",
        run_id=run_key,
        task_id="t",
        agent_id="",
        role="",
        feature_label="",
        model="",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=cost,
        quota_envelope=envelope,
    )


def test_exhausted_phase_envelope_halts_that_phase_only(tmp_path: Path) -> None:
    spec = _spec()
    p1, p2 = spec.phases
    from bernstein.core.orchestration.missions import phase_envelope_key

    key1 = phase_envelope_key(spec.mission_id, p1.phase_id)

    # Phase 1 already spent 38 of its 40 budget; phase 2 spent nothing.
    entries = [
        _spend_entry(p1.envelope, key1, 20.0, 100.0),
        _spend_entry(p1.envelope, key1, 18.0, 200.0),
    ]

    # A 5 USD dispatch for phase 1 would push 38 -> 43 over the 40 cap: halt.
    out1 = enforce_phase_dispatch(
        mission_id=spec.mission_id, phase=p1, entries=entries, projected_cost_usd=5.0, now_ts=300.0
    )
    assert out1.admit is False
    assert out1.halt is not None
    assert out1.halt.breached_dimension == "run"

    # The same tick for phase 2 (its own envelope, empty) is admitted.
    out2 = enforce_phase_dispatch(
        mission_id=spec.mission_id, phase=p2, entries=entries, projected_cost_usd=5.0, now_ts=300.0
    )
    assert out2.admit is True


def test_phase_spend_rollup_matches_envelope_report(tmp_path: Path) -> None:
    spec = _spec()
    p1 = spec.phases[0]
    usages = [
        TokenUsage(
            input_tokens=0,
            output_tokens=0,
            model="opus",
            cost_usd=20.0,
            agent_id="a",
            task_id="t",
            timestamp=100.0,
            quota_envelope=p1.envelope,
        ),
        TokenUsage(
            input_tokens=0,
            output_tokens=0,
            model="opus",
            cost_usd=18.0,
            agent_id="a",
            task_id="t",
            timestamp=200.0,
            quota_envelope=p1.envelope,
        ),
    ]
    row = phase_spend_report(p1, usages, now=300.0)
    assert row.total_spend == pytest.approx(38.0)
    assert row.cap == pytest.approx(40.0)
    # The rollup total equals exactly the committed spend the dispatch gate sees.
    assert row.name == p1.envelope


def test_halt_phase_records_receipt(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    receipt = halt_phase(ledger=ledger, spec=spec, phase_id="p1", spend_usd=41.0, reason="envelope_exhausted")
    ledger.close()
    assert receipt.reason == "envelope_exhausted"

    status = project_mission(list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries()), {})
    assert status.phases[0].state == PHASE_HALTED


# ---------------------------------------------------------------------------
# AC6 -- resume across restart reproduces the identical status hash
# ---------------------------------------------------------------------------


def test_resume_against_fresh_copy_reproduces_status_hash(tmp_path: Path) -> None:
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger_dir = mission_ledger_dir(sdd_dir, spec.mission_id)
    ledger = WorkLedger.open(ledger_dir)
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=7.0)
    # Simulate a kill mid phase 2: phase 2 entered but never passed.
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    ledger.close()  # abrupt stop

    before = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id="m-1")

    # "Reimage": copy the whole .sdd to a fresh host and resume there.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    shutil.copytree(sdd_dir, fresh / ".sdd")
    after = project_mission_from_ledger(sdd_dir=fresh / ".sdd", workdir=fresh, mission_id="m-1")

    assert before.status_hash == after.status_hash
    assert after.status.phases[0].state == PHASE_PASSED
    assert after.status.phases[1].state == PHASE_ACTIVE
    assert after.status.overall == MISSION_ACTIVE


# ---------------------------------------------------------------------------
# Hardening (#2652) -- the receipt must bind the declared gate
# ---------------------------------------------------------------------------


def _defined_ledger(sdd_dir: Path, spec: MissionSpec) -> WorkLedger:
    """Open a ledger with *spec* defined and phase 1 entered."""
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    return ledger


def _append_raw_receipt(ledger: WorkLedger, payload: dict[str, object], *, phase_id: str = "p1") -> None:
    """Append a ``mission.phase_passed`` entry verbatim (bypassing pass_phase).

    Models an attacker (or a buggy writer) that lands a well-formed chain entry
    whose receipt does not bind the phase's declared gate.
    """
    ledger.append(kind=KIND_MISSION_PHASE_PASSED, task_id=phase_id, payload=payload)


def _p1_status(sdd_dir: Path, spec: MissionSpec, evidence: dict[str, str]) -> PhaseStatus:
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    status = project_mission(entries, evidence)
    return next(p for p in status.phases if p.phase_id == "p1")


def test_receipt_for_an_unrelated_task_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    """#2652 critical: a receipt binding other evidence must not pass a gate.

    Phase ``p1`` gates on ``task-a``. A receipt that binds a perfectly valid,
    intact bundle for the unrelated ``task-z`` proves nothing about the gate,
    so the phase must project unverified rather than passed.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    unrelated_hash = _seal_evidence(tmp_path, "task-z")
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id="p1",
        gate_passed=True,
        evidence_task_ids=("task-z",),
        evidence_bundle_hashes=(unrelated_hash,),
        ledger_seq=ledger.next_seq,
        envelope="mission-m-1-p1",
        spend_usd=1.0,
    )
    _append_raw_receipt(ledger, receipt.to_payload())
    ledger.close()

    evidence = gather_evidence_hashes(tmp_path, ("task-a", "task-z"))
    phase = _p1_status(sdd_dir, spec, evidence)
    assert phase.state == PHASE_UNVERIFIED
    assert phase.gate_passed is False


def test_receipt_binding_no_evidence_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    """#2652 critical: an empty evidence binding must not vacuously pass."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id="p1",
        gate_passed=True,
        evidence_task_ids=(),
        evidence_bundle_hashes=(),
        ledger_seq=ledger.next_seq,
        envelope="mission-m-1-p1",
        spend_usd=1.0,
    )
    _append_raw_receipt(ledger, receipt.to_payload())
    ledger.close()

    phase = _p1_status(sdd_dir, spec, gather_evidence_hashes(tmp_path, ("task-a",)))
    assert phase.state == PHASE_UNVERIFIED


def test_receipt_with_failed_gate_verdict_does_not_project_passed(tmp_path: Path) -> None:
    """#2652 critical: ``gate_passed=false`` must never project as passed."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    bundle_hash = _seal_evidence(tmp_path, "task-a")
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id="p1",
        gate_passed=False,
        evidence_task_ids=("task-a",),
        evidence_bundle_hashes=(bundle_hash,),
        ledger_seq=ledger.next_seq,
        envelope="mission-m-1-p1",
        spend_usd=1.0,
    )
    _append_raw_receipt(ledger, receipt.to_payload())
    ledger.close()

    phase = _p1_status(sdd_dir, spec, gather_evidence_hashes(tmp_path, ("task-a",)))
    assert phase.state == PHASE_UNVERIFIED


def test_editing_a_persisted_receipt_hash_tears_the_chain(tmp_path: Path) -> None:
    """Guard: a forged receipt_hash is caught by the ledger, not the projection.

    The projection deliberately does not re-derive the sealed receipt_hash --
    doing so made the fold host-dependent for no adversarial gain. Editing that
    field on disk breaks the entry hash, so the chain walk refuses the whole
    ledger and the mission renders unverified. This test pins the layer that
    actually carries the defence.
    """
    import json as _json

    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=3.0)
    ledger.close()

    bucket = mission_ledger_dir(sdd_dir, spec.mission_id) / "000000.jsonl"
    lines = bucket.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        row = _json.loads(line)
        if row["kind"] == KIND_MISSION_PHASE_PASSED:
            row["payload"]["receipt_hash"] = "0" * 64
            lines[i] = _json.dumps(row, separators=(",", ":"))
            break
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

    proj = project_mission_from_ledger(sdd_dir=sdd_dir, workdir=tmp_path, mission_id=spec.mission_id)
    assert proj.ledger_verified is False
    assert proj.status.overall == MISSION_UNVERIFIED


def test_receipt_with_mismatched_evidence_lengths_does_not_crash_or_pass(tmp_path: Path) -> None:
    """#2652 critical: a ragged task-id/hash binding is a refusal, not a pass."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    bundle_hash = _seal_evidence(tmp_path, "task-a")
    receipt = PhaseReceipt(
        mission_id=spec.mission_id,
        phase_id="p1",
        gate_passed=True,
        evidence_task_ids=("task-a",),
        evidence_bundle_hashes=(bundle_hash, bundle_hash),
        ledger_seq=ledger.next_seq,
        envelope="mission-m-1-p1",
        spend_usd=1.0,
    )
    _append_raw_receipt(ledger, receipt.to_payload())
    ledger.close()

    phase = _p1_status(sdd_dir, spec, gather_evidence_hashes(tmp_path, ("task-a",)))
    assert phase.state == PHASE_UNVERIFIED


def test_honest_receipt_still_projects_passed(tmp_path: Path) -> None:
    """The hardened check must not reject a receipt sealed by pass_phase."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=3.0)
    ledger.close()

    phase = _p1_status(sdd_dir, spec, ev)
    assert phase.state == PHASE_PASSED
    assert phase.gate_passed is True


# ---------------------------------------------------------------------------
# Hardening (#2652) -- a ledger declares exactly one mission
# ---------------------------------------------------------------------------


def test_define_mission_refuses_a_second_definition(tmp_path: Path) -> None:
    """#2652: two definitions in one ledger split projection from evidence."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    with pytest.raises(MissionSpecError, match="already defined"):
        define_mission(ledger=ledger, spec=spec)
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    assert sum(1 for e in entries if e.kind == KIND_MISSION_DEFINED) == 1


def test_define_mission_refuses_a_non_empty_ledger(tmp_path: Path) -> None:
    """#2652: a mission must be the first transition in its own ledger."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    with pytest.raises(MissionSpecError, match="empty ledger"):
        define_mission(ledger=ledger, spec=spec)
    ledger.close()


def test_multiple_definitions_project_unverified(tmp_path: Path) -> None:
    """#2652: a ledger carrying two definitions must never project trusted."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    # Bypass the writer guard the way a hand-edited ledger would.
    ledger.append(
        kind=KIND_MISSION_DEFINED,
        task_id="",
        payload={
            "mission_id": spec.mission_id,
            "spec_hash": spec.spec_hash(),
            "goal_digest": spec.goal_digest(),
            "schema_version": spec.schema_version,
            "phases": [p.to_dict() for p in spec.phases],
        },
    )
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    assert project_mission(entries, {}).overall == MISSION_UNVERIFIED


# ---------------------------------------------------------------------------
# Hardening (#2652) -- spec loader boundary
# ---------------------------------------------------------------------------


def test_spec_from_dict_rejects_a_non_object_root() -> None:
    """#2652: a non-object root is a spec error, not an AttributeError."""
    for bad in ([], "mission", 7, None):
        with pytest.raises(MissionSpecError, match="object"):
            MissionSpec.from_dict(bad)  # type: ignore[arg-type]


def test_spec_from_dict_rejects_a_non_object_phase() -> None:
    """#2652: phases must be objects, not coerced scalars."""
    with pytest.raises(MissionSpecError, match="object"):
        MissionSpec.from_dict({"mission_id": "m", "goal": "g", "phases": ["p1"]})


def test_spec_from_dict_rejects_a_non_list_phases_field() -> None:
    """#2652: a scalar ``phases`` must not be silently coerced to empty."""
    with pytest.raises(MissionSpecError, match="phases"):
        MissionSpec.from_dict({"mission_id": "m", "goal": "g", "phases": "p1"})


def test_spec_rejects_unsupported_schema_version() -> None:
    """#2652: an unknown wire version must not be accepted as version 1."""
    with pytest.raises(MissionSpecError, match="schema_version"):
        MissionSpec.from_dict({**_spec().to_dict(), "schema_version": 99})


def test_spec_from_dict_rejects_non_string_scalars() -> None:
    """#2652: malformed scalars are refused rather than str()-coerced.

    The ``match`` is load-bearing. Without it the old ``str(...)`` coercion
    also raises MissionSpecError -- it stringifies the dict and the result
    fails the pre-existing mission_id regex -- so the test would pass against
    unfixed code and prove nothing about the typed boundary.
    """
    with pytest.raises(MissionSpecError, match="must be a string"):
        MissionSpec.from_dict({**_spec().to_dict(), "mission_id": {"nested": "object"}})


# ---------------------------------------------------------------------------
# Hardening (#2652) -- spend is validated before it is sealed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spend",
    [-1.0, float("nan"), float("inf"), float("-inf"), 40.01],
)
def test_pass_phase_refuses_unvalidated_spend(tmp_path: Path, spend: float) -> None:
    """#2652: negative, non-finite, or over-budget spend never gets sealed."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    before = ledger.next_seq
    with pytest.raises(MissionSpecError, match="spend"):
        pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=spend)
    # Nothing was sealed: the ledger did not advance.
    assert ledger.next_seq == before
    ledger.close()


def test_pass_phase_allows_spend_at_the_budget_ceiling(tmp_path: Path) -> None:
    """The guard refuses over-budget spend, not spend exactly at the cap."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    receipt = pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=40.0)
    ledger.close()
    assert receipt.spend_usd == 40.0


def test_halt_phase_refuses_non_finite_spend(tmp_path: Path) -> None:
    """#2652: a non-finite halt spend would poison the canonical receipt bytes."""
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    with pytest.raises(MissionSpecError, match="spend"):
        halt_phase(ledger=ledger, spec=spec, phase_id="p1", spend_usd=float("nan"), reason="x")
    ledger.close()


# ---------------------------------------------------------------------------
# Review hardening (#2680) -- the receipt hash must survive persistence
# ---------------------------------------------------------------------------


def _pass_with(tmp_path: Path, *, spend: float, envelope: str) -> str:
    """Seal an honest pass receipt and return the projected phase state."""
    sdd_dir = tmp_path / ".sdd"
    spec = MissionSpec(
        mission_id="m-1",
        goal="g",
        phases=(PhaseSpec(phase_id="p1", name="prep", gate=("task-a",), envelope=envelope, budget_usd=40.0),),
    )
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=spend)
    ledger.close()
    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    return project_mission(entries, ev).phases[0].state


def test_negative_zero_spend_still_projects_passed(tmp_path: Path) -> None:
    """#2680: -0.0 is a legal zero spend and must not condemn an honest phase.

    -0.0 passes the ``>= 0`` guard but is falsy, so a lossy ``or 0.0`` read
    would reconstruct +0.0, break the receipt hash comparison, and leave the
    phase unverified forever on an append-only chain.
    """
    assert _pass_with(tmp_path, spend=-0.0, envelope="env-p1") == PHASE_PASSED


def test_zero_spend_still_projects_passed(tmp_path: Path) -> None:
    """Control for the -0.0 case: ordinary zero spend is unaffected."""
    assert _pass_with(tmp_path, spend=0.0, envelope="env-p1") == PHASE_PASSED


def test_negative_zero_spend_is_normalised_before_sealing(tmp_path: Path) -> None:
    """#2680: -0.0 never reaches the chain, so no receipt binds the -0.0 token."""
    import json as _json

    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = _defined_ledger(sdd_dir, spec)
    _seal_evidence(tmp_path, "task-a")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    receipt = pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=-0.0)
    ledger.close()
    assert _json.dumps(receipt.spend_usd) == "0.0"
    bucket = (mission_ledger_dir(sdd_dir, spec.mission_id) / "000000.jsonl").read_text(encoding="utf-8")
    assert "-0.0" not in bucket


def test_redacted_envelope_still_projects_passed(tmp_path: Path) -> None:
    """Guard: a spec-legal envelope the redactor rewrites still passes.

    Seals and projects in one process, so it cannot observe a host dependence
    and does not fail against the defective code -- the cross-host and
    shipped-writer tests above are the detectors. This one exists so a future
    change that strands a redactable envelope fails loudly here too.
    """
    import os

    home = os.environ.get("HOME", "/tmp")
    assert _pass_with(tmp_path, spend=3.0, envelope=f"{home}/envelopes/p1") == PHASE_PASSED


def test_secret_bearing_envelope_still_projects_passed(tmp_path: Path) -> None:
    """Guard: a redacted key=value envelope must not strand an honest phase."""
    assert _pass_with(tmp_path, spend=3.0, envelope="api_key=supersecretvalue123456") == PHASE_PASSED


# ---------------------------------------------------------------------------
# Determinism locks: the projection rules must not drift without a version bump
# ---------------------------------------------------------------------------
#
# Every value below is a byte-stable snapshot of the canonical status. Any edit
# that moves one also moves mission_status_hash for existing ledgers, which may
# only ship with a MISSION_STATUS_SCHEMA_VERSION bump. The v2 goldens pin the
# current fold; the v1 goldens prove an archived ledger still reproduces its
# original hash by projecting under schema_version=1.


def test_status_projection_version_is_pinned_at_2() -> None:
    """The current projection is v2 (#2683); a further bump is a minor release."""
    from bernstein.core.orchestration.missions import MISSION_STATUS_SCHEMA_VERSION

    assert MISSION_STATUS_SCHEMA_VERSION == 2


def _build_halted_beside_runnable(sdd_dir: Path, spec: MissionSpec) -> None:
    """Halt phase 1 (exhausted envelope) with phase 2 entered and runnable."""
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    halt_phase(ledger=ledger, spec=spec, phase_id="p1", spend_usd=40.0, reason="envelope_exhausted")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    ledger.close()


def test_halted_phase_still_halts_the_mission_under_v1(tmp_path: Path) -> None:
    """Retained v1 golden: the coarse halt fold stays reproducible.

    An archived ledger whose status hash was recorded before #2683 must still
    reproduce byte for byte. Projecting the same ledger under schema_version=1
    reproduces the original active_phase, overall, and status hash, so a
    verifier can tell the honest older projection apart from tampering.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    _build_halted_beside_runnable(sdd_dir, spec)

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    status = project_mission(entries, {}, schema_version=1)
    assert status.phases[0].state == PHASE_HALTED
    assert status.phases[1].state == PHASE_ACTIVE
    # The coarse v1 fold: the halted phase is reported active and halts all.
    assert status.active_phase == "p1"
    assert status.overall == "halted"
    assert status.status_hash() == _PINNED_HALTED_STATUS_HASH_V1


def test_halted_phase_does_not_halt_a_mission_with_a_runnable_sibling(tmp_path: Path) -> None:
    """#2683: one halted phase must not halt a mission whose sibling can run.

    Phases run under isolated envelopes (:func:`phase_envelope_key`), so
    exhausting one must not stop a runnable sibling. The default (current)
    projection therefore skips a halted phase when selecting ``active_phase``
    and returns ``MISSION_HALTED`` only when no phase remains runnable. The
    ledger is identical to the v1 test above; only the projection rules differ.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    _build_halted_beside_runnable(sdd_dir, spec)

    status = project_mission(list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries()), {})
    assert status.phases[0].state == PHASE_HALTED
    assert status.phases[1].state == PHASE_ACTIVE
    # The runnable sibling is the active phase, and the mission is not halted.
    assert status.active_phase == "p2"
    assert status.overall == MISSION_ACTIVE
    assert status.overall != MISSION_HALTED


def test_halt_with_no_runnable_sibling_still_halts_the_mission_under_v2(tmp_path: Path) -> None:
    """#2683: isolation weakens no halt -- a mission with nothing runnable halts.

    Phase 1 passes and phase 2 halts, so no phase remains runnable. The v2 fold
    still reports the mission halted and leaves no phase active, proving the
    correction narrows the halt rule rather than removing it.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=12.0)
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p2")
    halt_phase(ledger=ledger, spec=spec, phase_id="p2", spend_usd=25.0, reason="envelope_exhausted")
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    status = project_mission(entries, ev)
    assert status.phases[0].state == PHASE_PASSED
    assert status.phases[1].state == PHASE_HALTED
    assert status.active_phase == ""
    assert status.overall == MISSION_HALTED


def test_halted_ledger_status_hash_is_pinned(tmp_path: Path) -> None:
    """v2 golden for the halted-beside-runnable shape the #2683 change moved.

    The other goldens contain no halt, so nothing else in the suite would catch
    a drift in the v2 halt fold.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = _spec()
    _build_halted_beside_runnable(sdd_dir, spec)

    status = project_mission(list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries()), {})
    assert status.status_hash() == _PINNED_HALTED_STATUS_HASH_V2


#: Pinned status hash for a mission with one halted phase beside a runnable one.
#: ``_V1`` is the coarse pre-#2683 fold (halted phase active, mission halted),
#: reproduced by projecting under schema_version=1; ``_V2`` is the current fold
#: (runnable sibling active, mission active). Regenerate only with a version bump.
_PINNED_HALTED_STATUS_HASH_V1 = "6637c72e25b193f85927b6fb7f5ab2f0902f565db268679b02ad74384bfabdc7"
_PINNED_HALTED_STATUS_HASH_V2 = "fd879e56b1572468c9fa2ee778e814971cc66960f61a7173f0edf2836261fc55"


# ---------------------------------------------------------------------------
# Review hardening (#2680) -- the fold must not read host state
# ---------------------------------------------------------------------------


def _become_host(monkeypatch: pytest.MonkeyPatch, home: str) -> None:
    """Point the process at a different $HOME, as a second machine would.

    The redactor compiles the $HOME pattern once into a module global, so
    setting the environment variable alone leaves the first host's pattern
    live and hides any host dependence entirely. Clearing that cache is what
    makes this equivalent to a fresh process on another machine -- without it
    these tests pass even against the defective code they exist to catch.
    """
    from bernstein.core.security import redactor

    monkeypatch.setenv("HOME", home)
    monkeypatch.setattr(redactor, "_HOME_RE", None, raising=False)


def test_receipt_hash_is_independent_of_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2680: receipt_hash must be a pure function of the receipt's fields.

    Routing the preimage through the ledger's redaction pulled ``$HOME`` into
    it (via ``collapse_home``), so the same receipt hashed differently on two
    machines. This value is folded into mission_status_hash and mirrored onto
    the audit chain, so host dependence here means an untampered mission
    verifies on one host and fails on another.
    """
    receipt = PhaseReceipt(
        mission_id="m-1",
        phase_id="p1",
        gate_passed=True,
        evidence_task_ids=("task-a",),
        evidence_bundle_hashes=("sha256:aa",),
        ledger_seq=2,
        envelope="/home/otheruser/envelopes/p1",
        spend_usd=3.0,
    )
    _become_host(monkeypatch, "/Users/somebody")
    first = receipt.receipt_hash()
    _become_host(monkeypatch, "/home/otheruser")
    assert receipt.receipt_hash() == first


def test_projection_is_independent_of_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#2680: an untampered mission projects identically on any host.

    The envelope is the second host's home path, which only that host's
    redactor rewrites -- the shape that diverged in the field.
    """
    sdd_dir = tmp_path / ".sdd"
    spec = MissionSpec(
        mission_id="m-1",
        goal="g",
        phases=(
            PhaseSpec(
                phase_id="p1",
                name="prep",
                gate=("task-a",),
                envelope="/home/otheruser/envelopes/p1",
                budget_usd=40.0,
            ),
        ),
    )
    _become_host(monkeypatch, "/Users/somebody")
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    pass_phase(ledger=ledger, spec=spec, phase_id="p1", evidence_hashes=ev, spend_usd=3.0)
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    on_host_a = project_mission(entries, ev)

    _become_host(monkeypatch, "/home/otheruser")
    on_host_b = project_mission(entries, ev)

    assert on_host_a.phases[0].state == PHASE_PASSED
    assert on_host_b.phases[0].state == PHASE_PASSED
    assert on_host_a.canonical_bytes() == on_host_b.canonical_bytes()
    assert on_host_a.status_hash() == on_host_b.status_hash()


def _seal_like_pre_pr_writer(ledger: WorkLedger, receipt: PhaseReceipt) -> None:
    """Append a receipt hashed over the raw binding, as the shipped writer does."""
    from bernstein.core.orchestration.missions import _canonical_bytes, _sha256_hex

    raw = receipt._binding()  # reproducing the on-disk shape verbatim
    ledger.append(
        kind=KIND_MISSION_PHASE_PASSED,
        task_id=receipt.phase_id,
        payload=raw | {"receipt_hash": _sha256_hex(_canonical_bytes(raw))},
    )


@pytest.mark.parametrize("envelope_kind", ["plain", "secret", "home_path"])
def test_receipts_sealed_by_the_shipped_writer_still_project_passed(tmp_path: Path, envelope_kind: str) -> None:
    """#2680: receipts already on disk must not become permanently unverifiable.

    The ledger is append-only, so a check that condemns an honest historical
    receipt can never be satisfied afterwards -- the operator cannot re-seal it.
    Any recompute-and-compare must therefore be a fixed point over the real
    write-then-read path, which is why these cases seal through the ledger and
    read back off disk rather than comparing objects in memory.
    """
    import os

    # The home_path case must use this host's real $HOME: collapse_home only
    # rewrites the running user's home, so a foreign path would not be redacted
    # and the case would prove nothing.
    envelope = {
        "plain": "mission-m-1-p1",
        "secret": "api_key=supersecretvalue123456",
        "home_path": f"{os.environ.get('HOME', '/tmp')}/envelopes/p1",
    }[envelope_kind]
    sdd_dir = tmp_path / ".sdd"
    spec = MissionSpec(
        mission_id="m-1",
        goal="g",
        phases=(PhaseSpec(phase_id="p1", name="prep", gate=("task-a",), envelope=envelope, budget_usd=40.0),),
    )
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    _seal_like_pre_pr_writer(
        ledger,
        PhaseReceipt(
            mission_id="m-1",
            phase_id="p1",
            gate_passed=True,
            evidence_task_ids=("task-a",),
            evidence_bundle_hashes=(ev["task-a"],),
            ledger_seq=ledger.next_seq,
            envelope=envelope,
            spend_usd=3.0,
        ),
    )
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    assert project_mission(entries, ev).phases[0].state == PHASE_PASSED


def test_sealed_negative_zero_spend_projects_the_hash_it_always_did(tmp_path: Path) -> None:
    """#2680: reading back a sealed -0.0 must not move an existing status hash.

    A value-preserving parse would surface ``-0.0``, which serialises as a
    different JSON token and changes canonical_bytes for a ledger nobody
    touched. Normalising reproduces the shipped behaviour exactly.

    The pinned hash was recorded under the pre-#2683 projection, so the ledger
    is projected under schema_version=1 to reproduce it byte for byte. The spend
    normalisation is version-independent (the phase-state fold does not depend on
    the projection version), so this also anchors v1 reproduction for a -0.0
    ledger; the assertion below proves the current fold normalises it identically.
    """
    import json as _json

    sdd_dir = tmp_path / ".sdd"
    spec = MissionSpec(
        mission_id="m-1",
        goal="g",
        phases=(PhaseSpec(phase_id="p1", name="prep", gate=("task-a",), envelope="env-p1", budget_usd=40.0),),
    )
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, spec.mission_id))
    define_mission(ledger=ledger, spec=spec)
    _seal_evidence(tmp_path, "task-a")
    enter_phase(ledger=ledger, mission_id=spec.mission_id, phase_id="p1")
    ev = gather_evidence_hashes(tmp_path, ("task-a",))
    _seal_like_pre_pr_writer(
        ledger,
        PhaseReceipt(
            mission_id="m-1",
            phase_id="p1",
            gate_passed=True,
            evidence_task_ids=("task-a",),
            evidence_bundle_hashes=(ev["task-a"],),
            ledger_seq=ledger.next_seq,
            envelope="env-p1",
            spend_usd=-0.0,
        ),
    )
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, spec.mission_id)).entries())
    status = project_mission(entries, ev, schema_version=1)
    assert _json.dumps(status.phases[0].spend_usd) == "0.0"
    assert status.status_hash() == _PINNED_NEGATIVE_ZERO_STATUS_HASH
    # The current fold normalises the sealed -0.0 identically (version-independent).
    assert _json.dumps(project_mission(entries, ev).phases[0].spend_usd) == "0.0"


#: Status hash for a ledger carrying a sealed -0.0 spend, as the pre-#2683
#: projection rendered it. Guards against a value-preserving parse silently
#: moving the hash for existing ledgers, and anchors v1 reproduction.
_PINNED_NEGATIVE_ZERO_STATUS_HASH = "f18f443bb6d3f13ae48ba7cac55cbd61337fe9487c77fbf42882e4c1156e49eb"


# ---------------------------------------------------------------------------
# Round-3 hardening (#2680) -- degenerate spec shapes
# ---------------------------------------------------------------------------


def test_spec_rejects_a_phase_with_an_empty_gate() -> None:
    """#2680: a gateless phase would project passed having bound no evidence.

    ``_receipt_binds_gate`` is vacuously satisfied by an empty gate: the
    verdict is true, the empty task-id set equals the empty declared set, the
    lengths agree at zero, and the evidence loop iterates nothing. Refusing at
    the boundary keeps the projection untouched -- existing ledgers still fold
    exactly as they did -- while making the shape unreachable for new missions.
    """
    with pytest.raises(MissionSpecError, match="at least one evidence task id"):
        MissionSpec(
            mission_id="m",
            goal="g",
            phases=(PhaseSpec(phase_id="p", name="a", gate=(), envelope="e", budget_usd=1.0),),
        ).validate()


def test_spec_from_dict_rejects_a_phase_with_an_empty_gate() -> None:
    """#2680: the same refusal through the JSON boundary the CLI uses."""
    with pytest.raises(MissionSpecError, match="at least one evidence task id"):
        MissionSpec.from_dict(
            {
                "mission_id": "m",
                "goal": "g",
                "phases": [{"phase_id": "p", "name": "a", "gate": [], "envelope": "e", "budget_usd": 1.0}],
            }
        )


def test_negative_zero_budget_hashes_as_positive_zero() -> None:
    """#2680: -0.0 in budget_usd must not fork spec_hash across patch levels.

    The shipped reader collapsed -0.0 via a falsy ``or 0.0``; the typed reader
    that replaced it preserved the sign, so byte-identical spec files would
    have produced different spec_hash values on two patch levels. The pinned
    digest is the one origin/main produces for this spec.
    """
    import json as _json

    spec = MissionSpec.from_dict(
        {
            "mission_id": "m-1",
            "goal": "g",
            "phases": [{"phase_id": "p1", "name": "p", "gate": ["task-a"], "envelope": "env", "budget_usd": -0.0}],
        }
    )
    assert _json.dumps(spec.phases[0].budget_usd) == "0.0"
    assert spec.spec_hash() == _PINNED_NEGATIVE_ZERO_BUDGET_SPEC_HASH


#: spec_hash origin/main produces for a spec declaring ``budget_usd: -0.0``.
_PINNED_NEGATIVE_ZERO_BUDGET_SPEC_HASH = "24015e262d978d966646ce31a480e0135da477219e1196996da8995c677f9f03"


def test_spec_rejects_a_budget_too_large_for_a_float() -> None:
    """#2680: an oversized JSON integer must be a spec error, not OverflowError.

    ``math.isfinite`` raises OverflowError on an int too large to convert, and
    that escaped the boundary -- aborting the whole projection and 500ing the
    route instead of rendering the mission unverified.
    """
    with pytest.raises(MissionSpecError, match="out of range"):
        MissionSpec.from_dict(
            {
                "mission_id": "m-1",
                "goal": "g",
                "phases": [{"phase_id": "p1", "name": "p", "gate": ["a"], "envelope": "e", "budget_usd": 10**400}],
            }
        )


def test_oversized_budget_in_a_ledger_projects_unverified(tmp_path: Path) -> None:
    """#2680: the fold must render the verdict, never raise through it."""
    sdd_dir = tmp_path / ".sdd"
    ledger = WorkLedger.open(mission_ledger_dir(sdd_dir, "m-1"))
    ledger.append(
        kind=KIND_MISSION_DEFINED,
        task_id="",
        payload={
            "mission_id": "m-1",
            "spec_hash": "x",
            "goal_digest": "y",
            "schema_version": 1,
            "phases": [{"phase_id": "p1", "name": "p", "gate": ["a"], "envelope": "e", "budget_usd": 10**400}],
        },
    )
    ledger.close()

    entries = list(LedgerReader(mission_ledger_dir(sdd_dir, "m-1")).entries())
    assert project_mission(entries, {}).overall == MISSION_UNVERIFIED
