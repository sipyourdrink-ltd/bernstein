"""Tests for the planning window functionality in the orchestrator."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from bernstein.core.models import (
    AgentSession,
    OrchestratorConfig,
    Task,
    TaskStatus,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.adapters.base import CLIAdapter, SpawnResult
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


class TestPlanningWindow:
    """Tests for the planning window functionality."""

    def test_initial_empty_ledger_does_not_terminate(
        self, tmp_path: Path
    ) -> None:
        """Orchestrator should not terminate on initial empty ledger (no tasks ever seen)."""
        # No tasks at all - initial state
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = OrchestratorConfig(
            planning_window_s=1.0,  # 1 second for fast test
            max_agents=1,
            poll_interval_s=0.1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Run multiple ticks - should not terminate
        for _ in range(10):
            result = orch.tick()
            assert orch._running is True, "Orchestrator should still be running"
            assert orch._closure_outcome == RunClosureOutcome.COMPLETED
            # Verify planning window state
            assert orch._ever_had_tasks is False
            assert orch._first_empty_ledger_ts is not None  # First empty ledger timestamp set

    def test_planning_window_terminates_after_seeing_tasks_then_empty(
        self, tmp_path: Path
    ) -> None:
        """Orchestrator should terminate after seeing tasks then empty ledger for planning_window_s."""
        # First return some tasks, then return empty
        task_call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal task_call_count
            if request.method == "GET" and request.url.path == "/tasks":
                task_call_count += 1
                if task_call_count <= 2:
                    # First two calls return a task
                    task = _make_task(id=f"T-{task_call_count}")
                    return httpx.Response(200, json=[_task_as_dict(task)])
                else:
                    # Subsequent calls return empty
                    return httpx.Response(200, json=[])
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=_task_as_dict(_make_task()))
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        # Set planning window to 0.5 seconds for fast test
        config = OrchestratorConfig(
            planning_window_s=0.5,
            max_agents=1,
            poll_interval_s=0.1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Run ticks until we've seen tasks and then empty for planning_window_s
        start_time = time.time()
        while orch._running and (time.time() - start_time) < 2.0:  # Safety timeout
            result = orch.tick()
            time.sleep(0.05)  # Small sleep to allow time to pass

        # Should have terminated due to planning window expiration
        assert orch._running is False, "Orchestrator should have stopped"
        assert orch._closure_outcome == RunClosureOutcome.FAILED
        assert orch._ever_had_tasks is True
        assert orch._first_empty_ledger_ts is not None

    def test_seeing_tasks_resets_empty_ledger_timer(
        self, tmp_path: Path
    ) -> None:
        """Seeing tasks after empty ledger should reset the planning window timer."""
        # Pattern: empty -> tasks -> empty -> should not terminate yet
        call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if request.method == "GET" and request.url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    # First call: empty
                    return httpx.Response(200, json=[])
                elif call_count == 2:
                    # Second call: has tasks
                    task = _make_task(id="T-1")
                    return httpx.Response(200, json=[_task_as_dict(task)])
                elif call_count == 3:
                    # Third call: empty again
                    return httpx.Response(200, json=[])
                else:
                    # Fourth call: empty again
                    return httpx.Response(200, json=[])
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=_task_as_dict(_make_task()))
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = OrchestratorConfig(
            planning_window_s=0.3,  # 300ms
            max_agents=1,
            poll_interval_s=0.05,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Run enough ticks to go through the sequence
        for i in range(20):  # Enough ticks at 50ms intervals = 1 second total
            result = orch.tick()
            assert orch._running is True, f"Should still be running after tick {i}"
            time.sleep(0.02)  # 20ms sleep

        # After seeing tasks again, the timer should be reset
        # We've seen: empty(1) -> tasks(2) -> empty(3) -> empty(4+)
        # The planning window should restart after seeing tasks at call_count=2
        # So we should not have terminated yet
        assert orch._ever_had_tasks is True
        assert orch._first_empty_ledger_ts is not None

    def test_planning_window_respected_when_tasks_appear_during_window(
        self, tmp_path: Path
    ) -> None:
        """If tasks appear during the planning window, timer should reset."""
        call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if request.method == "GET" and request.url.path == "/tasks":
                call_count += 1
                if call_count <= 3:
                    # First 3 calls: empty (starts planning window)
                    return httpx.Response(200, json=[])
                else:
                    # After that: has tasks (should reset timer)
                    task = _make_task(id=f"T-{call_count}")
                    return httpx.Response(200, json=[_task_as_dict(task)])
            if request.method == "POST" and "/claim" in request.url.path:
                return httpx.Response(200, json=_task_as_dict(_make_task()))
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = OrchestratorConfig(
            planning_window_s=0.4,  # 400ms
            max_agents=1,
            poll_interval_s=0.1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Run for longer than planning window but with tasks appearing during it
        start_time = time.time()
        while orch._running and (time.time() - start_time) < 1.5:  # Run for 1.5 seconds
            result = orch.tick()
            time.sleep(0.05)

        # Should still be running because tasks appeared during the window
        # and reset the timer
        assert orch._running is True
        assert orch._closure_outcome == RunClosureOutcome.COMPLETED
        assert orch._ever_had_tasks is True

    def test_planning_window_can_be_configured_via_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Planning window can be configured via BERNSTEIN_PLANNING_WINDOW_S env var."""
        monkeypatch.setenv("BERNSTEIN_PLANNING_WINDOW_S", "2.5")
        
        # Reload defaults to pick up the env var
        from bernstein.core import defaults
        from bernstein.core.defaults import reset
        
        reset()  # Clear any overrides
        
        # Check that the default was picked up
        assert defaults.ORCHESTRATOR.planning_window_s == 2.5
        
        reset()  # Clean up