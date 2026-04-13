from pathlib import Path

import pytest
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore

from bernstein.core.server import TaskCreate


@pytest.fixture
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture
def store(jsonl_path: Path) -> TaskStore:
    return TaskStore(jsonl_path)


@pytest.mark.asyncio
async def test_memory_task_store_create_and_get(store: TaskStore) -> None:
    """Test task creation and basic retrieval."""
    req = TaskCreate(
        title="Test Task",
        description="Desc",
        role="backend",
        priority=2,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id="cell1",
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )
    task = await store.create(req)  # type: ignore[arg-type]
    assert task.title == "Test Task"
    assert task.status == TaskStatus.OPEN
    assert task.id is not None
    assert task.version == 1

    fetched = store.get_task(task.id)
    assert fetched == task


@pytest.mark.asyncio
async def test_memory_task_store_claim_next(store: TaskStore) -> None:
    """Test claiming the next available task based on priority."""
    req_data = {
        "title": "T1",
        "description": "D1",
        "role": "backend",
        "priority": 1,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "depends_on": [],
        "owned_files": [],
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "model": "sonnet",
        "effort": "high",
        "completion_signals": [],
        "slack_context": None,
    }
    t1 = await store.create(TaskCreate(**req_data))  # type: ignore[arg-type]

    req_data2 = req_data.copy()
    req_data2["title"] = "T2"
    req_data2["priority"] = 2
    t2 = await store.create(TaskCreate(**req_data2))  # type: ignore[arg-type]

    # Claim highest priority (1)
    claimed = await store.claim_next("backend")
    assert claimed is not None
    assert claimed.id == t1.id
    assert claimed.status == TaskStatus.CLAIMED
    assert claimed.version == 2

    # Claim next (2)
    claimed2 = await store.claim_next("backend")
    assert claimed2 is not None
    assert claimed2.id == t2.id
    assert claimed2.status == TaskStatus.CLAIMED

    # No more tasks for backend
    assert await store.claim_next("backend") is None


@pytest.mark.asyncio
async def test_memory_task_store_complete(store: TaskStore) -> None:
    """Test marking a task as completed."""
    req = TaskCreate(
        title="T",
        description="D",
        role="backend",
        priority=1,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )
    t = await store.create(req)  # type: ignore[arg-type]
    await store.claim_next("backend")

    completed = await store.complete(t.id, "Finished successfully")
    assert completed.status == TaskStatus.DONE
    assert completed.result_summary == "Finished successfully"
    assert completed.version == 3


@pytest.mark.asyncio
async def test_memory_task_store_list_and_filtering(store: TaskStore) -> None:
    """Test listing tasks with status and cell_id filters."""
    req_backend = TaskCreate(
        title="B",
        description="D",
        role="backend",
        priority=1,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id="cellA",
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )
    req_frontend = TaskCreate(
        title="F",
        description="D",
        role="frontend",
        priority=1,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id="cellB",
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )

    await store.create(req_backend)  # type: ignore[arg-type]
    await store.create(req_frontend)  # type: ignore[arg-type]

    # List all
    all_tasks = store.list_tasks()
    assert len(all_tasks) == 2

    # Filter by cell_id
    cell_a_tasks = store.list_tasks(cell_id="cellA")
    assert len(cell_a_tasks) == 1
    assert cell_a_tasks[0].role == "backend"

    # Filter by status
    open_tasks = store.list_tasks(status="open")
    assert len(open_tasks) == 2

    # Claim one and check
    await store.claim_next("backend")
    open_tasks_after_claim = store.list_tasks(status="open")
    assert len(open_tasks_after_claim) == 1
    assert open_tasks_after_claim[0].role == "frontend"

    # Complete and check status filtering
    f_id = open_tasks_after_claim[0].id
    await store.claim_next("frontend")
    await store.complete(f_id, "Done")
    done_tasks = store.list_tasks(status="done")
    assert len(done_tasks) == 1
    assert done_tasks[0].id == f_id


@pytest.mark.asyncio
async def test_memory_task_store_status_summary(store: TaskStore) -> None:
    """Test the aggregated status summary."""
    req = TaskCreate(
        title="T",
        description="D",
        role="backend",
        priority=1,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )
    await store.create(req)  # type: ignore[arg-type]

    summary = store.status_summary()
    assert summary["total"] == 1
    assert summary["open"] == 1
    assert summary["claimed"] == 0
    assert summary["done"] == 0

    await store.claim_next("backend")
    summary = store.status_summary()
    assert summary["open"] == 0
    assert summary["claimed"] == 1


@pytest.mark.asyncio
async def test_memory_task_store_persistence(jsonl_path: Path) -> None:
    """Test that tasks are persisted to JSONL and can be replayed."""
    store1 = TaskStore(jsonl_path)
    req = TaskCreate(
        title="Persist me",
        description="D",
        role="backend",
        priority=1,
        scope="medium",
        complexity="medium",
        estimated_minutes=30,
        depends_on=[],
        owned_files=[],
        cell_id=None,
        task_type="standard",
        upgrade_details=None,
        model="sonnet",
        effort="high",
        completion_signals=[],
        slack_context=None,
    )
    task = await store1.create(req)  # type: ignore[arg-type]
    await store1.flush_buffer()

    # Create new store instance and replay
    store2 = TaskStore(jsonl_path)
    store2.replay_jsonl()

    loaded = store2.get_task(task.id)
    assert loaded is not None
    assert loaded.title == "Persist me"
    assert loaded.status == TaskStatus.OPEN
