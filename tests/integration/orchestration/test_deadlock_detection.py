import asyncio
import tempfile
from types import SimpleNamespace

import pytest

from bernstein.core.observability.loop_detector import LoopDetector
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.persistence.file_locks import FileLockManager
from bernstein.core.tasks.models import AgentSession, Task


@pytest.mark.asyncio
async def test_deadlock_cycle_breaker_integration() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        from pathlib import Path

        lock_mgr = FileLockManager(Path(tmpdir))
        loop_detector = LoopDetector()

        # Create two active agents
        agent1 = AgentSession(id="A-1", role="backend")
        agent1.status = "working"
        agent1.task_ids = ["T-1"]

        agent2 = AgentSession(id="A-2", role="backend")
        agent2.status = "working"
        agent2.task_ids = ["T-2"]

        orch = SimpleNamespace(
            _file_ownership={},
            _agents={"A-1": agent1, "A-2": agent2},
            _lock_manager=lock_mgr,
            _loop_detector=loop_detector,
            _workdir=Path(tmpdir),
        )

        orch._check_file_overlap = Orchestrator._check_file_overlap.__get__(orch)

        # They cross-hold two files
        # Agent 1 holds file 1 (older lock)
        lock_mgr.acquire(["src/file1.py"], agent_id="A-1", task_id="T-1")
        # Ensure lock timestamps are different
        await asyncio.sleep(0.01)
        # Agent 2 holds file 2 (newer lock)
        lock_mgr.acquire(["src/file2.py"], agent_id="A-2", task_id="T-2")

        # Task 3 belongs to Agent 1, needs file 2
        task3 = Task(id="T-3", title="T-3", description="", role="backend", owned_files=["src/file2.py"])
        task3.parent_task_id = "T-1"

        # Task 4 belongs to Agent 2, needs file 1
        task4 = Task(id="T-4", title="T-4", description="", role="backend", owned_files=["src/file1.py"])
        task4.parent_task_id = "T-2"

        # Simulate deferring batch 3
        orch._check_file_overlap([task3])

        # Simulate deferring batch 4
        orch._check_file_overlap([task4])

        # Tick the deadlock detection
        detections = loop_detector.detect_deadlocks(lock_mgr)
        assert len(detections) == 1, f"Expected exactly 1 deadlock cycle, got {len(detections)}"

        from bernstein.core.agents.agent_lifecycle import check_loops_and_deadlocks

        # The oldest lock is A-1's lock on file1.py. It should be released.
        check_loops_and_deadlocks(orch)

        # Simulate the orchestrator cleaning up agent wait states after tick
        loop_detector.clear_wait("A-1")
        loop_detector.clear_wait("A-2")

        assert len(loop_detector._wait_for) == 0, "wait_for graph should be completely empty after clear_wait() runs"

        locks = lock_mgr.all_locks()
        locked_files = [lock.file_path for lock in locks]
        assert "src/file1.py" not in locked_files, "Oldest lock should have been released"
        assert "src/file2.py" in locked_files, "Newer lock should still be held"
