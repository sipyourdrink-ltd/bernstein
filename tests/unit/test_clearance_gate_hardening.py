"""Hardening regressions for signal actions + clearance gates + bulletin (#2648).

Each test pins one integrity property that the first implementation did not
hold:

* materialize / resolve are atomic and durably idempotent (keyed on the chain,
  not on a process-local dict),
* gate creation and dependent-edge injection are a single atomic store step,
* the offline verifier refuses unauthenticated and unvalidated audit rows,
* the resolution vocabulary is refused at the store and audit-chain boundaries,
* ``post()`` never acknowledges a blocker whose action hook failed.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import stat
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.core.communication.bulletin import (
    BulletinBoard,
    BulletinMessage,
    SignalActionFailure,
)
from bernstein.core.communication.signal_actions import (
    ClearanceGateCoordinator,
    InMemoryClearanceInjector,
    verify_clearance_gates,
)
from bernstein.core.security.audit_chain import (
    EVENT_SIGNAL_GATE_PROJECTION,
    AuditChainStore,
    ClearanceResolutionRefusal,
    record_signal_gate_projection,
)

if TYPE_CHECKING:
    from bernstein.core.communication.signal_actions import ClearanceGateSpec

AUDIT_DIR = Path(".sdd/audit")


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _blocker(content: str = "shared dep broke", cell_id: str = "cell-a") -> BulletinMessage:
    return BulletinMessage(
        agent_id="worker-3", type="blocker", content=content, timestamp=1_700_000_000.0, cell_id=cell_id
    )


# ---------------------------------------------------------------------------
# CRITICAL 1: materialize / resolve are atomic and durably idempotent
# ---------------------------------------------------------------------------


class _SlowInjector(InMemoryClearanceInjector):
    """Injector that widens the check-then-act window between threads."""

    def __init__(self, *, open_by_cell: dict[str, list[str]]) -> None:
        super().__init__(open_by_cell=open_by_cell)
        self.barrier = threading.Barrier(2, timeout=10)
        self._tripped = False

    def create_clearance_task(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> None:
        if not self._tripped:
            self._tripped = True
            # Park inside the mutation so a second thread that skipped the
            # idempotency check would double-apply here.
            with __import__("contextlib").suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=1)
        super().create_clearance_task(spec, blocker)


def test_concurrent_materialize_does_not_double_apply(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = _SlowInjector(open_by_cell={"cell-a": ["task-x", "task-y"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    blocker = board.post(_blocker())

    errors: list[Exception] = []

    def run() -> None:
        try:
            coord.materialize(blocker)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert len(injector.created) == 1, "concurrent materialize double-created the clearance task"
    assert len(injector.edges) == 2, "concurrent materialize double-injected dependent edges"
    assert len(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)) == 1


def test_materialize_is_idempotent_across_a_restart(tmp_path: Path) -> None:
    """Idempotency is keyed on the chain, so a fresh process never re-injects."""
    board = BulletinBoard()
    chain = _chain(tmp_path)
    first = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    blocker = board.post(_blocker())
    spec = ClearanceGateCoordinator(bulletin=board, injector=first, chain=chain).materialize(blocker)
    assert spec is not None

    # A brand-new coordinator + injector (process restart) replaying the same
    # journal must recognise the already-sealed gate from the chain alone.
    second = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    replayed = ClearanceGateCoordinator(
        bulletin=board, injector=second, chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    ).materialize(blocker)

    assert replayed is not None
    assert replayed.clearance_task_id == spec.clearance_task_id
    assert second.created == [], "replay after restart re-created the clearance task"
    assert second.edges == [], "replay after restart re-injected dependent edges"
    assert len(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)) == 1


def test_resolve_is_a_noop_after_the_first_terminal_receipt(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None

    first = coord.resolve(spec.clearance_task_id, resolver="operator:alex")
    second = coord.resolve(spec.clearance_task_id, resolver="operator:mallory", resolution="expired")

    assert second.hmac == first.hmac, "a second resolve emitted a fresh terminal receipt"
    assert injector.released == [spec.clearance_task_id], "a second resolve re-released the gate"
    rows = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)
    terminal = [r for r in rows if r.details.get("resolution") != "pending"]
    assert len(terminal) == 1


def test_concurrent_resolve_emits_one_terminal_receipt(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None

    def run() -> None:
        coord.resolve(spec.clearance_task_id, resolver="operator:alex")

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    rows = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)
    terminal = [r for r in rows if r.details.get("resolution") != "pending"]
    assert len(terminal) == 1
    assert injector.released == [spec.clearance_task_id]


# ---------------------------------------------------------------------------
# CRITICAL 2: gate creation + edge injection are one atomic store step
# ---------------------------------------------------------------------------


def test_gate_creation_and_edge_injection_are_atomic(tmp_path: Path) -> None:
    from bernstein.core.server import TaskCreate
    from bernstein.core.tasks.task_store_core import TaskStore

    async def scenario() -> tuple[bool, bool]:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        dep = await store.create(TaskCreate(title="dependent", description="d", role="backend", cell_id="cell-a"))
        gate, edges = await store.create_gate_with_edges(
            clearance_task_id="clearance-abc123",
            title="clearance gate",
            role="clearance",
            cell_id="cell-a",
        )
        assert gate.id == "clearance-abc123"
        assert edges == [dep.id]
        # The dependent is gated the instant the gate exists: there is no
        # window in which the gate is open but the edge is missing.
        blocked = await store.claim_next("backend") is None
        return blocked, dep.id in store._tasks and "clearance-abc123" in store._tasks[dep.id].depends_on

    blocked, edged = asyncio.run(scenario())
    assert edged, "the dependent did not receive the depends_on edge"
    assert blocked, "the dependent was claimable while the gate was open"


def test_interrupted_gate_creation_leaves_no_orphan_edge(tmp_path: Path) -> None:
    """A failure mid-materialization rolls back in memory and on disk."""
    from bernstein.core.server import TaskCreate
    from bernstein.core.tasks.task_store_core import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"

    async def scenario() -> tuple[bool, bool, bool, bool]:
        store = TaskStore(jsonl)
        dep = await store.create(TaskCreate(title="dependent", description="d", role="backend", cell_id="cell-a"))

        async def flaky() -> None:
            raise OSError("disk full")

        store._flush_buffer_unlocked = flaky  # type: ignore[assignment,method-assign]
        with pytest.raises(OSError, match="disk full"):
            await store.create_gate_with_edges(
                clearance_task_id="clearance-abc123",
                title="clearance gate",
                role="clearance",
                cell_id="cell-a",
            )
        del store._flush_buffer_unlocked  # restore the bound method

        gate_absent = "clearance-abc123" not in store._tasks
        no_edge = store._tasks[dep.id].depends_on == []
        claimable = await store.claim_next("backend") is not None
        # Nothing on disk may carry the gate either: a replay of the crashed
        # materialization must not resurrect a half-applied gate, and the
        # tenant backlog mirror must not diverge from the primary journal.
        written = "".join(p.read_text() for p in tmp_path.rglob("*.jsonl") if p.is_file())
        journal_clean = "clearance-abc123" not in written
        return gate_absent, no_edge, claimable, journal_clean

    gate_absent, no_edge, claimable, journal_clean = asyncio.run(scenario())
    assert gate_absent, "a rolled-back gate task is still present in the store"
    assert no_edge, "a rolled-back gate left an orphan depends_on edge on the dependent"
    assert claimable, "the dependent stayed blocked by a gate that was never created"
    assert journal_clean, "a rolled-back gate was still written to a journal on disk"


# ---------------------------------------------------------------------------
# MAJOR: verify-gates must verify the HMAC chain before trusting rows
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from bernstein.core.security.audit import AUDIT_KEY_ENV

    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _materialize_on_cwd_chain() -> str:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=AuditChainStore(AUDIT_DIR))
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None
    return spec.clearance_task_id


def test_verify_gates_rejects_tampered_audit_rows(isolated_audit: Path) -> None:
    """The HMAC pre-check must be the reason a tampered chain is rejected.

    The tamper is deliberately one the semantic gate replay cannot see: the
    ``actor`` field is covered by the HMAC but by no gate invariant. A tamper
    the replay already catches (for example editing ``injected_edges``, which
    feeds ``graph_delta_hash``) would make this test pass with the production
    change reverted, and so would prove nothing.
    """
    from click.testing import CliRunner

    from bernstein.cli.commands.audit_cmd import audit_group
    from bernstein.core.communication.signal_actions import verify_clearance_gates
    from bernstein.core.security.audit import AuditLog

    _materialize_on_cwd_chain()

    log_path = next(iter(sorted(AUDIT_DIR.glob("*.jsonl"))))
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    for row in rows:
        if row.get("event_type") == EVENT_SIGNAL_GATE_PROJECTION:
            row["actor"] = "mallory"
            break
    else:  # pragma: no cover - defensive
        pytest.fail("no gate projection row found")
    log_path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    # Precondition: the semantic replay alone is blind to this tamper, so the
    # HMAC pre-check is the only thing that can reject it.
    assert verify_clearance_gates(AuditLog(AUDIT_DIR).query()).ok, (
        "tamper is visible to the semantic replay, so this test would not exercise the HMAC gate"
    )

    result = CliRunner().invoke(audit_group, ["verify-gates"])
    assert result.exit_code == 1, result.output
    assert "audit chain HMAC verification failed; gate replay not attempted" in result.output


# ---------------------------------------------------------------------------
# MAJOR: the verifier validates lineage before closing a gate
# ---------------------------------------------------------------------------


def _seal_gate(chain: AuditChainStore) -> tuple[str, str]:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None
    projection = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0]
    return spec.clearance_task_id, projection.hmac


def test_verifier_refuses_a_resolution_with_a_forged_blocker_entry_hash(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import record_task_claim_receipt

    chain = _chain(tmp_path)
    clearance_id, _real_hmac = _seal_gate(chain)
    pending = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0].details

    # A resolution that does not reference the materialization entry must not
    # close the gate, so a later claim of a scoped dependent is still a violation.
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash=str(pending["blocker_content_hash"]),
        clearance_task_id=clearance_id,
        injected_edges=[str(e) for e in pending["injected_edges"]],
        graph_delta_hash=str(pending["graph_delta_hash"]),
        scope_cell_id=str(pending["scope_cell_id"]),
        deadline=int(pending["deadline"] or 0),
        resolution="cleared",
        resolver="mallory",
        blocker_entry_hash="0" * 64,
    )
    record_task_claim_receipt(
        chain=chain,
        task_id="task-x",
        role="backend",
        claimed_by="sess-rogue",
        depends_on=[clearance_id],
        task_version=2,
        claim_path="by_id",
    )

    result = verify_clearance_gates(chain.query())
    assert not result.ok
    assert result.violations, "a forged resolution silently released the gate"


def test_verifier_refuses_a_resolution_whose_fields_diverge(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    clearance_id, real_hmac = _seal_gate(chain)
    pending = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0].details

    # Correct back-reference, but the recorded edge set was widened.
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash=str(pending["blocker_content_hash"]),
        clearance_task_id=clearance_id,
        injected_edges=["task-x", "task-smuggled"],
        graph_delta_hash=str(pending["graph_delta_hash"]),
        scope_cell_id=str(pending["scope_cell_id"]),
        deadline=int(pending["deadline"] or 0),
        resolution="cleared",
        resolver="mallory",
        blocker_entry_hash=real_hmac,
    )

    result = verify_clearance_gates(chain.query())
    assert not result.ok
    assert any("injected_edges" in err or "diverge" in err for err in result.errors), result.errors


def test_verifier_counts_orphan_resolution_rows_toward_gate_count(tmp_path: Path) -> None:
    """A chain of resolution rows alone must not report zero gates and pass."""
    chain = _chain(tmp_path)
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash="sha256:" + "0" * 64,
        clearance_task_id="clearance-forged",
        injected_edges=["task-x"],
        graph_delta_hash="0" * 64,
        scope_cell_id="cell-a",
        resolution="cleared",
        resolver="mallory",
    )

    result = verify_clearance_gates(chain.query())
    assert result.gate_count == 1, "an orphan resolution row was not counted as a gate"
    assert not result.ok


# ---------------------------------------------------------------------------
# MAJOR: resolution vocabulary is refused at both mutation boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "pending", "CLEARED", "released", "done"])
def test_store_refuses_a_resolution_outside_the_vocabulary(tmp_path: Path, bad: str) -> None:
    from bernstein.core.tasks.task_store_core import TaskStore

    async def scenario() -> None:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        gate, _edges = await store.create_gate_with_edges(
            clearance_task_id="clearance-abc123", title="gate", role="clearance", cell_id="cell-a"
        )
        with pytest.raises(ClearanceResolutionRefusal):
            await store.resolve_gate_task(gate.id, resolution=bad)
        # Refused before any state mutation: the gate is still open.
        assert store._tasks[gate.id].status.value == "open"

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", ["", "CLEARED", "released", "done", "resolved"])
def test_audit_chain_refuses_a_resolution_outside_the_vocabulary(tmp_path: Path, bad: str) -> None:
    chain = _chain(tmp_path)
    with pytest.raises(ClearanceResolutionRefusal):
        record_signal_gate_projection(
            chain=chain,
            blocker_content_hash="sha256:" + "0" * 64,
            clearance_task_id="clearance-abc123",
            injected_edges=[],
            graph_delta_hash="0" * 64,
            scope_cell_id="cell-a",
            resolution=bad,
        )
    # Refused before signing: nothing was appended to the chain.
    assert chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION) == []


def test_coordinator_resolution_refusal_is_typed(tmp_path: Path) -> None:
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=_chain(tmp_path))
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None
    with pytest.raises(ClearanceResolutionRefusal):
        coord.resolve(spec.clearance_task_id, resolver="op", resolution="released")
    assert injector.released == []


# ---------------------------------------------------------------------------
# MAJOR: post() never acknowledges a blocker whose hook failed
# ---------------------------------------------------------------------------


def test_post_refuses_to_acknowledge_a_failed_action_hook(tmp_path: Path) -> None:
    board = BulletinBoard()
    outbox = tmp_path / "signal_outbox.jsonl"

    def failing_hook(_msg: BulletinMessage) -> None:
        raise RuntimeError("materialization failed")

    board.set_post_hook(failing_hook, outbox_path=outbox)

    with pytest.raises(SignalActionFailure) as excinfo:
        board.post(_blocker())

    assert excinfo.value.message.type == "blocker"
    # The append-only board keeps the message, but the failure is durable and
    # replayable rather than silently dropped.
    assert board.count == 1
    assert board.pending_actions and board.pending_actions[0].content == "shared dep broke"
    assert outbox.exists()
    recorded = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()]
    assert recorded and recorded[0]["type"] == "blocker"


def test_pending_actions_drain_once_the_hook_recovers(tmp_path: Path) -> None:
    board = BulletinBoard()
    failures = {"n": 1}
    seen: list[BulletinMessage] = []

    def flaky_hook(msg: BulletinMessage) -> None:
        if failures["n"] > 0:
            failures["n"] -= 1
            raise RuntimeError("transient")
        seen.append(msg)

    board.set_post_hook(flaky_hook, outbox_path=tmp_path / "outbox.jsonl")
    with pytest.raises(SignalActionFailure):
        board.post(_blocker())

    drained = board.retry_pending_actions()
    assert drained == 1
    assert board.pending_actions == []
    assert len(seen) == 1


def test_observe_only_signals_are_unaffected_by_a_failing_hook(tmp_path: Path) -> None:
    """Regression: a non-blocker post still succeeds when its hook is clean."""
    board = BulletinBoard()
    board.set_post_hook(lambda _msg: None)
    stored = board.post(BulletinMessage(agent_id="w", type="status", content="up", timestamp=1.0, cell_id="cell-a"))
    assert stored.type == "status"
    assert board.pending_actions == []


# ---------------------------------------------------------------------------
# Follow-up hardening: the gate index must not trust unauthenticated rows,
# the saga must cover every sealing step, and the outbox must be readable.
# ---------------------------------------------------------------------------


def _audit_log_path(audit_dir: Path) -> Path:
    return next(iter(sorted(audit_dir.glob("*.jsonl"))))


def test_forged_pending_row_does_not_suppress_materialization(tmp_path: Path) -> None:
    """An unsigned `pending` row must never stand in for a real gate receipt.

    `AuditChainStore.query` performs no HMAC verification, so hydrating the
    idempotency index straight from it would let anyone with write access to
    the audit directory suppress gate materialization entirely.
    """
    from bernstein.core.communication.signal_actions import (
        ClearanceChainUnverified,
        clearance_task_id_for,
        journal_prefix_hash,
    )

    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x", "task-y"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    posted = board.post(_blocker())

    # Seed the chain so a log file exists, then forge a pending row for the
    # gate this blocker would materialize.
    chain.log(event_type="task.transition", actor="x", resource_type="task", resource_id="t1", details={})
    cid = clearance_task_id_for(blocker=posted, journal_prefix_hash=journal_prefix_hash([posted]))
    forged = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "event_type": EVENT_SIGNAL_GATE_PROJECTION,
        "actor": "mallory",
        "resource_type": "signal_gate_projection",
        "resource_id": cid,
        "details": {"clearance_task_id": cid, "resolution": "pending", "injected_edges": []},
        "prev_hmac": "0" * 64,
        "hmac": "deadbeef",
    }
    path = _audit_log_path(tmp_path / "audit")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")

    # The coordinator must refuse rather than silently treat the blocker as
    # already gated. Silently returning a spec with no injection is the failure.
    with pytest.raises(ClearanceChainUnverified):
        coord.materialize(posted)
    assert injector.created == [], "a forged row caused the gate to be treated as materialized"


def test_lineage_seal_failure_compensates_the_gate(tmp_path: Path) -> None:
    """A failure in any sealing step must compensate the graph mutation."""
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x", "task-y"]})

    def exploding_seal(_spec: object, _resolution: str) -> str:
        raise RuntimeError("lineage spine unavailable")

    coord = ClearanceGateCoordinator(
        bulletin=board, injector=injector, chain=_chain(tmp_path), lineage_seal=exploding_seal
    )
    with pytest.raises(RuntimeError, match="lineage spine unavailable"):
        coord.materialize(board.post(_blocker()))

    assert injector.released == ["clearance-" + injector.created[0].clearance_task_id[10:]], (
        "a lineage-seal failure left the gate injected with no receipt and no compensation"
    )


def test_resolve_seals_the_receipt_before_releasing_the_gate(tmp_path: Path) -> None:
    """A failed terminal append must not leave the gate released un-attested."""
    board = BulletinBoard()
    injector = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None

    def exploding_log(**_kwargs: object) -> None:
        raise OSError("chain unavailable")

    chain.log_with_prev_digest = exploding_log  # type: ignore[assignment,method-assign]
    with pytest.raises(OSError, match="chain unavailable"):
        coord.resolve(spec.clearance_task_id, resolver="operator:alex")

    assert injector.released == [], "the gate was released before its terminal receipt was sealed"


def test_project_gate_states_refuses_an_unanchored_resolution(tmp_path: Path) -> None:
    """The read-side projection must agree with the verifier about open gates."""
    from bernstein.core.communication.signal_actions import ClearanceStatus, project_gate_states

    chain = _chain(tmp_path)
    clearance_id, _hmac = _seal_gate(chain)
    pending = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0].details
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash=str(pending["blocker_content_hash"]),
        clearance_task_id=clearance_id,
        injected_edges=[str(e) for e in pending["injected_edges"]],
        graph_delta_hash=str(pending["graph_delta_hash"]),
        scope_cell_id=str(pending["scope_cell_id"]),
        deadline=int(pending["deadline"] or 0),
        resolution="cleared",
        resolver="mallory",
        blocker_entry_hash="0" * 64,
    )

    states = project_gate_states(chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION))
    assert states[clearance_id].status is ClearanceStatus.PENDING, (
        "the read-side projection closed a gate the verifier keeps open"
    )


def test_resolve_after_a_restart_seals_the_same_lineage_entry(tmp_path: Path) -> None:
    """The sealed lineage entry must not depend on whether a restart happened."""

    def seal(spec: ClearanceGateSpec, _resolution: str) -> str:
        return spec.journal_prefix_hash

    board = BulletinBoard()
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(
        bulletin=board,
        injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}),
        chain=chain,
        lineage_seal=seal,
    )
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None

    # Resolve through a fresh coordinator over the same chain (a restart).
    restarted = ClearanceGateCoordinator(
        bulletin=board,
        injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}),
        chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
        lineage_seal=seal,
    )
    event = restarted.resolve(spec.clearance_task_id, resolver="operator:alex")

    assert event.details["journal_entry_hash"] == spec.journal_prefix_hash, (
        "resolve after a restart sealed a different lineage entry than resolve in process"
    )


def test_outbox_replays_a_pending_action_after_a_restart(tmp_path: Path) -> None:
    """A durable outbox must be readable, or it preserves nothing."""
    outbox = tmp_path / "outbox.jsonl"

    def failing_hook(_msg: BulletinMessage) -> None:
        raise RuntimeError("materialization failed")

    board = BulletinBoard()
    board.set_post_hook(failing_hook, outbox_path=outbox)
    with pytest.raises(SignalActionFailure):
        board.post(_blocker())

    # Restart: a fresh board pointed at the same outbox must see the pending
    # action and be able to replay it.
    seen: list[BulletinMessage] = []
    restarted = BulletinBoard()
    restarted.set_post_hook(seen.append, outbox_path=outbox)
    assert len(restarted.pending_actions) == 1, "the outbox was never read back after a restart"
    assert restarted.retry_pending_actions() == 1
    assert len(seen) == 1
    assert seen[0].content == "shared dep broke"
    # A drained entry is compacted away, so a later loader cannot re-replay it.
    assert restarted.pending_actions == []
    assert outbox.read_text().strip() == "", "a drained outbox entry was never compacted"


def test_tenant_mirror_failure_does_not_fail_a_committed_gate(tmp_path: Path) -> None:
    """The secondary mirror must not report failure for a committed mutation."""
    from bernstein.core.server import TaskCreate
    from bernstein.core.tasks.task_store_core import TaskStore

    async def scenario() -> tuple[bool, bool]:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        dep = await store.create(TaskCreate(title="dependent", description="d", role="backend", cell_id="cell-a"))

        async def flaky(_record: object, _line: str) -> None:
            raise OSError("tenant backlog unavailable")

        store._append_tenant_backlog_record = flaky  # type: ignore[assignment,method-assign]
        gate, edges = await store.create_gate_with_edges(
            clearance_task_id="clearance-abc123", title="gate", role="clearance", cell_id="cell-a"
        )
        return gate.id in store._tasks, edges == [dep.id]

    committed, edged = asyncio.run(scenario())
    assert committed, "a committed gate was reported as failed because its mirror failed"
    assert edged


# ---------------------------------------------------------------------------
# Delta review: one anchor for writer and readers, read what you authenticate,
# honest edge attestation, and every post() call site guarded.
# ---------------------------------------------------------------------------


def _archive_all_segments(audit_dir: Path) -> None:
    """Move every live segment into ``archive/`` as retention would."""
    archive = audit_dir / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for segment in sorted(audit_dir.glob("*.jsonl")):
        with gzip.open(archive / (segment.name + ".gz"), "wb") as handle:
            handle.write(segment.read_bytes())
        segment.unlink()


def test_writer_and_verifier_agree_on_the_gate_anchor(tmp_path: Path) -> None:
    """The writer's back-reference must be one the verifier accepts.

    Guard against the two halves drifting apart again: the coordinator's chosen
    anchor and the verifier's anchor are compared directly, so a future edit to
    either side that changes "which row anchors this gate" fails here.
    """
    from bernstein.core.communication.signal_actions import (
        build_gate_anchors,
        journal_prefix_hash,
        project_clearance_gate,
    )

    board = BulletinBoard()
    posted = board.post(_blocker())
    spec = project_clearance_gate(
        blocker=posted, scope_task_ids=["task-x"], journal_prefix_hash=journal_prefix_hash([posted])
    )
    # Two pending rows for one gate id, so "first" and "last" are different
    # rows and an anchor disagreement is observable rather than degenerate.
    for _ in range(2):
        record_signal_gate_projection(
            chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
            blocker_content_hash=spec.blocker_content_hash,
            clearance_task_id=spec.clearance_task_id,
            injected_edges=list(spec.injected_edges),
            graph_delta_hash=spec.graph_delta_hash,
            scope_cell_id=spec.scope_cell_id,
            deadline=spec.deadline,
            resolution="pending",
        )

    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    rows = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION, include_archived=True)
    assert len({r.hmac for r in rows}) == 2, "fixture must produce two distinct pending rows"

    # Drive the hydration path the coordinator uses on restart, which is where
    # the writer picks its anchor.
    coord = ClearanceGateCoordinator(
        bulletin=board, injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}), chain=chain
    )
    coord._load_chain_state()
    writer_hmac = coord._entry_hmac[spec.clearance_task_id]

    reader_anchor = build_gate_anchors(rows)[spec.clearance_task_id]
    assert writer_hmac == reader_anchor.entry_hmac, "writer and reader disagree on the gate anchor"
    assert writer_hmac in reader_anchor.entry_hmacs


def test_legacy_duplicate_pending_rows_can_still_be_closed(tmp_path: Path) -> None:
    """Chains written before durable idempotency must remain closable.

    Pre-fix idempotency was process-local, so a restart re-materialized the same
    blocker onto the same chain, leaving several `pending` rows for one gate id.
    Those chains are authentic, so nothing else rejects them; if the writer and
    verifier disagree about which row anchors the gate, the gate can never be
    closed by any operator action.
    """
    from bernstein.core.communication.signal_actions import journal_prefix_hash, project_clearance_gate

    board = BulletinBoard()
    posted = board.post(_blocker())
    spec = project_clearance_gate(
        blocker=posted, scope_task_ids=["task-x"], journal_prefix_hash=journal_prefix_hash([posted])
    )
    for _ in range(2):  # two restarts, two pending rows, one gate id
        record_signal_gate_projection(
            chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
            blocker_content_hash=spec.blocker_content_hash,
            clearance_task_id=spec.clearance_task_id,
            injected_edges=list(spec.injected_edges),
            graph_delta_hash=spec.graph_delta_hash,
            scope_cell_id=spec.scope_cell_id,
            deadline=spec.deadline,
            resolution="pending",
        )

    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    assert chain.verify()[0], "the legacy chain is authentic"
    coord = ClearanceGateCoordinator(
        bulletin=board, injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}), chain=chain
    )
    coord.resolve(spec.clearance_task_id, resolver="operator:alex")

    result = verify_clearance_gates(chain.query(include_archived=True))
    assert result.ok, result.errors


def test_gate_index_reads_every_segment_it_authenticates(tmp_path: Path) -> None:
    """The verified set and the read set must be the same set.

    Asserted as an identity over segments rather than as behaviour on a
    live-only fixture: `verify()` walks archived plus live, so the read used to
    build the gate index must walk archived plus live too.
    """
    from bernstein.core.security.audit import AuditLog

    chain = _chain(tmp_path)
    board = BulletinBoard()
    ClearanceGateCoordinator(
        bulletin=board, injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}), chain=chain
    ).materialize(board.post(_blocker()))

    log = AuditLog(tmp_path / "audit", key=b"k" * 32)
    before = len(log.query(include_archived=True))
    _archive_all_segments(tmp_path / "audit")

    assert log.verify()[0], "archived chain still verifies"
    assert len(log.query()) == 0, "fixture did not actually archive the live segments"
    assert len(log.query(include_archived=True)) == before, "the read set shrank while the verified set did not"


def test_archiving_does_not_re_enable_double_injection(tmp_path: Path) -> None:
    """Routine retention archiving must not silently turn the gate index off."""
    board = BulletinBoard()
    first = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    posted = board.post(_blocker())
    spec = ClearanceGateCoordinator(bulletin=board, injector=first, chain=_chain(tmp_path)).materialize(posted)
    assert spec is not None

    _archive_all_segments(tmp_path / "audit")

    # A fresh coordinator over the archived chain must still recognise the gate.
    replay = InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]})
    coord = ClearanceGateCoordinator(
        bulletin=board, injector=replay, chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    )
    assert coord.materialize(posted) is not None
    assert replay.created == [], "archiving re-enabled double-injection of the clearance task"
    assert replay.edges == [], "archiving re-enabled double-injection of the dependent edges"
    # ... and the gate is still resolvable rather than a KeyError.
    coord.resolve(spec.clearance_task_id, resolver="operator:alex")


def test_receipt_attests_only_the_edges_the_injector_actually_created(tmp_path: Path) -> None:
    """A narrower applied edge set must be what the signed receipt records."""

    class _NarrowingInjector(InMemoryClearanceInjector):
        """Injector whose locked mutation gates fewer dependents than projected."""

        def apply_gate(self, spec: ClearanceGateSpec, blocker: BulletinMessage) -> list[str]:
            # 'task-y' was claimed between the unlocked scope read and this
            # locked mutation, so only 'task-x' actually receives an edge.
            applied = ["task-x"]
            self.created.append(spec)
            for dependent_id in applied:
                self.edges.append((dependent_id, spec.clearance_task_id))
            return applied

    board = BulletinBoard()
    injector = _NarrowingInjector(open_by_cell={"cell-a": ["task-x", "task-y"]})
    chain = _chain(tmp_path)
    coord = ClearanceGateCoordinator(bulletin=board, injector=injector, chain=chain)
    spec = coord.materialize(board.post(_blocker()))
    assert spec is not None

    recorded = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION)[0].details
    assert list(recorded["injected_edges"]) == ["task-x"], (
        "the signed receipt attests an edge the injector never created"
    )
    assert spec.injected_edges == ("task-x",)
    # The delta hash must recompute from the edges actually applied.
    result = verify_clearance_gates(chain.query())
    assert result.ok, result.errors


def test_post_bulletin_route_reports_a_pending_action_instead_of_500(tmp_path: Path) -> None:
    """The documented hook wiring must not turn POST /bulletin into a 500."""
    from fastapi.testclient import TestClient

    from bernstein.core.server import create_app

    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    with TestClient(app) as client:
        ok = client.post("/bulletin", json={"agent_id": "w", "type": "blocker", "content": "x"})
        assert ok.status_code == 201, ok.text

        board = app.state.bulletin
        board.set_post_hook(lambda _m: (_ for _ in ()).throw(RuntimeError("gate failed")))
        pending = client.post("/bulletin", json={"agent_id": "w", "type": "blocker", "content": "y"})

    assert pending.status_code == 202, f"expected 202 (stored, action pending), got {pending.status_code}"
    assert pending.json()["content"] == "y"
    assert len(board.pending_actions) == 1


# ---------------------------------------------------------------------------
# Round 3: bounded authenticated scan, a discriminating anchor guard, the CLI
# archive read path, multi-hmac acceptance, and tenant containment.
# ---------------------------------------------------------------------------


def test_authenticated_chain_scan_is_incremental(tmp_path: Path) -> None:
    """A warm scan must not re-read history it already authenticated.

    Asserted as bounded work (rows returned) rather than wall-clock, so it
    cannot flake, but it is the property that keeps gate materialization off
    the O(entire chain) path.
    """
    from bernstein.core.security.audit import AuditLog

    log = AuditLog(tmp_path / "audit", key=b"k" * 32)
    for i in range(200):
        log.log(event_type="task.transition", actor="x", resource_type="task", resource_id=f"t{i}", details={})

    first = log.scan_verified()
    assert first.ok, first.errors
    assert len(first.events) == 200
    assert first.rescanned is True

    log.log(event_type="task.transition", actor="x", resource_type="task", resource_id="new", details={})
    second = log.scan_verified(first.cursor)
    assert second.ok, second.errors
    assert len(second.events) == 1, "the scan re-read history it had already authenticated"
    assert second.rescanned is False

    third = log.scan_verified(second.cursor)
    assert third.events == [], "an unchanged chain still produced work"


def test_incremental_scan_still_catches_tampering(tmp_path: Path) -> None:
    """Neither the cursor nor the signed index may mask a tampered row."""
    from bernstein.core.security.audit import AuditLog

    audit = tmp_path / "audit"
    log = AuditLog(audit, key=b"k" * 32)
    for i in range(20):
        log.log(event_type="task.transition", actor="x", resource_type="task", resource_id=f"t{i}", details={})
    warm = log.scan_verified(event_type="task.transition")
    assert warm.ok

    # Rewrite an already-consumed row in place, preserving the byte length so a
    # size check alone would not notice.
    segment = next(iter(sorted(audit.glob("*.jsonl"))))
    rows = [json.loads(line) for line in segment.read_text().splitlines() if line.strip()]
    rows[5]["actor"] = "y"
    segment.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    cold = AuditLog(audit, key=b"k" * 32).scan_verified(event_type="task.transition")
    assert not cold.ok, "a tampered row survived the indexed scan"


def test_a_forged_segment_index_is_ignored(tmp_path: Path) -> None:
    """An attacker with write access to the audit dir cannot forge the index."""
    from bernstein.core.security.audit import AuditLog

    audit = tmp_path / "audit"
    log = AuditLog(audit, key=b"k" * 32)
    for i in range(10):
        log.log(event_type="task.transition", actor="x", resource_type="task", resource_id=f"t{i}", details={})
    assert log.scan_verified(event_type="task.transition").ok

    index_path = audit / ".segment-index.json"
    assert index_path.is_file(), "the segment index was never written"

    # Forge an entry that claims the whole segment is already covered and
    # contained no rows. The prefix digest is computed over public bytes, so an
    # attacker can satisfy it; only the HMAC signature stops this.
    segment = next(iter(sorted(audit.glob("*.jsonl"))))
    raw = segment.read_bytes()
    doc = json.loads(index_path.read_text())
    stem = segment.name[: -len(".jsonl")]
    doc["payload"]["segments"] = {
        stem: {
            "byte_len": len(raw),
            "prefix_sha256": hashlib.sha256(raw).hexdigest(),
            "start_hmac": "0" * 64,
            "end_hmac": "0" * 64,
            "rows": [],
        }
    }
    index_path.write_text(json.dumps(doc))  # signature is now stale

    fresh = AuditLog(audit, key=b"k" * 32).scan_verified(event_type="task.transition")
    assert len(fresh.events) == 10, "a forged index suppressed rows instead of being ignored"
    assert fresh.ok


def test_resolution_closes_a_gate_whose_pending_rows_differ(tmp_path: Path) -> None:
    """Writer and verifier must pick the same anchor when the rows disagree.

    Two authentic pending rows with *different* edge sets (a dependent was
    claimed between restarts). If the writer anchors on one row and the verifier
    on the other, the recorded fields diverge and the gate can never be closed.
    Unlike an agreement assertion over one shared helper, this discriminates:
    it fails for any pair of anchor rules that are not the same rule.
    """
    from bernstein.core.communication.signal_actions import journal_prefix_hash, project_clearance_gate

    board = BulletinBoard()
    posted = board.post(_blocker())
    jph = journal_prefix_hash([posted])
    wide = project_clearance_gate(blocker=posted, scope_task_ids=["task-x", "task-y"], journal_prefix_hash=jph)
    narrow = project_clearance_gate(blocker=posted, scope_task_ids=["task-x"], journal_prefix_hash=jph)
    assert wide.clearance_task_id == narrow.clearance_task_id
    assert wide.graph_delta_hash != narrow.graph_delta_hash

    for spec in (wide, narrow):
        record_signal_gate_projection(
            chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
            blocker_content_hash=spec.blocker_content_hash,
            clearance_task_id=spec.clearance_task_id,
            injected_edges=list(spec.injected_edges),
            graph_delta_hash=spec.graph_delta_hash,
            scope_cell_id=spec.scope_cell_id,
            deadline=spec.deadline,
            resolution="pending",
        )

    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    assert chain.verify()[0]
    coord = ClearanceGateCoordinator(
        bulletin=board, injector=InMemoryClearanceInjector(open_by_cell={"cell-a": ["task-x"]}), chain=chain
    )
    coord.resolve(wide.clearance_task_id, resolver="operator:alex")

    result = verify_clearance_gates(chain.query(include_archived=True))
    assert result.ok, result.errors


def test_verifier_accepts_a_resolution_anchored_on_an_earlier_pending_row(tmp_path: Path) -> None:
    """Every recorded pending HMAC is a valid back-reference, not just the last.

    This is the whole reason GateAnchor accumulates entry_hmacs: a resolution
    written against an earlier materialization must still close the gate.
    """
    from bernstein.core.communication.signal_actions import journal_prefix_hash, project_clearance_gate

    board = BulletinBoard()
    posted = board.post(_blocker())
    spec = project_clearance_gate(
        blocker=posted, scope_task_ids=["task-x"], journal_prefix_hash=journal_prefix_hash([posted])
    )
    for _ in range(2):
        record_signal_gate_projection(
            chain=AuditChainStore(tmp_path / "audit", key=b"k" * 32),
            blocker_content_hash=spec.blocker_content_hash,
            clearance_task_id=spec.clearance_task_id,
            injected_edges=list(spec.injected_edges),
            graph_delta_hash=spec.graph_delta_hash,
            scope_cell_id=spec.scope_cell_id,
            deadline=spec.deadline,
            resolution="pending",
        )

    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    pending_rows = chain.query(event_type=EVENT_SIGNAL_GATE_PROJECTION, include_archived=True)
    earliest_hmac = pending_rows[0].hmac
    latest_hmac = pending_rows[-1].hmac
    assert earliest_hmac != latest_hmac

    # Resolve against the EARLIER row, which no current writer would choose.
    record_signal_gate_projection(
        chain=chain,
        blocker_content_hash=spec.blocker_content_hash,
        clearance_task_id=spec.clearance_task_id,
        injected_edges=list(spec.injected_edges),
        graph_delta_hash=spec.graph_delta_hash,
        scope_cell_id=spec.scope_cell_id,
        deadline=spec.deadline,
        resolution="cleared",
        resolver="operator:alex",
        blocker_entry_hash=earliest_hmac,
    )

    result = verify_clearance_gates(chain.query(include_archived=True))
    assert result.ok, result.errors


def test_verify_gates_cli_reports_violations_after_archiving(isolated_audit: Path) -> None:
    """The CLI read path must survive retention archiving.

    Exercises the CLI over an archived chain, which is the difference between a
    silent PASS and a reported violation; the coordinator-side test covers the
    write path only.
    """
    from click.testing import CliRunner

    from bernstein.cli.commands.audit_cmd import audit_group
    from bernstein.core.security.audit_chain import record_task_claim_receipt

    clearance_id = _materialize_on_cwd_chain()
    record_task_claim_receipt(
        chain=AuditChainStore(AUDIT_DIR),
        task_id="task-x",
        role="backend",
        claimed_by="sess-rogue",
        depends_on=[clearance_id],
        task_version=2,
        claim_path="by_id",
    )

    before = CliRunner().invoke(audit_group, ["verify-gates"])
    assert before.exit_code == 1, before.output

    _archive_all_segments(AUDIT_DIR)

    after = CliRunner().invoke(audit_group, ["verify-gates"])
    assert after.exit_code == 1, f"archiving hid the violation from the CLI:\n{after.output}"
    assert "task-x" in after.output


def test_gate_creation_never_injects_a_cross_tenant_edge(tmp_path: Path) -> None:
    """A gate must not gate another tenant's work in the same cell."""
    from bernstein.core.server import TaskCreate
    from bernstein.core.tasks.task_store_core import TaskStore

    async def scenario() -> tuple[list[str], str, str]:
        store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
        mine = await store.create(
            TaskCreate(title="A", description="d", role="backend", cell_id="cell-a", tenant_id="tenant-a")
        )
        theirs = await store.create(
            TaskCreate(title="B", description="d", role="backend", cell_id="cell-a", tenant_id="tenant-b")
        )
        _gate, edges = await store.create_gate_with_edges(
            clearance_task_id="clearance-t",
            title="gate",
            role="clearance",
            cell_id="cell-a",
            tenant_id="tenant-a",
        )
        return edges, mine.id, theirs.id

    edges, mine, theirs = asyncio.run(scenario())
    assert mine in edges, "the gate did not gate its own tenant's open task"
    assert theirs not in edges, "the gate injected a cross-tenant depends_on edge"
