"""End-to-end tests for the BLOCKER clearance-gate coordinator (#2556).

Posting a ``blocker`` deterministically projects a clearance task plus injected
``depends_on`` edges, sealed as ``signal.gate_projection`` receipts on the HMAC
audit chain. The gate state is a projection of chained rows; resolving the
clearance emits a signed release entry referencing the blocker entry hash; and
an offline verifier reconstructs, from the chain alone, that no dependent task
was claimed while a gate was open.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.communication.signal_actions import (
    ClearanceGateCoordinator,
    ClearanceStatus,
    InMemoryClearanceInjector,
    journal_prefix_hash,
    project_clearance_gate,
    project_gate_states,
    verify_clearance_gates,
)
from bernstein.core.security.audit_chain import (
    EVENT_SIGNAL_GATE_PROJECTION,
    AuditChainStore,
    record_task_claim_receipt,
)


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _post_blocker(board: BulletinBoard, content: str = "shared dep broke", cell_id: str = "cell-a") -> BulletinMessage:
    return board.post(
        BulletinMessage(
            agent_id="worker-3", type="blocker", content=content, timestamp=1_700_000_000.0, cell_id=cell_id
        )
    )


# ---------------------------------------------------------------------------
# Materialization + idempotency (AC1)
# ---------------------------------------------------------------------------


def test_blocker_materializes_one_clearance_task_and_edges(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x", "task-y"]})
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=_chain(tmp_path))

    _post_blocker(board)
    specs = coord.process_new_blockers()

    assert len(specs) == 1
    spec = specs[0]
    assert len(injector.created) == 1
    assert injector.created[0].clearance_task_id == spec.clearance_task_id
    # Every open dependent task in scope receives an edge onto the clearance task.
    assert set(injector.edges) == {("task-x", spec.clearance_task_id), ("task-y", spec.clearance_task_id)}


def test_reprocessing_same_blocker_is_idempotent(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    blocker = _post_blocker(board)
    coord.materialize(blocker)
    coord.materialize(blocker)  # replay same blocker

    # One clearance task, one edge, one projection receipt: no double injection.
    assert len(injector.created) == 1
    assert len(injector.edges) == 1
    projections = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)
    assert len(projections) == 1


def test_non_blocker_signals_are_observe_only(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    for observe_type in ("alert", "finding", "status", "dependency"):
        board.post(BulletinMessage(agent_id="w", type=observe_type, content="hi", timestamp=1.0, cell_id="cell-a"))
    specs = coord.process_new_blockers()

    assert specs == []
    assert injector.created == []
    assert chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION) == []


# ---------------------------------------------------------------------------
# Determinism (AC2): replay yields byte-identical gate state
# ---------------------------------------------------------------------------


def test_replaying_journal_yields_identical_gate_state(tmp_path: Path) -> None:
    def run(sub: str) -> tuple[str, tuple[str, ...], str]:
        board = BulletinBoard()
        injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-y", "task-x"]})
        coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=_chain(tmp_path / sub))
        board.post(BulletinMessage(agent_id="w1", type="status", content="up", timestamp=1.0, cell_id="cell-a"))
        blocker = board.post(
            BulletinMessage(agent_id="worker-3", type="blocker", content="x", timestamp=2.0, cell_id="cell-a")
        )
        spec = coord.materialize(blocker)
        assert spec is not None
        return spec.clearance_task_id, spec.injected_edges, spec.graph_delta_hash

    assert run("a") == run("b")


# ---------------------------------------------------------------------------
# Gate state is a projection of chained rows (AC2 / AC3)
# ---------------------------------------------------------------------------


def test_gate_state_projects_from_chain_rows(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    blocker = _post_blocker(board)
    spec = coord.materialize(blocker)
    assert spec is not None

    states = project_gate_states(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION))
    assert states[spec.clearance_task_id].status is ClearanceStatus.PENDING

    coord.resolve(spec.clearance_task_id, resolver="operator:alex")
    assert injector.released == [spec.clearance_task_id]

    states_after = project_gate_states(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION))
    resolved = states_after[spec.clearance_task_id]
    assert resolved.status is ClearanceStatus.CLEARED
    assert resolved.resolver == "operator:alex"


def test_expiry_is_deterministic_function_of_recorded_inputs(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain, ttl_seconds=3600)

    blocker = _post_blocker(board)  # timestamp 1_700_000_000
    spec = coord.materialize(blocker)
    assert spec is not None

    events = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)
    # Before deadline: still pending. After deadline: deterministically expired.
    before = project_gate_states(events, as_of=1_700_000_000 + 10)
    after = project_gate_states(events, as_of=1_700_000_000 + 3601)
    assert before[spec.clearance_task_id].status is ClearanceStatus.PENDING
    assert after[spec.clearance_task_id].status is ClearanceStatus.EXPIRED


# ---------------------------------------------------------------------------
# Forensic replay (AC4)
# ---------------------------------------------------------------------------


def test_forensic_verify_passes_on_clean_chain(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    blocker = _post_blocker(board)
    spec = coord.materialize(blocker)
    assert spec is not None
    coord.resolve(spec.clearance_task_id, resolver="operator:alex")

    result = verify_clearance_gates(chain.query())
    assert result.ok, result.errors
    assert result.gate_count == 1
    assert result.violations == []


def test_forensic_verify_flags_claim_during_open_gate(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    blocker = _post_blocker(board)
    spec = coord.materialize(blocker)
    assert spec is not None

    # Simulate a rogue claim of a scoped dependent while the gate is still open.
    record_task_claim_receipt(
        chain=chain,
        task_id="task-x",
        role="backend",
        claimed_by="sess-rogue",
        depends_on=[spec.clearance_task_id],
        task_version=2,
        claim_path="by_id",
    )

    result = verify_clearance_gates(chain.query())
    assert not result.ok
    assert ("task-x" in v[0] for v in result.violations)
    assert result.violations
    offending_task, _claim_index = result.violations[0]
    assert offending_task == "task-x"


# ---------------------------------------------------------------------------
# AC1 with the real task store: claim_next withholds dependents until cleared
# ---------------------------------------------------------------------------


def test_real_store_withholds_dependent_until_clearance_cleared(tmp_path: Path) -> None:
    from bernstein.core.communication.bulletin import BulletinMessage as _BM
    from bernstein.core.server import TaskCreate
    from bernstein.core.tasks.task_store_core import TaskStore

    async def scenario() -> tuple[bool, bool]:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        dep = await store.create(TaskCreate(title="dependent work", description="d", role="backend", cell_id="cell-a"))

        # Deterministic projection over the real open-task scope in the cell.
        blocker = _BM(agent_id="worker-3", type="blocker", content="shared dep broke", timestamp=1.0, cell_id="cell-a")
        open_ids = [t.id for t in store.list_tasks(status="open", cell_id="cell-a")]
        spec = project_clearance_gate(
            blocker=blocker, scope_task_ids=open_ids, journal_prefix_hash=journal_prefix_hash([blocker])
        )

        # Materialize: create the clearance task with the projected id and inject
        # the edge onto every open dependent. The clearance task participates as
        # an ordinary depends_on edge, so the existing dependency gate applies.
        await store.create_gate_task(
            clearance_task_id=spec.clearance_task_id,
            title="clearance gate",
            role="clearance",
            cell_id="cell-a",
        )
        for dependent_id in spec.injected_edges:
            await store.inject_dependency(dependent_id, spec.clearance_task_id)

        # Dependent now depends on the still-open clearance task -> not claimable.
        blocked = await store.claim_next("backend") is None

        # Clear the gate -> the clearance task becomes terminal, releasing deps.
        await store.resolve_gate_task(spec.clearance_task_id, resolution="cleared")
        claimed = await store.claim_next("backend")
        released = claimed is not None and claimed.id == dep.id
        return blocked, released

    blocked, released = asyncio.run(scenario())
    assert blocked, "dependent must be withheld while the clearance gate is open"
    assert released, "dependent must be claimable once the clearance is cleared"


# ---------------------------------------------------------------------------
# Bulletin post hook (Phase 2)
# ---------------------------------------------------------------------------


def test_bulletin_post_hook_materializes_on_blocker(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    board.set_post_hook(coord.materialize)
    _post_blocker(board)  # hook fires on post

    assert len(injector.created) == 1
    assert len(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)) == 1


def test_bulletin_default_has_no_hook(tmp_path: Path) -> None:
    # Regression (AC5): a board with no hook behaves exactly as before.
    board = BulletinBoard()
    stored = _post_blocker(board)
    assert board.count == 1
    assert stored.type == "blocker"


@pytest.mark.parametrize("resolution", ["cleared", "expired"])
def test_resolution_release_entry_references_projection(tmp_path: Path, resolution: str) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)

    blocker = _post_blocker(board)
    spec = coord.materialize(blocker)
    assert spec is not None
    projection = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0]

    release = coord.resolve(spec.clearance_task_id, resolver="operator:alex", resolution=resolution)
    assert release.details["blocker_entry_hash"] == projection.hmac
    assert release.details["resolution"] == resolution
    ok, errors = chain.verify()
    assert ok, errors
