"""Tests for task claiming via parent session ID (T433).

Verifies that claimed_by_session is recorded, filtered, and cleared correctly
across all claim paths: claim_next, claim_by_id, claim_batch, force_claim.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore


def _task_request(
    *,
    title: str = "Implement parser",
    description: str = "Write the parser module.",
    role: str = "backend",
    priority: int = 1,
    scope: str = "medium",
    complexity: str = "medium",
    depends_on: list[str] | None = None,
) -> Any:
    """Build a create-task request matching TaskCreateRequest protocol."""
    return SimpleNamespace(
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        estimated_minutes=30,
        depends_on=depends_on or [],
        parent_task_id=None,
        depends_on_repo=None,
        owned_files=[],
        cell_id=None,
        repo=None,
        task_type="standard",
        upgrade_details=None,
        model=None,
        effort=None,
        batch_eligible=False,
        completion_signals=[],
        slack_context=None,
        eu_ai_act_risk="minimal",
        approval_required=False,
        risk_level="low",
        tenant_id="default",
    )


# ---------------------------------------------------------------------------
# claim_next
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_next_records_session_id(tmp_path: Path) -> None:
    """claim_next stores the claimed_by_session on the task."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    await store.create(_task_request())

    task = await store.claim_next("backend", claimed_by_session="orch-aaa")

    assert task is not None
    assert task.claimed_by_session == "orch-aaa"
    assert task.status == TaskStatus.CLAIMED


@pytest.mark.anyio
async def test_claim_next_without_session_leaves_none(tmp_path: Path) -> None:
    """claim_next without claimed_by_session leaves field as None."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    await store.create(_task_request())

    task = await store.claim_next("backend")

    assert task is not None
    assert task.claimed_by_session is None


# ---------------------------------------------------------------------------
# claim_by_id
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_by_id_records_session_id(tmp_path: Path) -> None:
    """claim_by_id stores the claimed_by_session on the task."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    created = await store.create(_task_request())

    task = await store.claim_by_id(
        created.id,
        expected_version=created.version,
        claimed_by_session="orch-bbb",
    )

    assert task.claimed_by_session == "orch-bbb"
    assert task.status == TaskStatus.CLAIMED


@pytest.mark.anyio
async def test_claim_by_id_without_session_leaves_none(tmp_path: Path) -> None:
    """claim_by_id without claimed_by_session leaves field as None."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    created = await store.create(_task_request())

    task = await store.claim_by_id(created.id, expected_version=created.version)

    assert task.claimed_by_session is None


# ---------------------------------------------------------------------------
# claim_batch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claim_batch_records_session_id(tmp_path: Path) -> None:
    """claim_batch stores claimed_by_session on all successfully claimed tasks."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    t1 = await store.create(_task_request(title="task-1"))
    t2 = await store.create(_task_request(title="task-2"))

    claimed, failed = await store.claim_batch(
        [t1.id, t2.id],
        agent_id="agent-1",
        claimed_by_session="orch-ccc",
    )

    assert len(claimed) == 2
    assert len(failed) == 0
    for tid in claimed:
        task = store.get_task(tid)
        assert task is not None
        assert task.claimed_by_session == "orch-ccc"


@pytest.mark.anyio
async def test_claim_batch_without_session_leaves_none(tmp_path: Path) -> None:
    """claim_batch without claimed_by_session leaves field as None."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    t1 = await store.create(_task_request())

    claimed, _ = await store.claim_batch([t1.id], agent_id="agent-1")

    task = store.get_task(claimed[0])
    assert task is not None
    assert task.claimed_by_session is None


# ---------------------------------------------------------------------------
# force_claim clears session
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_force_claim_clears_session_id(tmp_path: Path) -> None:
    """force_claim resets the claimed_by_session to None."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    created = await store.create(_task_request())

    await store.claim_by_id(
        created.id,
        expected_version=created.version,
        claimed_by_session="orch-ddd",
    )
    task = store.get_task(created.id)
    assert task is not None
    assert task.claimed_by_session == "orch-ddd"

    released = await store.force_claim(created.id)

    assert released.claimed_by_session is None
    assert released.status == TaskStatus.OPEN


# ---------------------------------------------------------------------------
# list_tasks filtering by claimed_by_session
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_tasks_filters_by_session(tmp_path: Path) -> None:
    """list_tasks with claimed_by_session returns only matching tasks."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    t1 = await store.create(_task_request(title="task-1"))
    t2 = await store.create(_task_request(title="task-2"))
    t3 = await store.create(_task_request(title="task-3"))

    await store.claim_by_id(t1.id, claimed_by_session="orch-AAA")
    await store.claim_by_id(t2.id, claimed_by_session="orch-BBB")
    # t3 stays open (no session)

    claimed_aaa = store.list_tasks(status="claimed", claimed_by_session="orch-AAA")
    claimed_bbb = store.list_tasks(status="claimed", claimed_by_session="orch-BBB")
    claimed_all = store.list_tasks(status="claimed")

    assert len(claimed_aaa) == 1
    assert claimed_aaa[0].id == t1.id
    assert len(claimed_bbb) == 1
    assert claimed_bbb[0].id == t2.id
    assert len(claimed_all) == 2


@pytest.mark.anyio
async def test_list_tasks_session_filter_returns_empty_on_mismatch(tmp_path: Path) -> None:
    """list_tasks with a non-matching session returns no tasks."""
    store = TaskStore(tmp_path / "runtime" / "tasks.jsonl")
    t1 = await store.create(_task_request())
    await store.claim_by_id(t1.id, claimed_by_session="orch-EEE")

    result = store.list_tasks(status="claimed", claimed_by_session="orch-NOPE")

    assert result == []


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_claimed_by_session_survives_jsonl_replay(tmp_path: Path) -> None:
    """claimed_by_session is persisted to JSONL and restored on replay."""
    jsonl_path = tmp_path / "runtime" / "tasks.jsonl"
    store = TaskStore(jsonl_path)

    created = await store.create(_task_request())
    await store.claim_by_id(
        created.id,
        expected_version=created.version,
        claimed_by_session="orch-FFF",
    )
    await store.flush_buffer()

    replayed = TaskStore(jsonl_path)
    replayed.replay_jsonl()
    restored = replayed.get_task(created.id)

    assert restored is not None
    assert restored.claimed_by_session == "orch-FFF"
    assert restored.status == TaskStatus.CLAIMED


# ---------------------------------------------------------------------------
# Task.from_dict
# ---------------------------------------------------------------------------


def test_task_from_dict_parses_claimed_by_session() -> None:
    """Task.from_dict correctly parses claimed_by_session from raw dict."""
    from bernstein.core.models import Task

    raw = {
        "id": "abc123",
        "title": "test",
        "description": "desc",
        "role": "backend",
        "claimed_by_session": "orch-GGG",
    }
    task = Task.from_dict(raw)
    assert task.claimed_by_session == "orch-GGG"


def test_task_from_dict_defaults_claimed_by_session_to_none() -> None:
    """Task.from_dict defaults claimed_by_session to None when absent."""
    from bernstein.core.models import Task

    raw = {
        "id": "abc123",
        "title": "test",
        "description": "desc",
        "role": "backend",
    }
    task = Task.from_dict(raw)
    assert task.claimed_by_session is None


# ---------------------------------------------------------------------------
# Archive includes claimed_by_session
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_archive_includes_claimed_by_session(tmp_path: Path) -> None:
    """Archived tasks record the claimed_by_session of the claiming orchestrator."""
    archive_path = tmp_path / "archive" / "tasks.jsonl"
    store = TaskStore(
        tmp_path / "runtime" / "tasks.jsonl",
        archive_path=archive_path,
    )

    created = await store.create(_task_request())
    await store.claim_by_id(
        created.id,
        expected_version=created.version,
        claimed_by_session="orch-HHH",
    )
    await store.complete(created.id, "shipped")
    await store.flush_buffer()

    records = store.read_archive(limit=1)
    assert len(records) == 1
    assert records[0]["claimed_by_session"] == "orch-HHH"
