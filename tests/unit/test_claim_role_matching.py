"""Tests for role-locked task claiming in the orchestrator.

Agents can ONLY claim tasks where agent.role == task.role.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bernstein.core.server import TaskCreate, TaskStore


async def _make_store() -> TaskStore:
    """Create and initialize an in-memory task store."""
    from bernstein.core.server import TaskStore as InMemoryStore

    tmp_dir = Path(tempfile.mkdtemp())
    jsonl_path = tmp_dir / "tasks.jsonl"
    return InMemoryStore(jsonl_path)


@pytest.mark.asyncio
async def test_claim_by_id_with_matching_role_succeeds() -> None:
    """Agent with matching role can claim a task."""
    store = await _make_store()
    task = await store.create(
        TaskCreate(
            title="Backend task",
            description="Implement feature",
            role="backend",
        )
    )
    # Claim with matching role should succeed
    claimed = await store.claim_by_id(task.id, agent_role="backend")
    assert claimed.status.value == "claimed"


@pytest.mark.asyncio
async def test_claim_by_id_with_mismatched_role_fails() -> None:
    """Agent with mismatched role cannot claim a task."""
    store = await _make_store()
    task = await store.create(
        TaskCreate(
            title="Backend task",
            description="Implement feature",
            role="backend",
        )
    )
    # Claim with mismatched role should fail
    with pytest.raises(ValueError, match="role mismatch"):
        await store.claim_by_id(task.id, agent_role="frontend")


@pytest.mark.asyncio
async def test_claim_batch_with_matching_roles_succeeds() -> None:
    """Agent can claim multiple tasks with matching roles."""
    store = await _make_store()
    task1 = await store.create(
        TaskCreate(
            title="Backend task 1",
            description="Implement feature",
            role="backend",
        )
    )
    task2 = await store.create(
        TaskCreate(
            title="Backend task 2",
            description="Fix bug",
            role="backend",
        )
    )
    # Claim with matching role should succeed for both
    claimed, failed = await store.claim_batch(
        [task1.id, task2.id],
        agent_id="agent-1",
        agent_role="backend",
    )
    assert len(claimed) == 2
    assert len(failed) == 0


@pytest.mark.asyncio
async def test_claim_batch_with_mixed_roles_fails_for_mismatched() -> None:
    """Agent cannot claim tasks with mismatched roles in batch."""
    store = await _make_store()
    task1 = await store.create(
        TaskCreate(
            title="Backend task",
            description="Implement feature",
            role="backend",
        )
    )
    task2 = await store.create(
        TaskCreate(
            title="Frontend task",
            description="Build UI",
            role="frontend",
        )
    )
    # Claim with matching role for one, mismatched for the other
    claimed, failed = await store.claim_batch(
        [task1.id, task2.id],
        agent_id="agent-1",
        agent_role="backend",
    )
    # Only task1 should be claimed, task2 should fail
    assert len(claimed) == 1
    assert task1.id in claimed
    assert len(failed) == 1
    assert task2.id in failed


@pytest.mark.asyncio
async def test_claim_batch_rejects_all_mismatched() -> None:
    """When all tasks have mismatched roles, all claims fail."""
    store = await _make_store()
    task1 = await store.create(
        TaskCreate(
            title="Frontend task 1",
            description="Build UI",
            role="frontend",
        )
    )
    task2 = await store.create(
        TaskCreate(
            title="Frontend task 2",
            description="Style component",
            role="frontend",
        )
    )
    # Try to claim frontend tasks with backend role
    claimed, failed = await store.claim_batch(
        [task1.id, task2.id],
        agent_id="agent-1",
        agent_role="backend",
    )
    assert len(claimed) == 0
    assert len(failed) == 2
