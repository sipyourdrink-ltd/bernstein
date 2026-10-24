"""Tests for cost-anomaly signal wiring into the orchestrator lifecycle.

Covers the three gaps described in issue #5942:
1. _stop_spawning flag is read by _pre_spawn_checks_pass (was set but never read).
2. check_task_completion is called at task completion with real cost/token figures.
3. check_spawn is called before each spawn (advisory).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import Complexity, CostAnomalyConfig, ModelConfig

from bernstein.core.cost.cost_anomaly import AnomalySignal, CostAnomalyDetector
from bernstein.core.tasks.task_lifecycle import _check_spawn_cost_anomalies, _pre_spawn_checks_pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_detector(tmp_path: Path, *, enabled: bool = True) -> CostAnomalyDetector:
    sdd = tmp_path / ".sdd" / "metrics"
    sdd.mkdir(parents=True, exist_ok=True)
    return CostAnomalyDetector(CostAnomalyConfig(enabled=enabled), tmp_path)


def _stop_signal(rule: str = "burn_rate") -> AnomalySignal:
    return AnomalySignal(
        rule=rule,
        severity="critical",
        action="stop_spawning",
        agent_id=None,
        task_id=None,
        message="burn rate critical",
        details={},
        timestamp=0.0,
    )


# ---------------------------------------------------------------------------
# Gap 1 — _stop_spawning is read by _pre_spawn_checks_pass
# ---------------------------------------------------------------------------


class TestStopSpawningGate:
    def _make_orch(self, *, stop: bool) -> SimpleNamespace:
        orch = SimpleNamespace(
            _stop_spawning=stop,
            _convergence_guard=None,
            _spawn_failure_history={},
            _merge_queue=None,
        )
        orch.is_shutting_down = lambda: False
        orch._spawner = SimpleNamespace(_adapter=None)
        return orch

    def test_spawn_proceeds_when_flag_clear(self) -> None:
        orch = self._make_orch(stop=False)
        assert _pre_spawn_checks_pass(orch, alive_count=0) is True

    def test_spawn_blocked_when_flag_set(self) -> None:
        orch = self._make_orch(stop=True)
        assert _pre_spawn_checks_pass(orch, alive_count=0) is False

    def test_spawn_resumes_after_flag_cleared(self) -> None:
        orch = self._make_orch(stop=True)
        assert _pre_spawn_checks_pass(orch, alive_count=0) is False
        orch._stop_spawning = False
        assert _pre_spawn_checks_pass(orch, alive_count=0) is True


# ---------------------------------------------------------------------------
# Gap 1b — orchestrator.resume_spawning() clears the flag
# ---------------------------------------------------------------------------


class TestResumeSpawning:
    def test_resume_spawning_clears_flag(self, tmp_path: Path) -> None:
        """resume_spawning() must reset _stop_spawning to False."""
        from bernstein.core.orchestration.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        orch._stop_spawning = True  # type: ignore[attr-defined]
        orch.resume_spawning()  # type: ignore[attr-defined]
        assert orch._stop_spawning is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Gap 2 — check_task_completion is wired at task completion
# ---------------------------------------------------------------------------


class TestCheckTaskCompletionWiring:
    def test_check_task_completion_called_on_done_task(self, tmp_path: Path) -> None:
        """_process_single_completed_task calls check_task_completion."""
        from bernstein.core.models import Task, TaskStatus

        from bernstein.core.tasks.task_lifecycle import _process_single_completed_task

        task = Task(
            id="task-001",
            title="fix something",
            description="",
            role="backend",
            status=TaskStatus.DONE,
            complexity=Complexity.MEDIUM,
        )

        detector = MagicMock(spec=CostAnomalyDetector)
        detector.check_task_completion.return_value = []

        orch = MagicMock()
        orch._anomaly_detector = detector
        orch._processed_done_tasks = {}
        orch._find_session_for_task.return_value = None
        orch._wal_writer = None
        orch._workdir = tmp_path

        result = MagicMock()
        result.verified = []
        result.verification_failures = []

        with (
            patch("bernstein.core.tasks.task_lifecycle._record_evolution_completion"),
            patch("bernstein.core.tasks.task_lifecycle._record_completion_metrics", return_value=(None, 0.0)),
            patch("bernstein.core.tasks.task_lifecycle._post_completion_bulletin"),
            patch("bernstein.core.tasks.task_lifecycle._resolve_janitor_result", return_value=True),
            patch("bernstein.core.tasks.task_lifecycle._run_verification_gates", return_value=(True, None)),
        ):
            _process_single_completed_task(orch, task, {}, result)

        detector.check_task_completion.assert_called_once()
        detector.check_task_completion.assert_called_once_with(
            "task-001", "medium", 0.0, 0, 0, is_retry=False, original_task_id=None
        )

    def test_anomaly_signals_from_completion_handled(self, tmp_path: Path) -> None:
        """Signals returned by check_task_completion flow through _handle_anomaly_signal."""
        from bernstein.core.models import Task, TaskStatus

        from bernstein.core.tasks.task_lifecycle import _process_single_completed_task

        task = Task(
            id="task-002",
            title="heavy work",
            description="",
            role="backend",
            status=TaskStatus.DONE,
            complexity=Complexity.HIGH,
        )

        sig = _stop_signal("per_task_ceiling")
        detector = MagicMock(spec=CostAnomalyDetector)
        detector.check_task_completion.return_value = [sig]

        orch = MagicMock()
        orch._anomaly_detector = detector
        orch._processed_done_tasks = {}
        orch._find_session_for_task.return_value = None
        orch._wal_writer = None
        orch._workdir = tmp_path

        result = MagicMock()
        result.verified = []
        result.verification_failures = []

        with (
            patch("bernstein.core.tasks.task_lifecycle._record_evolution_completion"),
            patch("bernstein.core.tasks.task_lifecycle._record_completion_metrics", return_value=(None, 0.0)),
            patch("bernstein.core.tasks.task_lifecycle._post_completion_bulletin"),
            patch("bernstein.core.tasks.task_lifecycle._resolve_janitor_result", return_value=True),
            patch("bernstein.core.tasks.task_lifecycle._run_verification_gates", return_value=(True, None)),
        ):
            _process_single_completed_task(orch, task, {}, result)

        orch._handle_anomaly_signal.assert_called_once_with(sig)


# ---------------------------------------------------------------------------
# Gap 3 — check_spawn is called before each spawn (advisory)
# ---------------------------------------------------------------------------


class TestCheckSpawnWiring:
    def test_check_spawn_called_for_single_task_batch(self) -> None:
        """The advisory check_spawn block fires for single-task batches when detector is set."""
        from bernstein.core.models import Task

        task = Task(
            id="task-003",
            title="work",
            description="",
            role="backend",
            complexity=Complexity.MEDIUM,
            model="claude-sonnet-5-5",
        )

        detector = MagicMock(spec=CostAnomalyDetector)
        detector.check_spawn.return_value = []

        orch = MagicMock()
        orch._anomaly_detector = detector

        _check_spawn_cost_anomalies(orch, [task], ModelConfig("claude-sonnet-5-5", "high"))

        detector.check_spawn.assert_called_once_with("task-003", "medium", "claude-sonnet-5-5")

    def test_check_spawn_signals_dispatched(self) -> None:
        """Advisory signals from check_spawn are passed to _handle_anomaly_signal."""
        from bernstein.core.models import Task

        task = Task(
            id="task-004", title="work", description="", role="backend", complexity=Complexity.HIGH, model="wrong-model"
        )

        sig = AnomalySignal("model_mismatch", "info", "log", None, task.id, "model advisory", {}, 0.0)
        detector = MagicMock(spec=CostAnomalyDetector)
        detector.check_spawn.return_value = [sig]

        orch = MagicMock()
        orch._anomaly_detector = detector

        _check_spawn_cost_anomalies(orch, [task], ModelConfig("wrong-model", "high"))

        orch._handle_anomaly_signal.assert_called_once_with(sig)
