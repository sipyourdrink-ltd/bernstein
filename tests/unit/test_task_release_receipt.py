"""Release-receipt regression tests (#3037).

Claiming a task mints a ``task.claim_receipt`` on the audit chain. These tests
pin the other half: every transition that surrenders a held claim mints the
matching ``task.release_receipt``, so folding the chain reports the last
claimant of a task instead of every node that ever acquired it.

Two things are deliberately not surrenders, and both are pinned here rather
than left to the reader:

* delivery. ``DONE`` and ``CLOSED`` mint nothing, because the worker finished
  the job it claimed. The claim ends only if the task goes back to the pool,
  which ``reopen`` does and does mint;
* a claim that never existed. A task can reach ``WAITING_FOR_SUBTASKS`` or
  ``BLOCKED`` without ever having been claimed, and minting a surrender there
  writes a record a verifier cannot tell apart from a real one.

The tests are organised as:

* an enumeration over every surrender path in :class:`TaskStore`, so a new
  path that forgets the receipt is caught by name rather than by luck;
* an offline reconstruction of claim -> release -> re-claim asserted from the
  chain alone;
* the same reconstruction on a plain ``bernstein serve`` node, with the
  orchestrator's ``BERNSTEIN_AUDIT`` lifecycle wiring absent;
* a release guard that walks every ``transition_task`` call site in the store
  and fails on any claim-ending one that is not paired with a receipt, then
  proves the pairing actually appends.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
from fastapi.testclient import TestClient

from bernstein.core.security.audit_chain import (
    EVENT_TASK_CLAIM_RECEIPT,
    EVENT_TASK_RELEASE_RECEIPT,
    AuditChainStore,
    reconstruct_claim_holders,
)
from bernstein.core.tasks.contracts import ContractViolation, RefusalKind, WorkerRefusal
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store_core import (
    CLAIM_DELIVERED_STATUSES,
    CLAIM_HELD_STATUSES,
    ClaimSnapshot,
    TaskStore,
)

if TYPE_CHECKING:
    from bernstein.core.security.audit import AuditEvent

_KEY = b"release-receipt-test-key"
_HOLDER = "node-a"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> tuple[TaskStore, AuditChainStore]:
    """Build a TaskStore with an audit chain attached, as ``create_app`` does."""
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    store = TaskStore(runtime / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    store.attach_audit_chain(chain)
    return store, chain


def _held(store: TaskStore, task_id: str = "T-1", **overrides: Any) -> Task:
    """Insert a task that a worker currently holds a claim on."""
    base: dict[str, Any] = {
        "id": task_id,
        "title": "t",
        "description": "d",
        "role": "backend",
        "status": TaskStatus.IN_PROGRESS,
        "claimed_at": time.time(),
        "claimed_by_session": _HOLDER,
    }
    base.update(overrides)
    task = Task(**base)
    store._tasks[task.id] = task
    store._index_add(task)
    return task


def _releases(chain: AuditChainStore) -> list[AuditEvent]:
    return chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)


# ---------------------------------------------------------------------------
# One test per un-claim path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEveryUnclaimPathMintsAReceipt:
    """Enumerates the un-claim paths instead of spot-checking one of them."""

    async def test_force_claim(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.force_claim("T-1")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "force_claim"
        assert events[0].details["released_by"] == _HOLDER
        assert events[0].details["from_status"] == "in_progress"
        assert events[0].details["to_status"] == "open"

    async def test_reopen(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, status=TaskStatus.DONE)
        await store.reopen("T-1", "janitor verification failed")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "reopen"
        assert events[0].details["released_by"] == _HOLDER
        assert events[0].details["to_status"] == "open"

    async def test_release(self, tmp_path: Path) -> None:
        """A worker handing an unstartable task back to the pool (#3018)."""
        store, chain = _store(tmp_path)
        _held(store)
        await store.release("T-1", "workspace is not a usable checkout")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "release"
        assert events[0].details["released_by"] == _HOLDER
        assert events[0].details["from_status"] == "in_progress"
        assert events[0].details["to_status"] == "open"

    async def test_release_of_a_never_claimed_task_mints_nothing(self, tmp_path: Path) -> None:
        """release() requires CLAIMED/IN_PROGRESS, but claim evidence still decides."""
        store, chain = _store(tmp_path)
        _held(store, status=TaskStatus.CLAIMED, claimed_at=None, claimed_by_session="")
        await store.release("T-1", "spawn failed")
        assert _releases(chain) == []

    async def test_cancel(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.cancel("T-1", "operator cancelled")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "cancel"
        assert events[0].details["to_status"] == "cancelled"

    async def test_cancel_cascade(self, tmp_path: Path) -> None:
        # The /tasks/{id}/cancel route cascades, so the cascade body needs its
        # own receipt: a child held by another node is un-claimed here too.
        store, chain = _store(tmp_path)
        _held(store, "T-1")
        _held(store, "T-2", parent_task_id="T-1", claimed_by_session="node-b")
        await store.cancel_cascade("T-1", "operator cancelled the tree")
        released = {e.details["task_id"]: e.details["released_by"] for e in _releases(chain)}
        assert released == {"T-1": _HOLDER, "T-2": "node-b"}

    async def test_fail(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.fail("T-1", "tests red")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "fail"
        assert events[0].details["to_status"] == "failed"

    async def test_fail_contract_violation(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.fail_contract_violation("T-1", ContractViolation(path="$.status", message="missing"))
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "fail_contract_violation"

    async def test_refuse(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.refuse(
            "T-1",
            WorkerRefusal(kind=RefusalKind.SCOPE_EXCEEDED, detail="needs a spec change"),
        )
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "refuse"
        assert events[0].details["to_status"] == "refused"

    async def test_abandon(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.abandon("T-1", "out_of_scope", "spec mismatch")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "abandon"
        assert events[0].details["to_status"] == "abandoned"

    async def test_abandon_cascade_releases_a_downstream_held_by_another_node(self, tmp_path: Path) -> None:
        # The cascade ends claims the abandoning node never granted: a
        # downstream task can be IN_PROGRESS under a different node, and
        # BLOCKED_BY_ABANDON is a legal source for OPEN, so without the
        # receipt that task is re-claimed with no surrender in between.
        store, chain = _store(tmp_path)
        _held(store, "T-up")
        _held(store, "T-down", depends_on=["T-up"], claimed_by_session="node-b")
        await store.abandon("T-up", "out_of_scope", "spec mismatch")
        assert store._tasks["T-down"].status is TaskStatus.BLOCKED_BY_ABANDON
        released = {e.details["task_id"]: e.details for e in _releases(chain)}
        assert set(released) == {"T-up", "T-down"}
        assert released["T-up"]["release_path"] == "abandon"
        assert released["T-down"]["release_path"] == "abandon_cascade"
        assert released["T-down"]["released_by"] == "node-b"
        assert released["T-down"]["from_status"] == "in_progress"
        assert released["T-down"]["to_status"] == "blocked_by_abandon"

    async def test_abandon_cascade_skips_a_downstream_no_one_claimed(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, "T-up")
        _held(store, "T-down", depends_on=["T-up"], status=TaskStatus.OPEN, claimed_at=None, claimed_by_session=None)
        await store.abandon("T-up", "out_of_scope", "spec mismatch")
        assert store._tasks["T-down"].status is TaskStatus.BLOCKED_BY_ABANDON
        assert {e.details["task_id"] for e in _releases(chain)} == {"T-up"}

    async def test_fail_empty_completion(self, tmp_path: Path) -> None:
        # complete() auto-fails a held task when the summary is empty. Its own
        # docstring calls that releasing the slot, so the ledger owes a receipt.
        from bernstein.core.tasks.task_store_core import EmptyCompletionError

        store, chain = _store(tmp_path)
        _held(store)
        with pytest.raises(EmptyCompletionError):
            await store.complete("T-1", "")
        assert store._tasks["T-1"].status is TaskStatus.FAILED
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "fail_empty_completion"
        assert events[0].details["released_by"] == _HOLDER
        assert events[0].details["to_status"] == "failed"

    async def test_restart_recovery(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, "T-1", status=TaskStatus.CLAIMED)
        _held(store, "T-2", status=TaskStatus.IN_PROGRESS)
        assert store.recover_stale_claimed_tasks() == 2
        events = _releases(chain)
        assert {e.details["task_id"] for e in events} == {"T-1", "T-2"}
        assert {e.details["release_path"] for e in events} == {"restart_recovery"}

    async def test_node_departure(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, "T-1")
        _held(store, "T-2", claimed_by_session="node-b")
        assert store.reopen_tasks_for_node(_HOLDER) == 1
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["task_id"] == "T-1"
        assert events[0].details["release_path"] == "node_departure"
        assert events[0].details["released_by"] == _HOLDER


@pytest.mark.asyncio
class TestANeverHeldClaimSurrendersNothing:
    """A fabricated surrender is the worst failure mode this event has.

    A missing receipt is visible as an asymmetry; a receipt for a claim that
    never existed is indistinguishable, to any offline verifier, from a real
    surrender by a real worker. Deciding ``held`` on status membership mints
    exactly that, because ``OPEN -> WAITING_FOR_SUBTASKS`` and the paths into
    ``BLOCKED`` are legal with no claim anywhere. Each status a task can sit
    in unclaimed is enumerated here rather than spot-checked on ``OPEN``.
    """

    async def test_open(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, status=TaskStatus.OPEN, claimed_at=None, claimed_by_session=None)
        await store.cancel("T-1", "never started")
        assert _releases(chain) == []

    async def test_waiting_for_subtasks(self, tmp_path: Path) -> None:
        # OPEN -> WAITING_FOR_SUBTASKS needs no claim: a planner can split a
        # task nobody ever picked up.
        store, chain = _store(tmp_path)
        _held(store, status=TaskStatus.OPEN, claimed_at=None, claimed_by_session=None)
        await store.wait_for_subtasks("T-1", 3)
        assert store._tasks["T-1"].status is TaskStatus.WAITING_FOR_SUBTASKS
        await store.cancel("T-1", "operator changed their mind")
        assert _releases(chain) == []

    async def test_blocked(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store, status=TaskStatus.OPEN, claimed_at=None, claimed_by_session=None)
        await store.wait_for_subtasks("T-1", 3)
        await store.block("T-1", "needs a human")
        assert store._tasks["T-1"].status is TaskStatus.BLOCKED
        await store.cancel("T-1", "operator changed their mind")
        assert _releases(chain) == []

    async def test_a_held_status_alone_does_not_make_a_surrender(self, tmp_path: Path) -> None:
        # The regression in one line: every status in CLAIM_HELD_STATUSES that
        # a task can be cancelled from, with no claim evidence on the task.
        store, chain = _store(tmp_path)
        cancellable_held = [
            status
            for status in sorted(CLAIM_HELD_STATUSES, key=lambda s: s.value)
            if status
            in {TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_SUBTASKS, TaskStatus.BLOCKED}
        ]
        assert cancellable_held, "the guard set must not be empty or this test asserts nothing"
        for index, status in enumerate(cancellable_held):
            task_id = f"T-nc-{index}"
            _held(store, task_id, status=status, claimed_at=None, claimed_by_session=None)
            await store.cancel(task_id, "never started")
        assert _releases(chain) == []


@pytest.mark.asyncio
class TestDeliveryIsNotASurrender:
    """The narrowed contract, pinned so it cannot drift into the docs alone.

    A worker that delivers has not surrendered anything, so ``DONE`` and
    ``CLOSED`` mint nothing. The claim ends when the task goes back to the
    pool, and ``reopen`` is the path that does that.
    """

    async def test_complete_and_close_mint_nothing(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        await store.complete("T-1", "shipped")
        assert _releases(chain) == []
        await store.close("T-1")
        assert _releases(chain) == []

    async def test_reopening_a_delivered_task_is_the_surrender(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        _held(store)
        _seed_claim(chain, "T-1", _HOLDER)
        await store.complete("T-1", "shipped")
        assert reconstruct_claim_holders(chain.query()) == {"T-1": _HOLDER}
        await store.reopen("T-1", "janitor verification failed")
        events = _releases(chain)
        assert len(events) == 1
        assert events[0].details["release_path"] == "reopen"
        assert events[0].details["from_status"] == "done"
        assert reconstruct_claim_holders(chain.query()) == {}


@pytest.mark.asyncio
class TestReceiptShape:
    async def test_receipt_carries_the_post_transition_version_and_reason(self, tmp_path: Path) -> None:
        store, chain = _store(tmp_path)
        task = _held(store)
        before = task.version
        await store.fail("T-1", "tests red")
        details = _releases(chain)[0].details
        assert details["task_version"] == before + 1
        assert details["reason"] == "tests red"
        assert details["role"] == "backend"
        # Chain-anchored exactly like the claim receipt it answers.
        assert details["prev_chain_digest"]
        ok, errors = chain.verify()
        assert ok, errors

    async def test_a_chain_append_failure_never_blocks_the_transition(self, tmp_path: Path) -> None:
        store, _chain = _store(tmp_path)

        class _Broken:
            def log_with_prev_digest(self, **_: Any) -> None:
                raise OSError("chain volume is read-only")

        store.attach_audit_chain(_Broken())  # type: ignore[arg-type]
        _held(store)
        task = await store.force_claim("T-1")
        assert task.status is TaskStatus.OPEN


# ---------------------------------------------------------------------------
# Offline reconstruction from the chain alone
# ---------------------------------------------------------------------------


def _seed_claim(chain: AuditChainStore, task_id: str, holder: str) -> None:
    from bernstein.core.security.audit_chain import record_task_claim_receipt

    record_task_claim_receipt(
        chain=chain,
        task_id=task_id,
        role="backend",
        claimed_by=holder,
        depends_on=[],
        task_version=1,
        claim_path="by_id",
    )


@pytest.mark.asyncio
async def test_replay_reconstructs_the_holder_at_each_point(tmp_path: Path) -> None:
    """claim -> release -> re-claim, read back offline from the chain."""
    store, chain = _store(tmp_path)
    _held(store, status=TaskStatus.CLAIMED)
    _seed_claim(chain, "T-1", _HOLDER)
    assert reconstruct_claim_holders(chain.query()) == {"T-1": _HOLDER}

    await store.force_claim("T-1")
    assert reconstruct_claim_holders(chain.query()) == {}

    _held(store, status=TaskStatus.CLAIMED, claimed_by_session="node-b")
    _seed_claim(chain, "T-1", "node-b")
    assert reconstruct_claim_holders(chain.query()) == {"T-1": "node-b"}

    # Every prefix answers the question, not just the head: a verifier holding
    # a copy of the chain replays ownership as of any point.
    events = chain.query()
    holders_by_prefix = [reconstruct_claim_holders(events[:n]) for n in range(len(events) + 1)]
    assert holders_by_prefix[-1] == {"T-1": "node-b"}
    assert {} in holders_by_prefix

    # A second store over the same on-disk chain reaches the same answer.
    reloaded = AuditChainStore(tmp_path / "audit", key=_KEY)
    assert reconstruct_claim_holders(reloaded.query()) == {"T-1": "node-b"}


def test_an_acquisition_only_chain_misreports_the_holder(tmp_path: Path) -> None:
    """The failure this issue is about, pinned as the contrast case."""
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    _seed_claim(chain, "T-1", "node-a")
    _seed_claim(chain, "T-1", "node-b")
    # Claims alone cannot distinguish "node-a handed it over" from "both hold
    # it": only the release receipt in between makes the sequence legible.
    claims = [e.details["claimed_by"] for e in chain.query(event_type=EVENT_TASK_CLAIM_RECEIPT)]
    assert claims == ["node-a", "node-b"]
    assert reconstruct_claim_holders(chain.query()) == {"T-1": "node-b"}


# ---------------------------------------------------------------------------
# Plain ``bernstein serve`` node -- no BERNSTEIN_AUDIT wiring
# ---------------------------------------------------------------------------


@pytest.fixture()
def plain_serve_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A server built the way ``bernstein serve`` builds one.

    ``BERNSTEIN_AUDIT`` is what wires the orchestrator's lifecycle audit log,
    which is where the generic ``task.transition`` event comes from. It is
    cleared here, and the lifecycle module's global is reset, so the only
    events this app can produce are the ones the server itself writes.
    """
    from bernstein.core.server import create_app
    from bernstein.core.tasks import lifecycle

    monkeypatch.delenv("BERNSTEIN_AUDIT", raising=False)
    monkeypatch.setattr(lifecycle, "_audit_log", None)
    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    app.state.draining = False
    return app


def _create_and_claim(client: TestClient, title: str = "release me") -> str:
    created = client.post("/tasks", json={"title": title, "description": "d", "role": "backend"})
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    claimed = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": _HOLDER})
    assert claimed.status_code == 200, claimed.text
    return str(task_id)


@pytest.mark.parametrize(
    ("endpoint", "body", "release_path"),
    [
        ("force-claim", None, "force_claim"),
        ("cancel", {"reason": "operator cancelled"}, "cancel_cascade"),
        ("fail", {"reason": "tests red"}, "fail"),
        ("release", {"reason": "workspace unusable"}, "release"),
    ],
)
def test_plain_serve_node_mints_the_receipt(
    plain_serve_app: Any,
    endpoint: str,
    body: dict[str, str] | None,
    release_path: str,
) -> None:
    from bernstein.core.tasks.lifecycle import get_audit_log

    assert get_audit_log() is None, "the orchestrator's BERNSTEIN_AUDIT wiring must be absent"
    chain = plain_serve_app.state.audit_chain
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, f"release via {endpoint}")
        before = len(chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT))
        resp = client.post(f"/tasks/{task_id}/{endpoint}", json=body)
        assert resp.status_code == 200, resp.text

    events = chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT)
    assert len(events) == before + 1
    mine = [e for e in events if e.details["task_id"] == task_id]
    assert len(mine) == 1
    assert mine[0].details["release_path"] == release_path
    assert mine[0].details["released_by"] == _HOLDER


def test_plain_serve_node_reconstructs_the_holder_over_http(plain_serve_app: Any) -> None:
    """The whole loop on a plain node: claim, release, re-claim, replay."""
    chain = plain_serve_app.state.audit_chain
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, "reclaimed after release")
        assert reconstruct_claim_holders(chain.query()).get(task_id) == _HOLDER

        assert client.post(f"/tasks/{task_id}/force-claim").status_code == 200
        assert task_id not in reconstruct_claim_holders(chain.query())

        reclaimed = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": "node-b"})
        assert reclaimed.status_code == 200, reclaimed.text
        assert reconstruct_claim_holders(chain.query()).get(task_id) == "node-b"

    ok, errors = chain.verify()
    assert ok, errors


def test_plain_serve_node_does_not_strand_a_released_task(plain_serve_app: Any) -> None:
    """A task handed back by /release is in the pool, so the fold must not hold it.

    The window that matters is between the release and the next claim: a
    verifier replaying the chain there must not name a node that has already
    surrendered the task (#3018).
    """
    chain = plain_serve_app.state.audit_chain
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, "released back to the pool")
        assert reconstruct_claim_holders(chain.query()).get(task_id) == _HOLDER

        released = client.post(f"/tasks/{task_id}/release", json={"reason": "workspace unusable"})
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "open"
        assert task_id not in reconstruct_claim_holders(chain.query())

        reclaimed = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": "node-b"})
        assert reclaimed.status_code == 200, reclaimed.text
        assert reconstruct_claim_holders(chain.query()).get(task_id) == "node-b"

    ok, errors = chain.verify()
    assert ok, errors


def test_plain_serve_node_mints_the_receipt_on_reopen(plain_serve_app: Any) -> None:
    chain = plain_serve_app.state.audit_chain
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, "reopened after janitor")
        assert client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"}).status_code == 200
        resp = client.post(f"/tasks/{task_id}/reopen", json={"reason": "janitor signals failed"})
        assert resp.status_code == 200, resp.text

    mine = [e for e in chain.query(event_type=EVENT_TASK_RELEASE_RECEIPT) if e.details["task_id"] == task_id]
    assert len(mine) == 1
    assert mine[0].details["release_path"] == "reopen"
    assert task_id not in reconstruct_claim_holders(chain.query())


# ---------------------------------------------------------------------------
# Release guard: no claim-ending transition can skip the receipt
# ---------------------------------------------------------------------------
#
# The previous guard matched methods that assign ``claimed_by_session = None``.
# That is four methods, and none of the terminal paths (fail, cancel, abandon,
# refuse) clears the field at all, so the guard could not see the class of
# omission it was written to catch. It also matched per method, so a method
# that mints a receipt on one branch and forgets another read as clean.
#
# This one walks every ``transition_task`` call site in the store, works out
# the status it moves to, and requires a receipt in the same block for every
# site that leaves the held set for something other than a delivery. It then
# proves the pairing is not decorative by exercising the helper.


_STORE_SOURCE = Path(__file__).resolve().parents[2] / "src" / "bernstein" / "core" / "tasks" / "task_store_core.py"

#: Targets a transition may reach without surrendering a claim: the statuses a
#: holder can sit in while still owning the task, plus the delivery statuses.
#: Read off the source module so the guard and the store cannot disagree.
_EXEMPT_TARGETS: frozenset[str] = frozenset(status.name for status in (CLAIM_HELD_STATUSES | CLAIM_DELIVERED_STATUSES))

#: Blocks that scope a receipt to its call site. A transition inside a loop
#: body needs the receipt inside that same body, not merely somewhere in the
#: method: the abandon cascade regression was a receipt for the root task
#: sitting in the method body while the loop below it released nothing.
_SCOPING_BLOCKS = (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)


class _ReleaseSite(NamedTuple):
    """A ``transition_task`` call that ends a claim, and how it is paired."""

    method: str
    line: int
    target: str
    release_paths: tuple[str, ...]


def _parents(root: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node under *root* to its parent."""
    table: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            table[child] = node
    return table


def _transition_target(call: ast.Call) -> str | None:
    """Return the ``TaskStatus`` member name a ``transition_task`` call moves to.

    ``None`` when the target cannot be read statically, which the guard treats
    as needing a receipt: an unreadable transition is not a safe one.
    """
    candidates: list[ast.expr] = list(call.args[1:2])
    candidates += [kw.value for kw in call.keywords if kw.arg in {"new_status", "status"}]
    for candidate in candidates:
        if (
            isinstance(candidate, ast.Attribute)
            and isinstance(candidate.value, ast.Name)
            and candidate.value.id == "TaskStatus"
        ):
            return candidate.attr
    return None


def _scope_body(node: ast.AST, method: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.stmt]:
    """Return the statement list that must contain *node*'s receipt.

    The innermost enclosing loop or ``with`` block, or the method body when the
    call site sits directly in it.
    """
    current: ast.AST | None = node
    while current is not None and current is not method:
        parent = parents.get(current)
        if isinstance(parent, _SCOPING_BLOCKS) and current in parent.body:
            return parent.body
        current = parent
    return list(getattr(method, "body", []))


def _receipt_paths(body: list[ast.stmt]) -> tuple[str, ...]:
    """Return the ``release_path`` literals of receipts minted inside *body*.

    Nested loops and ``with`` blocks are skipped: they are scopes of their own,
    so a receipt inside one does not pay for a transition outside it. Without
    that, the receipt in ``abandon()``'s cascade loop would satisfy the root
    transition sitting above the loop.
    """
    paths: list[str] = []
    for statement in body:
        if isinstance(statement, _SCOPING_BLOCKS):
            continue
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "_record_release_receipt"):
                continue
            literal = next(
                (
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "release_path"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ),
                "",
            )
            paths.append(str(literal))
    return tuple(paths)


def _release_sites() -> tuple[list[_ReleaseSite], list[_ReleaseSite]]:
    """Return (paired, unpaired) claim-ending transition sites in ``TaskStore``."""
    tree = ast.parse(_STORE_SOURCE.read_text(encoding="utf-8"))
    store_class = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "TaskStore")
    parents = _parents(store_class)
    paired: list[_ReleaseSite] = []
    unpaired: list[_ReleaseSite] = []
    for method in store_class.body:
        if not isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(method):
            if not (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "transition_task"
            ):
                continue
            target = _transition_target(node)
            if target is not None and target in _EXEMPT_TARGETS:
                continue
            receipts = _receipt_paths(_scope_body(node, method, parents))
            site = _ReleaseSite(method.name, node.lineno, target or "<unreadable>", receipts)
            (paired if receipts else unpaired).append(site)
    return paired, unpaired


def test_every_claim_ending_transition_is_paired_with_a_receipt(tmp_path: Path) -> None:
    """Per call site, not per method, and the pairing has to actually append.

    Two halves, because either one alone is a gate that passes on work it
    never evaluated. The AST half catches a claim-ending transition with no
    receipt beside it. The runtime half catches the pairing being present and
    inert, which no amount of source walking can see.
    """
    _paired, unpaired = _release_sites()

    assert unpaired == [], (
        "these transition_task call sites end a claim with no task.release_receipt in the same block: "
        + "; ".join(f"{site.method}() line {site.line} -> TaskStatus.{site.target}" for site in unpaired)
        + ". Call self._record_release_receipt with a snapshot taken before the transition, in the same "
        "block as the transition. If the target status is not a surrender, add it to CLAIM_HELD_STATUSES "
        "or CLAIM_DELIVERED_STATUSES in task_store_core.py and say why."
    )

    store, chain = _store(tmp_path)
    task = _held(store, "T-guard")
    store._record_release_receipt(
        task,
        ClaimSnapshot(status=TaskStatus.IN_PROGRESS, holder=_HOLDER, held=True),
        release_path="guard_probe",
        reason="guard probe",
    )
    assert [event.details["release_path"] for event in _releases(chain)] == ["guard_probe"], (
        "every claim-ending transition is paired with a _record_release_receipt call, but the call "
        "appended nothing to the chain, so the whole ledger half is inert"
    )


def test_the_release_guard_covers_the_known_surrender_paths() -> None:
    """The guard is only worth anything if it matches the real call sites.

    Pins both directions: the surrender paths must be seen and paired, and the
    delivery paths must be exempt rather than accidentally uncovered.
    """
    paired, unpaired = _release_sites()
    assert unpaired == []

    by_method: dict[str, set[str]] = {}
    for site in paired:
        by_method.setdefault(site.method, set()).update(site.release_paths)

    expected = {
        "force_claim": "force_claim",
        "reopen": "reopen",
        "release": "release",
        "cancel": "cancel",
        "cancel_cascade": "cancel_cascade",
        "fail": "fail",
        "fail_contract_violation": "fail_contract_violation",
        "refuse": "refuse",
        "abandon": "abandon",
        "complete": "fail_empty_completion",
        "recover_stale_claimed_tasks": "restart_recovery",
        "reopen_tasks_for_node": "node_departure",
    }
    missing = {method: path for method, path in expected.items() if path not in by_method.get(method, set())}
    assert missing == {}, f"the guard no longer sees these surrender paths: {missing}"

    # The abandon cascade is a second, separately scoped site inside abandon().
    assert "abandon_cascade" in by_method["abandon"], (
        "abandon() cascades held downstream tasks to BLOCKED_BY_ABANDON in a loop of its own; "
        "that loop needs its own receipt"
    )

    # Delivery must be exempt by name, not by having drifted out of the walk.
    assert {"DONE", "CLOSED"} <= _EXEMPT_TARGETS
    assert "SUSPENDED" not in _EXEMPT_TARGETS, "SUSPENDED has no transitions in the task FSM"
