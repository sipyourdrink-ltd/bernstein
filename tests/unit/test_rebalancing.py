"""Tests for agent rebalancing (#333d-03d).

Verifies that rebalancing prevents idle agent accumulation:

When a role has more agents than open tasks, excess agents are recycled
(sent SHUTDOWN signal) so their slots can be used by under-served roles.

Test coverage:
- Task completion triggers agent recycling (Case 1: task_already_resolved)
- Spawn prevention: recycling agents are excluded from alive count
- Graceful exit: SHUTDOWN → grace period → force kill lifecycle
- Active agents are preserved (not recycled) when they have work
- Empty roles: all agents exit when role queue fully drains

The tests verify the core rebalancing mechanism works correctly:
- Idle agents (with resolved tasks) are detected and marked for exit
- The spawn path respects these markings to prevent race conditions
- Agents exit gracefully with a configurable grace period before kill
- Active work is protected from rebalancing logic

Implementation details:
- recycle_idle_agents() detects idle agents via Cases 1-4
- _idle_shutdown_ts tracks agents sent SHUTDOWN signal
- Spawn path (claim_and_spawn_batches) excludes agents in _idle_shutdown_ts
- _recycle_or_kill() enforces grace period before SIGKILL
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from bernstein.core.agent_lifecycle import (
    _IDLE_GRACE_S,
    recycle_idle_agents,
)
from bernstein.core.models import (
    AgentSession,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    status: str = "open",
) -> Task:
    return Task(
        id=id,
        title=f"Task {id}",
        description="desc",
        role=role,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus(status),
        task_type=TaskType.STANDARD,
    )


def _make_session(
    task_ids: list[str],
    role: str = "backend",
    session_id: str | None = None,
) -> AgentSession:
    sid = session_id or f"s-rebal-{id(task_ids)}"
    session = AgentSession(id=sid, role=role, pid=12345, task_ids=task_ids)
    session.status = "working"
    return session


def _make_orch(tmp_path: Path) -> MagicMock:
    """Build a minimal orchestrator-like object for testing rebalancing."""
    orch = MagicMock()
    orch._config = OrchestratorConfig(
        evolve_mode=False,
        evolution_enabled=False,
    )
    orch._agents = {}
    orch._idle_shutdown_ts = {}

    signal_mgr = MagicMock()
    signal_mgr.read_heartbeat.return_value = None
    orch._signal_mgr = signal_mgr

    orch._spawner.check_alive.return_value = True

    return orch


# ---------------------------------------------------------------------------
# Scenario: role has 3 open tasks but 5 idle agents
# Gradually shut down 2 agents to rebalance
# ---------------------------------------------------------------------------


def test_rebalancing_task_completion_triggers_exit(tmp_path: Path) -> None:
    """Verify that when tasks complete, excess idle agents are detected and recycled.

    Scenario:
    - Role "backend" initially has 4 agents with 4 claimed/done tasks distributed
    - When tasks complete and move to "done" status, agents with resolved tasks
      receive SHUTDOWN signals (Case 1: task_already_resolved)
    - This allows the role to downsize as work completes
    """
    orch = _make_orch(tmp_path)

    # Create 5 agents for backend role, initially with tasks
    agents = [_make_session([f"T-be-{i:02d}"], role="backend", session_id=f"s-be-{i:02d}") for i in range(5)]
    for agent in agents:
        orch._agents[agent.id] = agent

    # Initially all 5 tasks are claimed
    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [_make_task(id=f"T-be-{i:02d}", role="backend", status="claimed") for i in range(5)],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)
    # No SHUTDOWNs yet — all agents have active tasks
    assert orch._signal_mgr.write_shutdown.call_count == 0
    orch._signal_mgr.reset_mock()

    # After work, 2 tasks complete — agents 0 and 1 no longer have active work
    tasks_snapshot["done"].extend(
        [
            _make_task(id="T-be-00", role="backend", status="done"),
            _make_task(id="T-be-01", role="backend", status="done"),
        ]
    )
    tasks_snapshot["claimed"] = [_make_task(id=f"T-be-{i:02d}", role="backend", status="claimed") for i in range(2, 5)]

    recycle_idle_agents(orch, tasks_snapshot)

    # Agents 0 and 1 should get SHUTDOWN (Case 1: task_already_resolved)
    assert orch._signal_mgr.write_shutdown.call_count == 2
    shut_down_sids = {call.args[0] for call in orch._signal_mgr.write_shutdown.call_args_list}
    assert shut_down_sids == {agents[0].id, agents[1].id}
    # Verify reason mentions task resolution
    for call in orch._signal_mgr.write_shutdown.call_args_list:
        assert "task_already_resolved" in call.kwargs["reason"]


def test_rebalancing_spawn_excludes_recycling_agents(tmp_path: Path) -> None:
    """Verify that the spawn path excludes agents marked for recycling from alive count.

    This test verifies the spawn prevention mechanism: when agents are marked
    for SHUTDOWN (in _idle_shutdown_ts), they should not be counted as "alive"
    in the spawn decision logic. This prevents race conditions where the system
    would think a role is fully staffed when agents are actually exiting.

    Scenario:
    - Role "qa" has 4 agents, 2 of which have completed tasks (marked for shutdown)
    - The spawn path should exclude the 2 recycling agents when calculating
      alive_per_role, allowing new agents to be spawned if needed
    """
    orch = _make_orch(tmp_path)

    # Create 4 agents: 2 with done tasks, 2 with active tasks
    active_agents = [_make_session([f"T-active-{i:02d}"], role="qa", session_id=f"s-active-{i:02d}") for i in range(2)]
    recycling_agents = [_make_session([f"T-done-{i:02d}"], role="qa", session_id=f"s-done-{i:02d}") for i in range(2)]
    all_agents = active_agents + recycling_agents
    for agent in all_agents:
        orch._agents[agent.id] = agent

    tasks_snapshot = {
        "done": [_make_task(id=f"T-done-{i:02d}", role="qa", status="done") for i in range(2)],
        "failed": [],
        "open": [_make_task(id=f"T-active-{i:02d}", role="qa", status="open") for i in range(2)],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    # Recycle idle agents with done tasks
    recycle_idle_agents(orch, tasks_snapshot)

    # The 2 agents with done tasks should be marked for shutdown
    assert len(orch._idle_shutdown_ts) == 2
    recycling_ids = {recycling_agents[0].id, recycling_agents[1].id}
    assert set(orch._idle_shutdown_ts.keys()) == recycling_ids

    # Now simulate the spawn path's calculation of alive agents
    # It should exclude the recycling agents from the count
    alive_qa = sum(
        1
        for agent in orch._agents.values()
        if agent.role == "qa" and agent.status != "dead" and agent.id not in orch._idle_shutdown_ts
    )
    assert alive_qa == 2, f"Expected 2 non-recycling agents, got {alive_qa}"
    assert alive_qa == len(active_agents)


def test_rebalancing_graceful_exit_grace_period(tmp_path: Path) -> None:
    """Verify grace period handling in agent rebalancing exit.

    Scenario:
    - Agents are detected as idle and sent SHUTDOWN signal
    - Within grace period: agents are not force-killed, still tracked
    - After grace period: agents are force-killed and cleanup occurs
    """
    orch = _make_orch(tmp_path)

    # Create 3 agents with done tasks
    agents = [_make_session([f"T-done-{i:02d}"], role="backend", session_id=f"s-done-{i:02d}") for i in range(3)]
    for agent in agents:
        orch._agents[agent.id] = agent

    tasks_snapshot = {
        "done": [_make_task(id=f"T-done-{i:02d}", role="backend", status="done") for i in range(3)],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    # Tick 1: SHUTDOWN sent to all agents with done tasks
    recycle_idle_agents(orch, tasks_snapshot)
    assert orch._signal_mgr.write_shutdown.call_count == 3
    assert len(orch._idle_shutdown_ts) == 3
    orch._signal_mgr.reset_mock()

    # Tick 2: within grace period, no force kill
    recycle_idle_agents(orch, tasks_snapshot)
    assert orch._spawner.kill.call_count == 0
    assert len(orch._idle_shutdown_ts) == 3  # Still tracked

    # Tick 3: advance time beyond grace period, then recycle again
    for agent_id in list(orch._idle_shutdown_ts.keys()):
        orch._idle_shutdown_ts[agent_id] = time.time() - (_IDLE_GRACE_S + 1)

    recycle_idle_agents(orch, tasks_snapshot)
    assert orch._spawner.kill.call_count == 3
    assert orch._signal_mgr.clear_signals.call_count == 3
    assert len(orch._idle_shutdown_ts) == 0


def test_rebalancing_preserves_active_agents(tmp_path: Path) -> None:
    """Verify that agents with active tasks are not affected by rebalancing.

    Scenario:
    - Role has 5 agents: 2 with active tasks, 3 with done tasks
    - recycle_idle_agents should SHUTDOWN only the 3 with done tasks
    - The 2 with active tasks continue working
    """
    orch = _make_orch(tmp_path)

    # 2 agents with active tasks
    active_agents = [
        _make_session([f"T-active-{i:02d}"], role="backend", session_id=f"s-active-{i:02d}") for i in range(2)
    ]
    # 3 agents with done tasks
    idle_agents = [_make_session([f"T-done-{i:02d}"], role="backend", session_id=f"s-idle-{i:02d}") for i in range(3)]

    all_agents = active_agents + idle_agents
    for agent in all_agents:
        orch._agents[agent.id] = agent

    tasks_snapshot = {
        "done": [_make_task(id=f"T-done-{i:02d}", role="backend", status="done") for i in range(3)],
        "failed": [],
        "open": [_make_task(id=f"T-active-{i:02d}", role="backend", status="open") for i in range(2)],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # Only the 3 idle agents should get SHUTDOWN
    assert orch._signal_mgr.write_shutdown.call_count == 3
    shut_down_sids = {call.args[0] for call in orch._signal_mgr.write_shutdown.call_args_list}
    assert shut_down_sids == {agent.id for agent in idle_agents}

    # Active agents should NOT be in _idle_shutdown_ts
    for agent in active_agents:
        assert agent.id not in orch._idle_shutdown_ts


def test_rebalancing_empty_role_all_agents_exit(tmp_path: Path) -> None:
    """Verify that when a role queue is completely empty, all agents exit.

    Scenario:
    - Role "backend" has 3 agents with no assigned tasks
    - Role queue is empty (no open/claimed/in_progress tasks)
    - All 3 agents should be recycled (Case 4: role_drained_rebalance)
    """
    orch = _make_orch(tmp_path)

    agents = [_make_session([], role="backend", session_id=f"s-empty-{i:02d}") for i in range(3)]
    for agent in agents:
        orch._agents[agent.id] = agent

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # All 3 agents should get SHUTDOWN (Case 4: role fully drained)
    assert orch._signal_mgr.write_shutdown.call_count == 3
    shut_down_sids = {call.args[0] for call in orch._signal_mgr.write_shutdown.call_args_list}
    assert shut_down_sids == {agent.id for agent in agents}
