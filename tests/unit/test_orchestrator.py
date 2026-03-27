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
    _task_from_dict,
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
    """
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        key = f"{request.method} {url.path}"
        if url.query:
            key += f"?{url.query.decode()}"
        if key in responses:
            return responses[key]
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


# --- _task_from_dict ---


class TestTaskFromDict:
    def test_round_trip(self) -> None:
        task = _make_task(id="T-099", role="qa", priority=1)
        raw = _task_as_dict(task)
        parsed = _task_from_dict(raw)

        assert parsed.id == "T-099"
        assert parsed.role == "qa"
        assert parsed.priority == 1
        assert parsed.status == TaskStatus.OPEN
        assert parsed.scope == Scope.MEDIUM

    def test_defaults_for_missing_fields(self) -> None:
        raw = {"id": "T-min", "title": "x", "description": "y", "role": "z"}
        parsed = _task_from_dict(raw)

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
            "GET /tasks?status=open": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            "GET /tasks?status=open": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert result.open_tasks == 0
        assert len(result.spawned) == 0

    def test_handles_server_error_on_open_fetch(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(500, text="Internal error"),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "fetch_open" in result.errors[0]

    def test_handles_server_error_on_done_fetch(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(500, text="Internal error"),
        })
        orch = _build_orchestrator(tmp_path, transport)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "fetch_done" in result.errors[0]

    def test_tracks_agents_across_ticks(self, tmp_path: Path) -> None:
        tasks_tick1 = [_make_task(id="T-1")]
        tasks_tick2 = [_make_task(id="T-2", role="qa")]

        call_count = 0
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            url = request.url
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks?status=done":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=open":
                call_count += 1
                if call_count == 1:
                    return httpx.Response(200, json=[_task_as_dict(t) for t in tasks_tick1])
                return httpx.Response(200, json=[_task_as_dict(t) for t in tasks_tick2])
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
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            "GET /tasks?status=open": httpx.Response(200, json=[_task_as_dict(t) for t in tasks]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
        })
        adapter = _mock_adapter()
        adapter.spawn.side_effect = RuntimeError("process failed to start")
        orch = _build_orchestrator(tmp_path, transport, adapter=adapter)

        result = orch.tick()

        assert len(result.errors) == 1
        assert "spawn" in result.errors[0]
        assert len(result.spawned) == 0


# --- Reaping stale agents ---


class TestReaping:
    def test_reaps_stale_heartbeat(self, tmp_path: Path) -> None:
        transport = _mock_transport({
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
                return httpx.Response(200, json=[])
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
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            "GET /tasks?status=open": httpx.Response(200, json=[]),
            "GET /tasks?status=done": httpx.Response(200, json=[]),
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
                return httpx.Response(200, json=[])
            if key == "GET /tasks/T-done":
                return httpx.Response(200, json=task_dict)
            if "complete" in key:
                complete_called = True
                return httpx.Response(200, json={})
            if "fail" in key:
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
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks?status=open":
                call_count += 1
                if call_count == 1:
                    return httpx.Response(200, json=[_task_as_dict(task1)])
                return httpx.Response(200, json=[_task_as_dict(task2)])
            if key == "GET /tasks?status=done":
                return httpx.Response(200, json=[])
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
            key = f"{request.method} {url.path}"
            if url.query:
                key += f"?{url.query.decode()}"
            if key == "GET /tasks?status=open":
                call_count += 1
                if call_count == 1:
                    return httpx.Response(200, json=[_task_as_dict(task1)])
                return httpx.Response(200, json=[_task_as_dict(task2)])
            if key == "GET /tasks?status=done":
                return httpx.Response(200, json=[])
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
            if key == "GET /tasks?status=open":
                return httpx.Response(200, json=[])
            if key == "GET /tasks?status=done":
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
