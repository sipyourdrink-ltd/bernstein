"""Tests for the Orchestrator — httpx calls and spawner are always mocked."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import httpx

from bernstein.adapters.base import CLIAdapter, SpawnResult
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
from bernstein.core.orchestrator import (
    Orchestrator,
    TickResult,
    group_by_role,
)
from bernstein.core.router import (
    ModelConfig as RouterModelConfig,
    ProviderConfig,
    ProviderHealthStatus,
    RouterState,
    Tier,
    TierAwareRouter,
)
from bernstein.core.spawner import AgentSpawner

# --- Helpers ---


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
) -> Task:
    return Task(
        id=id,
        title=title,
        description=description,
        role=role,
        priority=priority,
        scope=Scope(scope),
        complexity=Complexity(complexity),
        status=TaskStatus(status),
        task_type=task_type,
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
    }
    return result


def _tasks_response(url: httpx.URL, tasks: list[dict]) -> httpx.Response:
    """Return tasks, filtered by ?status= query param when present.

    Used in inline mock handlers so they handle both GET /tasks and
    GET /tasks?status=X correctly.
    """
    status = url.params.get("status")
    if status is not None:
        tasks = [t for t in tasks if t.get("status") == status]
    return httpx.Response(200, json=tasks)


def _mock_adapter(pid: int = 42) -> CLIAdapter:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = True
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _mock_transport(responses: dict[str, httpx.Response]) -> httpx.MockTransport:
    """Build a mock transport that returns canned responses by URL path+query.

    Args:
        responses: Mapping of "METHOD path?query" to httpx.Response.
                   e.g. {"GET /tasks?status=open": httpx.Response(200, json=[...])}
                   Also supports "GET /tasks" directly.  If "GET /tasks" is not
                   explicitly provided, it is auto-synthesised by aggregating all
                   200-status "GET /tasks?status=X" entries so that existing tests
                   do not need to be rewritten when the orchestrator switches to a
                   single bulk fetch.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"
        if key in responses:
            return responses[key]
        # Auto-filter: "GET /tasks?status=X" falls back to bulk "GET /tasks" ──
        if key.startswith("GET /tasks?status=") and "GET /tasks" in responses:
            bulk_resp = responses["GET /tasks"]
            if bulk_resp.status_code != 200:
                return bulk_resp
            status_val = url.params.get("status", "")
            filtered = [t for t in bulk_resp.json() if t.get("status") == status_val]
            return httpx.Response(200, json=filtered)
        # Auto-empty: unregistered status filters return [] (not 404) ──────────
        if key.startswith("GET /tasks?status="):
            return httpx.Response(200, json=[])
        # Auto-aggregate for legacy bulk-fetch path ────────────────────────────
        if key == "GET /tasks":
            aggregated: list[object] = []
            for resp_key, resp in responses.items():
                if resp_key.startswith("GET /tasks?status=") and resp.status_code == 200:
                    aggregated.extend(resp.json())
            if aggregated or any(
                k.startswith("GET /tasks?status=") for k in responses
            ):
                return httpx.Response(200, json=aggregated)
        return httpx.Response(404, json={"detail": f"No mock for {key}"})

    return httpx.MockTransport(handler)


def _build_orchestrator(
    tmp_path: Path,
    transport: httpx.MockTransport,
    adapter: CLIAdapter | None = None,
    config: OrchestratorConfig | None = None,
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
    spawner = AgentSpawner(adp, templates_dir, tmp_path)
    client = httpx.Client(transport=transport, base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


# --- Task.from_dict ---


class TestTaskFromDict:
    def test_round_trip(self) -> None:
        task = _make_task(id="T-099", role="qa", priority=1)
        raw = _task_as_dict(task)
        parsed = Task.from_dict(raw)

        assert parsed.id == "T-099"
        assert parsed.role == "qa"
        assert parsed.priority == 1
        assert parsed.status == TaskStatus.OPEN
        assert parsed.scope == Scope.MEDIUM

    def test_defaults_for_missing_fields(self) -> None:
        raw = {"id": "T-min", "title": "x", "description": "y", "role": "z"}
        parsed = Task.from_dict(raw)

        assert parsed.priority == 2
        assert parsed.scope == Scope.MEDIUM
        assert parsed.complexity == Complexity.MEDIUM
        assert parsed.status == TaskStatus.OPEN


# --- group_by_role ---


class TestGroupByRole:
    def test_single_role_single_batch(self) -> None:
        tasks = [_make_task(id="T-1"), _make_task(id="T-2")]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        assert len(batches[0]) == 2

    def test_single_role_splits_at_max(self) -> None:
        tasks = [_make_task(id=f"T-{i}") for i in range(5)]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 3  # 2+2+1
        assert len(batches[0]) == 2
        assert len(batches[1]) == 2
        assert len(batches[2]) == 1

    def test_multiple_roles_separate_batches(self) -> None:
        tasks = [
            _make_task(id="T-1", role="backend"),
            _make_task(id="T-2", role="qa"),
            _make_task(id="T-3", role="backend"),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 2
        for batch in batches:
            roles = {t.role for t in batch}
            assert len(roles) == 1  # each batch is same role

    def test_priority_ordering_within_role(self) -> None:
        tasks = [
            _make_task(id="T-low", priority=3),
            _make_task(id="T-crit", priority=1),
            _make_task(id="T-norm", priority=2),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        ids = [t.id for t in batches[0]]
        assert ids == ["T-crit", "T-norm", "T-low"]

    def test_empty_returns_empty(self) -> None:
        assert group_by_role([], max_per_batch=3) == []

    def test_critical_batch_sorted_first(self) -> None:
        tasks = [
            _make_task(id="T-1", role="qa", priority=3),
            _make_task(id="T-2", role="backend", priority=1),
        ]
        batches = group_by_role(tasks, max_per_batch=1)

        assert len(batches) == 2
        # The batch with priority=1 should come first
        assert batches[0][0].id == "T-2"
        assert batches[1][0].id == "T-1"

    def test_upgrade_proposal_gets_priority_boost(self) -> None:
        """Upgrade proposal tasks should be prioritized over same-priority standard tasks."""
        tasks = [
            _make_task(id="T-normal", priority=2, task_type=TaskType.STANDARD),
            _make_task(id="T-upgrade", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Upgrade should come first due to priority boost
        assert batches[0][0].id == "T-upgrade"
        assert batches[0][1].id == "T-normal"

    def test_upgrade_proposal_boost_respects_minimum_priority(self) -> None:
        """Priority boost should not go below 1."""
        tasks = [
            _make_task(id="T-crit-normal", priority=1, task_type=TaskType.STANDARD),
            _make_task(id="T-crit-upgrade", priority=1, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Both have effective priority 1 (upgrade would be 0, but capped), so original priority breaks tie
        # The upgrade should still come first due to secondary sort
        assert batches[0][0].id == "T-crit-upgrade"

    def test_upgrade_proposal_beats_lower_priority_standard(self) -> None:
        """Upgrade proposal with priority=2 should beat standard task with priority=1."""
        tasks = [
            _make_task(id="T-crit", priority=1, task_type=TaskType.STANDARD),
            _make_task(id="T-upgrade", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=2)

        assert len(batches) == 1
        # Upgrade with priority=2 gets boosted to effective priority=1, ties with crit
        # Original priority breaks tie, so crit (priority=1) comes first
        assert batches[0][0].id == "T-crit"
        assert batches[0][1].id == "T-upgrade"

    def test_multiple_upgrade_proposals_priority_ordering(self) -> None:
        """Multiple upgrade proposals should be ordered by their boosted priority."""
        tasks = [
            _make_task(id="T-upg-low", priority=3, task_type=TaskType.UPGRADE_PROPOSAL),
            _make_task(id="T-upg-crit", priority=1, task_type=TaskType.UPGRADE_PROPOSAL),
            _make_task(id="T-upg-norm", priority=2, task_type=TaskType.UPGRADE_PROPOSAL),
        ]
        batches = group_by_role(tasks, max_per_batch=3)

        assert len(batches) == 1
        ids = [t.id for t in batches[0]]
        # After boost: crit=0->1, norm=1, low=2
        assert ids == ["T-upg-crit", "T-upg-norm", "T-upg-low"]


# --- Orchestrator.tick ---


class TestOrchestratorTick:
    def test_spawns_agent_for_open_tasks(self, tmp_path: Path) -> None:
        tasks = [_make_task(id="T-1"), _make_task(id="T-2")]
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert result.open_tasks == 2
        assert len(result.spawned) == 1  # one batch of 2 tasks
        assert len(result.errors) == 0

    def test_respects_max_agents(self, tmp_path: Path) -> None:
        # 6 tasks across 3 roles -- but max_agents=2
        tasks = [
            _make_task(id="T-1", role="backend"),
            _make_task(id="T-2", role="backend"),
            _make_task(id="T-3", role="qa"),
            _make_task(id="T-4", role="qa"),
            _make_task(id="T-5", role="devops"),
            _make_task(id="T-6", role="devops"),
        ]
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
        })
        config = OrchestratorConfig(
            max_agents=2,
            poll_interval_s=1,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        result = orch.tick()

        assert len(result.spawned) == 2  # capped at max_agents

    def test_no_spawn_when_no_open_tasks(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert result.open_tasks == 0
        assert len(result.spawned) == 0

    def test_depends_on_blocks_scheduling_until_dep_done(self, tmp_path: Path) -> None:
        """Task B with depends_on=[A.id] is not scheduled until A is in status 'done'."""
        task_a = _make_task(id="T-A", role="backend")
        task_b = _make_task(id="T-B", role="backend")
        task_b.depends_on = ["T-A"]

        # Tick 1: A is open, B depends on A — only A should be scheduled
        transport = _mock_transport({
            "GET /tasks": httpx.Response(
                200, json=[_task_as_dict(task_a), _task_as_dict(task_b)]
            ),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        # Only task_a's batch spawned; task_b blocked by unmet dependency
        spawned_task_ids: list[str] = []
        for session in orch.active_agents.values():
            spawned_task_ids.extend(session.task_ids)
        assert "T-A" in spawned_task_ids
        assert "T-B" not in spawned_task_ids

    def test_depends_on_unblocked_when_dep_done(self, tmp_path: Path) -> None:
        """Task B with depends_on=[A.id] is scheduled once A appears in 'done'."""
        task_b = _make_task(id="T-B", role="backend")
        task_b.depends_on = ["T-A"]
        task_a_done = _make_task(id="T-A", role="backend", status="done")

        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task_b), _task_as_dict(task_a_done)]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        spawned_task_ids: list[str] = []
        for session in orch.active_agents.values():
            spawned_task_ids.extend(session.task_ids)
        assert "T-B" in spawned_task_ids

    def test_handles_server_error_on_fetch(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(500, text="Internal error"),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "fetch_all" in result.errors[0]

    def test_tracks_agents_across_ticks(self, tmp_path: Path) -> None:
        tasks_tick1 = [_make_task(id="T-1")]
        tasks_tick2 = [_make_task(id="T-2", role="qa")]

        tick_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal tick_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                status_filter = url.params.get("status")
                # Advance tick on first status query ("open") of each tick
                if status_filter == "open":
                    tick_count += 1
                tasks = tasks_tick1 if tick_count <= 1 else tasks_tick2
                filtered = [_task_as_dict(t) for t in tasks if status_filter is None or t.status.value == status_filter]
                return httpx.Response(200, json=filtered)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        r1 = orch.tick()
        assert len(r1.spawned) == 1

        r2 = orch.tick()
        assert len(r2.spawned) == 1

        # Two agents should be tracked now
        assert len(orch.active_agents) == 2

    def test_writes_log_file(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        orch.tick()

        log_path = tmp_path / ".sdd" / "runtime" / "orchestrator.log"
        assert log_path.exists()
        content = log_path.read_text()
        assert "open=0" in content
        assert "agents=" in content

    def test_spawn_failure_records_error(self, tmp_path: Path) -> None:
        tasks = [_make_task(id="T-1")]
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
        })
        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("process failed to start")
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "spawn" in result.errors[0]
        assert len(result.spawned) == 0

    def test_tick_skips_spawning_when_budget_exceeded(self, tmp_path: Path) -> None:
        """tick() must not spawn agents when cumulative cost has reached the budget cap."""
        # Simulate $0.10 already spent by writing a cost_efficiency metrics file
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        today = time.strftime("%Y-%m-%d")
        cost_file = metrics_dir / f"cost_efficiency_{today}.jsonl"
        cost_file.write_text(
            json.dumps({
                "timestamp": time.time(),
                "metric_type": "cost_efficiency",
                "value": 0.10,
                "labels": {"task_id": "T-prev", "role": "backend", "model": "sonnet"},
            }) + "\n"
        )

        task = _make_task(id="T-001")
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
        })
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=0.05,  # budget is $0.05, but $0.10 already spent
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert result.spawned == []

    def test_tick_spawns_normally_when_under_budget(self, tmp_path: Path) -> None:
        """tick() spawns normally when spent < budget_usd."""
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        today = time.strftime("%Y-%m-%d")
        cost_file = metrics_dir / f"cost_efficiency_{today}.jsonl"
        cost_file.write_text(
            json.dumps({
                "timestamp": time.time(),
                "metric_type": "cost_efficiency",
                "value": 0.01,
                "labels": {"task_id": "T-prev", "role": "backend", "model": "sonnet"},
            }) + "\n"
        )

        task = _make_task(id="T-001")
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
        })
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=1.00,  # $0.01 spent < $1.00 budget
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        result = orch.tick()

        assert len(result.spawned) == 1

    def test_tick_no_budget_check_when_budget_is_zero(self, tmp_path: Path) -> None:
        """tick() never enforces a budget when budget_usd=0 (default)."""
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        today = time.strftime("%Y-%m-%d")
        cost_file = metrics_dir / f"cost_efficiency_{today}.jsonl"
        cost_file.write_text(
            json.dumps({
                "timestamp": time.time(),
                "metric_type": "cost_efficiency",
                "value": 999.99,
                "labels": {"task_id": "T-prev", "role": "backend", "model": "sonnet"},
            }) + "\n"
        )

        task = _make_task(id="T-001")
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
        })
        adapter = _mock_adapter()
        # Default config has budget_usd=0 (no cap)
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            budget_usd=0.0,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        result = orch.tick()

        assert len(result.spawned) == 1

    def test_dry_run_prevents_spawning(self, tmp_path: Path) -> None:
        """tick() with dry_run=True logs planned spawns but never calls adapter.spawn."""
        task = _make_task(id="T-dry", role="backend", title="Build something")
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
        })
        adapter = _mock_adapter()
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert result.spawned == []
        assert len(result.dry_run_planned) == 1
        role, title, _model, _effort = result.dry_run_planned[0]
        assert role == "backend"
        assert title == "Build something"


# --- Spawn resilience: claim-before-spawn, backoff, and failure escalation ---


class TestSpawnResiliency:
    """Server outage and spawn failure scenarios."""

    def test_claim_500_aborts_spawn(self, tmp_path: Path) -> None:
        """Server 500 on task claim aborts spawn — agent must not be launched."""
        task = _make_task(id="T-claim-500")

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(task)])
            if request.method == "POST" and url.path == "/tasks/T-claim-500/claim":
                return httpx.Response(500, json={"detail": "internal server error"})
            return httpx.Response(404)

        adapter = _mock_adapter()
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert len(result.spawned) == 0
        assert any("claim" in e for e in result.errors)

    def test_claim_connection_error_aborts_spawn(self, tmp_path: Path) -> None:
        """Server unreachable during claim aborts spawn without crashing."""
        task = _make_task(id="T-claim-conn")

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(task)])
            if request.method == "POST" and url.path == "/tasks/T-claim-conn/claim":
                raise httpx.ConnectError("Connection refused")
            return httpx.Response(404)

        adapter = _mock_adapter()
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        result = orch.tick()

        adapter.spawn.assert_not_called()
        assert len(result.spawned) == 0
        assert any("claim" in e for e in result.errors)

    def test_spawn_failure_not_retried_within_backoff_window(self, tmp_path: Path) -> None:
        """A batch that failed to spawn is not retried until the backoff window expires."""
        task = _make_task(id="T-backoff")
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[_task_as_dict(task)]),
        })
        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("subprocess died")
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Tick 1: spawn attempt fails, failure is recorded
        r1 = orch.tick()
        assert adapter.spawn.call_count == 1
        assert any("spawn" in e for e in r1.errors)

        # Tick 2: immediately after — still within backoff window, batch must be skipped
        r2 = orch.tick()
        assert len(r2.spawned) == 0
        assert adapter.spawn.call_count == 1  # not retried

    def test_consecutive_spawn_failures_mark_tasks_failed(self, tmp_path: Path) -> None:
        """After MAX_SPAWN_FAILURES consecutive failures, tasks are marked failed on the server."""
        task = _make_task(id="T-maxfail")

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                status_filter = url.params.get("status")
                all_tasks = [_task_as_dict(task)]
                filtered = [t for t in all_tasks if status_filter is None or t.get("status") == status_filter]
                return httpx.Response(200, json=filtered)
            if request.method == "POST" and url.path == "/tasks/T-maxfail/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("always fails")
        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler), adapter=adapter)

        # Pre-seed failure count at (max - 1) with an expired backoff timestamp
        batch_key = frozenset(["T-maxfail"])
        max_failures = orch._MAX_SPAWN_FAILURES
        orch._spawn_failures[batch_key] = (max_failures - 1, 0.0)

        # This tick hits the limit and should mark the task as failed
        orch.tick()

        assert fail_called
        # Failure tracking is cleared after escalation
        assert batch_key not in orch._spawn_failures


# --- Reaping stale agents ---


class TestReaping:
    def test_reaps_stale_heartbeat(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True  # process is alive but heartbeat stale
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject a stale agent
        stale_session = AgentSession(
            id="backend-stale",
            role="backend",
            pid=999,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 120,  # 120s ago, threshold is 60
            status="working",
        )
        orch._agents["backend-stale"] = stale_session

        # Need fail endpoint for the reaped task
        fail_called = False
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if key == "GET /tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-stale":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-stale")))
            if key == "POST /tasks":
                return httpx.Response(201, json={"id": "T-stale-retry"})
            if key == "POST /tasks/T-stale/fail":
                fail_called = True
                return httpx.Response(200, json=_task_as_dict(
                    _make_task(id="T-stale", status="failed")
                ))
            return httpx.Response(404)

        # Rebuild with custom transport
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
        orch._client = client

        result = orch.tick()

        assert "backend-stale" in result.reaped
        assert stale_session.status == "dead"
        assert fail_called
        adapter.kill.assert_called_once_with(999)

    def test_does_not_reap_fresh_heartbeat(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject a fresh agent
        fresh_session = AgentSession(
            id="backend-fresh",
            role="backend",
            pid=100,
            task_ids=["T-fresh"],
            heartbeat_ts=time.time(),  # just now
            status="working",
        )
        orch._agents["backend-fresh"] = fresh_session

        result = orch.tick()

        assert len(result.reaped) == 0
        assert fresh_session.status == "working"

    def test_dead_process_marked_dead_on_refresh(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process exited
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-dead",
            role="backend",
            pid=77,
            status="working",
        )
        orch._agents["backend-dead"] = session

        orch.tick()

        assert session.status == "dead"

    def test_zero_heartbeat_not_reaped_if_alive(self, tmp_path: Path) -> None:
        """An agent that never heartbeated but whose process is alive is NOT reaped."""
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        session = AgentSession(
            id="backend-new",
            role="backend",
            pid=55,
            heartbeat_ts=0.0,  # never heartbeated
            status="working",
        )
        orch._agents["backend-new"] = session

        result = orch.tick()

        assert len(result.reaped) == 0
        assert session.status == "working"


# --- run / stop ---


class TestRunStop:
    def test_stop_breaks_loop(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        config = OrchestratorConfig(
            poll_interval_s=0,  # no sleep between ticks for test speed
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Patch tick to stop after 3 calls
        call_count = 0
        original_tick = orch.tick

        def counting_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                orch.stop()
            return original_tick()

        orch.tick = counting_tick  # type: ignore[assignment]
        orch.run()

        assert call_count == 3


# --- TickResult ---


class TestTickResult:
    def test_defaults(self) -> None:
        r = TickResult()
        assert r.open_tasks == 0
        assert r.active_agents == 0
        assert r.spawned == []
        assert r.reaped == []
        assert r.verified == []
        assert r.verification_failures == []
        assert r.errors == []


# --- Feature 1: Agent Completion Protocol ---


class TestAgentCompletionProtocol:
    """When an agent dies, orphaned tasks are verified and completed/failed."""

    def test_orphaned_task_with_signals_passes_janitor(self, tmp_path: Path) -> None:
        """Dead agent + open task + passing janitor => auto-complete."""
        # Create the file that the signal checks for
        (tmp_path / "output.txt").write_text("done")

        task = _make_task(id="T-orphan", status="in_progress")
        task_dict = _task_as_dict(task)
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "output.txt"}]
        task_dict["status"] = "in_progress"

        complete_called = False
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal complete_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key in ("GET /tasks", "GET /tasks?status=open", "GET /tasks?status=claimed",
                       "GET /tasks?status=done", "GET /tasks?status=failed"):
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orphan":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orphan/complete":
                complete_called = True
                return httpx.Response(200, json={"status": "done"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process died
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-dying",
            role="backend",
            pid=42,
            task_ids=["T-orphan"],
            status="working",
        )
        orch._agents["backend-dying"] = session

        orch.tick()

        assert session.status == "dead"
        assert complete_called

    def test_orphaned_task_with_signals_fails_janitor(self, tmp_path: Path) -> None:
        """Dead agent + open task + failing janitor => fail task."""
        # Do NOT create "missing.txt" so the signal fails

        task = _make_task(id="T-orphan-fail", status="in_progress")
        task_dict = _task_as_dict(task)
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "missing.txt"}]
        task_dict["status"] = "in_progress"

        fail_called = False
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key in ("GET /tasks", "GET /tasks?status=open", "GET /tasks?status=claimed",
                       "GET /tasks?status=done", "GET /tasks?status=failed"):
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-orphan-fail":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-orphan-fail/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-fail",
            role="backend",
            pid=42,
            task_ids=["T-orphan-fail"],
            status="working",
        )
        orch._agents["backend-fail"] = session

        orch.tick()

        assert session.status == "dead"
        assert fail_called

    def test_orphaned_task_no_signals_fails(self, tmp_path: Path) -> None:
        """Dead agent + open task + no completion signals => fail task."""
        task_dict = _task_as_dict(_make_task(id="T-nosig", status="in_progress"))
        task_dict["status"] = "in_progress"
        # no completion_signals field

        fail_called = False
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-nosig":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-nosig/fail":
                fail_called = True
                return httpx.Response(200, json={"status": "failed"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-nosig",
            role="backend",
            pid=42,
            task_ids=["T-nosig"],
            status="working",
        )
        orch._agents["backend-nosig"] = session

        orch.tick()

        assert session.status == "dead"
        assert fail_called

    def test_orphaned_task_already_done_skipped(self, tmp_path: Path) -> None:
        """If the task is already done on the server, do nothing."""
        task_dict = _task_as_dict(_make_task(id="T-done", status="done"))

        complete_called = False
        fail_called = False
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal complete_called, fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-done":
                return httpx.Response(200, json=task_dict)
            if "complete" in key:
                complete_called = True
                return httpx.Response(200, json={})
            if key.endswith("/fail"):
                fail_called = True
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-done",
            role="backend",
            pid=42,
            task_ids=["T-done"],
            status="working",
        )
        orch._agents["backend-done"] = session

        orch.tick()

        assert not complete_called
        assert not fail_called


# --- Feature 2: File Ownership Enforcement ---


class TestFileOwnership:
    """Track file ownership and skip batches with conflicting files."""

    def test_skips_batch_with_conflicting_files(self, tmp_path: Path) -> None:
        """Batch with owned_files that overlap active agent is skipped."""
        # Two tasks that own the same file, in different roles
        task1 = _make_task(id="T-1", role="backend")
        task1.owned_files = ["src/main.py"]
        task2 = _make_task(id="T-2", role="qa")
        task2.owned_files = ["src/main.py"]

        call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        # First tick: spawns agent for task1, claims src/main.py
        r1 = orch.tick()
        assert len(r1.spawned) == 1
        assert "src/main.py" in orch._file_ownership

        # Second tick: task2 also needs src/main.py => skipped
        r2 = orch.tick()
        assert len(r2.spawned) == 0

    def test_releases_ownership_on_death(self, tmp_path: Path) -> None:
        """File ownership is released when an agent dies."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False  # process died
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Pre-populate ownership
        orch._file_ownership["src/main.py"] = "backend-owner"
        session = AgentSession(
            id="backend-owner",
            role="backend",
            pid=42,
            task_ids=[],  # no tasks to avoid orphan handler needing endpoints
            status="working",
        )
        orch._agents["backend-owner"] = session

        orch.tick()

        assert session.status == "dead"
        assert "src/main.py" not in orch._file_ownership

    def test_no_conflict_when_files_differ(self, tmp_path: Path) -> None:
        """Batches with non-overlapping owned_files spawn normally."""
        task1 = _make_task(id="T-1", role="backend")
        task1.owned_files = ["src/a.py"]
        task2 = _make_task(id="T-2", role="qa")
        task2.owned_files = ["src/b.py"]

        call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport)

        r1 = orch.tick()
        assert len(r1.spawned) == 1

        r2 = orch.tick()
        assert len(r2.spawned) == 1  # no conflict, spawns fine

    def test_ownership_released_on_reap(self, tmp_path: Path) -> None:
        """File ownership released when a stale agent is reaped."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            heartbeat_timeout_s=60,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        orch._file_ownership["src/owned.py"] = "backend-stale"
        session = AgentSession(
            id="backend-stale",
            role="backend",
            pid=99,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 120,  # stale
            status="working",
        )
        orch._agents["backend-stale"] = session

        # Add fail endpoint for reaped tasks
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "POST /tasks/T-stale/fail":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://testserver",
        )

        orch.tick()

        assert "src/owned.py" not in orch._file_ownership


# --- Feature 3: Metrics Emission ---


class TestOrphanMetrics:
    """Orphaned task handling emits MetricsRecord to .sdd/metrics/."""

    def test_metrics_written_on_orphan_complete(self, tmp_path: Path) -> None:
        """Successful auto-complete writes a metrics JSONL record."""
        (tmp_path / "result.txt").write_text("ok")

        task_dict = _task_as_dict(_make_task(id="T-met", status="in_progress"))
        task_dict["completion_signals"] = [{"type": "path_exists", "value": "result.txt"}]
        task_dict["status"] = "in_progress"

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-met":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-met/complete":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-met",
            role="backend",
            pid=42,
            task_ids=["T-met"],
            status="working",
        )
        orch._agents["backend-met"] = session

        orch.tick()

        # Check that a metrics file was written
        metrics_dir = tmp_path / ".sdd" / "metrics"
        jsonl_files = list(metrics_dir.glob("*.jsonl"))
        assert len(jsonl_files) >= 1

        # Parse the record
        lines = jsonl_files[0].read_text().strip().split("\n")
        record = json.loads(lines[-1])  # last line is our record

        assert record["task_id"] == "T-met"
        assert record["agent_id"] == "backend-met"
        assert record["role"] == "backend"
        assert record["success"] is True
        assert record["error_type"] is None
        assert record["schema_version"] == 1
        assert "timestamp" in record
        assert "duration_seconds" in record

    def test_metrics_written_on_orphan_fail(self, tmp_path: Path) -> None:
        """Failed orphan writes a metrics record with error_type set."""
        task_dict = _task_as_dict(_make_task(id="T-fail-met", status="claimed"))
        task_dict["status"] = "claimed"
        # No completion signals

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-fail-met":
                return httpx.Response(200, json=task_dict)
            if key == "POST /tasks/T-fail-met/fail":
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        session = AgentSession(
            id="backend-failmet",
            role="backend",
            pid=42,
            task_ids=["T-fail-met"],
            status="working",
        )
        orch._agents["backend-failmet"] = session

        orch.tick()

        metrics_dir = tmp_path / ".sdd" / "metrics"
        jsonl_files = list(metrics_dir.glob("*.jsonl"))
        assert len(jsonl_files) >= 1

        lines = jsonl_files[0].read_text().strip().split("\n")
        record = json.loads(lines[-1])

        assert record["task_id"] == "T-fail-met"
        assert record["success"] is False
        assert record["error_type"] == "no_signals"


# --- TierAwareRouter wiring ---


def _make_router_with_provider() -> TierAwareRouter:
    """Create a TierAwareRouter with a single test provider."""
    router = TierAwareRouter()
    router.register_provider(ProviderConfig(
        name="test_provider",
        models={
            "sonnet": RouterModelConfig("sonnet", "high"),
            "opus": RouterModelConfig("opus", "max"),
        },
        tier=Tier.STANDARD,
        cost_per_1k_tokens=0.003,
    ))
    return router


class TestTierAwareRouterWiring:
    """Verify TierAwareRouter is wired into the orchestrator correctly."""

    def test_orchestrator_accepts_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch._router = router

        assert orch._router is router

    def test_orchestrator_constructor_with_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path, router=router)
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        assert orch._router is router
        assert orch._router.state.providers["test_provider"].name == "test_provider"

    def test_record_provider_health_updates_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        session = AgentSession(
            id="backend-123",
            role="backend",
            pid=42,
            provider="test_provider",
        )
        orch._record_provider_health(session, success=True, latency_ms=100.0)

        provider = router.state.providers["test_provider"]
        assert provider.health.consecutive_successes == 1
        assert provider.health.avg_latency_ms > 0

    def test_record_provider_cost_updates_router(self, tmp_path: Path) -> None:
        router = _make_router_with_provider()
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        session = AgentSession(
            id="backend-123",
            role="backend",
            pid=42,
            provider="test_provider",
        )
        orch._record_provider_health(
            session, success=True, cost_usd=0.05, tokens=1000,
        )

        provider = router.state.providers["test_provider"]
        assert provider.cost_tracker.total_cost_usd == 0.05
        assert provider.cost_tracker.total_tokens == 1000

    def test_no_router_is_noop(self, tmp_path: Path) -> None:
        """When no router is configured, health/cost recording is a no-op."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        assert orch._router is None

        session = AgentSession(
            id="backend-123", role="backend", pid=42, provider="x",
        )
        # Should not raise
        orch._record_provider_health(session, success=True, cost_usd=1.0, tokens=500)

    def test_loads_router_from_providers_yaml(self, tmp_path: Path) -> None:
        """TierAwareRouter auto-loads providers when providers.yaml exists."""
        config_dir = tmp_path / ".sdd" / "config"
        config_dir.mkdir(parents=True)
        providers_yaml = config_dir / "providers.yaml"
        providers_yaml.write_text(
            "providers:\n"
            "  yaml_provider:\n"
            "    tier: standard\n"
            "    cost_per_1k_tokens: 0.01\n"
            "    models:\n"
            "      opus:\n"
            "        model: opus\n"
            "        effort: max\n"
        )

        router = TierAwareRouter()
        # Router starts with no providers; constructor loads from YAML
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        cfg = OrchestratorConfig(server_url="http://testserver")
        adapter = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adapter, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)

        # The orchestrator's __init__ should have loaded from YAML
        assert "yaml_provider" in orch._router.state.providers


# --- Feature 4: Backlog Sync ---


class TestBacklogSync:
    """When a task is marked done, its .md file moves from backlog/open/ to backlog/closed/."""

    def _setup_backlog(self, tmp_path: Path, filenames: list[str]) -> Path:
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        open_dir.mkdir(parents=True)
        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        closed_dir.mkdir(parents=True)
        for name in filenames:
            (open_dir / name).write_text(f"# {name}\n\nTask description here.\n")
        return open_dir

    def test_done_task_moves_matching_backlog_file(self, tmp_path: Path) -> None:
        """A done task with title matching a backlog file moves it to closed/."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-done-1",
            title="Implement risk-stratified ApprovalGate",
            status="done",
        )

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        assert not (open_dir / "104-approval-gate-router.md").exists()
        assert (closed_dir / "104-approval-gate-router.md").exists()

    def test_closed_file_has_completion_timestamp(self, tmp_path: Path) -> None:
        """Moved file has a completion timestamp appended."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-done-2",
            title="Implement risk-stratified ApprovalGate",
            status="done",
        )
        done_task.result_summary = "All tests pass"

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        closed_path = tmp_path / ".sdd" / "backlog" / "closed" / "104-approval-gate-router.md"
        content = closed_path.read_text()
        assert "completed" in content.lower() or "done" in content.lower()

    def test_no_match_leaves_open_unchanged(self, tmp_path: Path) -> None:
        """A done task with no matching backlog file leaves open/ intact."""
        self._setup_backlog(tmp_path, ["104-approval-gate-router.md"])

        done_task = _make_task(
            id="T-nomatch",
            title="Some completely unrelated task xyz",
            status="done",
        )

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        assert (open_dir / "104-approval-gate-router.md").exists()

    def test_sync_not_repeated_for_already_processed_task(self, tmp_path: Path) -> None:
        """A task processed in tick 1 is not re-synced in tick 2."""
        self._setup_backlog(tmp_path, ["114-sync-backlog-files-with-server.md"])

        done_task = _make_task(
            id="T-rep",
            title="Sync .sdd/backlog files with task server state",
            status="done",
        )
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()

        closed_dir = tmp_path / ".sdd" / "backlog" / "closed"
        assert (closed_dir / "114-sync-backlog-files-with-server.md").exists()

        # Second tick: file already moved, no crash
        orch.tick()
        assert (closed_dir / "114-sync-backlog-files-with-server.md").exists()

    def test_no_backlog_dir_is_noop(self, tmp_path: Path) -> None:
        """If .sdd/backlog/open/ does not exist, sync silently does nothing."""
        done_task = _make_task(id="T-nodir", title="Whatever task", status="done")
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(done_task)]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        orch.tick()  # Should not raise


# --- Feature 5: Evolve Mode (idle detection + re-planning) ---


def _write_evolve_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    max_cycles: int = 0,
    budget_usd: float = 0.0,
    interval_s: int = 0,
    cycle_count: int = 0,
    last_cycle_ts: float = 0.0,
    consecutive_empty: int = 0,
    spent_usd: float = 0.0,
) -> Path:
    """Write an evolve.json config for testing."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = {
        "enabled": enabled,
        "max_cycles": max_cycles,
        "budget_usd": budget_usd,
        "interval_s": interval_s,
        "_cycle_count": cycle_count,
        "_last_cycle_ts": last_cycle_ts,
        "_consecutive_empty": consecutive_empty,
        "_spent_usd": spent_usd,
    }
    path = runtime / "evolve.json"
    path.write_text(json.dumps(config))
    return path


def _evolve_handler(
    *,
    open_tasks: list[dict[str, object]] | None = None,
    claimed_tasks: list[dict[str, object]] | None = None,
    done_tasks: list[dict[str, object]] | None = None,
    manager_task_created: list[dict[str, object]] | None = None,
) -> httpx.MockTransport:
    """Build a transport that tracks manager task creation for evolve tests."""
    _open = open_tasks or []
    _claimed = claimed_tasks or []
    _done = done_tasks or []
    created = manager_task_created if manager_task_created is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks":
            return _tasks_response(url, _open + _claimed + _done)
        if request.method == "POST" and url.path == "/tasks":
            body = json.loads(request.content)
            created.append(body)
            return httpx.Response(200, json={"id": "T-evolve-mgr"})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler)


class TestEvolveIdleDetection:
    """Tests for _check_evolve: idle detection and re-planning trigger."""

    def test_no_evolve_config_is_noop(self, tmp_path: Path) -> None:
        """No evolve.json => evolve check silently does nothing."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        result = orch.tick()
        assert result.errors == []

    def test_evolve_disabled_is_noop(self, tmp_path: Path) -> None:
        """evolve.json with enabled=false does nothing."""
        _write_evolve_config(tmp_path, enabled=False)
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        result = orch.tick()
        assert result.errors == []

    def test_evolve_triggers_when_idle(self, tmp_path: Path) -> None:
        """When idle (no open/claimed tasks, no agents), creates a manager task."""
        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,  # disable EvolutionCoordinator to isolate _check_evolve
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        # Patch out test/commit steps to avoid subprocess calls
        orch._evolve_run_tests = lambda: {"passed": 5, "failed": 0, "summary": "5 passed"}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        assert len(created) == 1
        assert "manager" == created[0]["role"]
        assert "Evolve cycle" in str(created[0]["title"])

    def test_evolve_does_not_trigger_when_tasks_open(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger when there are still open tasks."""
        _write_evolve_config(tmp_path, interval_s=0)
        task = _make_task(id="T-open")
        created: list[dict[str, object]] = []
        transport = _evolve_handler(
            open_tasks=[_task_as_dict(task)],
            manager_task_created=created,
        )
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_does_not_trigger_when_agents_alive(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger when agents are still running."""
        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        # Inject an alive agent
        session = AgentSession(
            id="backend-busy", role="backend", pid=42,
            task_ids=["T-x"], status="working",
        )
        orch._agents["backend-busy"] = session

        orch.tick()

        assert len(created) == 0

    def test_evolve_stops_at_max_cycles(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger after max_cycles is reached."""
        _write_evolve_config(tmp_path, max_cycles=3, cycle_count=3, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_stops_at_budget(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger after budget_usd is exhausted."""
        _write_evolve_config(
            tmp_path, budget_usd=10.0, spent_usd=10.0, interval_s=0,
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_respects_interval(self, tmp_path: Path) -> None:
        """Evolve does NOT trigger before the interval elapses."""
        _write_evolve_config(
            tmp_path,
            interval_s=9999,
            last_cycle_ts=time.time(),  # just ran
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        assert len(created) == 0

    def test_evolve_logs_cycle_to_jsonl(self, tmp_path: Path) -> None:
        """Each evolve cycle is logged to evolve_cycles.jsonl."""
        _write_evolve_config(tmp_path, interval_s=0)
        transport = _evolve_handler()
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        log_path = tmp_path / ".sdd" / "metrics" / "evolve_cycles.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["cycle"] == 1
        assert "focus_area" in entry
        assert "timestamp" in entry

    def test_evolve_rotates_focus_areas(self, tmp_path: Path) -> None:
        """Successive cycles rotate through different focus areas."""
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        titles: list[str] = []
        for i in range(3):
            _write_evolve_config(tmp_path, interval_s=0, cycle_count=i)
            created.clear()
            orch.tick()
            if created:
                titles.append(str(created[0]["title"]))

        # Each cycle should have a different focus
        assert len(titles) == 3
        assert titles[0] != titles[1]

    def test_evolve_updates_cycle_count(self, tmp_path: Path) -> None:
        """After a cycle, _cycle_count is incremented in evolve.json."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0)
        transport = _evolve_handler()
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        orch.tick()

        updated = json.loads(evolve_path.read_text())
        assert updated["_cycle_count"] == 1
        assert updated["_last_cycle_ts"] > 0

    def test_evolve_diminishing_returns_backoff(self, tmp_path: Path) -> None:
        """After 3+ consecutive empty cycles, interval increases via backoff."""
        # 3 consecutive empty cycles with interval_s=100 => effective interval = 100 * 2 = 200
        _write_evolve_config(
            tmp_path,
            interval_s=100,
            consecutive_empty=3,
            last_cycle_ts=time.time() - 150,  # 150s ago (< 200s effective interval)
        )
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch.tick()

        # Should NOT trigger: 150s < 200s (100 * 2^1)
        assert len(created) == 0

    def test_evolve_includes_research_context(self, tmp_path: Path) -> None:
        """When Tavily research succeeds, the manager task gets market context."""
        from unittest.mock import patch

        from bernstein.core.researcher import ResearchReport, ResearchResult

        _write_evolve_config(tmp_path, interval_s=0)
        created: list[dict[str, object]] = []
        transport = _evolve_handler(manager_task_created=created)
        config = OrchestratorConfig(
            server_url="http://testserver",
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)
        orch._evolve_run_tests = lambda: {"passed": 0, "failed": 0, "summary": ""}  # type: ignore[assignment]
        orch._evolve_auto_commit = lambda: False  # type: ignore[assignment]

        fake_report = ResearchReport(
            competitors=[ResearchResult(query="q", content="CompetitorX data", timestamp=1.0)],
            searches_performed=1,
        )
        with patch("bernstein.core.researcher.run_research_sync", return_value=fake_report):
            orch.tick()

        assert len(created) == 1
        desc = str(created[0]["description"])
        assert "CompetitorX data" in desc


# --- Provider health recording and evolution metrics ---


class TestProviderHealthRecording:
    """Orchestrator records provider health feedback on done task processing."""

    def _build_with_router(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        router = MagicMock(spec=TierAwareRouter)
        router.state = RouterState(providers={})
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(_make_task(id="T-done", status="done"))]),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, router=router)
        return orch, router

    def test_tick_records_provider_health_on_success(self, tmp_path: Path) -> None:
        orch, router = self._build_with_router(tmp_path)

        # Inject a session that owns T-done with a known provider
        session = AgentSession(
            id="backend-a",
            role="backend",
            pid=10,
            task_ids=["T-done"],
            provider="anthropic",
            status="working",
        )
        orch._agents["backend-a"] = session

        orch.tick()

        router.update_provider_health.assert_called_once_with("anthropic", True, 0.0)

    def test_tick_records_provider_health_on_failure(self, tmp_path: Path) -> None:
        orch, router = self._build_with_router(tmp_path)

        # Build task JSON with completion_signals so the janitor runs
        done_task_json = _task_as_dict(_make_task(id="T-done-sig", status="done"))
        done_task_json["completion_signals"] = [
            {"type": "file_exists", "value": "definitely_missing_file.txt"}
        ]

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[done_task_json]),
        })
        orch._client = httpx.Client(transport=transport, base_url="http://testserver")

        session = AgentSession(
            id="backend-c",
            role="backend",
            pid=12,
            task_ids=["T-done-sig"],
            provider="openai",
            status="working",
        )
        orch._agents["backend-c"] = session

        orch.tick()

        # Janitor fails (file does not exist) → success=False
        router.update_provider_health.assert_called_once_with("openai", False, 0.0)

    def test_tick_without_router_skips_health(self, tmp_path: Path) -> None:
        """No crash when router is None and a done task is processed."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(_make_task(id="T-done2", status="done"))]),
        })
        orch = _build_orchestrator(tmp_path, transport)
        assert orch._router is None

        session = AgentSession(
            id="backend-d",
            role="backend",
            pid=13,
            task_ids=["T-done2"],
            provider="anthropic",
            status="working",
        )
        orch._agents["backend-d"] = session

        # Should not raise even without a router
        result = orch.tick()
        assert len(result.errors) == 0


class TestEvolutionMetricsRecording:
    """Orchestrator records task completion to EvolutionCoordinator."""

    def _build_with_evolution(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[_task_as_dict(_make_task(id="T-evo", status="done"))]),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)
        return orch, evolution

    def test_tick_records_evolution_metrics(self, tmp_path: Path) -> None:
        orch, evolution = self._build_with_evolution(tmp_path)

        session = AgentSession(
            id="backend-evo",
            role="backend",
            pid=20,
            task_ids=["T-evo"],
            provider="anthropic",
            spawn_ts=time.time() - 5.0,  # 5 seconds ago
            status="working",
        )
        orch._agents["backend-evo"] = session

        orch.tick()

        evolution.record_task_completion.assert_called_once()
        call_kwargs = evolution.record_task_completion.call_args
        assert call_kwargs.kwargs["janitor_passed"] is True
        assert call_kwargs.kwargs["duration_seconds"] >= 0.0

    def test_tick_evolution_record_failure_logged(self, tmp_path: Path) -> None:
        """If record_task_completion raises, the orchestrator catches it and does not crash."""
        orch, evolution = self._build_with_evolution(tmp_path)
        evolution.record_task_completion.side_effect = RuntimeError("db failure")

        session = AgentSession(
            id="backend-evo2",
            role="backend",
            pid=21,
            task_ids=["T-evo"],
            status="working",
        )
        orch._agents["backend-evo2"] = session

        # Must not raise
        result = orch.tick()
        assert len(result.errors) == 0
        evolution.record_task_completion.assert_called_once()


class TestConsecutiveTickFailureCircuitBreaker:
    """run() exits after max_consecutive_failures tick exceptions."""

    def test_run_stops_after_max_consecutive_failures(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        config = OrchestratorConfig(
            poll_interval_s=0,
            server_url="http://testserver",
        )
        orch = _build_orchestrator(tmp_path, transport, config=config)

        call_count = 0

        def always_failing_tick() -> TickResult:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("tick exploded")

        orch.tick = always_failing_tick  # type: ignore[assignment]
        orch.run()

        # 10 consecutive failures → loop breaks
        assert call_count == 10


# --- New edge-case coverage ---


class TestDeadAgentFileOwnershipEdgeCases:
    """Edge cases for file ownership release and respawn after agent death."""

    def test_dead_agent_file_ownership_released(self, tmp_path: Path) -> None:
        """When an agent process dies, all its file ownership entries are removed."""
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.is_alive.return_value = False
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Pre-claim two files for the dying agent
        orch._file_ownership["src/main.py"] = "backend-dying"
        orch._file_ownership["src/utils.py"] = "backend-dying"

        session = AgentSession(
            id="backend-dying",
            role="backend",
            pid=42,
            task_ids=[],  # no tasks avoids needing orphan-handler endpoints
            status="working",
        )
        orch._agents["backend-dying"] = session

        orch.tick()

        assert session.status == "dead"
        assert "src/main.py" not in orch._file_ownership
        assert "src/utils.py" not in orch._file_ownership

    def test_file_overlap_cleared_after_dead_agent_allows_respawn(
        self, tmp_path: Path
    ) -> None:
        """Spawn is blocked while an agent owns a file; after it dies the next tick spawns."""
        task1 = _make_task(id="T-owner", role="backend")
        task1.owned_files = ["src/shared.py"]
        task2 = _make_task(id="T-waiter", role="qa")
        task2.owned_files = ["src/shared.py"]

        # Reflect task1 as "in_progress" so the orphan handler skips completing it
        task1_inprog = _make_task(id="T-owner", role="backend", status="in_progress")

        tick = 0
        is_alive_flag = True

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal tick
            url = request.url
            key = f"{request.method} {url.path}"
            if request.method == "GET" and url.path == "/tasks":
                tick += 1
                if tick == 1:
                    return _tasks_response(url, [_task_as_dict(task1)])
                return _tasks_response(url, [_task_as_dict(task2)])
            if key == "GET /tasks/T-owner":
                # Orphan handler fetches the task; return it as in_progress (no signals → fail)
                return httpx.Response(200, json=_task_as_dict(task1_inprog))
            if key == "POST /tasks/T-owner/fail":
                return httpx.Response(200, json={})
            # claim endpoint and other best-effort calls
            return httpx.Response(200, json={})

        adapter = _mock_adapter()
        adapter.is_alive.side_effect = lambda session: is_alive_flag

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        # Tick 1: spawns agent for task1, claims src/shared.py
        r1 = orch.tick()
        assert len(r1.spawned) == 1
        assert "src/shared.py" in orch._file_ownership

        # Tick 2: task2 blocked because src/shared.py is still owned (agent alive)
        r2 = orch.tick()
        assert len(r2.spawned) == 0

        # Capture the id of the agent that owns the file before it dies
        dead_agent_id = orch._file_ownership["src/shared.py"]

        # Agent for task1 dies
        is_alive_flag = False

        # Tick 3: dead agent detected → file released → task2 can spawn
        r3 = orch.tick()
        assert len(r3.spawned) == 1
        # The dead agent must no longer own the file
        assert orch._file_ownership.get("src/shared.py") != dead_agent_id


class TestStaleHeartbeatReapingDefault:
    """An agent whose heartbeat exceeds the configured timeout is reaped and its tasks failed."""

    def test_stale_heartbeat_reaps_agent(self, tmp_path: Path) -> None:
        """Heartbeat older than heartbeat_timeout_s triggers reaping and task failure."""
        adapter = _mock_adapter()
        adapter.is_alive.return_value = True  # process alive but heartbeat is stale
        config = OrchestratorConfig(
            heartbeat_timeout_s=600,
            server_url="http://testserver",
        )

        fail_called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal fail_called
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if request.method == "GET" and url.path == "/tasks":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-stale":
                return httpx.Response(200, json=_task_as_dict(_make_task(id="T-stale")))
            if key == "POST /tasks":
                return httpx.Response(201, json={"id": "T-stale-retry"})
            if key == "POST /tasks/T-stale/fail":
                fail_called = True
                return httpx.Response(200, json={})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=config)

        stale_session = AgentSession(
            id="backend-stale-hb",
            role="backend",
            pid=77,
            task_ids=["T-stale"],
            heartbeat_ts=time.time() - 700,  # 700s ago > 600s threshold
            status="working",
            # spawn_ts defaults to time.time() so wall-clock timeout won't fire
        )
        orch._agents["backend-stale-hb"] = stale_session

        result = orch.tick()

        assert "backend-stale-hb" in result.reaped
        assert stale_session.status != "working"  # reaped but not necessarily "dead" yet
        assert fail_called
        adapter.kill.assert_called()


class TestAssignedTaskIdDoubleSpawn:
    """Two consecutive ticks with identical open tasks must not double-spawn agents."""

    def test_assigned_task_ids_prevents_double_spawn(self, tmp_path: Path) -> None:
        """Second tick skips batches whose tasks are already owned by alive agents."""
        task = _make_task(id="T-singleton", role="backend")

        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                call_count += 1
                # Same task returned on every tick (simulate server not yet updated)
                return _tasks_response(url, [_task_as_dict(task)])
            # claim / other endpoints
            return httpx.Response(200, json={})

        adapter = _mock_adapter()
        adapter.is_alive.return_value = True  # agent stays alive between ticks

        transport = httpx.MockTransport(handler)
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        r1 = orch.tick()
        assert len(r1.spawned) == 1  # first tick spawns

        r2 = orch.tick()
        assert len(r2.spawned) == 0  # second tick skips — task already assigned

        # Only one agent should exist in total
        non_dead = [s for s in orch.active_agents.values() if s.status != "dead"]
        assert len(non_dead) == 1


class TestEvolveAutoCommitRuntimeExclusion:
    """_evolve_auto_commit stages all changes then unstages .sdd/runtime/ and .sdd/metrics/."""

    def test_evolve_auto_commit_excludes_runtime_files(self, tmp_path: Path) -> None:
        """git add -A is followed by git reset HEAD -- .sdd/runtime/ .sdd/metrics/."""
        from unittest.mock import patch

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        status_result = MagicMock()
        status_result.stdout = "M src/bernstein/foo.py\n"

        test_result = MagicMock()
        test_result.returncode = 0

        completed_ok = MagicMock()
        completed_ok.returncode = 0
        completed_ok.stdout = ""

        def _fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[:2] == ["git", "status"]:
                return status_result
            if cmd[:2] == ["uv", "run"]:
                return test_result
            return completed_ok

        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            result = orch._evolve_auto_commit()

        assert result is True

        cmds = [c.args[0] for c in mock_run.call_args_list]

        # git add -A must appear
        assert ["git", "add", "-A"] in cmds

        # git reset HEAD -- .sdd/runtime/ .sdd/metrics/ must appear
        reset_cmd = [
            "git", "reset", "HEAD", "--", ".sdd/runtime/", ".sdd/metrics/",
        ]
        assert reset_cmd in cmds

        # reset must come after add
        add_idx = cmds.index(["git", "add", "-A"])
        reset_idx = cmds.index(reset_cmd)
        assert reset_idx > add_idx


# --- _retry_or_fail_task ---


class TestRetryOrFailTask:
    """Unit tests for Orchestrator._retry_or_fail_task."""

    def _build(
        self,
        tmp_path: Path,
        task: Task,
        *,
        max_retries: int = 2,
    ) -> tuple[Orchestrator, list[dict]]:
        """Return (orchestrator, captured_post_bodies).

        The mock transport:
        - GET /tasks/{id} → returns task JSON
        - POST /tasks       → records body, returns 201
        - POST /tasks/{id}/fail → returns 200
        """
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path == f"/tasks/{task.id}":
                return httpx.Response(200, json=_task_as_dict(task))
            if request.method == "POST" and path == "/tasks":
                posted.append(request.read() and __import__("json").loads(request.content))
                return httpx.Response(201, json={"id": "NEW-001"})
            if request.method == "POST" and path.endswith("/fail"):
                return httpx.Response(200, json={})
            return httpx.Response(404, json={"detail": f"No mock for {request.method} {path}"})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            max_task_retries=max_retries,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)
        return orch, posted

    def test_first_retry_creates_new_task(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert len(posted) == 1
        assert posted[0]["description"] == "[retry:1] Do the thing."

    def test_second_retry_increments_counter(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="[retry:1] Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed again")

        assert len(posted) == 1
        assert posted[0]["description"] == "[retry:2] Do the thing."

    def test_max_retries_exceeded_does_not_create_new_task(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="[retry:2] Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed yet again")

        # No new task should be created
        assert posted == []

    def test_zero_max_retries_always_fails(self, tmp_path: Path) -> None:
        task = _make_task(id="T-retry", description="Do the thing.")
        orch, posted = self._build(tmp_path, task, max_retries=0)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert posted == []

    def test_retry_preserves_task_fields(self, tmp_path: Path) -> None:
        task = _make_task(
            id="T-retry",
            role="security",
            priority=1,
            scope="large",
            complexity="high",
            description="Fix the vuln.",
        )
        orch, posted = self._build(tmp_path, task, max_retries=2)

        orch._retry_or_fail_task("T-retry", "agent crashed")

        assert len(posted) == 1
        body = posted[0]
        assert body["role"] == "security"
        assert body["priority"] == 1
        assert body["scope"] == "large"
        assert body["complexity"] == "high"
        assert body["title"] == task.title


# --- _maybe_retry_task ---


class TestMaybeRetryTask:
    """Unit tests for Orchestrator._maybe_retry_task."""

    def _build(
        self,
        tmp_path: Path,
        *,
        max_retries: int = 2,
    ) -> tuple[Orchestrator, list[dict]]:
        """Return (orchestrator, captured_post_bodies) with POST /tasks mocked."""
        posted: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(__import__("json").loads(request.content))
                return httpx.Response(201, json={"id": "NEW-RETRY"})
            return httpx.Response(404, json={"detail": "no mock"})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(server_url="http://testserver", max_task_retries=max_retries)
        orch = _build_orchestrator(tmp_path, transport, config=cfg)
        return orch, posted

    def test_first_retry_bumps_effort_keeps_model(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="low",
        )
        orch, posted = self._build(tmp_path)

        result = orch._maybe_retry_task(task)

        assert result is True
        assert len(posted) == 1
        body = posted[0]
        assert body["model"] == "sonnet"   # model unchanged
        assert body["effort"] == "medium"   # low → medium

    def test_first_retry_title_prefixed(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["title"] == "[RETRY 1] Do work"
        assert posted[0]["description"].startswith("[RETRY 1]")

    def test_second_retry_escalates_model(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 1] Do work",
            description="[RETRY 1] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="medium",
        )
        orch, posted = self._build(tmp_path)

        result = orch._maybe_retry_task(task)

        assert result is True
        body = posted[0]
        assert body["model"] == "opus"     # sonnet → opus
        assert body["effort"] == "high"    # reset to high

    def test_max_retries_respected(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 2] Do work",
            description="[RETRY 2] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path, max_retries=2)

        result = orch._maybe_retry_task(task)

        assert result is False
        assert posted == []

    def test_already_retried_task_not_retried_again(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)
        result = orch._maybe_retry_task(task)  # second call same task

        assert result is False
        assert len(posted) == 1  # only one POST made

    def test_retry_records_task_id_in_retried_set(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, _ = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert "T-fail" in orch._retried_task_ids

    def test_effort_capped_at_max(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="sonnet",
            effort="max",
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["effort"] == "max"  # already at max, stays max

    def test_haiku_escalates_to_sonnet_on_second_retry(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="[RETRY 1] Do work",
            description="[RETRY 1] Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
            model="haiku",
            effort="medium",
        )
        orch, posted = self._build(tmp_path)

        orch._maybe_retry_task(task)

        assert posted[0]["model"] == "sonnet"

    def test_zero_max_retries_never_retries(self, tmp_path: Path) -> None:
        task = Task(
            id="T-fail",
            title="Do work",
            description="Do the thing.",
            role="backend",
            status=TaskStatus.FAILED,
        )
        orch, posted = self._build(tmp_path, max_retries=0)

        result = orch._maybe_retry_task(task)

        assert result is False
        assert posted == []


# --- _replenish_backlog ---


class TestReplenishBacklog:
    """Tests for Orchestrator._replenish_backlog()."""

    _RUFF_VIOLATIONS = [
        {
            "filename": "src/foo.py",
            "code": "E501",
            "message": "Line too long (92 > 88 characters)",
            "location": {"row": 10, "column": 1},
        },
        {
            "filename": "src/bar.py",
            "code": "F401",
            "message": "`os` imported but unused",
            "location": {"row": 1, "column": 1},
        },
        {
            "filename": "src/baz.py",
            "code": "E501",  # duplicate rule — should deduplicate
            "message": "Line too long (99 > 88 characters)",
            "location": {"row": 20, "column": 1},
        },
    ]

    def _build_orch_evolve(
        self,
        tmp_path: Path,
        *,
        evolve_mode: bool = True,
        open_tasks_json: list[object] | None = None,
        done_tasks_json: list[object] | None = None,
        post_handler: object = None,
    ) -> tuple[Orchestrator, list[dict[str, object]]]:
        """Build an orchestrator in evolve mode with mocked HTTP and collected POST /tasks bodies."""
        if open_tasks_json is None:
            open_tasks_json = []
        if done_tasks_json is None:
            done_tasks_json = []

        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, list(open_tasks_json) + list(done_tasks_json))
            if request.method == "POST" and url.path == "/tasks":
                body = json.loads(request.content)
                posted.append(body)
                return httpx.Response(201, json={"id": f"T-ruff-{len(posted)}"})
            return httpx.Response(404)

        cfg = OrchestratorConfig(
            max_agents=4,
            poll_interval_s=1,
            server_url="http://testserver",
            evolve_mode=evolve_mode,
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client)
        return orch, posted

    def test_creates_tasks_from_ruff_output(self, tmp_path: Path) -> None:
        """Replenishment creates one task per unique ruff rule code (async two-phase)."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(self._RUFF_VIOLATIONS),
                returncode=1,
            )
            result = MagicMock()
            result.open_tasks = 0
            # Phase 1: submit future
            orch._replenish_backlog(result)
            assert len(posted) == 0  # no tasks yet — future is pending
            # Wait for background thread to finish
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            # Phase 2: harvest result and create tasks
            orch._replenish_backlog(result)

        # E501 appears twice but should produce only one task; F401 = one task
        assert len(posted) == 2
        codes = {p["title"].split()[-1] for p in posted}
        assert codes == {"E501", "F401"}
        # Verify required fields
        for body in posted:
            assert body["role"] == "backend"
            assert body["priority"] == 3
            assert body["model"] == "sonnet"
            assert body["effort"] == "low"

    def test_does_not_run_when_evolve_mode_false(self, tmp_path: Path) -> None:
        """Replenishment is a no-op when evolve_mode=False."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path, evolve_mode=False)

        with patch("subprocess.run") as mock_run:
            result = MagicMock()
            result.open_tasks = 0
            orch._replenish_backlog(result)
            mock_run.assert_not_called()

        assert posted == []

    def test_does_not_run_when_open_tasks_present(self, tmp_path: Path) -> None:
        """Replenishment is a no-op when there are open tasks."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            result = MagicMock()
            result.open_tasks = 3
            orch._replenish_backlog(result)
            mock_run.assert_not_called()

        assert posted == []

    def test_caps_at_five_tasks(self, tmp_path: Path) -> None:
        """At most 5 tasks are created per replenishment cycle."""
        from unittest.mock import patch

        many_violations = [
            {
                "filename": f"src/f{i}.py",
                "code": f"E{100 + i}",
                "message": "some issue",
                "location": {"row": i, "column": 1},
            }
            for i in range(10)
        ]
        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(many_violations),
                returncode=1,
            )
            result = MagicMock()
            result.open_tasks = 0
            orch._replenish_backlog(result)  # submit
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()  # wait for thread
            orch._replenish_backlog(result)  # harvest

        assert len(posted) == 5

    def test_respects_60s_cooldown(self, tmp_path: Path) -> None:
        """After harvesting, a second submission is blocked by cooldown."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(self._RUFF_VIOLATIONS),
                returncode=1,
            )
            result = MagicMock()
            result.open_tasks = 0
            # Phase 1: submit future
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()  # wait for thread
            # Phase 2: harvest — creates 2 tasks
            orch._replenish_backlog(result)
            tasks_after_harvest = len(posted)
            # Phase 3: immediate retry — cooldown blocks new submission
            orch._replenish_backlog(result)

        assert tasks_after_harvest == 2
        assert len(posted) == 2  # no new tasks from the third call

    def test_cooldown_resets_after_60s(self, tmp_path: Path) -> None:
        """After 60s the replenishment runs again."""
        from unittest.mock import patch

        orch, posted = self._build_orch_evolve(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(self._RUFF_VIOLATIONS),
                returncode=1,
            )
            result = MagicMock()
            result.open_tasks = 0
            # First cycle: submit → wait → harvest
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            orch._replenish_backlog(result)
            # Fake that 61 seconds have passed
            orch._last_replenish_ts -= 61
            # Second cycle: submit → wait → harvest
            orch._replenish_backlog(result)
            assert orch._pending_ruff_future is not None
            orch._pending_ruff_future.result()
            orch._replenish_backlog(result)

        assert len(posted) == 4  # 2 unique rules × 2 cycles

    def test_tick_does_not_block_on_ruff_or_pytest(self, tmp_path: Path) -> None:
        """tick() must return in under 1 second even when ruff/pytest are slow."""
        import time
        from unittest.mock import patch

        orch, _posted = self._build_orch_evolve(tmp_path)

        def slow_subprocess(*_args: object, **_kwargs: object) -> object:
            time.sleep(2)
            from unittest.mock import MagicMock as _MM
            return _MM(stdout="[]", returncode=0)

        # Set up minimal HTTP mock so tick() can complete (no open tasks)
        result = MagicMock()
        result.open_tasks = 0

        with patch("subprocess.run", side_effect=slow_subprocess):
            start = time.monotonic()
            # _replenish_backlog is called inside tick(); we call it directly here
            # to test the non-blocking contract without needing a full HTTP mock
            orch._replenish_backlog(result)
            elapsed = time.monotonic() - start

        assert elapsed < 1.0, (
            f"_replenish_backlog blocked for {elapsed:.2f}s; expected < 1s"
        )
        # Future should be pending (submitted to thread pool, not yet complete)
        assert orch._pending_ruff_future is not None
        # Clean up — wait for the background thread so it doesn't leak
        orch._pending_ruff_future.result()


# --- Per-task timeout calculation ---


def test_per_task_timeout_short_task(tmp_path: Path) -> None:
    """A 5-minute task gets ~450s timeout (5 * 60 * 1.5)."""
    task = Task(
        id="T-short",
        title="Short task",
        description=".",
        role="backend",
        estimated_minutes=5,
        status=TaskStatus.OPEN,
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
    )
    task_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "estimated_minutes": task.estimated_minutes,
        "status": "open",
        "scope": "small",
        "complexity": "low",
        "priority": 2,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "task_type": "standard",
    }
    transport = _mock_transport({
        "GET /tasks?status=open": httpx.Response(200, json=[task_dict]),
        "GET /tasks?status=done": httpx.Response(200, json=[]),
        "GET /tasks?status=failed": httpx.Response(200, json=[]),
        "GET /status": httpx.Response(200, json={"open": 1, "done": 0}),
        f"POST /tasks/{task.id}/claim": httpx.Response(200, json={}),
    })
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        max_tasks_per_agent=1,
        server_url="http://testserver",
    )
    orch = _build_orchestrator(tmp_path, transport, config=cfg)
    orch.tick()

    # Verify that the spawned session has the per-task timeout set
    sessions = list(orch._agents.values())
    assert sessions, "Expected one agent to be spawned"
    session = sessions[0]
    # 5 min * 60 * 1.5 = 450s
    assert session.timeout_s == 450


def test_per_task_timeout_long_task_clamped(tmp_path: Path) -> None:
    """A 60-minute task would be 5400s but is clamped to max_agent_runtime_s (600s)."""
    task = Task(
        id="T-long",
        title="Long task",
        description=".",
        role="backend",
        estimated_minutes=60,
        status=TaskStatus.OPEN,
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
    )
    task_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "estimated_minutes": task.estimated_minutes,
        "status": "open",
        "scope": "large",
        "complexity": "high",
        "priority": 2,
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "task_type": "standard",
    }
    transport = _mock_transport({
        "GET /tasks?status=open": httpx.Response(200, json=[task_dict]),
        "GET /tasks?status=done": httpx.Response(200, json=[]),
        "GET /tasks?status=failed": httpx.Response(200, json=[]),
        "GET /status": httpx.Response(200, json={"open": 1, "done": 0}),
        f"POST /tasks/{task.id}/claim": httpx.Response(200, json={}),
    })
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        max_tasks_per_agent=1,
        server_url="http://testserver",
    )
    orch = _build_orchestrator(tmp_path, transport, config=cfg)
    orch.tick()

    sessions = list(orch._agents.values())
    assert sessions, "Expected one agent to be spawned"
    session = sessions[0]
    # 60 * 60 * 1.5 = 5400s, clamped to max_agent_runtime_s=600
    assert session.timeout_s == 600


def test_reap_uses_per_session_timeout(tmp_path: Path) -> None:
    """_reap_dead_agents uses session.timeout_s when set, not config.max_agent_runtime_s."""
    cfg = OrchestratorConfig(
        max_agents=6,
        poll_interval_s=1,
        max_agent_runtime_s=600,
        server_url="http://testserver",
    )
    transport = _mock_transport({
        "GET /tasks?status=open": httpx.Response(200, json=[]),
        "GET /tasks?status=done": httpx.Response(200, json=[]),
        "GET /tasks?status=failed": httpx.Response(200, json=[]),
        "GET /status": httpx.Response(200, json={"open": 0, "done": 0}),
    })
    orch = _build_orchestrator(tmp_path, transport, config=cfg)

    # Inject a session with a short timeout (120s) that has been running for 130s
    session = AgentSession(id="sess-1", role="backend", pid=9999, task_ids=["T-x"])
    session.timeout_s = 120
    session.spawn_ts = time.time() - 130  # running for 130s > 120s timeout
    orch._agents[session.id] = session

    result = TickResult()
    orch._spawner.kill = MagicMock()  # type: ignore[method-assign]
    orch._reap_dead_agents(result, {})

    assert session.id in result.reaped, "Session should be reaped due to per-session timeout"


# --- Run completion summary ---


class TestRunCompletionSummary:
    """tick() writes .sdd/runtime/summary.md when all tasks are done and evolve_mode is off."""

    def _build(
        self,
        tmp_path: Path,
        *,
        done_tasks: list[dict] | None = None,
        failed_tasks: list[dict] | None = None,
    ) -> Orchestrator:
        _done = done_tasks or []
        _failed = failed_tasks or []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, _done + _failed)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            evolve_mode=False,
            evolution_enabled=False,
        )
        return _build_orchestrator(tmp_path, transport, config=cfg)

    def test_summary_created_when_all_tasks_done(self, tmp_path: Path) -> None:
        """summary.md is created when open=0, agents=0, evolve_mode=False."""
        done = [_task_as_dict(_make_task(id="T-1", title="Fix auth bug", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()

        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert summary_path.exists(), "summary.md should be created"

    def test_summary_contains_task_counts(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id=f"T-{i}", title=f"Task {i}", status="done")) for i in range(3)]
        failed = [_task_as_dict(_make_task(id="T-fail", title="Failed task", status="failed"))]
        orch = self._build(tmp_path, done_tasks=done, failed_tasks=failed)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "**Total completed:** 3" in content
        assert "**Total failed:** 1" in content

    def test_summary_lists_task_titles(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id="T-1", title="Implement login", status="done"))]
        failed = [_task_as_dict(_make_task(id="T-2", title="Write tests", status="failed"))]
        orch = self._build(tmp_path, done_tasks=done, failed_tasks=failed)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "Implement login" in content
        assert "Write tests" in content
        assert "*(failed)*" in content

    def test_summary_not_written_twice(self, tmp_path: Path) -> None:
        """Second tick with same state does not overwrite summary.md."""
        done = [_task_as_dict(_make_task(id="T-1", title="Task A", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()
        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        first_mtime = summary_path.stat().st_mtime

        orch.tick()
        second_mtime = summary_path.stat().st_mtime

        assert first_mtime == second_mtime, "summary.md should not be rewritten on second tick"

    def test_summary_not_created_in_evolve_mode(self, tmp_path: Path) -> None:
        """summary.md is NOT created when evolve_mode=True."""
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                return _tasks_response(url, [_task_as_dict(_make_task(id="T-1", status="done"))])
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        cfg = OrchestratorConfig(
            server_url="http://testserver",
            evolve_mode=True,
            evolution_enabled=False,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)

        orch.tick()

        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert not summary_path.exists(), "summary.md should not be created in evolve_mode"

    def test_summary_includes_duration_and_cost(self, tmp_path: Path) -> None:
        done = [_task_as_dict(_make_task(id="T-1", title="Deploy", status="done"))]
        orch = self._build(tmp_path, done_tasks=done)

        orch.tick()

        content = (tmp_path / ".sdd" / "runtime" / "summary.md").read_text()
        assert "**Wall-clock duration:**" in content
        assert "**Estimated cost:**" in content
        assert "**Files modified:**" in content


# --- DryRun ---


class TestDryRun:
    """dry_run=True should populate TickResult.dry_run_planned but never spawn agents."""

    def test_dry_run_populates_planned(self, tmp_path: Path) -> None:
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[open_task]),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, config=cfg)

        result = orch.tick()

        assert len(result.dry_run_planned) == 1
        role, title, _model, _effort = result.dry_run_planned[0]
        assert role == "backend"
        assert title == "Add feature"

    def test_dry_run_does_not_spawn_agents(self, tmp_path: Path) -> None:
        adapter = _mock_adapter()
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[open_task]),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=True,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=cfg)

        orch.tick()

        adapter.spawn.assert_not_called()

    def test_dry_run_false_does_spawn(self, tmp_path: Path) -> None:
        """Sanity check: without dry_run, spawn IS called for open tasks."""
        adapter = _mock_adapter()
        open_task = _task_as_dict(_make_task(id="T-1", role="backend", title="Add feature"))
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[open_task]),
            "POST /tasks/T-1/claim": httpx.Response(200, json=_task_as_dict(
                _make_task(id="T-1", status="claimed")
            )),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            heartbeat_timeout_s=120,
            server_url="http://testserver",
            dry_run=False,
        )
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter, config=cfg)

        orch.tick()

        adapter.spawn.assert_called_once()


# --- _run_evolution_cycle ---


class TestRunEvolutionCycle:
    """Unit tests for Orchestrator._run_evolution_cycle."""

    def _build_with_evolution_mock(self, tmp_path: Path) -> tuple[Orchestrator, MagicMock]:
        from bernstein.core.evolution import EvolutionCoordinator

        evolution = MagicMock(spec=EvolutionCoordinator)
        evolution.execute_pending_upgrades.return_value = []
        transport = _mock_transport({
            "GET /tasks": httpx.Response(200, json=[]),
        })
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=True,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        orch = Orchestrator(cfg, spawner, tmp_path, client=client, evolution=evolution)
        return orch, evolution

    def _make_proposal(self, proposal_id: str = "P-001", title: str = "Improve routing") -> MagicMock:
        proposal = MagicMock()
        proposal.id = proposal_id
        proposal.title = title
        proposal.description = f"Description for {title}"
        return proposal

    def test_happy_path_creates_http_task_per_proposal(self, tmp_path: Path) -> None:
        """run_analysis_cycle returns proposals → POST /tasks for each."""
        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p1 = self._make_proposal("P-001", "Proposal One")
        p2 = self._make_proposal("P-002", "Proposal Two")
        evolution.run_analysis_cycle.return_value = [p1, p2]

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(posted) == 2
        assert posted[0]["title"] == "Upgrade: Proposal One"
        assert posted[1]["title"] == "Upgrade: Proposal Two"
        assert result.errors == []

    def test_task_payload_structure(self, tmp_path: Path) -> None:
        """Posted task body has correct fields: title, description, role, priority, task_type."""
        posted: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p = self._make_proposal("P-001", "Optimize model router")
        evolution.run_analysis_cycle.return_value = [p]

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(posted) == 1
        body = posted[0]
        assert body["title"] == "Upgrade: Optimize model router"
        assert body["description"] == p.description
        assert body["role"] == "backend"
        assert body["priority"] == 2
        assert body["task_type"] == TaskType.UPGRADE_PROPOSAL.value

    def test_no_proposals_makes_no_http_calls(self, tmp_path: Path) -> None:
        """When run_analysis_cycle returns [], no POST /tasks calls are made."""
        posted: list[object] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/tasks":
                posted.append(request)
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )
        evolution.run_analysis_cycle.return_value = []

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert posted == []
        assert result.errors == []

    def test_http_post_failure_logs_warning_and_continues(self, tmp_path: Path) -> None:
        """If one POST fails, logs warning, adds to errors, continues with remaining proposals."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            if request.method == "POST" and request.url.path == "/tasks":
                call_count += 1
                if call_count == 1:
                    return httpx.Response(500, json={"detail": "server error"})
                return httpx.Response(200, json={"id": "T-new"})
            return httpx.Response(200, json=[])

        orch, evolution = self._build_with_evolution_mock(tmp_path)
        orch._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://testserver",
        )

        p1 = self._make_proposal("P-001", "First proposal")
        p2 = self._make_proposal("P-002", "Second proposal")
        evolution.run_analysis_cycle.return_value = [p1, p2]

        result = TickResult()
        orch._run_evolution_cycle(result)

        # Both proposals attempted
        assert call_count == 2
        # One error recorded for the failed POST
        assert len(result.errors) == 1
        assert "evolution_task:" in result.errors[0]

    def test_analysis_cycle_raises_adds_error(self, tmp_path: Path) -> None:
        """If run_analysis_cycle raises, error is added to result.errors."""
        orch, evolution = self._build_with_evolution_mock(tmp_path)
        evolution.run_analysis_cycle.side_effect = RuntimeError("analysis failed")

        result = TickResult()
        orch._run_evolution_cycle(result)

        assert len(result.errors) == 1
        assert "evolution:" in result.errors[0]
        assert "analysis failed" in result.errors[0]


# --- _collect_completion_data ---


class TestExtractFromAgentLog:
    def _make_session(self, session_id: str = "sess-001") -> AgentSession:
        from bernstein.core.models import ModelConfig
        return AgentSession(id=session_id, role="backend", model_config=ModelConfig("sonnet", "high"))

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        return _build_orchestrator(tmp_path, transport)

    def _write_log(self, tmp_path: Path, session_id: str, content: str) -> Path:
        log_dir = tmp_path / ".sdd" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"
        log_path.write_text(content, encoding="utf-8")
        return log_path

    def test_modified_and_created_files(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s1")
        self._write_log(tmp_path, "s1", (
            "Some output\n"
            "Modified: src/foo.py\n"
            "Created: src/bar.py\n"
            "More output\n"
            "Modified: tests/test_foo.py\n"
        ))
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == ["src/foo.py", "src/bar.py", "tests/test_foo.py"]

    def test_deduplicates_file_paths(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s2")
        self._write_log(tmp_path, "s2", (
            "Modified: src/foo.py\n"
            "Modified: src/foo.py\n"
            "Created: src/foo.py\n"
            "Modified: src/bar.py\n"
        ))
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == ["src/foo.py", "src/bar.py"]

    def test_extracts_pytest_summary(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s3")
        self._write_log(tmp_path, "s3", (
            "collecting ...\n"
            "test_foo.py::test_bar PASSED\n"
            "===== 3 passed, 1 failed in 0.42s =====\n"
        ))
        result = orch._collect_completion_data(session)
        assert result["test_results"] == {"summary": "===== 3 passed, 1 failed in 0.42s ====="}

    def test_log_file_does_not_exist(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("missing-session")
        result = orch._collect_completion_data(session)
        assert result == {"files_modified": [], "test_results": {}}

    def test_oserror_on_read(self, tmp_path: Path) -> None:
        from unittest.mock import patch
        orch = self._make_orch(tmp_path)
        session = self._make_session("s4")
        log_path = tmp_path / ".sdd" / "runtime" / "s4.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("some content")
        with patch.object(log_path.__class__, "read_text", side_effect=OSError("disk error")):
            result = orch._collect_completion_data(session)
        assert result == {"files_modified": [], "test_results": {}}

    def test_empty_log_file(self, tmp_path: Path) -> None:
        orch = self._make_orch(tmp_path)
        session = self._make_session("s5")
        self._write_log(tmp_path, "s5", "")
        result = orch._collect_completion_data(session)
        assert result["files_modified"] == []


# --- _check_evolve: cycle management unit tests ---


class TestCheckEvolve:
    """Direct unit tests for Orchestrator._check_evolve."""

    def _make_orch(self, tmp_path: Path) -> Orchestrator:
        transport = _mock_transport({"GET /tasks": httpx.Response(200, json=[])})
        cfg = OrchestratorConfig(
            max_agents=6,
            poll_interval_s=1,
            server_url="http://testserver",
            evolution_enabled=False,
        )
        adp = _mock_adapter()
        templates_dir = tmp_path / "templates" / "roles"
        templates_dir.mkdir(parents=True, exist_ok=True)
        spawner = AgentSpawner(adp, templates_dir, tmp_path)
        client = httpx.Client(transport=transport, base_url="http://testserver")
        return Orchestrator(cfg, spawner, tmp_path, client=client)

    def _patch_evolve_helpers(
        self,
        orch: Orchestrator,
        *,
        committed: bool = False,
        test_info: dict[str, object] | None = None,
    ) -> tuple[MagicMock, MagicMock, MagicMock]:
        """Patch sub-methods to avoid subprocess/git calls; return mocks."""
        from unittest.mock import patch

        _test_info = test_info or {"passed": 5, "failed": 0, "summary": "5 passed"}
        mock_run_tests = MagicMock(return_value=_test_info)
        mock_auto_commit = MagicMock(return_value=committed)
        mock_spawn_manager = MagicMock(return_value=None)
        orch._evolve_run_tests = mock_run_tests  # type: ignore[assignment]
        orch._evolve_auto_commit = mock_auto_commit  # type: ignore[assignment]
        orch._evolve_spawn_manager = mock_spawn_manager  # type: ignore[assignment]
        orch._log_evolve_cycle = MagicMock(return_value=None)  # type: ignore[assignment]
        return mock_run_tests, mock_auto_commit, mock_spawn_manager

    def test_no_evolve_json_is_noop(self, tmp_path: Path) -> None:
        """No evolve.json → _check_evolve returns without doing anything."""
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        mock_run.assert_not_called()
        mock_commit.assert_not_called()
        mock_spawn.assert_not_called()

    def test_invalid_json_is_noop(self, tmp_path: Path) -> None:
        """evolve.json with invalid JSON → no crash, no cycle triggered."""
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "evolve.json").write_text("{not valid json!!")
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})  # must not raise

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_oserror_on_read_is_noop(self, tmp_path: Path) -> None:
        """OSError reading evolve.json → no crash, no cycle triggered."""
        from unittest.mock import patch

        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        evolve_path = runtime / "evolve.json"
        evolve_path.write_text('{"enabled": true}')
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        with patch.object(evolve_path.__class__, "read_text", side_effect=OSError("disk error")):
            orch._check_evolve(TickResult(), {})  # must not raise

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_enabled_false_is_noop(self, tmp_path: Path) -> None:
        """enabled=false in evolve.json → no cycle triggered."""
        _write_evolve_config(tmp_path, enabled=False)
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        mock_run.assert_not_called()
        mock_spawn.assert_not_called()

    def test_triggers_cycle_when_all_tasks_complete(self, tmp_path: Path) -> None:
        """When enabled and no open/claimed tasks or alive agents, cycle runs."""
        _write_evolve_config(tmp_path, interval_s=0)
        orch = self._make_orch(tmp_path)
        mock_run, mock_commit, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {"done": [_make_task(id="T-1", status="done")]})

        mock_run.assert_called_once()
        mock_commit.assert_called_once()
        mock_spawn.assert_called_once()

    def test_cycle_count_increments_and_written_back(self, tmp_path: Path) -> None:
        """After a successful cycle, _cycle_count increments in evolve.json."""
        evolve_path = _write_evolve_config(tmp_path, interval_s=0, cycle_count=2)
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_cycle_count"] == 3
        assert updated["_last_cycle_ts"] > 0

    def test_focus_area_uses_cycle_count_modulo(self, tmp_path: Path) -> None:
        """Focus area passed to _evolve_spawn_manager rotates by cycle_count % len."""
        focus_areas = Orchestrator._EVOLVE_FOCUS_AREAS
        for i, expected_focus in enumerate(focus_areas):
            _write_evolve_config(tmp_path, interval_s=0, cycle_count=i)
            orch = self._make_orch(tmp_path)
            _, _, mock_spawn = self._patch_evolve_helpers(orch)

            orch._check_evolve(TickResult(), {})

            call_kwargs = mock_spawn.call_args
            assert call_kwargs is not None
            actual_focus = call_kwargs.kwargs.get("focus_area") or call_kwargs.args[1]
            assert actual_focus == expected_focus, (
                f"cycle_count={i}: expected focus={expected_focus!r}, got {actual_focus!r}"
            )

    def test_focus_area_wraps_around(self, tmp_path: Path) -> None:
        """Focus area wraps when cycle_count exceeds len(_EVOLVE_FOCUS_AREAS)."""
        areas = Orchestrator._EVOLVE_FOCUS_AREAS
        wrap_cycle = len(areas)  # should map back to index 0
        _write_evolve_config(tmp_path, interval_s=0, cycle_count=wrap_cycle)
        orch = self._make_orch(tmp_path)
        _, _, mock_spawn = self._patch_evolve_helpers(orch)

        orch._check_evolve(TickResult(), {})

        call_kwargs = mock_spawn.call_args
        actual_focus = call_kwargs.kwargs.get("focus_area") or call_kwargs.args[1]
        assert actual_focus == areas[0]

    def test_spawn_manager_receives_cycle_number_and_test_summary(self, tmp_path: Path) -> None:
        """_evolve_spawn_manager is called with correct cycle_number and test_summary."""
        _write_evolve_config(tmp_path, interval_s=0, cycle_count=4)
        orch = self._make_orch(tmp_path)
        _, _, mock_spawn = self._patch_evolve_helpers(
            orch, test_info={"passed": 7, "failed": 1, "summary": "7 passed, 1 failed"}
        )

        orch._check_evolve(TickResult(), {})

        mock_spawn.assert_called_once()
        kwargs = mock_spawn.call_args.kwargs
        assert kwargs["cycle_number"] == 5
        assert kwargs["test_summary"] == "7 passed, 1 failed"

    def test_consecutive_empty_resets_when_committed(self, tmp_path: Path) -> None:
        """If committed=True, _consecutive_empty resets to 0."""
        evolve_path = _write_evolve_config(
            tmp_path, interval_s=0, consecutive_empty=5
        )
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch, committed=True)

        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 0

    def test_consecutive_empty_increments_when_no_changes(self, tmp_path: Path) -> None:
        """If nothing committed and no done tasks, _consecutive_empty increments."""
        evolve_path = _write_evolve_config(
            tmp_path, interval_s=0, consecutive_empty=2
        )
        orch = self._make_orch(tmp_path)
        self._patch_evolve_helpers(orch, committed=False)

        # tasks_by_status has no "done" key → tasks_completed = 0
        orch._check_evolve(TickResult(), {})

        updated = json.loads(evolve_path.read_text())
        assert updated["_consecutive_empty"] == 3


# --- Parallel verification ---


class TestParallelVerification:
    """verify_task() calls for multiple done tasks run concurrently."""

    def test_multiple_done_tasks_verified_concurrently(self, tmp_path: Path) -> None:
        """Multiple done tasks with signals are verified in parallel.

        Mocks verify_task with a 0.2s sleep. With 4 tasks running serially
        this would take ~0.8s; in parallel it should finish in ~0.2s.
        """
        import threading
        from unittest.mock import patch

        call_times: list[float] = []
        lock = threading.Lock()

        def slow_verify(task: object, workdir: object) -> tuple[bool, list[str]]:
            start = time.time()
            time.sleep(0.15)
            with lock:
                call_times.append(start)
            return (True, [])

        task_dicts = []
        for i in range(4):
            t = _make_task(id=f"T-par-{i}", status="done")
            td = _task_as_dict(t)
            td["completion_signals"] = [{"type": "path_exists", "value": "x"}]
            task_dicts.append(td)
        tasks_with_signals = [Task.from_dict(td) for td in task_dicts]

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=task_dicts),
        })
        orch = _build_orchestrator(tmp_path, transport)

        with patch("bernstein.core.orchestrator.verify_task", side_effect=slow_verify):
            t_start = time.time()
            result = TickResult()
            orch._process_completed_tasks(tasks_with_signals, result)
            elapsed = time.time() - t_start

        # All 4 tasks verified
        assert len(result.verified) == 4
        # Total wall time should be much less than 4 × 0.15s = 0.6s
        assert elapsed < 0.5, f"Expected parallel execution but took {elapsed:.2f}s"
        # All 4 verify calls started within ~0.15s of each other
        assert len(call_times) == 4
        spread = max(call_times) - min(call_times)
        assert spread < 0.1, f"Calls spread too far apart: {spread:.3f}s"


# --- Parallel verification ---


class TestProcessCompletedTasksParallel:
    """_process_completed_tasks runs verify_task() concurrently."""

    def test_multiple_done_tasks_verified_concurrently(self, tmp_path: Path) -> None:
        """verify_task() for N done tasks must run in parallel, not serially.

        We mock verify_task to sleep 0.2 s per task.  With 4 tasks the serial
        total would be >= 0.8 s; the parallel total (max_workers=4) should be
        well under 0.5 s.
        """
        import time
        from unittest.mock import patch

        SLEEP = 0.2
        N = 4

        task_dicts = []
        for i in range(N):
            t = _make_task(id=f"T-par-{i}", status="done")
            d = _task_as_dict(t)
            d["status"] = "done"
            d["completion_signals"] = [{"type": "path_exists", "value": "x.txt"}]
            task_dicts.append(d)

        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=task_dicts),
        })
        orch = _build_orchestrator(tmp_path, transport)

        def slow_verify(task: object, workdir: object) -> tuple[bool, list[str]]:
            time.sleep(SLEEP)
            return True, []

        with patch(
            "bernstein.core.orchestrator.verify_task", side_effect=slow_verify
        ):
            tick_result = TickResult()
            start = time.monotonic()
            orch._process_completed_tasks(
                [Task.from_dict(d) for d in task_dicts], tick_result
            )
            elapsed = time.monotonic() - start

        # All tasks verified successfully
        assert len(tick_result.verified) == N
        assert tick_result.verification_failures == []
        # Parallel: total wall time should be much less than N * SLEEP
        assert elapsed < SLEEP * N * 0.75, (
            f"Expected parallel execution (<{SLEEP * N * 0.75:.2f}s), got {elapsed:.2f}s"
        )


class TestComputeTotalSpentCache:
    """Tests for mtime-based caching in _compute_total_spent."""

    def test_no_reparse_when_files_unchanged(self, tmp_path: Path) -> None:
        """Second call with unchanged files must not re-parse them."""
        import pytest
        from unittest.mock import patch, call
        from bernstein.core import orchestrator as orch_mod
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        jsonl = metrics_dir / "cost_efficiency_agent1.jsonl"
        jsonl.write_text(
            '{"value": 0.05, "labels": {"task_id": "t1"}}\n'
            '{"value": 0.10, "labels": {"task_id": "t2"}}\n'
        )

        _total_spent_cache.clear()

        first = _compute_total_spent(tmp_path)
        assert first == pytest.approx(0.15)

        # Second call: _parse_file_total should not be called at all.
        with patch.object(orch_mod, "_parse_file_total", wraps=orch_mod._parse_file_total) as mock_parse:
            second = _compute_total_spent(tmp_path)
            assert second == pytest.approx(0.15)
            mock_parse.assert_not_called()

    def test_reparsed_after_modification(self, tmp_path: Path) -> None:
        """Cache is invalidated when a file's mtime changes."""
        import pytest
        import time as _time
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        jsonl = metrics_dir / "cost_efficiency_agent1.jsonl"
        jsonl.write_text('{"value": 0.05, "labels": {"task_id": "t1"}}\n')

        _total_spent_cache.clear()

        first = _compute_total_spent(tmp_path)
        assert first == pytest.approx(0.05)

        _time.sleep(0.01)
        jsonl.write_text(
            '{"value": 0.05, "labels": {"task_id": "t1"}}\n'
            '{"value": 0.20, "labels": {"task_id": "t3"}}\n'
        )

        second = _compute_total_spent(tmp_path)
        assert second == pytest.approx(0.25)

    def test_empty_metrics_dir(self, tmp_path: Path) -> None:
        """Returns 0.0 when no cost_efficiency files exist."""
        from bernstein.core.orchestrator import _compute_total_spent, _total_spent_cache

        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        _total_spent_cache.clear()

        assert _compute_total_spent(tmp_path) == 0.0
