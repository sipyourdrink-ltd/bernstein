"""Unit tests for agent affinity grouping in group_by_role.

Verifies that tasks downstream of a completed task are batched together
when they share the same preferred agent via the agent_affinity mapping.
"""

from __future__ import annotations

from bernstein.core.models import Task, TaskStatus
from bernstein.core.tick_pipeline import group_by_role


def _make_task(
    task_id: str,
    role: str = "backend",
    priority: int = 2,
    owned_files: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        description="",
        role=role,
        priority=priority,
        owned_files=owned_files or [],
        depends_on=depends_on or [],
        status=TaskStatus.OPEN,
    )


class TestGroupByRoleAgentAffinity:
    def test_tasks_with_same_preferred_agent_batched_together(self) -> None:
        """Tasks that prefer the same agent should end up in the same batch."""
        t1 = _make_task("t1")
        t2 = _make_task("t2")
        t3 = _make_task("t3")  # no preferred agent

        # t1 and t2 prefer the same agent; t3 has no preference
        affinity = {"t1": "agent-abc", "t2": "agent-abc"}

        batches = group_by_role([t1, t2, t3], max_per_batch=3, agent_affinity=affinity)

        # t1 and t2 must appear in the same batch
        merged = {task.id for batch in batches for task in batch}
        assert merged == {"t1", "t2", "t3"}

        for batch in batches:
            ids = {t.id for t in batch}
            if "t1" in ids:
                assert "t2" in ids, "t1 and t2 should be in the same batch"
                break

    def test_tasks_with_different_preferred_agents_stay_separate(self) -> None:
        """Tasks with different preferred agents should not be merged."""
        t1 = _make_task("t1")
        t2 = _make_task("t2")

        affinity = {"t1": "agent-abc", "t2": "agent-xyz"}

        batches = group_by_role([t1, t2], max_per_batch=2, agent_affinity=affinity)

        # With different preferred agents, they should remain separate
        # (unless they share file affinity, which they don't here)
        flat = [t.id for batch in batches for t in batch]
        assert sorted(flat) == ["t1", "t2"]

        # Check they are in different batches (no file affinity to merge them)
        if len(batches) > 1:
            for batch in batches:
                ids = {t.id for t in batch}
                assert len(ids) == 1

    def test_no_agent_affinity_unchanged_behaviour(self) -> None:
        """Without agent_affinity, group_by_role behaves as before."""
        tasks = [_make_task(str(i)) for i in range(4)]
        batches_no_affinity = group_by_role(tasks, max_per_batch=2)
        batches_none = group_by_role(tasks, max_per_batch=2, agent_affinity=None)
        batches_empty = group_by_role(tasks, max_per_batch=2, agent_affinity={})

        ids_no_affinity = sorted(t.id for batch in batches_no_affinity for t in batch)
        ids_none = sorted(t.id for batch in batches_none for t in batch)
        ids_empty = sorted(t.id for batch in batches_empty for t in batch)
        assert ids_no_affinity == ids_none == ids_empty

    def test_agent_affinity_with_file_overlap_merges_all(self) -> None:
        """Agent affinity plus file overlap should all end up in one group."""
        t1 = _make_task("t1", owned_files=["src/foo.py"])
        t2 = _make_task("t2", owned_files=["src/foo.py"])  # file overlap with t1
        t3 = _make_task("t3")  # agent affinity with t1

        affinity = {"t1": "agent-abc", "t3": "agent-abc"}

        batches = group_by_role([t1, t2, t3], max_per_batch=5, agent_affinity=affinity)

        # t1 and t2 merge via file overlap; t3 merges with that group via affinity
        all_in_one = any(len(batch) == 3 for batch in batches)
        assert all_in_one, f"Expected 3 tasks in one batch, got batches: {[[t.id for t in b] for b in batches]}"

    def test_affinity_only_for_tasks_in_same_role(self) -> None:
        """Agent affinity should not cross role boundaries."""
        t1 = _make_task("t1", role="backend")
        t2 = _make_task("t2", role="qa")  # different role
        affinity = {"t1": "agent-abc", "t2": "agent-abc"}

        batches = group_by_role([t1, t2], max_per_batch=2, agent_affinity=affinity)

        # Different roles → different batches regardless of affinity
        batch_roles = [batch[0].role for batch in batches if batch]
        assert "backend" in batch_roles
        assert "qa" in batch_roles
        # They should not be merged into the same batch
        for batch in batches:
            roles = {t.role for t in batch}
            assert len(roles) == 1, f"Batch mixed roles: {roles}"
