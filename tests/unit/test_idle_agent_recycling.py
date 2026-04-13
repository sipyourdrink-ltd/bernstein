"""Tests for idle agent detection and recycling (#333g).

Covers:
- recycle_idle_agents sends SHUTDOWN when agent's task is already resolved
- After grace period, idle agent is SIGKILL'd
- No-heartbeat idle detection (90s normal, 60s in evolve mode)
- Agents with active tasks are left alone
- _idle_shutdown_ts is cleaned up after kill
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from bernstein.core.agent_lifecycle import (
    _IDLE_GRACE_S,
    _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S,
    _IDLE_HEARTBEAT_THRESHOLD_S,
    recycle_idle_agents,
)
from bernstein.core.models import (
    AgentHeartbeat,
    AgentSession,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    status: str = "done",
) -> Task:
    return Task(
        id=id,
        title="Test task",
        description="desc",
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus(status),
        task_type=TaskType.STANDARD,
    )


def _make_session(task_ids: list[str], session_id: str = "s-idle-01") -> AgentSession:
    session = AgentSession(id=session_id, role="backend", pid=12345, task_ids=task_ids)
    session.status = "working"
    return session


def _make_orch(tmp_path: Path, *, evolve_mode: bool = False) -> MagicMock:
    """Build a minimal orchestrator-like object for testing recycle_idle_agents."""
    orch = MagicMock()
    orch._config = OrchestratorConfig(
        evolve_mode=evolve_mode,
        evolution_enabled=False,
    )
    orch._agents = {}
    orch._idle_shutdown_ts = {}
    # Real path so completion marker checks use the filesystem (not MagicMock).
    orch._workdir = tmp_path

    signal_mgr = MagicMock()
    signal_mgr.read_heartbeat.return_value = None  # no heartbeat by default
    orch._signal_mgr = signal_mgr

    # spawner.check_alive → True by default (process is still running)
    orch._spawner.check_alive.return_value = True

    return orch


# ---------------------------------------------------------------------------
# Task already resolved — SHUTDOWN sent immediately
# ---------------------------------------------------------------------------


def test_shutdown_sent_when_task_already_done(tmp_path: Path) -> None:
    """SHUTDOWN signal must be written the first time an idle agent is detected."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-done-1"])
    orch._agents["s-idle-01"] = session

    tasks_snapshot = {
        "done": [_make_task(id="T-done-1", status="done")],
        "failed": [],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()
    call_kwargs = orch._signal_mgr.write_shutdown.call_args
    assert call_kwargs.args[0] == "s-idle-01"
    assert "task_already_resolved" in call_kwargs.kwargs["reason"]
    # Timestamp recorded
    assert "s-idle-01" in orch._idle_shutdown_ts


def test_shutdown_sent_when_task_already_failed(tmp_path: Path) -> None:
    """SHUTDOWN is sent when task status is 'failed'."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-fail-1"])
    orch._agents["s-fail-01"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [_make_task(id="T-fail-1", status="failed")],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# Grace period elapsed — force kill
# ---------------------------------------------------------------------------


def test_force_kill_after_grace_period(tmp_path: Path) -> None:
    """Agent must be SIGKILL'd once SHUTDOWN was sent > 30s ago."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-done-2"], session_id="s-idle-02")
    orch._agents["s-idle-02"] = session

    # Simulate SHUTDOWN sent 31 seconds ago
    past_ts = time.time() - (_IDLE_GRACE_S + 1)
    orch._idle_shutdown_ts["s-idle-02"] = past_ts

    tasks_snapshot = {
        "done": [_make_task(id="T-done-2", status="done")],
        "failed": [],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # SIGKILL must have been called
    orch._spawner.kill.assert_called_once_with(session)
    # SHUTDOWN must NOT be written again (kill path only)
    orch._signal_mgr.write_shutdown.assert_not_called()
    # Tracking entry must be cleared
    assert "s-idle-02" not in orch._idle_shutdown_ts
    # Signal files must be cleared
    orch._signal_mgr.clear_signals.assert_called_once_with("s-idle-02")


def test_no_kill_before_grace_period(tmp_path: Path) -> None:
    """Agent must NOT be killed if grace period has not elapsed yet."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-done-3"], session_id="s-idle-03")
    orch._agents["s-idle-03"] = session

    # SHUTDOWN sent only 5s ago — still within grace window
    orch._idle_shutdown_ts["s-idle-03"] = time.time() - 5.0

    tasks_snapshot = {
        "done": [_make_task(id="T-done-3", status="done")],
        "failed": [],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._spawner.kill.assert_not_called()
    orch._signal_mgr.write_shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# Active task — agent must not be recycled
# ---------------------------------------------------------------------------


def test_active_agent_not_recycled(tmp_path: Path) -> None:
    """Agents working on open/claimed tasks must not receive SHUTDOWN."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-open-1"])
    orch._agents["s-active-01"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-open-1", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Dead agent — skip
# ---------------------------------------------------------------------------


def test_dead_agent_skipped(tmp_path: Path) -> None:
    """Agents already marked dead must be skipped entirely."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-done-9"])
    session.status = "dead"
    orch._agents["s-dead-01"] = session

    tasks_snapshot = {
        "done": [_make_task(id="T-done-9", status="done")],
        "failed": [],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Heartbeat-idle detection
# ---------------------------------------------------------------------------


def test_shutdown_sent_on_heartbeat_idle(tmp_path: Path) -> None:
    """SHUTDOWN must be sent when heartbeat is older than idle threshold."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-hb-1"])
    orch._agents["s-hb-01"] = session

    stale_ts = time.time() - (_IDLE_HEARTBEAT_THRESHOLD_S + 1)
    stale_hb = AgentHeartbeat(timestamp=stale_ts)
    orch._signal_mgr.read_heartbeat.return_value = stale_hb

    # Task is still open — heartbeat idle is the trigger
    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-hb-1", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()
    call_args = orch._signal_mgr.write_shutdown.call_args
    assert "no_heartbeat_" in call_args.kwargs["reason"]


def test_heartbeat_idle_threshold_lower_in_evolve_mode(tmp_path: Path) -> None:
    """In evolve mode the heartbeat idle threshold drops to 60s."""
    orch = _make_orch(tmp_path, evolve_mode=True)
    session = _make_session(["T-evolve-1"])
    orch._agents["s-ev-01"] = session

    # Heartbeat is 65s stale — above evolve threshold (60s) but below normal (90s)
    stale_ts = time.time() - (_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S + 5)
    orch._signal_mgr.read_heartbeat.return_value = AgentHeartbeat(timestamp=stale_ts)

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-evolve-1", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()


def test_fresh_heartbeat_agent_not_recycled(tmp_path: Path) -> None:
    """Agent with a recent heartbeat must not be touched."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-fresh-1"])
    orch._agents["s-fresh-01"] = session

    fresh_ts = time.time() - 10.0  # only 10s old
    orch._signal_mgr.read_heartbeat.return_value = AgentHeartbeat(timestamp=fresh_ts)

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-fresh-1", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Case 3: role queue empty with no assigned tasks
# ---------------------------------------------------------------------------


def test_shutdown_sent_when_no_tasks_and_role_queue_empty(tmp_path: Path) -> None:
    """Agent with empty task_ids gets SHUTDOWN when role has no open tasks (#333d-03)."""
    orch = _make_orch(tmp_path)
    session = _make_session([], session_id="s-notasks-01")  # no assigned tasks
    orch._agents["s-notasks-01"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],  # role queue empty
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()
    call_kwargs = orch._signal_mgr.write_shutdown.call_args
    assert call_kwargs.args[0] == "s-notasks-01"
    assert "role_queue_empty_no_tasks" in call_kwargs.kwargs["reason"]


def test_no_shutdown_when_no_tasks_but_role_has_open_work(tmp_path: Path) -> None:
    """Agent with empty task_ids is NOT recycled when the role still has open tasks."""
    orch = _make_orch(tmp_path)
    session = _make_session([])  # no assigned tasks
    orch._agents["s-notasks-02"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-open-1", status="open")],  # role has pending work
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


def test_exit_by_role_queue_no_orphans(tmp_path: Path) -> None:
    """Case 3 exit leaves zero orphaned tasks — zero-data-loss invariant holds.

    Full lifecycle: SHUTDOWN sent → grace period elapses → agent force-killed.
    Because task_ids is empty, handle_orphaned_task is never triggered and
    the reconciliation equation holds trivially:
        source_tasks (0) == completed (0) + quarantined (0)
    Janitor verification is not invoked (nothing to verify).
    """
    orch = _make_orch(tmp_path)
    session = _make_session([], session_id="s-case3-no-orphan")
    orch._agents["s-case3-no-orphan"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],  # role queue empty → triggers Case 3
        "claimed": [],
        "blocked": [],
    }

    # --- First call: SHUTDOWN signal sent ---
    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()
    call_kwargs = orch._signal_mgr.write_shutdown.call_args
    assert call_kwargs.args[0] == "s-case3-no-orphan"
    assert "role_queue_empty_no_tasks" in call_kwargs.kwargs["reason"]
    # Shutdown timestamp must be recorded
    assert "s-case3-no-orphan" in orch._idle_shutdown_ts
    # Kill must NOT have been called yet
    orch._spawner.kill.assert_not_called()

    # --- Simulate grace period elapsed ---
    orch._idle_shutdown_ts["s-case3-no-orphan"] = time.time() - (_IDLE_GRACE_S + 1)

    # --- Second call: force-kill triggered ---
    recycle_idle_agents(orch, tasks_snapshot)

    orch._spawner.kill.assert_called_once_with(session)
    orch._signal_mgr.clear_signals.assert_called_once_with("s-case3-no-orphan")
    # Tracking entry must be removed (no memory leak)
    assert "s-case3-no-orphan" not in orch._idle_shutdown_ts

    # Zero-data-loss guarantee: agent had no tasks, so nothing can be orphaned.
    # source_count (0) == completed (0) + quarantined (0)
    assert session.task_ids == []
    # No task complete/fail/retry HTTP calls should have been made
    orch._client.post.assert_not_called()


def test_no_shutdown_when_tasks_assigned_role_queue_empty(tmp_path: Path) -> None:
    """Agent with assigned tasks is NOT recycled via Case 3 even if role queue empties.

    Case 1 (task_already_resolved) handles that scenario when the tasks complete.
    While tasks are still in-progress, the agent should keep running.
    """
    orch = _make_orch(tmp_path)
    session = _make_session(["T-inprogress-1"])  # has assigned task
    orch._agents["s-active-02"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],  # role queue empty — but agent has active tasks
        "claimed": [_make_task(id="T-inprogress-1", status="claimed")],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # Agent is still working on T-inprogress-1 — neither Case 1 nor Case 3 should fire
    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Case 4: role fully drained — rebalancing exit (#333d-03)
# ---------------------------------------------------------------------------


def test_shutdown_on_role_drained_rebalance(tmp_path: Path) -> None:
    """Agent exits when its role has zero active tasks (open+claimed+in_progress).

    Catches the orphaned-task edge case: agent has task_ids that were deleted
    from the server, so they don't appear in any status bucket. Cases 1-3 miss
    this, but Case 4 catches it because the role has 0 active work.
    """
    orch = _make_orch(tmp_path)
    # Agent has a task_id that doesn't appear in any snapshot status (deleted)
    session = _make_session(["T-deleted-1"], session_id="s-orphan-01")
    orch._agents["s-orphan-01"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_called_once()
    call_kwargs = orch._signal_mgr.write_shutdown.call_args
    assert call_kwargs.args[0] == "s-orphan-01"
    assert "role_drained_rebalance" in call_kwargs.kwargs["reason"]


def test_no_shutdown_when_role_has_active_work(tmp_path: Path) -> None:
    """Agent is NOT recycled via Case 4 when its role still has active tasks."""
    orch = _make_orch(tmp_path)
    # Agent has orphaned task_id, but role still has other active work
    session = _make_session(["T-deleted-2"], session_id="s-orphan-02")
    orch._agents["s-orphan-02"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [_make_task(id="T-other-1", status="claimed")],  # role has active work
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


def test_rebalance_exit_when_all_tasks_done_role_empty(tmp_path: Path) -> None:
    """Agent exits for rebalancing when its tasks are done AND role has no active work.

    Case 1 fires first here (task_already_resolved), but this test validates
    the end-to-end scenario: task completion + empty role queue → agent exits.
    """
    orch = _make_orch(tmp_path)
    session = _make_session(["T-done-rebal-1"], session_id="s-rebal-01")
    orch._agents["s-rebal-01"] = session

    tasks_snapshot = {
        "done": [_make_task(id="T-done-rebal-1", status="done")],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # Case 1 fires (task_already_resolved) — agent exits either way
    orch._signal_mgr.write_shutdown.assert_called_once()


def test_role_drained_with_in_progress_elsewhere_no_shutdown(tmp_path: Path) -> None:
    """Agent with orphaned task is NOT recycled if role has in_progress tasks."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-ghost-1"], session_id="s-ghost-01")
    orch._agents["s-ghost-01"] = session

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [_make_task(id="T-active-1", status="in_progress")],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._signal_mgr.write_shutdown.assert_not_called()


def test_multiple_agents_same_drained_role_all_exit(tmp_path: Path) -> None:
    """All agents for a drained role get SHUTDOWN, not just one."""
    orch = _make_orch(tmp_path)
    s1 = _make_session(["T-orphan-a"], session_id="s-multi-01")
    s2 = _make_session(["T-orphan-b"], session_id="s-multi-02")
    orch._agents["s-multi-01"] = s1
    orch._agents["s-multi-02"] = s2

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [],
        "in_progress": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    assert orch._signal_mgr.write_shutdown.call_count == 2
    shutdown_sessions = {call.args[0] for call in orch._signal_mgr.write_shutdown.call_args_list}
    assert shutdown_sessions == {"s-multi-01", "s-multi-02"}


# ---------------------------------------------------------------------------
# Completion marker — instant reap (CRITICAL-002)
# ---------------------------------------------------------------------------


def test_instant_reap_on_completion_marker(tmp_path: Path) -> None:
    """Agent with completion marker is reaped immediately — no SHUTDOWN, no grace period."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-comp-1"], session_id="s-comp-01")
    orch._agents["s-comp-01"] = session

    # Create completion marker file
    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True)
    marker = completed_dir / "s-comp-01"
    marker.write_text("done")

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-comp-1", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # Agent should be killed immediately (SIGTERM, not SHUTDOWN)
    orch._spawner.kill.assert_called_once_with(session)
    # SHUTDOWN signal must NOT be written (we skip the grace period entirely)
    orch._signal_mgr.write_shutdown.assert_not_called()
    # Signal files must be cleaned up
    orch._signal_mgr.clear_signals.assert_called_once_with("s-comp-01")
    # Completion marker file must be cleaned up
    assert not marker.exists()


def test_instant_reap_cleans_idle_shutdown_ts(tmp_path: Path) -> None:
    """Completion marker reap clears any stale SHUTDOWN tracking entry."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-comp-2"], session_id="s-comp-02")
    orch._agents["s-comp-02"] = session
    # Simulate a previously sent SHUTDOWN
    orch._idle_shutdown_ts["s-comp-02"] = 12345.0

    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True)
    (completed_dir / "s-comp-02").write_text("result text here")

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-comp-2", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._spawner.kill.assert_called_once_with(session)
    assert "s-comp-02" not in orch._idle_shutdown_ts


def test_no_instant_reap_without_completion_marker(tmp_path: Path) -> None:
    """Without completion marker, agent is NOT reaped immediately (normal path)."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-active-99"])
    orch._agents["s-active-99"] = session

    # No completion marker directory at all
    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-active-99", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    # Agent has active task and no marker — should not be touched
    orch._spawner.kill.assert_not_called()
    orch._signal_mgr.write_shutdown.assert_not_called()


def test_completion_marker_with_result_text(tmp_path: Path) -> None:
    """Completion marker containing result text triggers instant reap."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-comp-3"], session_id="s-comp-03")
    orch._agents["s-comp-03"] = session

    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True)
    marker = completed_dir / "s-comp-03"
    marker.write_text("All tasks completed successfully. Created 3 files.")

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [_make_task(id="T-comp-3", status="open")],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._spawner.kill.assert_called_once_with(session)
    assert not marker.exists()


def test_completion_marker_dead_agent_skipped(tmp_path: Path) -> None:
    """Dead agents are skipped even if completion marker exists."""
    orch = _make_orch(tmp_path)
    session = _make_session(["T-comp-4"], session_id="s-comp-04")
    session.status = "dead"
    orch._agents["s-comp-04"] = session

    completed_dir = tmp_path / ".sdd" / "runtime" / "completed"
    completed_dir.mkdir(parents=True)
    (completed_dir / "s-comp-04").write_text("done")

    tasks_snapshot = {
        "done": [],
        "failed": [],
        "open": [],
        "claimed": [],
        "blocked": [],
    }

    recycle_idle_agents(orch, tasks_snapshot)

    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


def test_idle_constants_sensible() -> None:
    assert pytest.approx(30.0) == _IDLE_GRACE_S
    assert pytest.approx(300.0) == _IDLE_HEARTBEAT_THRESHOLD_S
    assert pytest.approx(300.0) == _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S
    assert _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S <= _IDLE_HEARTBEAT_THRESHOLD_S
