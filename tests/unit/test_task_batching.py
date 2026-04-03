"""Tests for task batching with file overlap awareness and small task
compaction."""

from __future__ import annotations

from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.task_grouping import compact_small_tasks
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


class TestCompactSmallTasks:
    """Tests for compact_small_tasks task grouping."""

    @staticmethod
    def _small_task(task_id: str, *, minutes: int = 10, role: str = "backend") -> Task:
        return Task(
            id=task_id,
            title=f"Small {task_id}",
            description="...",
            role=role,
            complexity=Complexity.LOW,
            estimated_minutes=minutes,
            owned_files=[f"src/foo_{task_id}.py"],
        )

    @staticmethod
    def _normal_task(task_id: str, *, minutes: int = 30, role: str = "backend") -> Task:
        return Task(
            id=task_id,
            title=f"Normal {task_id}",
            description="...",
            role=role,
            complexity=Complexity.MEDIUM,
            estimated_minutes=minutes,
            owned_files=[f"src/bar_{task_id}.py"],
        )

    def test_no_compaction_when_only_normal_tasks(self) -> None:
        """Batches with no small tasks pass through unchanged."""
        t1 = self._normal_task("t1")
        t2 = self._normal_task("t2")
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        assert len(result) == 2

    def test_single_batch_passthrough(self) -> None:
        """A single batch is returned as-is."""
        t1 = self._small_task("t1")
        result = compact_small_tasks([[t1]], max_per_batch=3)
        assert result == [[t1]]
        assert len(result) == 1

    def test_compact_two_small_batches(self) -> None:
        """Two single-task small batches merge into one combined batch."""
        t1 = self._small_task("t1")
        t2 = self._small_task("t2")
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        # Two small batches merge into one
        assert len(result) == 1
        assert {t.id for t in result[0]} == {"t1", "t2"}

    def test_no_compaction_different_roles(self) -> None:
        """Same-small tasks but different roles should NOT merge."""
        t1 = self._small_task("t1", role="backend")
        t2 = self._small_task("t2", role="qa")
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        assert len(result) == 2

    def test_no_compaction_file_conflict(self) -> None:
        """Small tasks sharing files should NOT merge (conflict)."""
        t1 = self._small_task("t1")
        t1.owned_files = ["src/shared.py"]
        t2 = self._small_task("t2")
        t2.owned_files = ["src/shared.py"]
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        assert len(result) == 2

    def test_respect_max_per_batch(self) -> None:
        """Compaction should never exceed max_per_batch."""
        tasks: list[list[Task]] = [[self._small_task(f"t{i}")] for i in range(5)]
        result = compact_small_tasks(tasks, max_per_batch=2)
        for batch in result:
            assert len(batch) <= 2

    def test_combined_minutes_limit(self) -> None:
        """Should not merge if combined minutes exceed the cap."""
        t1 = self._small_task("t1", minutes=40)
        t2 = self._small_task("t2", minutes=30)
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        # 40 + 30 = 70 > 60 — should NOT merge
        assert len(result) == 2

    def test_scope_small_triggers_compact(self) -> None:
        """Tasks with scope=SMALL and estimated_minutes ≤ 15 should compact."""
        t1 = Task(
            id="t1",
            title="T1",
            description="...",
            role="backend",
            scope=Scope.SMALL,
            estimated_minutes=15,
            owned_files=["src/a.py"],
        )
        t2 = Task(
            id="t2",
            title="T2",
            description="...",
            role="backend",
            scope=Scope.SMALL,
            estimated_minutes=10,
            owned_files=["src/b.py"],
        )
        result = compact_small_tasks([[t1], [t2]], max_per_batch=3)
        assert len(result) == 1
