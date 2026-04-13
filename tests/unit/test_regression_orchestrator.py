"""Regression tests for the Orchestrator — task lifecycle, routing, cleanup, cost tracking.

Covers the core orchestration loop end-to-end: dependency filtering, retry
with model/effort escalation, crash recovery strategies, provider health and
budget enforcement, janitor signal evaluation, and cost tracking thresholds.

All HTTP communication and subprocess spawning is mocked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.cost_tracker import BudgetStatus, CostTracker, TokenUsage, estimate_cost
from bernstein.core.lifecycle import (
    AGENT_TRANSITIONS,
    TASK_TRANSITIONS,
    IllegalTransitionError,
    transition_agent,
    transition_task,
)
from bernstein.core.models import (
    AgentSession,
    CompletionSignal,
    Complexity,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import Orchestrator, TickResult, group_by_role
from bernstein.core.router import ModelConfig as RouterModelConfig
from bernstein.core.spawner import AgentSpawner
from bernstein.core.task_lifecycle import (
    check_file_overlap,
    collect_completion_data,
    maybe_retry_task,
)
from bernstein.core.tick_pipeline import prioritize_starving_roles

from bernstein.adapters.base import CLIAdapter, SpawnResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    id: str = "T-001",
    role: str = "backend",
    title: str = "Implement feature X",
    description: str = "Write the code.",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    status: str = "open",
    task_type: TaskType = TaskType.STANDARD,
    depends_on: list[str] | None = None,
    owned_files: list[str] | None = None,
    model: str | None = None,
    effort: str | None = None,
    completion_signals: list[CompletionSignal] | None = None,
) -> Task:
    t = Task(
        id=id,
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=Scope(scope),
        complexity=Complexity(complexity),
        status=TaskStatus(status),
        task_type=task_type,
        depends_on=depends_on or [],
        owned_files=owned_files or [],
        completion_signals=completion_signals or [],
    )
    if model is not None:
        t.model = model
    if effort is not None:
        t.effort = effort
    return t


def _task_as_dict(task: Task) -> dict[str, object]:
    """Serialise a Task the way the server JSON would look."""
    result: dict[str, object] = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "status": task.status.value,
        "depends_on": task.depends_on,
        "owned_files": task.owned_files,
        "assigned_agent": task.assigned_agent,
        "result_summary": task.result_summary,
        "task_type": task.task_type.value,
    }
    if task.model is not None:
        result["model"] = task.model
    if task.effort is not None:
        result["effort"] = task.effort
    return result


def _mock_adapter(pid: int = 42) -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _build_orchestrator(
    tmp_path: Path,
    transport: httpx.MockTransport,
    adapter: CLIAdapter | None = None,
    config: OrchestratorConfig | None = None,
) -> Orchestrator:
    cfg = config or OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        heartbeat_timeout_s=120,
        max_tasks_per_agent=3,
        server_url="http://testserver",
    )
    adp = adapter or _mock_adapter()
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(adp, templates_dir, tmp_path)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


def _empty_transport() -> httpx.MockTransport:
    """Transport that returns empty task lists and 200 for everything."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/tasks":
            return httpx.Response(200, json=[])
        if request.method == "POST":
            return httpx.Response(200, json={"id": "new-task", "status": "open"})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


# ===========================================================================
# 1. Task Lifecycle Regression Tests
# ===========================================================================


class TestDependencyFiltering:
    """Tasks with unmet dependencies must not be spawned."""

    def test_task_with_unmet_dependency_held_back(self, tmp_path: Path) -> None:
        """A task depending on an incomplete task should not be dispatched."""
        t1 = _make_task(id="T-dep", role="backend", status="open")
        t2 = _make_task(id="T-blocked", role="backend", status="open", depends_on=["T-dep"])
        task_dicts = [_task_as_dict(t1), _task_as_dict(t2)]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in url.path:
                task_id = url.path.split("/")[-2]
                td = next((t for t in task_dicts if t["id"] == task_id), task_dicts[0])
                return httpx.Response(200, json=td)
            return httpx.Response(200, json={})

        orch = _build_orchestrator(
            tmp_path,
            httpx.MockTransport(handler),
            config=OrchestratorConfig(
                max_agents=6,
                poll_interval_s=1,
                max_tasks_per_agent=1,
                server_url="http://testserver",
            ),
        )
        result = orch.tick()

        # Only T-dep should be spawnable, T-blocked is held
        assert len(result.spawned) == 1
        spawned_session = orch._agents.get(result.spawned[0])
        assert spawned_session is not None
        assert "T-dep" in spawned_session.task_ids

    def test_task_with_met_dependency_dispatched(self, tmp_path: Path) -> None:
        """When all dependencies are done, the task should be dispatched."""
        t_done = _make_task(id="T-dep", role="backend", status="done")
        t_ready = _make_task(id="T-ready", role="backend", status="open", depends_on=["T-dep"])
        task_dicts = [_task_as_dict(t_done), _task_as_dict(t_ready)]

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in url.path:
                return httpx.Response(200, json=task_dicts[1])
            return httpx.Response(200, json={})

        orch = _build_orchestrator(
            tmp_path,
            httpx.MockTransport(handler),
            config=OrchestratorConfig(
                max_agents=6,
                poll_interval_s=1,
                max_tasks_per_agent=1,
                server_url="http://testserver",
            ),
        )
        result = orch.tick()

        # T-ready should be dispatched since its dependency T-dep is done
        assert len(result.spawned) >= 1
        spawned_ids = []
        for sid in result.spawned:
            s = orch._agents.get(sid)
            if s is not None:
                spawned_ids.extend(s.task_ids)
        assert "T-ready" in spawned_ids

    def test_diamond_dependency_chain(self, tmp_path: Path) -> None:
        """Diamond dep graph: A->B, A->C, B+C->D. Only A should be spawnable initially."""
        t_a = _make_task(id="A", role="backend", status="open")
        t_b = _make_task(id="B", role="backend", status="open", depends_on=["A"])
        t_c = _make_task(id="C", role="backend", status="open", depends_on=["A"])
        t_d = _make_task(id="D", role="backend", status="open", depends_on=["B", "C"])
        task_dicts = [_task_as_dict(t_a), _task_as_dict(t_b), _task_as_dict(t_c), _task_as_dict(t_d)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=task_dicts[0])
            return httpx.Response(200, json={})

        orch = _build_orchestrator(
            tmp_path,
            httpx.MockTransport(handler),
            config=OrchestratorConfig(
                max_agents=6,
                poll_interval_s=1,
                max_tasks_per_agent=1,
                server_url="http://testserver",
            ),
        )
        result = orch.tick()

        # Only task A (no deps) should be spawnable
        spawned_task_ids = []
        for sid in result.spawned:
            s = orch._agents.get(sid)
            if s is not None:
                spawned_task_ids.extend(s.task_ids)
        assert "A" in spawned_task_ids
        assert "B" not in spawned_task_ids
        assert "C" not in spawned_task_ids
        assert "D" not in spawned_task_ids


class TestTaskStateTransitions:
    """Verify all allowed transitions succeed and illegal ones raise."""

    @pytest.mark.parametrize(
        "from_status,to_status",
        list(TASK_TRANSITIONS.keys()),
        ids=[f"{f.value}->{t.value}" for f, t in TASK_TRANSITIONS],
    )
    def test_allowed_task_transition(self, from_status: TaskStatus, to_status: TaskStatus) -> None:
        task = _make_task(id="T-fsm", status=from_status.value)
        event = transition_task(task, to_status, actor="test", reason="regression")
        assert task.status == to_status
        assert event.entity_type == "task"

    def test_illegal_task_transition_raises(self) -> None:
        """DONE -> OPEN is not in the transition table."""
        task = _make_task(id="T-bad", status="done")
        with pytest.raises(IllegalTransitionError):
            transition_task(task, TaskStatus.OPEN)

    def test_illegal_agent_transition_raises(self) -> None:
        """dead -> working is not allowed."""
        session = AgentSession(
            id="A-dead",
            role="backend",
            pid=1,
            task_ids=["T-1"],
            status="dead",
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        with pytest.raises(IllegalTransitionError):
            transition_agent(session, "working")

    @pytest.mark.parametrize(
        "from_status,to_status",
        list(AGENT_TRANSITIONS.keys()),
        ids=[f"{f}->{t}" for f, t in AGENT_TRANSITIONS],
    )
    def test_allowed_agent_transition(self, from_status: str, to_status: str) -> None:
        session = AgentSession(
            id="A-fsm",
            role="qa",
            pid=1,
            task_ids=["T-1"],
            status=from_status,
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        event = transition_agent(session, to_status, actor="test")
        assert session.status == to_status
        assert event.entity_type == "agent"


class TestRetryEscalation:
    """Verify the retry ladder: effort bump -> model escalation -> max retries."""

    def test_first_retry_bumps_effort(self) -> None:
        """First retry escalates effort (high -> max), keeps model."""
        task = _make_task(id="T-fail", status="failed", model="sonnet", effort="high")
        retried: set[str] = set()
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "T-retry-1"}
        resp.raise_for_status = MagicMock()
        client.post.return_value = resp

        result = maybe_retry_task(
            task,
            retried_task_ids=retried,
            max_task_retries=2,
            client=client,
            server_url="http://test",
            quarantine=MagicMock(),
        )
        assert result is True
        assert task.id in retried
        # Check the payload sent
        call_args = client.post.call_args
        payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
        assert payload["effort"] == "max"  # high -> max
        assert payload["model"] == "sonnet"  # model unchanged

    def test_second_retry_escalates_model(self) -> None:
        """Second retry escalates model (sonnet -> opus)."""
        task = _make_task(
            id="T-fail2",
            title="[RETRY 1] Fix bug",
            status="failed",
            model="sonnet",
            effort="max",
        )
        task.retry_count = 1
        retried: set[str] = set()
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "T-retry-2"}
        resp.raise_for_status = MagicMock()
        client.post.return_value = resp

        result = maybe_retry_task(
            task,
            retried_task_ids=retried,
            max_task_retries=2,
            client=client,
            server_url="http://test",
            quarantine=MagicMock(),
        )
        assert result is True
        payload = client.post.call_args[1]["json"]
        assert payload["model"] == "opus"  # sonnet -> opus

    def test_max_retries_exhausted_no_retry(self) -> None:
        """No retry created when max retries reached."""
        task = _make_task(
            id="T-exhausted",
            title="[RETRY 2] Fix bug",
            status="failed",
        )
        task.retry_count = 2
        task.max_retries = 2
        retried: set[str] = set()
        quarantine = MagicMock()

        result = maybe_retry_task(
            task,
            retried_task_ids=retried,
            max_task_retries=2,
            client=MagicMock(),
            server_url="http://test",
            quarantine=quarantine,
        )
        assert result is False
        quarantine.record_failure.assert_called_once()

    def test_high_stakes_role_gets_opus_max(self) -> None:
        """Architect/security roles always get opus/max on any retry."""
        task = _make_task(
            id="T-sec",
            role="security",
            status="failed",
            model="sonnet",
            effort="medium",
        )
        retried: set[str] = set()
        client = MagicMock(spec=httpx.Client)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "T-sec-retry"}
        resp.raise_for_status = MagicMock()
        client.post.return_value = resp

        maybe_retry_task(
            task,
            retried_task_ids=retried,
            max_task_retries=2,
            client=client,
            server_url="http://test",
            quarantine=MagicMock(),
        )
        payload = client.post.call_args[1]["json"]
        assert payload["model"] == "opus"
        assert payload["effort"] == "max"


class TestFileOverlapDetection:
    """File ownership conflict detection between concurrent agents."""

    def test_overlap_detected_with_active_agent(self) -> None:
        task = _make_task(id="T-ov", owned_files=["src/main.py"])
        session = AgentSession(
            id="A-owner",
            role="backend",
            pid=1,
            task_ids=["T-x"],
            status="working",
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        ownership = {"src/main.py": "A-owner"}
        agents = {"A-owner": session}

        assert check_file_overlap([task], ownership, agents) is True

    def test_no_overlap_with_dead_agent(self) -> None:
        task = _make_task(id="T-safe", owned_files=["src/main.py"])
        session = AgentSession(
            id="A-dead",
            role="backend",
            pid=1,
            task_ids=["T-x"],
            status="dead",
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        ownership = {"src/main.py": "A-dead"}
        agents = {"A-dead": session}

        assert check_file_overlap([task], ownership, agents) is False

    def test_no_overlap_when_no_files_owned(self) -> None:
        task = _make_task(id="T-nf", owned_files=[])
        assert check_file_overlap([task], {}, {}) is False


class TestCompletionDataExtraction:
    """Tests for collect_completion_data log parsing."""

    def test_extracts_modified_files_from_log(self, tmp_path: Path) -> None:
        session = AgentSession(
            id="A-log",
            role="backend",
            pid=1,
            task_ids=["T-1"],
            status="working",
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        log_dir = tmp_path / ".sdd" / "runtime"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "A-log.log"
        log_file.write_text("Modified: src/foo.py\nCreated: src/bar.py\nSome other output\n")

        data = collect_completion_data(tmp_path, session)
        assert "src/foo.py" in data["files_modified"]
        assert "src/bar.py" in data["files_modified"]
        assert len(data["files_modified"]) == 2

    def test_handles_missing_log_file(self, tmp_path: Path) -> None:
        session = AgentSession(
            id="A-nolog",
            role="backend",
            pid=1,
            task_ids=["T-1"],
            status="working",
            model_config=RouterModelConfig(model="sonnet", effort="normal"),
            spawn_ts=time.time(),
        )
        data = collect_completion_data(tmp_path, session)
        assert data["files_modified"] == []
        assert data["test_results"] == {}


# ===========================================================================
# 2. Cost Tracking Regression Tests
# ===========================================================================


class TestCostTrackerBudgetEnforcement:
    """Cost tracker: warn/stop thresholds, unlimited mode, persistence."""

    def test_warns_at_80_percent(self) -> None:
        tracker = CostTracker(run_id="test-warn", budget_usd=10.0)
        # Spend $8.00 (80%)
        status = tracker.record("A-1", "T-1", "sonnet", 100_000, 100_000, cost_usd=8.0)
        assert status.should_warn is True
        assert status.should_stop is False

    def test_stops_at_100_percent(self) -> None:
        tracker = CostTracker(run_id="test-stop", budget_usd=10.0)
        status = tracker.record("A-1", "T-1", "opus", 200_000, 200_000, cost_usd=10.0)
        assert status.should_stop is True

    def test_unlimited_budget_never_stops(self) -> None:
        tracker = CostTracker(run_id="test-unlim", budget_usd=0.0)
        status = tracker.record("A-1", "T-1", "opus", 1_000_000, 1_000_000, cost_usd=999.0)
        assert status.should_warn is False
        assert status.should_stop is False
        assert status.remaining_usd == float("inf")

    def test_cumulative_spending(self) -> None:
        tracker = CostTracker(run_id="test-cum", budget_usd=5.0)
        tracker.record("A-1", "T-1", "sonnet", 10_000, 10_000, cost_usd=1.0)
        tracker.record("A-2", "T-2", "sonnet", 10_000, 10_000, cost_usd=2.0)
        status = tracker.status()
        assert abs(status.spent_usd - 3.0) < 0.001
        assert abs(status.remaining_usd - 2.0) < 0.001
        assert abs(status.percentage_used - 0.6) < 0.001

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        tracker = CostTracker(run_id="test-persist", budget_usd=20.0)
        tracker.record("A-1", "T-1", "sonnet", 5000, 5000, cost_usd=2.5)
        tracker.record("A-2", "T-2", "opus", 3000, 3000, cost_usd=4.0)
        tracker.save(tmp_path)

        loaded = CostTracker.load(tmp_path, "test-persist")
        assert loaded is not None
        assert abs(loaded.spent_usd - 6.5) < 0.001
        assert loaded.budget_usd == pytest.approx(20.0)
        assert len(loaded.usages) == 2

    def test_cost_estimation(self) -> None:
        # Opus should be more expensive than haiku
        opus_cost = estimate_cost("opus", 1000, 1000)
        haiku_cost = estimate_cost("haiku", 1000, 1000)
        assert opus_cost > haiku_cost

    def test_budget_blocks_spawn_in_tick(self, tmp_path: Path) -> None:
        """When budget is exhausted, tick should not spawn agents."""
        task = _make_task(id="T-budget", role="backend")
        task_dicts = [_task_as_dict(task)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            return httpx.Response(200, json={})

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
            budget_usd=1.0,
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)

        # Exhaust the budget
        orch._cost_tracker.record("A-0", "T-0", "opus", 100_000, 100_000, cost_usd=1.0)
        assert orch._cost_tracker.status().should_stop is True

        result = orch.tick()
        assert len(result.spawned) == 0, "No agents should spawn when budget is exhausted"


class TestTokenUsageSerialization:
    """Token usage data classes serialize/deserialize correctly."""

    def test_token_usage_round_trip(self) -> None:
        usage = TokenUsage(
            input_tokens=1000,
            output_tokens=500,
            model="sonnet",
            cost_usd=0.05,
            agent_id="A-1",
            task_id="T-1",
            timestamp=1000.0,
        )
        d = usage.to_dict()
        restored = TokenUsage.from_dict(d)
        assert restored.input_tokens == 1000
        assert restored.output_tokens == 500
        assert restored.model == "sonnet"
        assert abs(restored.cost_usd - 0.05) < 0.0001

    def test_budget_status_serialization(self) -> None:
        status = BudgetStatus(
            run_id="r-1",
            budget_usd=10.0,
            spent_usd=8.5,
            remaining_usd=1.5,
            percentage_used=0.85,
            should_warn=True,
            should_stop=False,
        )
        d = status.to_dict()
        assert d["should_warn"] is True
        assert d["should_stop"] is False
        assert abs(d["spent_usd"] - 8.5) < 0.001


# ===========================================================================
# 3. Janitor / Cleanup Regression Tests
# ===========================================================================


class TestJanitorSignalEvaluation:
    """Verify that janitor correctly evaluates completion signals."""

    def test_path_exists_signal_passes(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        target = tmp_path / "output.txt"
        target.write_text("result")
        signal = CompletionSignal(type="path_exists", value="output.txt")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_path_exists_signal_fails(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        signal = CompletionSignal(type="path_exists", value="missing.txt")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_glob_exists_signal_passes(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("pass")
        signal = CompletionSignal(type="glob_exists", value="tests/test_*.py")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_glob_exists_signal_fails_no_match(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        signal = CompletionSignal(type="glob_exists", value="nonexistent/**/*.xyz")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_file_contains_signal_passes(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        target = tmp_path / "src" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_text("class MyFeature:\n    pass\n")
        signal = CompletionSignal(type="file_contains", value="src/module.py :: class MyFeature")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is True

    def test_file_contains_signal_fails(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import evaluate_signal

        target = tmp_path / "src" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_text("# empty\n")
        signal = CompletionSignal(type="file_contains", value="src/module.py :: class NotHere")
        passed, _detail = evaluate_signal(signal, tmp_path)
        assert passed is False

    def test_verify_task_all_signals_pass(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import verify_task

        (tmp_path / "output.txt").write_text("done")
        task = _make_task(
            id="T-verify",
            completion_signals=[
                CompletionSignal(type="path_exists", value="output.txt"),
            ],
        )
        passed, failed = verify_task(task, tmp_path)
        assert passed is True
        assert failed == []

    def test_verify_task_partial_failure(self, tmp_path: Path) -> None:
        from bernstein.core.janitor import verify_task

        (tmp_path / "exists.txt").write_text("here")
        task = _make_task(
            id="T-partial",
            completion_signals=[
                CompletionSignal(type="path_exists", value="exists.txt"),
                CompletionSignal(type="path_exists", value="missing.txt"),
            ],
        )
        passed, failed = verify_task(task, tmp_path)
        assert passed is False
        assert len(failed) == 1


# ===========================================================================
# 4. Property-Based Tests for Scheduler Fairness
# ===========================================================================


class TestSchedulerFairness:
    """Property-based tests: scheduler distributes work fairly across roles."""

    def test_no_role_starvation_random_distributions(self) -> None:
        """Over many random task distributions, every role gets at least one batch."""
        import random

        rng = random.Random(42)  # deterministic seed
        roles = ["backend", "qa", "frontend", "docs", "security"]

        for trial in range(100):
            # Generate 5-20 random tasks across random roles
            n_tasks = rng.randint(5, 20)
            tasks = []
            for i in range(n_tasks):
                role = rng.choice(roles)
                tasks.append(_make_task(id=f"T-{trial}-{i}", role=role, priority=rng.randint(1, 3)))

            batches = group_by_role(tasks, max_per_batch=1)

            # Verify: every role that has tasks appears in the batches
            roles_with_tasks = {t.role for t in tasks}
            roles_in_batches = {b[0].role for b in batches}
            assert roles_with_tasks == roles_in_batches, (
                f"Trial {trial}: missing roles {roles_with_tasks - roles_in_batches}"
            )

    def test_starving_role_always_first_under_competition(self) -> None:
        """When alive_per_role marks one role as over-served, starving roles lead."""
        import random

        rng = random.Random(99)

        for trial in range(50):
            # Create 3-5 tasks for "overserved" role, 1-2 for "starving"
            n_overserved = rng.randint(3, 5)
            n_starving = rng.randint(1, 2)
            tasks = []
            for i in range(n_overserved):
                tasks.append(_make_task(id=f"T-os-{trial}-{i}", role="backend", priority=2))
            for i in range(n_starving):
                tasks.append(_make_task(id=f"T-sv-{trial}-{i}", role="qa", priority=2))

            alive_per_role = {"backend": rng.randint(2, 5)}
            batches = group_by_role(tasks, max_per_batch=1, alive_per_role=alive_per_role)
            result = prioritize_starving_roles(batches, alive_per_role)

            # First batch should always be the starving role
            if result:
                first_role = result[0][0].role
                assert first_role == "qa", f"Trial {trial}: starving qa should be first, got {first_role}"

    def test_priority_ordering_preserved_within_role(self) -> None:
        """Within each role, tasks are always sorted by priority (lower number = higher priority)."""
        import random

        rng = random.Random(77)

        for trial in range(100):
            n_tasks = rng.randint(3, 10)
            tasks = [
                _make_task(id=f"T-{trial}-{i}", role="backend", priority=rng.randint(1, 3)) for i in range(n_tasks)
            ]
            batches = group_by_role(tasks, max_per_batch=10)

            for batch in batches:
                priorities = [t.priority for t in batch]
                assert priorities == sorted(priorities), f"Trial {trial}: priorities not sorted: {priorities}"

    def test_batch_homogeneity_invariant(self) -> None:
        """Every batch produced by group_by_role contains tasks of exactly one role."""
        import random

        rng = random.Random(55)
        roles = ["backend", "qa", "frontend", "docs"]

        for trial in range(100):
            n_tasks = rng.randint(1, 15)
            tasks = [
                _make_task(id=f"T-{trial}-{i}", role=rng.choice(roles), priority=rng.randint(1, 3))
                for i in range(n_tasks)
            ]
            max_per = rng.randint(1, 5)
            batches = group_by_role(tasks, max_per_batch=max_per)

            for batch_idx, batch in enumerate(batches):
                roles_in_batch = {t.role for t in batch}
                assert len(roles_in_batch) == 1, f"Trial {trial}, batch {batch_idx}: mixed roles {roles_in_batch}"
                assert len(batch) <= max_per, f"Trial {trial}, batch {batch_idx}: {len(batch)} > max {max_per}"

    def test_total_tasks_preserved(self) -> None:
        """Batching never duplicates or drops tasks."""
        import random

        rng = random.Random(33)
        roles = ["backend", "qa", "frontend"]

        for trial in range(100):
            n_tasks = rng.randint(1, 20)
            tasks = [_make_task(id=f"T-{trial}-{i}", role=rng.choice(roles)) for i in range(n_tasks)]
            batches = group_by_role(tasks, max_per_batch=rng.randint(1, 5))

            all_ids = sorted(t.id for batch in batches for t in batch)
            original_ids = sorted(t.id for t in tasks)
            assert all_ids == original_ids, f"Trial {trial}: task count mismatch: {len(all_ids)} vs {len(original_ids)}"


# ===========================================================================
# 5. Chaos Tests
# ===========================================================================


@pytest.mark.skip(reason="Requires real git repo in tmp_path; needs integration test setup")
class TestAgentCrashMidTask:
    """Simulate agent process dying while working on a task."""

    def test_dead_agent_task_is_handled(self, tmp_path: Path) -> None:
        """When an agent's PID dies, its tasks should be handled (retried or failed)."""
        task = _make_task(id="T-crash", role="backend")
        task_dicts = [_task_as_dict(task)]
        # Track state transitions through the handler
        claimed_tasks: list[str] = []
        failed_tasks: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in url.path:
                task_id = url.path.split("/")[-2]
                claimed_tasks.append(task_id)
                return httpx.Response(200, json=task_dicts[0])
            if request.method == "POST" and "/fail" in url.path:
                task_id = url.path.split("/")[-2]
                failed_tasks.append(task_id)
                return httpx.Response(200, json={})
            if request.method == "GET" and "/tasks/" in url.path:
                return httpx.Response(200, json=task_dicts[0])
            if request.method == "POST" and url.path == "/tasks":
                return httpx.Response(200, json={"id": "T-retry-new", "status": "open"})
            return httpx.Response(200, json={})

        adapter = _mock_adapter(pid=9999)
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
            max_task_retries=0,
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter, config=cfg)

        # First tick: spawn the agent
        result1 = orch.tick()
        assert len(result1.spawned) == 1

        # Simulate agent dying
        adapter.is_alive.return_value = False

        # Second tick: orchestrator should detect dead agent
        orch.tick()

        # The agent should now be dead
        all_dead = [s for s in orch._agents.values() if s.status == "dead"]
        assert len(all_dead) >= 1

    def test_crash_recovery_preserves_worktree_on_resume(self, tmp_path: Path) -> None:
        """With recovery='resume', crash recovery preserves the worktree path."""
        task = _make_task(id="T-resume", role="backend")
        task_dicts = [_task_as_dict(task)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=task_dicts[0])
            if request.method == "POST" and "/fail" in request.url.path:
                return httpx.Response(200, json={})
            if request.method == "GET" and "/tasks/" in request.url.path:
                return httpx.Response(200, json=task_dicts[0])
            if request.method == "POST" and request.url.path == "/tasks":
                return httpx.Response(200, json={"id": "T-new", "status": "open"})
            return httpx.Response(200, json={})

        adapter = _mock_adapter(pid=8888)
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
            recovery="resume",
            max_crash_retries=2,
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter, config=cfg)

        # Spawn agent
        orch.tick()

        # Simulate crash
        adapter.is_alive.return_value = False

        # Add a mock worktree path for the session
        for sid, session in orch._agents.items():
            if session.status != "dead":
                orch._spawner._worktree_paths = {sid: tmp_path / "worktrees" / sid}  # type: ignore[attr-defined]

        # Tick should detect crash and preserve worktree
        orch.tick()

        # Crash count should be incremented
        assert orch._crash_counts.get("T-resume", 0) >= 1


class TestServerCommunicationFailure:
    """Simulate task server being unreachable."""

    def test_fetch_failure_returns_empty_result(self, tmp_path: Path) -> None:
        """When GET /tasks fails, tick should return with errors but not crash."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(500, json={"error": "Internal Server Error"})
            return httpx.Response(200, json={})

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        result = orch.tick()

        assert len(result.errors) > 0
        assert result.spawned == []

    def test_claim_failure_does_not_crash(self, tmp_path: Path) -> None:
        """When POST /tasks/{id}/claim returns 409 (conflict), tick handles gracefully."""
        task = _make_task(id="T-conflict", role="backend")
        task_dicts = [_task_as_dict(task)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(409, json={"error": "Already claimed"})
            return httpx.Response(200, json={})

        orch = _build_orchestrator(
            tmp_path,
            httpx.MockTransport(handler),
            config=OrchestratorConfig(
                max_agents=6,
                poll_interval_s=1,
                max_tasks_per_agent=1,
                server_url="http://testserver",
            ),
        )

        # Should not raise — claim failures are handled gracefully
        result = orch.tick()
        assert isinstance(result, TickResult)


class TestNetworkPartition:
    """Simulate network issues during orchestrator operations."""

    def test_timeout_during_fetch_adds_error(self, tmp_path: Path) -> None:
        """Network timeout during task fetch should add error to result."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(200, json={})

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        result = orch.tick()
        assert len(result.errors) > 0

    def test_multiple_ticks_after_recovery(self, tmp_path: Path) -> None:
        """Orchestrator recovers after network comes back."""
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if request.method == "GET" and request.url.path == "/tasks":
                if call_count["n"] <= 1:
                    raise httpx.ConnectError("Connection refused")
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={})

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))

        # First tick: network failure
        result1 = orch.tick()
        assert len(result1.errors) > 0

        # Second tick: network recovered
        result2 = orch.tick()
        assert len(result2.errors) == 0


class TestSpawnBackoff:
    """Exponential backoff after repeated spawn failures."""

    def test_backoff_tracked_per_batch(self, tmp_path: Path) -> None:
        """Spawn failure backoff is tracked per batch key (set of task IDs)."""
        orch = _build_orchestrator(tmp_path, _empty_transport())
        batch_key = frozenset(["T-1", "T-2"])

        # Record a spawn failure
        orch._spawn_failures[batch_key] = (1, time.time())
        assert batch_key in orch._spawn_failures

        # After MAX_SPAWN_FAILURES consecutive failures, tasks should be marked failed
        orch._spawn_failures[batch_key] = (Orchestrator._MAX_SPAWN_FAILURES, time.time())
        count, _ = orch._spawn_failures[batch_key]
        assert count >= Orchestrator._MAX_SPAWN_FAILURES

    def test_backoff_expires_after_max_time(self, tmp_path: Path) -> None:
        """Stale backoff entries are cleaned up after SPAWN_BACKOFF_MAX_S."""
        orch = _build_orchestrator(tmp_path, _empty_transport())
        batch_key = frozenset(["T-old"])

        # Record an old failure (beyond max backoff window)
        orch._spawn_failures[batch_key] = (2, time.time() - Orchestrator._SPAWN_BACKOFF_MAX_S - 10)

        # Trigger cleanup via tick (refresh_agent_states purges expired entries)
        orch.tick()

        # Stale entry should have been purged
        assert batch_key not in orch._spawn_failures


class TestDeadAgentPurge:
    """Dead agents are purged to prevent unbounded memory growth."""

    def test_dead_agents_purged_beyond_max(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path, _empty_transport())

        # Add more dead agents than the max
        for i in range(Orchestrator._MAX_DEAD_AGENTS_KEPT + 10):
            session = AgentSession(
                id=f"dead-{i}",
                role="backend",
                pid=None,
                task_ids=[f"T-{i}"],
                status="dead",
                model_config=RouterModelConfig(model="sonnet", effort="normal"),
                spawn_ts=time.time(),
                heartbeat_ts=float(i),  # older agents have lower ts
            )
            orch._agents[session.id] = session

        # Purge should happen during tick
        from bernstein.core.agent_lifecycle import purge_dead_agents

        purge_dead_agents(orch)

        dead_count = sum(1 for a in orch._agents.values() if a.status == "dead")
        assert dead_count <= Orchestrator._MAX_DEAD_AGENTS_KEPT

    def test_purge_removes_oldest_first(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path, _empty_transport())
        n = Orchestrator._MAX_DEAD_AGENTS_KEPT + 5

        for i in range(n):
            session = AgentSession(
                id=f"dead-{i}",
                role="backend",
                pid=None,
                task_ids=[f"T-{i}"],
                status="dead",
                model_config=RouterModelConfig(model="sonnet", effort="normal"),
                spawn_ts=time.time(),
                heartbeat_ts=float(i),  # i=0 is oldest
            )
            orch._agents[session.id] = session

        from bernstein.core.agent_lifecycle import purge_dead_agents

        purge_dead_agents(orch)

        # The oldest agents (lowest heartbeat_ts) should have been removed
        remaining_ids = set(orch._agents.keys())
        for i in range(5):
            assert f"dead-{i}" not in remaining_ids, f"dead-{i} (oldest) should have been purged"


class TestDryRunMode:
    """Dry run mode should plan but not spawn."""

    def test_dry_run_plans_without_spawning(self, tmp_path: Path) -> None:
        task = _make_task(id="T-dry", role="backend", model="sonnet", effort="high")
        task_dicts = [_task_as_dict(task)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            return httpx.Response(200, json={})

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
        result = orch.tick()

        assert len(result.spawned) == 0, "Dry run should not spawn agents"
        assert len(result.dry_run_planned) > 0, "Dry run should record planned actions"


class TestProcessedDoneTasksCap:
    """_processed_done_tasks set is capped to prevent unbounded memory growth."""

    def test_set_capped_at_max(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path, _empty_transport())

        # Fill beyond max
        for i in range(Orchestrator._MAX_PROCESSED_DONE + 100):
            orch._processed_done_tasks[f"T-done-{i}"] = None

        assert len(orch._processed_done_tasks) > Orchestrator._MAX_PROCESSED_DONE

        # Trigger cap enforcement via tick (refresh_agent_states caps the set)
        orch.tick()

        assert len(orch._processed_done_tasks) <= Orchestrator._MAX_PROCESSED_DONE


# ===========================================================================
# 6. Integration Regression: Full Tick Cycle
# ===========================================================================


@pytest.mark.skip(reason="Requires real git repo; move to integration tests")
class TestFullTickCycle:
    """End-to-end tick cycle: fetch -> batch -> spawn -> verify -> reap."""

    def test_happy_path_single_task(self, tmp_path: Path) -> None:
        """Full cycle: one open task -> claimed -> agent spawned."""
        task = _make_task(id="T-happy", role="backend")
        task_dicts = [_task_as_dict(task)]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=task_dicts[0])
            return httpx.Response(200, json={})

        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
        result = orch.tick()

        assert len(result.spawned) == 1
        assert result.open_tasks >= 1
        assert result.active_agents >= 1

    def test_no_tasks_empty_tick(self, tmp_path: Path) -> None:
        """Empty task list produces an idle tick."""
        orch = _build_orchestrator(tmp_path, _empty_transport())
        result = orch.tick()

        assert result.spawned == []
        assert result.open_tasks == 0
        assert result.active_agents == 0

    def test_max_agents_cap_respected(self, tmp_path: Path) -> None:
        """No more than max_agents agents spawned at once."""
        tasks = [_make_task(id=f"T-{i}", role="backend") for i in range(10)]
        task_dicts = [_task_as_dict(t) for t in tasks]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=task_dicts)
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=task_dicts[0])
            return httpx.Response(200, json={})

        cfg = OrchestratorConfig(
            max_agents=3,
            poll_interval_s=1,
            max_tasks_per_agent=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), config=cfg)
        result = orch.tick()

        assert result.active_agents <= 3, f"Should not exceed max_agents=3, got {result.active_agents}"

    def test_tick_count_increments(self, tmp_path: Path) -> None:
        orch = _build_orchestrator(tmp_path, _empty_transport())
        assert orch._tick_count == 0
        orch.tick()
        assert orch._tick_count == 1
        orch.tick()
        assert orch._tick_count == 2


@pytest.mark.skip(reason="Requires real git repo; move to integration tests")
class TestBacklogIngestion:
    """Backlog files are ingested before task fetching."""

    def test_backlog_file_ingested(self, tmp_path: Path) -> None:
        """A .backlog.jsonl file in .sdd/backlog/open/ is sent to the server."""
        posted_payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "POST" and request.url.path == "/tasks":
                payload = json.loads(request.content)
                posted_payloads.append(payload)
                return httpx.Response(200, json={"id": "T-new", "status": "open"})
            return httpx.Response(200, json={})

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))

        # Create a backlog file
        backlog_dir = tmp_path / ".sdd" / "backlog" / "open"
        backlog_dir.mkdir(parents=True, exist_ok=True)
        backlog_file = backlog_dir / "test.backlog.jsonl"
        backlog_file.write_text(
            json.dumps(
                {
                    "title": "Backlog task",
                    "description": "From backlog",
                    "role": "backend",
                }
            )
            + "\n"
        )

        orch.tick()

        # The backlog task should have been posted to the server
        assert len(posted_payloads) >= 1
        assert any(p.get("title") == "Backlog task" for p in posted_payloads)
