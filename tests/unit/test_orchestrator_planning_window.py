"""Tests for the planning window functionality in the orchestrator."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.models import (
    OrchestratorConfig,
    Task,
    TaskStatus,
)
from bernstein.core.orchestrator import Orchestrator

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.orchestration.run_stall import (
    RunStallState,
    evaluate_run_stall,
    resolve_planning_window_s,
)
from bernstein.core.security.run_closure import RunClosureOutcome


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
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        status=TaskStatus(status),
    )


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
        # audit-017: ship typed retry fields so the orchestrator reads the
        # single source of truth on GET /tasks/{id}.
        "retry_count": task.retry_count,
        "max_retries": task.max_retries,
    }
    return result


def _tasks_response(url: httpx.URL, tasks: list[dict]) -> httpx.Response:
    """Return tasks, filtered by ?status= query param when present."""
    status = url.params.get("status")
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    return httpx.Response(200, json=tasks)


def _mock_adapter(pid: int = 42) -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _mock_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Build a mock transport that returns canned responses by URL path+query."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"
        if key in responses:
            return responses[key]
        if request.method == "GET" and url.path == "/tasks":
            # Try status filter, then aggregate
            status = url.params.get("status")
            if "GET /tasks" in responses:
                bulk_resp = responses["GET /tasks"]
                if bulk_resp.status_code == 200:
                    all_tasks = bulk_resp.json()
                    if status is not None:
                        filtered = [t for t in all_tasks if t.get("status") == status]
                        return httpx.Response(200, json=filtered)
                    return httpx.Response(200, json=all_tasks)
            # Fallback to empty
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"detail": f"No mock for {key}"})

    return httpx.MockTransport(handler)


def _build_orchestrator(
    tmp_path: Path,
    transport: httpx.MockTransport,
    adapter: CLIAdapter | None = None,
    config: OrchestratorConfig | None = None,
    default_model: str | None = "mock-model",
) -> Orchestrator:
    """Convenience: wire up orchestrator with mocked transport."""
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
    spawner = AgentSpawner(adp, templates_dir, tmp_path, default_model=default_model)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


def _empty_ledger() -> dict[str, list[Task]]:
    """A ledger with no task in any status - the shape from the incident."""
    return {"open": [], "claimed": [], "done": [], "failed": []}


class TestPlanningWindow:
    """Tests for the planning window functionality."""

    def test_empty_ledger_inside_the_window_is_not_a_stall(self) -> None:
        """Planning is allowed to be slow; an empty ledger starts benign."""
        _state, verdict = evaluate_run_stall(
            RunStallState(),
            _empty_ledger(),
            now=1000.0,
            grace_s=1800.0,
            min_ticks=1,
            planning_window_s=300.0,
        )
        assert verdict.stalled is False
        assert "startup window" in verdict.reason

    def test_empty_ledger_past_the_window_is_a_stall(self) -> None:
        """Past the bound an empty ledger is planning having failed, not startup."""
        state, verdict = evaluate_run_stall(
            RunStallState(),
            _empty_ledger(),
            now=1000.0,
            grace_s=1800.0,
            min_ticks=1,
            planning_window_s=300.0,
        )
        assert verdict.stalled is False, "the first tick only starts the clock"
        state, verdict = evaluate_run_stall(
            state,
            _empty_ledger(),
            now=1301.0,
            grace_s=1800.0,
            min_ticks=1,
            planning_window_s=300.0,
        )
        assert verdict.stalled is True
        assert "planning never produced a task graph" in verdict.reason

    def test_empty_ledger_clock_accumulates_across_ticks(self) -> None:
        """The clock must accumulate; resetting it each tick is the original defect."""
        state = RunStallState()
        verdict = None
        for tick in range(6):
            state, verdict = evaluate_run_stall(
                state,
                _empty_ledger(),
                now=1000.0 + tick * 10.0,
                grace_s=1800.0,
                min_ticks=1,
                planning_window_s=300.0,
            )
        assert verdict is not None
        assert verdict.stalled is False
        assert state.observed_ticks == 6, "each empty tick must count toward the window"
        assert verdict.quiet_for_s == 50.0

    def test_empty_ledger_stall_still_needs_the_consecutive_tick_floor(self) -> None:
        """A single time discontinuity must not end a run on its own."""
        state, verdict = evaluate_run_stall(
            RunStallState(),
            _empty_ledger(),
            now=1000.0,
            grace_s=1800.0,
            min_ticks=5,
            planning_window_s=300.0,
        )
        state, verdict = evaluate_run_stall(
            state,
            _empty_ledger(),
            now=99999.0,
            grace_s=1800.0,
            min_ticks=5,
            planning_window_s=300.0,
        )
        assert verdict.stalled is False, "2 ticks is under the 5-tick floor"

    def test_a_task_appearing_clears_the_empty_ledger_clock(self) -> None:
        """A slow planner that lands a graph must not inherit the empty clock."""
        state, _ = evaluate_run_stall(
            RunStallState(),
            _empty_ledger(),
            now=1000.0,
            grace_s=1800.0,
            min_ticks=1,
            planning_window_s=300.0,
        )
        populated = _empty_ledger()
        populated["open"] = [_make_task(id="T-001")]
        state, verdict = evaluate_run_stall(
            state,
            populated,
            now=1400.0,
            grace_s=1800.0,
            min_ticks=1,
            planning_window_s=300.0,
        )
        assert verdict.stalled is False, "work exists; the empty-ledger bound no longer applies"

    def test_run_with_an_empty_ledger_stops_as_failed(self, tmp_path: Path) -> None:
        """End to end: the tick loop terminates such a run, and says it failed.

        This is the shape from the incident - the planning task never lands a
        graph, so ``done``/``failed``/``open`` all stay empty and no agent can
        ever be acquired. Driven with a zero-length window so the test does not
        wait out a real one. ``tick()`` is called directly, so ``_running``
        (which only ``run()`` raises) is not the signal here - the stall
        backstop having stopped the run is.
        """
        monkey = pytest.MonkeyPatch()
        monkey.setenv("BERNSTEIN_PLANNING_WINDOW_S", "0")
        monkey.setenv("BERNSTEIN_STALLED_RUN_TICKS", "1")
        monkey.setenv("BERNSTEIN_QUIESCENCE_SETTLE_S", "0")
        try:

            def handler(request: httpx.Request) -> httpx.Response:
                if request.method == "GET" and request.url.path == "/tasks":
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json={})

            orch = _build_orchestrator(
                tmp_path,
                httpx.MockTransport(handler),
                config=OrchestratorConfig(
                    max_agents=1,
                    poll_interval_s=1,
                    server_url="http://testserver",
                ),
            )
            for _ in range(5):
                orch.tick()

            assert orch._run_stall_stopped is True, "an empty ledger must not tick forever"
            assert orch._closure_outcome == RunClosureOutcome.FAILED, (
                "a run that produced no task graph did not succeed"
            )
        finally:
            monkey.undo()

    def test_planning_window_can_be_configured_via_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BERNSTEIN_PLANNING_WINDOW_S overrides the configured window."""
        monkeypatch.setenv("BERNSTEIN_PLANNING_WINDOW_S", "2.5")
        assert resolve_planning_window_s(300.0) == 2.5

    def test_planning_window_falls_back_to_config_without_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without the env var the configured value is used unchanged."""
        monkeypatch.delenv("BERNSTEIN_PLANNING_WINDOW_S", raising=False)
        assert resolve_planning_window_s(300.0) == 300.0

    def test_planning_window_env_var_garbage_falls_back_to_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An unparseable env var must not crash the orchestrator tick."""
        monkeypatch.setenv("BERNSTEIN_PLANNING_WINDOW_S", "not-a-number")
        assert resolve_planning_window_s(300.0) == 300.0

    def test_orchestrator_config_takes_planning_window_from_defaults(self) -> None:
        """The config field is derived from the canonical defaults section."""
        from bernstein.core import defaults

        assert OrchestratorConfig().planning_window_s == defaults.ORCHESTRATOR.planning_window_s
