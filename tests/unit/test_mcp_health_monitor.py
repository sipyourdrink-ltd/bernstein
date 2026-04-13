"""Tests for MCP server health monitoring with auto-restart (MCP-001)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.mcp_health_monitor import (
    BACKOFF_MULTIPLIER,
    DEFAULT_INITIAL_BACKOFF,
    DEFAULT_MAX_BACKOFF,
    DEFAULT_MAX_RESTARTS,
    HealthProbeResult,
    McpHealthMonitor,
)
from bernstein.core.mcp_manager import MCPManager, MCPServerConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager_with_mock(
    *names: str,
) -> tuple[MCPManager, MagicMock]:
    """Create an MCPManager with mocked Popen for the given server names."""
    configs = [MCPServerConfig(name=n, command=["echo"]) for n in names]
    mgr = MCPManager(configs)
    mock_proc = MagicMock()
    mock_proc.pid = 100
    mock_proc.poll.return_value = None
    with patch("bernstein.core.protocols.mcp_manager.subprocess.Popen", return_value=mock_proc):
        mgr.start_all()
    return mgr, mock_proc


# ---------------------------------------------------------------------------
# HealthProbeResult
# ---------------------------------------------------------------------------


class TestHealthProbeResult:
    """Tests for HealthProbeResult dataclass."""

    def test_defaults(self) -> None:
        r = HealthProbeResult(ts=1.0, server_name="test", alive=True)
        assert r.restarted is False
        assert r.restart_success is None
        assert r.given_up is False

    def test_to_dict(self) -> None:
        r = HealthProbeResult(
            ts=1.0,
            server_name="test",
            alive=False,
            restarted=True,
            restart_success=False,
            given_up=False,
        )
        d = r.to_dict()
        assert d["server_name"] == "test"
        assert d["alive"] is False
        assert d["restarted"] is True
        assert d["restart_success"] is False


# ---------------------------------------------------------------------------
# McpHealthMonitor — probe_once
# ---------------------------------------------------------------------------


class TestProbeOnce:
    """Tests for synchronous probe_once method."""

    def test_probe_alive_server(self) -> None:
        mgr, _mock_proc = _make_manager_with_mock("server-a")
        monitor = McpHealthMonitor(mgr)

        results = monitor.probe_once()
        assert len(results) == 1
        assert results[0].alive is True
        assert results[0].restarted is False

    def test_probe_dead_server_attempts_restart(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("dying")
        monitor = McpHealthMonitor(mgr)

        # Mark server as dead
        mock_proc.poll.return_value = 1

        # Restart will re-call _start_server which uses Popen
        new_proc = MagicMock()
        new_proc.pid = 200
        new_proc.poll.return_value = None
        with patch("bernstein.core.protocols.mcp_manager.subprocess.Popen", return_value=new_proc):
            results = monitor.probe_once()

        assert len(results) == 1
        assert results[0].restarted is True
        assert results[0].restart_success is True

    def test_probe_records_history(self) -> None:
        mgr, _ = _make_manager_with_mock("srv")
        monitor = McpHealthMonitor(mgr)

        monitor.probe_once()
        assert len(monitor.history) == 1
        assert monitor.history[0].server_name == "srv"

    def test_probe_multiple_servers(self) -> None:
        configs = [
            MCPServerConfig(name="a", command=["echo"]),
            MCPServerConfig(name="b", url="http://x", transport="sse"),
        ]
        mgr = MCPManager(configs)
        mock_proc = MagicMock()
        mock_proc.pid = 100
        mock_proc.poll.return_value = None
        with patch("bernstein.core.protocols.mcp_manager.subprocess.Popen", return_value=mock_proc):
            mgr.start_all()

        monitor = McpHealthMonitor(mgr)
        results = monitor.probe_once()
        assert len(results) == 2
        names = {r.server_name for r in results}
        assert names == {"a", "b"}


# ---------------------------------------------------------------------------
# Exponential backoff
# ---------------------------------------------------------------------------


class TestExponentialBackoff:
    """Tests for restart backoff behavior."""

    def test_backoff_increases_on_failure(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("failing")
        monitor = McpHealthMonitor(mgr, initial_backoff=1.0)

        mock_proc.poll.return_value = 1  # dead

        # Restart will also fail (Popen raises)
        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            side_effect=FileNotFoundError("not found"),
        ):
            monitor.probe_once()

        state = monitor.get_restart_state("failing")
        assert state is not None
        assert state.consecutive_failures == 1
        assert state.next_backoff == DEFAULT_INITIAL_BACKOFF * BACKOFF_MULTIPLIER

    def test_backoff_capped_at_max(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("capped")
        monitor = McpHealthMonitor(
            mgr,
            initial_backoff=1.0,
            max_backoff=4.0,
            max_restarts=10,
        )
        mock_proc.poll.return_value = 1

        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            side_effect=FileNotFoundError("not found"),
        ):
            # Probe multiple times to increase backoff
            for _ in range(5):
                monitor.probe_once()

        state = monitor.get_restart_state("capped")
        assert state is not None
        assert state.next_backoff <= 4.0

    def test_reset_on_recovery(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("recovering")
        monitor = McpHealthMonitor(mgr, initial_backoff=1.0)

        # First: server dies, restart fails
        mock_proc.poll.return_value = 1
        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            side_effect=FileNotFoundError("not found"),
        ):
            monitor.probe_once()

        state = monitor.get_restart_state("recovering")
        assert state is not None
        assert state.consecutive_failures == 1

        # Advance last_attempt_ts so backoff has elapsed
        state.last_attempt_ts -= 10.0

        # Then: restart succeeds
        new_proc = MagicMock()
        new_proc.pid = 200
        new_proc.poll.return_value = None
        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            return_value=new_proc,
        ):
            monitor.probe_once()

        state = monitor.get_restart_state("recovering")
        assert state is not None
        assert state.consecutive_failures == 0
        assert state.next_backoff == DEFAULT_INITIAL_BACKOFF


# ---------------------------------------------------------------------------
# Max restarts / give up
# ---------------------------------------------------------------------------


class TestMaxRestarts:
    """Tests for the max restart limit."""

    def test_gives_up_after_max_restarts(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("doomed")
        monitor = McpHealthMonitor(mgr, max_restarts=2, initial_backoff=1.0)

        mock_proc.poll.return_value = 1

        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            side_effect=FileNotFoundError("not found"),
        ):
            # First failure: attempt #1
            results = monitor.probe_once()
            assert results[0].restarted is True
            assert results[0].restart_success is False
            assert results[0].given_up is False

            # Skip backoff
            state = monitor.get_restart_state("doomed")
            assert state is not None
            state.last_attempt_ts -= 100.0

            # Second failure: attempt #2 (reaches max)
            results = monitor.probe_once()
            assert results[0].restarted is True
            assert results[0].restart_success is False
            # Not given_up yet -- that fires on the NEXT check
            assert results[0].given_up is False

        # Next probe sees consecutive_failures >= max_restarts -> given_up
        results = monitor.probe_once()
        assert results[0].given_up is True
        assert results[0].restarted is False

        # Subsequent probes stay given_up
        results = monitor.probe_once()
        assert results[0].given_up is True

    def test_default_max_restarts_is_5(self) -> None:
        assert DEFAULT_MAX_RESTARTS == 5


# ---------------------------------------------------------------------------
# Monitor lifecycle (start/stop)
# ---------------------------------------------------------------------------


class TestMonitorLifecycle:
    """Tests for start/stop background thread."""

    def test_not_running_initially(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr)
        assert monitor.running is False

    def test_start_sets_running(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr, probe_interval=100.0)
        monitor.start()
        assert monitor.running is True
        monitor.stop()

    def test_stop_clears_running(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr, probe_interval=100.0)
        monitor.start()
        monitor.stop()
        assert monitor.running is False

    def test_start_idempotent(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr, probe_interval=100.0)
        monitor.start()
        monitor.start()  # second call is no-op
        assert monitor.running is True
        monitor.stop()

    def test_stop_idempotent(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr, probe_interval=100.0)
        monitor.stop()  # no-op when not running
        assert monitor.running is False


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    """Tests for the status summary."""

    def test_status_alive_server(self) -> None:
        mgr, _ = _make_manager_with_mock("healthy")
        monitor = McpHealthMonitor(mgr)
        monitor.probe_once()

        status = monitor.get_status()
        assert "healthy" in status
        assert status["healthy"]["alive"] is True

    def test_status_dead_server(self) -> None:
        mgr, mock_proc = _make_manager_with_mock("dead")
        monitor = McpHealthMonitor(mgr)

        mock_proc.poll.return_value = 1
        with patch(
            "bernstein.core.mcp_manager.subprocess.Popen",
            side_effect=FileNotFoundError("not found"),
        ):
            monitor.probe_once()

        status = monitor.get_status()
        assert "dead" in status
        assert status["dead"]["alive"] is False
        assert status["dead"]["consecutive_failures"] == 1

    def test_status_empty_manager(self) -> None:
        mgr = MCPManager()
        monitor = McpHealthMonitor(mgr)
        status = monitor.get_status()
        assert status == {}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify default values match spec."""

    def test_probe_interval(self) -> None:
        from bernstein.core.mcp_health_monitor import DEFAULT_PROBE_INTERVAL

        assert pytest.approx(30.0) == DEFAULT_PROBE_INTERVAL

    def test_initial_backoff(self) -> None:
        assert pytest.approx(1.0) == DEFAULT_INITIAL_BACKOFF

    def test_max_backoff(self) -> None:
        assert pytest.approx(30.0) == DEFAULT_MAX_BACKOFF

    def test_backoff_multiplier(self) -> None:
        assert pytest.approx(2.0) == BACKOFF_MULTIPLIER
