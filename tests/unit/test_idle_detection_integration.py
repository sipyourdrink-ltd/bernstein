"""Tests for log-growth idle detection wiring (audit-006).

The log-growth heuristic in ``bernstein.core.idle_detection`` had a fully
implemented ``integrate_idle_detection`` function with zero callers in
``src/`` or ``tests/``. This module covers:

1. ``integrate_idle_detection`` behaviour in isolation (baseline, shutdown,
   dead-session skip, active-session skip).
2. A regression guard that ``Orchestrator._tick_internal`` actually wires
   the heuristic in — so future refactors can't silently delete the call
   again.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from bernstein.core.agent_log_aggregator import AgentLogAggregator, AgentLogSummary
from bernstein.core.idle_detection import integrate_idle_detection


def _make_orch(tmp_path: Path, sessions: dict[str, MagicMock]) -> MagicMock:
    """Build a mock orchestrator with the attributes ``integrate_idle_detection`` touches."""
    orch = MagicMock()
    orch._workdir = tmp_path
    orch._agents = sessions
    orch._config = MagicMock(idle_timeout_seconds=180)
    orch._signal_mgr = MagicMock()
    # Remove auto-created attributes so integrate_idle_detection creates them
    # (it uses hasattr as a guard).
    for attr in ("_last_known_log_lines", "_idle_shutdown_ts"):
        if hasattr(orch, attr):
            delattr(orch, attr)
    return orch


def _make_session(sid: str, status: str = "working") -> MagicMock:
    sess = MagicMock()
    sess.id = sid
    sess.status = status
    sess.task_ids = [f"task-{sid}"]
    return sess


def _make_summary(sid: str, total_lines: int) -> AgentLogSummary:
    return AgentLogSummary(
        session_id=sid,
        total_lines=total_lines,
        events=[],
        error_count=0,
        warning_count=0,
        files_modified=[],
        tests_run=False,
        tests_passed=False,
        test_summary="",
        rate_limit_hits=0,
        compile_errors=0,
        tool_failures=0,
        first_meaningful_action_line=1,
        last_activity_line=total_lines,
        dominant_failure_category=None,
    )


class TestIntegrateIdleDetection:
    """Behavioural tests for ``integrate_idle_detection``."""

    def test_establishes_baseline_on_first_tick(self, tmp_path: Path) -> None:
        """First tick records line counts without signalling shutdown."""
        sess = _make_session("sess-1")
        orch = _make_orch(tmp_path, {"sess-1": sess})

        aggregator = MagicMock(spec=AgentLogAggregator)
        aggregator.parse_log.return_value = _make_summary("sess-1", 42)

        with (
            patch(
                "bernstein.core.agent_log_aggregator.AgentLogAggregator",
                return_value=aggregator,
            ),
            patch(
                "bernstein.core.agents.idle_detection._check_git_changes",
                return_value=False,
            ),
        ):
            tracking = integrate_idle_detection(orch)

        assert tracking == {"sess-1": 42}
        assert orch._last_known_log_lines == {"sess-1": 42}
        orch._signal_mgr.write_shutdown.assert_not_called()

    def test_signals_shutdown_when_idle(self, tmp_path: Path) -> None:
        """Unchanged log + no git activity triggers SHUTDOWN + timestamp record."""
        sess = _make_session("sess-2")
        orch = _make_orch(tmp_path, {"sess-2": sess})
        # Pre-populate baseline so the heuristic can compare across ticks.
        orch._last_known_log_lines = {"sess-2": 100}

        aggregator = MagicMock(spec=AgentLogAggregator)
        aggregator.parse_log.return_value = _make_summary("sess-2", 100)

        with (
            patch(
                "bernstein.core.agent_log_aggregator.AgentLogAggregator",
                return_value=aggregator,
            ),
            patch(
                "bernstein.core.agents.idle_detection._check_git_changes",
                return_value=False,
            ),
        ):
            integrate_idle_detection(orch)

        orch._signal_mgr.write_shutdown.assert_called_once()
        kwargs = orch._signal_mgr.write_shutdown.call_args.kwargs
        assert "log_unchanged" in kwargs["reason"]
        # The orchestrator should have recorded the shutdown timestamp for the
        # force-kill grace period watcher.
        assert "sess-2" in orch._idle_shutdown_ts

    def test_skips_dead_sessions(self, tmp_path: Path) -> None:
        """Dead agents are not considered for idle shutdown."""
        sess = _make_session("sess-dead", status="dead")
        orch = _make_orch(tmp_path, {"sess-dead": sess})

        aggregator = MagicMock(spec=AgentLogAggregator)
        aggregator.parse_log.return_value = _make_summary("sess-dead", 10)

        with (
            patch(
                "bernstein.core.agent_log_aggregator.AgentLogAggregator",
                return_value=aggregator,
            ),
            patch(
                "bernstein.core.agents.idle_detection._check_git_changes",
                return_value=False,
            ),
        ):
            integrate_idle_detection(orch)

        aggregator.parse_log.assert_not_called()
        orch._signal_mgr.write_shutdown.assert_not_called()

    def test_leaves_active_agents_alone(self, tmp_path: Path) -> None:
        """Growing log means agent is active — no shutdown signal."""
        sess = _make_session("sess-active")
        orch = _make_orch(tmp_path, {"sess-active": sess})
        orch._last_known_log_lines = {"sess-active": 50}

        aggregator = MagicMock(spec=AgentLogAggregator)
        aggregator.parse_log.return_value = _make_summary("sess-active", 200)

        with (
            patch(
                "bernstein.core.agent_log_aggregator.AgentLogAggregator",
                return_value=aggregator,
            ),
            patch(
                "bernstein.core.agents.idle_detection._check_git_changes",
                return_value=False,
            ),
        ):
            integrate_idle_detection(orch)

        orch._signal_mgr.write_shutdown.assert_not_called()
        assert orch._last_known_log_lines["sess-active"] == 200


class TestOrchestratorTickWiringAudit006:
    """Regression guard: the orchestrator tick must call the heuristic.

    Before audit-006, the log-growth idle detector was defined but never
    invoked, so agents stuck in a dead MCP call were only reaped at
    ``max_agent_runtime_s`` (30-90 min). This test is independent of full
    orchestrator construction (which requires a live task server) by doing
    source inspection.
    """

    def test_orchestrator_tick_calls_integrate_idle_detection(self) -> None:
        """``orchestrator._tick_internal`` must reference ``integrate_idle_detection``."""
        import inspect

        from bernstein.core.orchestration import orchestrator as orch_mod

        src = inspect.getsource(orch_mod.Orchestrator._tick_internal)
        assert "integrate_idle_detection" in src, (
            "integrate_idle_detection must be wired into the tick loop (audit-006)"
        )
