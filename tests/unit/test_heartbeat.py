"""Focused tests for heartbeat and stall detection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.heartbeat import check_stale_agents, check_stalled_tasks
from bernstein.core.models import AgentSession, ModelConfig, ProgressSnapshot


def _session(task_id: str) -> AgentSession:
    """Create a deterministic live agent session for heartbeat tests."""
    return AgentSession(
        id="A-1",
        role="backend",
        task_ids=[task_id],
        status="working",
        spawn_ts=100.0,
        model_config=ModelConfig("sonnet", "high"),
    )


def test_check_stale_agents_writes_wakeup_after_sixty_seconds() -> None:
    """check_stale_agents emits a WAKEUP signal once the heartbeat age reaches 60 seconds."""
    session = _session("T-1")
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
    )
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=130.0)

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=200.0):
        check_stale_agents(orch)

    orch._signal_mgr.write_wakeup.assert_called_once()
    orch._signal_mgr.write_shutdown.assert_not_called()


def test_check_stale_agents_writes_shutdown_after_one_hundred_twenty_seconds() -> None:
    """check_stale_agents emits SHUTDOWN once heartbeat age reaches 120 seconds."""
    session = _session("T-1")
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
    )
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=70.0)

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=200.0):
        check_stale_agents(orch)

    orch._signal_mgr.write_shutdown.assert_called_once()


def test_check_stalled_tasks_writes_wakeup_after_three_identical_snapshots() -> None:
    """check_stalled_tasks sends WAKEUP after the third identical progress snapshot."""
    session = _session("T-1")
    snapshot = {"timestamp": 10.0, "files_changed": 1, "tests_passing": 2, "errors": 0, "last_file": "src/a.py"}
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _config=SimpleNamespace(server_url="http://server"),
        _client=MagicMock(),
        _last_snapshot_ts={},
        _last_snapshot={
            "T-1": ProgressSnapshot(timestamp=9.0, files_changed=1, tests_passing=2, errors=0, last_file="src/a.py")
        },
        _stall_counts={"T-1": 2},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
    )
    orch._client.get.return_value.json.return_value = [snapshot]
    orch._client.get.return_value.raise_for_status.return_value = None

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=220.0):
        check_stalled_tasks(orch)

    orch._signal_mgr.write_wakeup.assert_called_once()


def test_check_stalled_tasks_kills_agent_after_seven_identical_snapshots() -> None:
    """check_stalled_tasks asks the spawner to kill the agent after seven identical snapshots."""
    session = _session("T-1")
    snapshot = {"timestamp": 10.0, "files_changed": 1, "tests_passing": 2, "errors": 0, "last_file": "src/a.py"}
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _config=SimpleNamespace(server_url="http://server"),
        _client=MagicMock(),
        _last_snapshot_ts={},
        _last_snapshot={
            "T-1": ProgressSnapshot(timestamp=9.0, files_changed=1, tests_passing=2, errors=0, last_file="src/a.py")
        },
        _stall_counts={"T-1": 6},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
    )
    orch._client.get.return_value.json.return_value = [snapshot]
    orch._client.get.return_value.raise_for_status.return_value = None

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=220.0):
        check_stalled_tasks(orch)

    orch._spawner.kill.assert_called_once_with(session)
    assert orch._stall_counts["T-1"] == 0
