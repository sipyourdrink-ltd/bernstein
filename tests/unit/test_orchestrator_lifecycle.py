"""Tests for orchestrator lifecycle helpers (ORCH-009)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.orchestrator_lifecycle import (
    cleanup_orchestrator,
    drain_before_cleanup,
    reconcile_claimed_tasks,
    save_session_state,
)


class TestDrainBeforeCleanup:
    def test_already_drained(self) -> None:
        orch = MagicMock()
        orch._executor_drained = True
        drain_before_cleanup(orch, timeout_s=1.0)
        orch._executor.shutdown.assert_not_called()

    def test_drains_executor(self) -> None:
        orch = MagicMock()
        orch._executor_drained = False
        orch._agents = {}
        drain_before_cleanup(orch, timeout_s=0.1)
        orch._executor.shutdown.assert_called_once()
        assert orch._executor_drained is True

    def test_waits_for_active_agents(self) -> None:
        orch = MagicMock()
        orch._executor_drained = False
        session = MagicMock()
        session.status = "working"
        orch._agents = {"s1": session}
        orch._spawner.check_alive.return_value = False
        drain_before_cleanup(orch, timeout_s=0.1)
        assert orch._executor_drained is True


class TestSaveSessionState:
    def test_save_on_success(self) -> None:
        orch = MagicMock()
        orch._config.server_url = "http://test"
        resp = MagicMock()
        resp.json.return_value = [
            {"id": "T-1", "status": "done"},
            {"id": "T-2", "status": "claimed"},
        ]
        orch._client.get.return_value = resp
        orch._cost_tracker.spent_usd = 1.5

        with patch("bernstein.core.session.save_session"):
            save_session_state(orch)
            # Should not raise

    def test_handles_exception_gracefully(self) -> None:
        orch = MagicMock()
        orch._config.server_url = "http://test"
        orch._client.get.side_effect = Exception("network error")
        # Should not raise
        save_session_state(orch)


class TestReconcileClaimedTasks:
    def test_unclaims_orphaned_tasks(self) -> None:
        orch = MagicMock()
        orch._config.server_url = "http://test"
        orch._task_to_session = {}
        resp = MagicMock()
        resp.json.return_value = [
            {"id": "T-1", "title": "orphan task"},
        ]
        orch._client.get.return_value = resp

        count = reconcile_claimed_tasks(orch)
        assert count == 1
        orch._client.post.assert_called_once()

    def test_skips_known_tasks(self) -> None:
        orch = MagicMock()
        orch._config.server_url = "http://test"
        orch._task_to_session = {"T-1": "agent-1"}
        resp = MagicMock()
        resp.json.return_value = [
            {"id": "T-1", "title": "known task"},
        ]
        orch._client.get.return_value = resp

        count = reconcile_claimed_tasks(orch)
        assert count == 0

    def test_handles_server_error(self) -> None:
        orch = MagicMock()
        orch._config.server_url = "http://test"
        orch._client.get.side_effect = Exception("timeout")

        count = reconcile_claimed_tasks(orch)
        assert count == 0


class TestCleanupOrchestrator:
    def test_cleanup_calls_save_state(self) -> None:
        orch = MagicMock()
        orch._audit_mode = False
        orch._audit_log = None
        orch._heartbeat_client = None
        orch._pending_ruff_future = None
        orch._pending_test_future = None
        orch._executor_drained = True
        orch._config.server_url = "http://test"
        orch._client.get.side_effect = Exception("skip")

        cleanup_orchestrator(orch)
        # Should not raise, executor already drained
