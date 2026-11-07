"""Real-task cost-anomaly lifecycle regressions with no provider processes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, Complexity, CostAnomalyConfig, ModelConfig, Task, TaskStatus
from bernstein.core.orchestrator import Orchestrator, TickResult

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.cost.cost_anomaly import CostAnomalyDetector, TierStats
from bernstein.core.tasks import task_lifecycle as lifecycle
from tests.unit.test_task_lifecycle import _claim_orch


def _completion(orch: Any, task: Task, *, cost: float = 1.0, tokens_in: int = 100, tokens_out: int = 10) -> None:
    metrics = SimpleNamespace(tokens_prompt=tokens_in, tokens_completion=tokens_out)
    with (
        patch.object(lifecycle, "_record_evolution_completion"),
        patch.object(lifecycle, "_record_completion_metrics", return_value=(metrics, cost)),
        patch.object(lifecycle, "_post_completion_bulletin"),
        patch.object(lifecycle, "_resolve_janitor_result", return_value=True),
    ):
        lifecycle._process_single_completed_task(orch, task, {}, TickResult())


def _completion_orch(tmp_path: Path, *, enabled: bool = True) -> Any:
    orch = MagicMock()
    orch._find_session_for_task.return_value = None
    orch._wal_writer = None
    orch._workdir = tmp_path
    orch._agents = {}
    orch._stop_spawning = False
    orch._anomaly_detector = CostAnomalyDetector(CostAnomalyConfig(enabled=enabled, baseline_min_samples=1), tmp_path)
    orch._handle_anomaly_signal.side_effect = lambda signal: Orchestrator._handle_anomaly_signal(orch, signal)
    return orch


def _spawn(orch: Any, task: Task) -> TickResult:
    result = TickResult()
    with (
        patch.object(
            lifecycle, "_claim_task_with_conflict_retry", return_value=(SimpleNamespace(status_code=200), None)
        ),
        patch.object(lifecycle, "try_fast_path_batch", return_value=False),
        patch.object(lifecycle, "classify_task", return_value=SimpleNamespace(level=lifecycle.TaskLevel.L2)),
        patch.object(lifecycle, "_claim_file_ownership"),
        patch.object(lifecycle, "_spawn_supervisor_for"),
        patch.object(lifecycle, "RecommendationEngine"),
        patch.object(lifecycle, "TeamStateStore"),
        patch.object(lifecycle, "get_collector"),
    ):
        lifecycle.claim_and_spawn_batches(orch, [[task]], 0, set(), set(), result)
    return result


def _spawn_orch(tmp_path: Path, *, enabled: bool = True) -> Any:
    orch = _claim_orch(tmp_path)
    orch._stop_spawning = False
    orch._anomaly_detector = CostAnomalyDetector(CostAnomalyConfig(enabled=enabled), tmp_path)
    orch._handle_anomaly_signal = MagicMock(
        side_effect=lambda signal: Orchestrator._handle_anomaly_signal(orch, signal)
    )
    orch._spawner.get_worktree_path.return_value = None
    orch._spawner.spawn_for_tasks.return_value = AgentSession(
        id="agent-cost", role="backend", task_ids=["cost-task"], model_config=ModelConfig("opus", "high")
    )

    def dispatch(tasks: list[Task]) -> AgentSession:
        session = orch._spawner.spawn_for_tasks.return_value
        orch._spawner._cost_anomaly_spawn_observer(tasks, session.model_config)
        return session

    orch._spawner.spawn_for_tasks.side_effect = dispatch
    return orch


@pytest.mark.parametrize(
    ("complexity", "tier"), [(Complexity.LOW, "small"), (Complexity.MEDIUM, "medium"), (Complexity.HIGH, "large")]
)
def test_completion_uses_real_task_complexity_tiers(tmp_path: Path, complexity: Complexity, tier: str) -> None:
    orch = _completion_orch(tmp_path)
    task = Task(id="cost-task", title="normal work", description="", role="backend", complexity=complexity)

    _completion(orch, task, cost=2.5)

    assert set(orch._anomaly_detector._baseline.per_tier) == {tier}
    assert orch._anomaly_detector._baseline.per_tier[tier].median_cost_usd == 2.5


def test_high_complexity_completion_uses_high_tier_ceiling(tmp_path: Path) -> None:
    orch = _completion_orch(tmp_path)
    detector = orch._anomaly_detector
    detector._baseline.per_tier["large"] = TierStats(median_cost_usd=1.0, p95_cost_usd=1.0, sample_count=2)
    task = Task(id="cost-task", title="complex work", description="", role="backend", complexity=Complexity.HIGH)

    _completion(orch, task, cost=10.0)

    signal = orch._handle_anomaly_signal.call_args.args[0]
    assert signal.rule == "per_task_ceiling"
    assert signal.details["tier"] == "large"


@pytest.mark.parametrize("legacy_title", [False, True])
def test_retry_lineage_metadata_reaches_spiral_detection(tmp_path: Path, legacy_title: bool) -> None:
    orch = _completion_orch(tmp_path)
    for index in range(3):
        task = Task(
            id=f"retry-{index}",
            title="[RETRY 2] work" if legacy_title else "work",
            description="",
            role="backend",
            retry_count=0 if legacy_title else index + 1,
            metadata={"original_task_id": "root-task"},
        )
        _completion(orch, task)

    assert orch._stop_spawning is True
    signal = orch._handle_anomaly_signal.call_args.args[0]
    assert signal.rule == "retry_spiral"
    assert signal.details["original_task_id"] == "root-task"
    assert signal.details["cumulative"] == 3.0


def test_completion_preserves_cost_and_token_measurements(tmp_path: Path) -> None:
    orch = _completion_orch(tmp_path)
    task = Task(id="cost-task", title="work", description="", role="backend", status=TaskStatus.DONE)

    _completion(orch, task, cost=2.5, tokens_in=100, tokens_out=6000)

    signal = orch._handle_anomaly_signal.call_args.args[0]
    assert signal.rule == "token_ratio"
    assert signal.details["tokens_in"] == 100
    assert signal.details["tokens_out"] == 6000
    assert orch._anomaly_detector._baseline.per_tier["medium"].median_cost_usd == 2.5


def test_disabled_detector_leaves_completion_baseline_and_signals_untouched(tmp_path: Path) -> None:
    orch = _completion_orch(tmp_path, enabled=False)
    task = Task(id="cost-task", title="work", description="", role="backend")

    _completion(orch, task, cost=100.0, tokens_out=100000)

    orch._handle_anomaly_signal.assert_not_called()
    assert orch._anomaly_detector._baseline.sample_count == 0
    assert not (tmp_path / ".sdd/metrics/cost_baseline.json").exists()


def test_spawn_anomaly_check_observes_routed_model_before_dispatch(tmp_path: Path) -> None:
    orch = _spawn_orch(tmp_path)
    orch._bandit_router = MagicMock()
    orch._bandit_router.select.return_value = SimpleNamespace(model="opus", effort="high", reason="fixture route")
    orch._bandit_routing_mode = "bandit"
    task = Task(id="cost-task", title="work", description="", role="backend", complexity=Complexity.LOW)

    def dispatch(_tasks: list[Task]) -> AgentSession:
        orch._spawner._cost_anomaly_spawn_observer(_tasks, ModelConfig("opus", "high"))
        signal = orch._handle_anomaly_signal.call_args.args[0]
        assert signal.rule == "model_mismatch"
        assert signal.action == "log"
        assert signal.details["model"] == "opus"
        assert orch._stop_spawning is False
        return AgentSession(
            id="agent-cost", role="backend", task_ids=[task.id], model_config=ModelConfig("opus", "high")
        )

    orch._spawner.spawn_for_tasks.side_effect = dispatch

    result = _spawn(orch, task)

    assert result.spawned == ["agent-cost"]
    assert result.errors == []


def test_disabled_detector_does_not_dispatch_spawn_signals(tmp_path: Path) -> None:
    orch = _spawn_orch(tmp_path, enabled=False)
    task = Task(id="cost-task", title="work", description="", role="backend", complexity=Complexity.LOW)

    assert _spawn(orch, task).spawned == ["agent-cost"]

    orch._handle_anomaly_signal.assert_not_called()
    assert orch._stop_spawning is False


def test_cost_anomaly_failure_is_visible_without_blocking_completion(tmp_path: Path, caplog: Any) -> None:
    orch = _completion_orch(tmp_path)
    task = Task(id="cost-task", title="work", description="", role="backend")
    with patch.object(orch._anomaly_detector, "check_task_completion", side_effect=ValueError("bad metrics")):
        _completion(orch, task)
    assert "cost anomaly completion check failed" in caplog.text.lower()
    assert task.id in caplog.text


def test_cost_anomaly_failure_is_visible_without_blocking_spawn(tmp_path: Path, caplog: Any) -> None:
    orch = _spawn_orch(tmp_path)
    task = Task(id="cost-task", title="work", description="", role="backend", model="opus")
    with patch.object(orch._anomaly_detector, "check_spawn", side_effect=ValueError("bad model")):
        result = _spawn(orch, task)
    assert result.spawned == ["agent-cost"]
    assert "cost anomaly spawn check failed" in caplog.text.lower()


def test_critical_burn_signal_blocks_real_dispatch_until_operator_resume(tmp_path: Path) -> None:
    orch = _spawn_orch(tmp_path)
    tracker = SimpleNamespace(status=lambda: SimpleNamespace(budget_usd=10.0, spent_usd=9.5))
    for signal in orch._anomaly_detector.check_tick([], tracker):
        orch._handle_anomaly_signal(signal)
    task = Task(id="cost-task", title="work", description="", role="backend", model="opus")

    assert _spawn(orch, task).spawned == []
    orch._spawner.spawn_for_tasks.assert_not_called()
    Orchestrator.resume_spawning(orch)
    assert _spawn(orch, task).spawned == ["agent-cost"]


@pytest.mark.parametrize("model_source", ["default", "role", "template"])
@pytest.mark.parametrize("resume", [False, True])
def test_spawn_check_receives_authoritative_model_before_launch(
    tmp_path: Path, model_source: str, resume: bool
) -> None:
    class BeforeAgentLaunch(Exception):
        pass

    orch = _spawn_orch(tmp_path)
    spawner: Any = object.__new__(AgentSpawner)
    spawner._workdir = tmp_path
    spawner._templates_dir = tmp_path / "templates"
    spawner._default_model = "opus" if model_source == "default" else "sonnet"
    spawner._role_model_policy = {"backend": {"model": "opus"}} if model_source == "role" else {}
    spawner._shutdown_event = None
    spawner._agent_failure_timestamps = {}
    spawner._adapter = SimpleNamespace(name=lambda: "claude", is_rate_limited=lambda: False)
    spawner._adapter_health = SimpleNamespace(is_healthy=lambda _name: True)
    spawner._preflight_posture_drift = lambda: None
    spawner._apply_provider_availability = lambda _role, _task, policy: policy
    spawner._resolve_routing = lambda _tasks, model, _policy, _provider: (model, None, "fixture routing")
    spawner._spawn_rate_limiter = MagicMock()
    spawner._spawn_rate_limiter.acquire.side_effect = BeforeAgentLaunch
    spawner.spawn_for_tasks = spawner._spawn_for_tasks_internal
    orch._spawner = spawner
    if resume:
        orch._preserved_worktrees = {"cost-task": tmp_path / "preserved"}
        spawner._enforce_model_registry_gate = MagicMock(side_effect=BeforeAgentLaunch)
    if model_source == "template":
        role_dir = spawner._templates_dir / "backend"
        role_dir.mkdir(parents=True)
        (role_dir / "config.yaml").write_text("default_model: opus\ndefault_effort: high\n")
    task = Task(id="cost-task", title="work", description="", role="backend", complexity=Complexity.LOW, priority=1)

    # Stop at the fresh-spawn rate limiter or resume registry gate after real
    # model resolution, before any worktree, process or provider can launch.
    with (
        patch.object(lifecycle, "_get_changed_files_in_worktree", return_value=[]),
        pytest.raises(BeforeAgentLaunch),
    ):
        _spawn(orch, task)

    assert orch._handle_anomaly_signal.call_args is not None
    signal = orch._handle_anomaly_signal.call_args.args[0]
    assert signal.rule == "model_mismatch"
    assert signal.details["model"] == "opus"
    assert task.model is None
    assert orch._stop_spawning is False
