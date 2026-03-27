"""Integration test: agent exits when role queue empties (#333d-03b).

Tests that recycle_idle_agents detects and sends SHUTDOWN when an agent's
role queue becomes empty after task completion (Case 4: role_drained_rebalance).
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.agent_lifecycle import recycle_idle_agents
from bernstein.core.models import (
    AgentSession,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.router import ModelConfig as RouterModelConfig

# --- Helpers ---


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    status: str = "open",
) -> Task:
    """Create a minimal task for testing."""
    return Task(
        id=id,
        title="Test task",
        description="Task description",
        role=role,
        priority=2,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus(status),
        task_type=TaskType.STANDARD,
    )


def _make_session(task_ids: list[str], session_id: str = "s-rebal-01") -> AgentSession:
    """Create a mock agent session."""
    session = AgentSession(
        id=session_id,
        role="backend",
        pid=12345,
        task_ids=task_ids,
        model_config=RouterModelConfig(model="claude-sonnet-4-6", effort="normal"),
        spawn_ts=time.time(),
    )
    session.status = "working"
    return session


def _make_orch(tmp_path: Path) -> MagicMock:
    """Build a minimal orchestrator-like object for testing recycle_idle_agents."""
    orch = MagicMock()
    orch._config = OrchestratorConfig(
        evolve_mode=False,
        evolution_enabled=False,
    )
    orch._agents = {}
    orch._idle_shutdown_ts = {}
    orch._workdir = tmp_path

    signal_mgr = MagicMock()
    signal_mgr.read_heartbeat.return_value = None  # no heartbeat by default
    orch._signal_mgr = signal_mgr

    # spawner.check_alive → True by default (process is still running)
    orch._spawner.check_alive.return_value = True

    return orch


# --- Tests ---


class TestAgentExitRebalancing:
    """Agent exits when role queue empties (task completed, no more work in role).

    Tests verify that recycle_idle_agents (Case 4: role_drained_rebalance)
    detects and SHUTDOWN signals agents when their role's task queue becomes empty.
    """

    def test_shutdown_sent_when_task_completed_and_role_queue_empty(self, tmp_path: Path) -> None:
        """When task completes and role has no active tasks, SHUTDOWN is sent (#333d-03b).

        Scenario:
        1. Agent assigned to task T-complete-01 in role "backend"
        2. Task completes (status="done")
        3. No other open/claimed/in_progress tasks in "backend" role
        4. recycle_idle_agents detects idle condition (Case 1: task_already_resolved)
        5. SHUTDOWN signal is sent for agent to exit

        Note: When an agent's task_ids contain only resolved tasks, Case 1 triggers
        (task_already_resolved) before Case 4 (role_drained_rebalance).
        """
        orch = _make_orch(tmp_path)

        # Setup: one agent with assigned task, task is now completed
        session = _make_session(["T-complete-01"], session_id="s-rebal-01")
        orch._agents["s-rebal-01"] = session

        # Task snapshot: the task is done (completed)
        completed_task = _make_task(id="T-complete-01", status="done")
        tasks_snapshot = {
            "done": [completed_task],
            "failed": [],
            "open": [],  # No more open tasks in backend role
            "claimed": [],
            "blocked": [],
        }

        # Call recycle_idle_agents — should detect idle and send SHUTDOWN
        recycle_idle_agents(orch, tasks_snapshot)

        # Verify SHUTDOWN signal was written
        orch._signal_mgr.write_shutdown.assert_called_once()
        call_kwargs = orch._signal_mgr.write_shutdown.call_args
        assert call_kwargs.args[0] == "s-rebal-01"

        # Verify idle timestamp was recorded (for grace period tracking)
        assert "s-rebal-01" in orch._idle_shutdown_ts

    def test_agent_not_shutdown_when_role_has_open_tasks(self, tmp_path: Path) -> None:
        """Agent is NOT recycled when role still has open tasks.

        Scenario:
        1. Agent with no assigned tasks (empty task_ids)
        2. Role "backend" has open task T-open-01 still pending
        3. recycle_idle_agents should NOT send SHUTDOWN (role not drained)
           because active_per_role["backend"] = 1 (T-open-01 is open)

        Note: This tests Case 4 (role_drained_rebalance) detection.
        """
        orch = _make_orch(tmp_path)

        # Agent with no assigned tasks
        session = _make_session([], session_id="s-notready-01")
        orch._agents["s-notready-01"] = session

        # Role has pending work
        open_task = _make_task(id="T-open-01", role="backend", status="open")

        tasks_snapshot = {
            "done": [],
            "failed": [],
            "open": [open_task],  # Role still has active work
            "claimed": [],
            "blocked": [],
        }

        recycle_idle_agents(orch, tasks_snapshot)

        # Verify NO SHUTDOWN signal (role not drained — still has active tasks)
        orch._signal_mgr.write_shutdown.assert_not_called()

    def test_agent_shutdown_when_claimed_task_completed(self, tmp_path: Path) -> None:
        """Agent is recycled when claimed task completes and no other tasks in role.

        Scenario:
        1. Agent assigned to T-claimed-01 (claimed/in_progress)
        2. Task completes (status="done")
        3. No other open/claimed/in_progress in role
        4. Should trigger SHUTDOWN for rebalancing
        """
        orch = _make_orch(tmp_path)

        session = _make_session(["T-claimed-01"], session_id="s-claimed-01")
        orch._agents["s-claimed-01"] = session

        # Task transitioned from claimed to done
        done_task = _make_task(id="T-claimed-01", status="done")

        tasks_snapshot = {
            "done": [done_task],
            "failed": [],
            "open": [],
            "claimed": [],
            "blocked": [],
        }

        recycle_idle_agents(orch, tasks_snapshot)

        # SHUTDOWN should be sent (role drained)
        orch._signal_mgr.write_shutdown.assert_called_once()

    def test_multiple_agents_same_role_one_gets_shutdown_one_stays(self, tmp_path: Path) -> None:
        """When multiple agents in same role: agent with no tasks gets SHUTDOWN.

        Scenario:
        1. Two backend agents: s-idle-01 (no tasks), s-active-02 (task in progress)
        2. Both have tasks or are assigned, but s-idle-01 has empty task_ids
        3. Role "backend" has no OPEN tasks (T-active-02 is in_progress)
        4. s-idle-01 triggers Case 3: role_queue_empty_no_tasks → SHUTDOWN
        5. s-active-02 has assigned task → Case 1 doesn't trigger → not SHUTDOWN

        Note: This tests Case 3 (role_queue_empty_no_tasks) — agent with no
        tasks gets SHUTDOWN when role has no open/pending tasks.
        """
        orch = _make_orch(tmp_path)

        # Two agents in same role
        idle_session = _make_session([], session_id="s-idle-01")  # No tasks
        active_session = _make_session(["T-in-progress"], session_id="s-active-02")
        orch._agents["s-idle-01"] = idle_session
        orch._agents["s-active-02"] = active_session

        # One task in progress (role has no OPEN tasks)
        in_progress_task = _make_task(id="T-in-progress", status="in_progress")

        tasks_snapshot = {
            "done": [],
            "failed": [],
            "open": [],  # No open tasks — role queue is empty
            "claimed": [],
            "blocked": [],
            "in_progress": [in_progress_task],
        }

        recycle_idle_agents(orch, tasks_snapshot)

        # s-idle-01 should get SHUTDOWN (Case 3: no tasks + no open queue)
        # s-active-02 should NOT get SHUTDOWN (still has active task)
        call_count = orch._signal_mgr.write_shutdown.call_count
        assert call_count == 1, f"Expected 1 SHUTDOWN call, got {call_count}"

        # Verify it was s-idle-01 that got the signal
        call_args = orch._signal_mgr.write_shutdown.call_args
        assert call_args.args[0] == "s-idle-01"

    def test_agent_with_no_tasks_and_empty_role_gets_shutdown(self, tmp_path: Path) -> None:
        """Agent with empty task_ids and empty role queue gets SHUTDOWN.

        Scenario:
        1. Agent assigned [] (no tasks)
        2. Role "backend" has no open/claimed/in_progress tasks
        3. Triggers Case 3: role_queue_empty_no_tasks AND Case 4: role_drained
        4. Should send SHUTDOWN
        """
        orch = _make_orch(tmp_path)

        # Agent with no tasks
        session = _make_session([], session_id="s-notasks-01")
        orch._agents["s-notasks-01"] = session

        # Empty role queue
        tasks_snapshot = {
            "done": [],
            "failed": [],
            "open": [],
            "claimed": [],
            "blocked": [],
        }

        recycle_idle_agents(orch, tasks_snapshot)

        # SHUTDOWN should be sent
        orch._signal_mgr.write_shutdown.assert_called_once()
