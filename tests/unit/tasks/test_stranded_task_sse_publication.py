"""Unit tests for stranded task SSE event publication and observer notification (#4259).

Asserts that tasks moved to ``BLOCKED_BY_FAILED_DEP`` during dependency propagation
notify registered store listeners and publish ``task_update`` SSE events across
all terminal failure paths (fail, fail_contract_violation, refuse, cancel, cancel_cascade, claim_next).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.server import SSEBus, TaskCreate, create_app
from bernstein.core.tasks.contracts import ContractViolation, RefusalKind, WorkerRefusal
from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_store import TaskStore


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / "tasks.jsonl"


@pytest.mark.asyncio
async def test_stranded_task_notifies_store_listener(jsonl_path: Path) -> None:
    """Store listener is notified when a dependent task is moved to BLOCKED_BY_FAILED_DEP."""
    store = TaskStore(jsonl_path)

    # Create root task A and dependent task B
    task_a = await store.create(TaskCreate(title="Task A", description="desc", role="backend"))
    task_b = await store.create(TaskCreate(title="Task B", description="desc", role="backend", depends_on=[task_a.id]))

    notifications: list[Task] = []
    store.add_task_listener(notifications.append)

    # Fail task A -> task B is stranded to BLOCKED_BY_FAILED_DEP
    await store.fail(task_a.id, "execution failed")

    stranded = [t for t in notifications if t.id == task_b.id]
    assert len(stranded) == 1
    assert stranded[0].status == TaskStatus.BLOCKED_BY_FAILED_DEP
    assert stranded[0].metadata.get("blocking_task_id") == task_a.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    ["cancel", "cancel_cascade", "refuse", "fail_contract_violation", "claim_next"],
)
async def test_all_stranding_paths_notify_listener(tmp_path: Path, method: str) -> None:
    """All terminal paths (cancel, cancel_cascade, refuse, fail_contract_violation, claim_next) notify listener of stranded tasks."""
    runtime_dir = tmp_path / f"runtime_{method}"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(runtime_dir / "tasks.jsonl")

    task_a = await store.create(TaskCreate(title="Task A", description="desc", role="backend"))
    task_b = await store.create(TaskCreate(title="Task B", description="desc", role="backend", depends_on=[task_a.id]))

    notifications: list[Task] = []
    store.add_task_listener(notifications.append)

    if method == "cancel":
        await store.cancel(task_a.id, "user cancel")
    elif method == "cancel_cascade":
        await store.cancel_cascade(task_a.id, "cascade cancel")
    elif method == "refuse":
        claimed = await store.claim_next("backend")
        assert claimed is not None and claimed.id == task_a.id
        refusal = WorkerRefusal(kind=RefusalKind.SCOPE_EXCEEDED, detail="out of bounds")
        await store.refuse(claimed.id, refusal)
    elif method == "fail_contract_violation":
        claimed = await store.claim_next("backend")
        assert claimed is not None and claimed.id == task_a.id
        violation = ContractViolation(path="summary", message="missing summary")
        await store.fail_contract_violation(claimed.id, violation)
    elif method == "claim_next":
        # Manually fail task_a without trigger to test claim_next backstop cascade
        task_a.status = TaskStatus.FAILED
        store._index_add(task_a)
        # claim_next triggers unreachable projection cascade
        await store.claim_next("backend")

    stranded = [t for t in notifications if t.id == task_b.id]
    assert len(stranded) == 1
    assert stranded[0].status == TaskStatus.BLOCKED_BY_FAILED_DEP


@pytest.mark.asyncio
async def test_create_app_wires_task_update_sse_publisher(tmp_path: Path) -> None:
    """The server app created by create_app publishes task_update SSE events for stranded tasks."""
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(jsonl_path=runtime_dir / "tasks.jsonl")

    store: TaskStore = app.state.store
    sse_bus: SSEBus = app.state.sse_bus

    queue = sse_bus.subscribe()

    task_a = await store.create(TaskCreate(title="Task A", description="desc", role="backend"))
    task_b = await store.create(TaskCreate(title="Task B", description="desc", role="backend", depends_on=[task_a.id]))

    await store.fail(task_a.id, "failure reason")

    events: list[str] = []
    while not queue.empty():
        events.append(queue.get_nowait())

    b_updates = [msg for msg in events if task_b.id in msg and "blocked_by_failed_dep" in msg]
    assert len(b_updates) >= 1
