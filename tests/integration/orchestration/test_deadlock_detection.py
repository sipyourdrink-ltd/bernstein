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
            _batch_sessions={},
            _task_to_session={},
            _lock_manager=lock_mgr,
            _loop_detector=loop_detector,
            _workdir=Path(tmpdir),
        )

        orch._check_file_overlap = Orchestrator._check_file_overlap.__get__(orch)
        # Bind the real resolver rather than giving the stand-in its own: a
        # fake that reimplements the lookup would pass while the shipped one
        # resolved to something else, which is the drift this test is for.
        orch.resolve_waiting_agent = Orchestrator.resolve_waiting_agent.__get__(orch)

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


def test_claiming_a_child_task_clears_the_parent_agent_wait(tmp_path, make_task) -> None:
    """The wait recorded when a batch was deferred must go when the claim lands.

    The clearing happens in ``claim_and_spawn_batches``, which reaches the
    detector through an orchestrator-shaped object rather than a real
    Orchestrator. Nothing else drives that path: the other tests here call
    ``clear_wait`` themselves, so a detector that stopped being found would
    leave the wait-for graph growing and every later deadlock report keyed on
    a phantom edge, with the suite still green.
    """
    from unittest.mock import MagicMock, patch

    from bernstein.core.tasks.task_lifecycle import claim_and_spawn_batches

    loop_detector = LoopDetector()
    parent_agent = AgentSession(id="A-1", role="backend")
    parent_agent.status = "working"
    parent_agent.task_ids = ["T-parent"]

    loop_detector.record_lock_wait("A-1", ["src/held.py"], {"src/held.py": "A-2"})
    assert loop_detector._wait_for["A-1"], "precondition: A-1 is recorded as waiting"

    child = make_task(id="T-child", title="child", role="backend")
    child.parent_task_id = "T-parent"

    post_response = MagicMock()
    post_response.status_code = 200
    post_response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = post_response

    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_tasks_per_agent=1,
            max_agents=2,
            force_parallel=False,
            ab_test=False,
            max_agent_runtime_s=600,
        ),
        _workdir=tmp_path,
        _agents={},
        _batch_sessions={"A-1": parent_agent},
        _task_to_session={},
        _file_ownership={},
        _spawn_failures={},
        _spawn_failure_history={},
        _MAX_SPAWN_FAILURES=3,
        _SPAWN_BACKOFF_BASE_S=30.0,
        _SPAWN_BACKOFF_MAX_S=300.0,
        _idle_shutdown_ts={},
        _quarantine=MagicMock(),
        _decomposed_task_ids=set(),
        _preserved_worktrees={},
        _client=client,
        _spawner=MagicMock(),
        _lock_manager=MagicMock(),
        _loop_detector=loop_detector,
        _rate_limit_tracker=None,
        _wal_writer=None,
        _response_cache=None,
        _fast_path_stats=MagicMock(),
        _bulletin=None,
    )
    orch.resolve_waiting_agent = Orchestrator.resolve_waiting_agent.__get__(orch)
    orch._quarantine.is_quarantined.return_value = False
    orch._spawner._adapter.is_rate_limited.return_value = False

    result = SimpleNamespace(spawned=[], errors=[])
    with patch("bernstein.core.tasks.task_lifecycle.fail_task"):
        claim_and_spawn_batches(orch, [[child]], 0, set(), set(), result)

    assert not result.errors, f"claim did not land: {result.errors}"
    assert not loop_detector._wait_for.get("A-1"), "claiming the child left the parent's wait in place"
