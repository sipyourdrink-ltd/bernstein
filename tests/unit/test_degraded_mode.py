"""Tests for ORCH-004: Degraded mode when task server is unreachable."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.degraded_mode import (
    DegradedModeConfig,
    DegradedModeManager,
    probe_server_health,
)

# ---------------------------------------------------------------------------
# DegradedModeConfig
# ---------------------------------------------------------------------------


class TestDegradedModeConfig:
    """Tests for config defaults."""

    def test_defaults(self) -> None:
        config = DegradedModeConfig()
        assert config.enter_after_failures == 3
        assert config.exit_after_successes == 2
        assert config.probe_base_delay_s == pytest.approx(5.0)
        assert config.probe_max_delay_s == pytest.approx(60.0)
        assert config.max_degraded_ticks == 0


# ---------------------------------------------------------------------------
# Entering degraded mode
# ---------------------------------------------------------------------------


class TestEnterDegradedMode:
    """Tests for entering degraded mode."""

    def test_not_degraded_initially(self) -> None:
        mgr = DegradedModeManager()
        assert mgr.is_degraded is False

    def test_enters_after_threshold_failures(self) -> None:
        config = DegradedModeConfig(enter_after_failures=3)
        mgr = DegradedModeManager(config=config)
        assert mgr.record_server_failure() is False
        assert mgr.record_server_failure() is False
        assert mgr.record_server_failure() is True  # enters
        assert mgr.is_degraded is True

    def test_success_resets_failure_counter(self) -> None:
        config = DegradedModeConfig(enter_after_failures=3)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        mgr.record_server_failure()
        mgr.record_server_success()
        # Counter reset; need 3 more failures
        mgr.record_server_failure()
        mgr.record_server_failure()
        assert mgr.is_degraded is False

    def test_single_failure_threshold(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1)
        mgr = DegradedModeManager(config=config)
        assert mgr.record_server_failure() is True
        assert mgr.is_degraded is True


# ---------------------------------------------------------------------------
# Exiting degraded mode
# ---------------------------------------------------------------------------


class TestExitDegradedMode:
    """Tests for exiting degraded mode."""

    def test_exits_after_threshold_successes(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1, exit_after_successes=2)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        assert mgr.is_degraded is True
        assert mgr.record_server_success() is False  # need 2
        assert mgr.record_server_success() is True  # exits
        assert mgr.is_degraded is False

    def test_failure_during_recovery_resets_successes(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1, exit_after_successes=2)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        assert mgr.is_degraded is True
        mgr.record_server_success()
        # Failure resets success counter
        mgr.record_server_failure()
        mgr.record_server_success()
        assert mgr.is_degraded is True  # still degraded


# ---------------------------------------------------------------------------
# Spawning control
# ---------------------------------------------------------------------------


class TestSpawningControl:
    """Tests for spawn blocking in degraded mode."""

    def test_allows_spawn_when_not_degraded(self) -> None:
        mgr = DegradedModeManager()
        assert mgr.should_allow_spawn() is True

    def test_blocks_spawn_when_degraded(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        assert mgr.should_allow_spawn() is False


# ---------------------------------------------------------------------------
# Server probing
# ---------------------------------------------------------------------------


class TestServerProbing:
    """Tests for server probe timing."""

    def test_always_allows_probe_when_not_degraded(self) -> None:
        mgr = DegradedModeManager()
        assert mgr.should_probe_server() is True

    def test_probe_respects_backoff(self) -> None:
        config = DegradedModeConfig(
            enter_after_failures=1,
            probe_base_delay_s=100.0,  # large enough that we won't reach it
        )
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        mgr.record_probe_attempt()
        # Immediately after a probe, should not probe again
        assert mgr.should_probe_server() is False


# ---------------------------------------------------------------------------
# Orchestrator stop
# ---------------------------------------------------------------------------


class TestOrchestratorStop:
    """Tests for max degraded ticks."""

    def test_no_stop_with_unlimited_ticks(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1, max_degraded_ticks=0)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        for _ in range(100):
            mgr.record_server_failure()
        assert mgr.should_stop_orchestrator() is False

    def test_stops_after_max_ticks(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1, max_degraded_ticks=5)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        # Simulate ticks in degraded mode
        for _ in range(5):
            mgr.record_server_failure()
        assert mgr.should_stop_orchestrator() is True


# ---------------------------------------------------------------------------
# WAL preservation
# ---------------------------------------------------------------------------


class TestWALPreservation:
    """Tests for WAL writes during degraded mode."""

    def test_preserves_state_with_wal_writer(self, tmp_path: Path) -> None:
        from bernstein.core.wal import WALWriter

        sdd_dir = tmp_path / ".sdd"
        sdd_dir.mkdir(parents=True)
        wal_writer = WALWriter("test-run", sdd_dir)
        config = DegradedModeConfig(enter_after_failures=1)
        mgr = DegradedModeManager(config=config, wal_writer=wal_writer)
        mgr.record_server_failure()
        mgr.preserve_state_to_wal(pending_tasks=[{"id": "T-001"}])
        # WAL file should exist and have entries
        wal_path = sdd_dir / "runtime" / "wal" / "test-run.wal.jsonl"
        assert wal_path.exists()

    def test_no_crash_without_wal_writer(self) -> None:
        config = DegradedModeConfig(enter_after_failures=1)
        mgr = DegradedModeManager(config=config)
        mgr.record_server_failure()
        # Should not raise
        mgr.preserve_state_to_wal()


# ---------------------------------------------------------------------------
# probe_server_health
# ---------------------------------------------------------------------------


class TestProbeServerHealth:
    """Tests for the health probe function."""

    def test_healthy_server(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp
        assert probe_server_health(mock_client, "http://localhost:8052") is True

    def test_unhealthy_server(self) -> None:
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client.get.return_value = mock_resp
        assert probe_server_health(mock_client, "http://localhost:8052") is False

    def test_connection_error(self) -> None:
        mock_client = MagicMock()
        mock_client.get.side_effect = ConnectionError("refused")
        assert probe_server_health(mock_client, "http://localhost:8052") is False
