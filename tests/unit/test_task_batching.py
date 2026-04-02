"""Tests for task batching with file overlap awareness."""

from __future__ import annotations

from bernstein.core.models import Task
from bernstein.core.tick_pipeline import group_by_role


def test_group_by_role_with_file_overlap() -> None:
    """Test that tasks touching the same files are grouped into the same batch."""
    # 3 tasks, all backend. max_per_batch = 2.
    # Task 1 and 3 touch auth.py. Task 2 touches main.py.
    # Without overlap awareness, 1 & 2 would be in batch 1, 3 in batch 2.
    # With overlap awareness, 1 & 3 should be in batch 1, 2 in batch 2.

    t1 = Task(id="t1", title="T1", description="...", role="backend", owned_files=["auth.py"])
    t2 = Task(id="t2", title="T2", description="...", role="backend", owned_files=["main.py"])
    t3 = Task(id="t3", title="T3", description="...", role="backend", owned_files=["auth.py"])

    batches = group_by_role([t1, t2, t3], max_per_batch=2)

    # We expect 2 batches for backend
    assert len(batches) == 2

    # Batch 1 should have t1 and t3 because they both touch auth.py
    batch1_ids = {t.id for t in batches[0]}
    assert "t1" in batch1_ids
    assert "t3" in batch1_ids

    # Batch 2 should have t2
    batch2_ids = {t.id for t in batches[1]}
    assert "t2" in batch2_ids


def test_group_by_role_different_roles_no_overlap_grouping() -> None:
    """Test that tasks with different roles are NOT grouped even if files overlap.

    They should still be in separate batches because agents are specialized.
    Serialization is handled by the orchestrator tick loop, not by grouping.
    """
    t1 = Task(id="t1", title="T1", description="...", role="backend", owned_files=["auth.py"])
    t2 = Task(id="t2", title="T2", description="...", role="qa", owned_files=["auth.py"])

    batches = group_by_role([t1, t2], max_per_batch=5)

    # Should be 2 batches (one for backend, one for qa)
    assert len(batches) == 2
    assert batches[0][0].role != batches[1][0].role
