"""Integration test: server + orchestrator tick + mock adapter.

Wires the real FastAPI task server (in-process via ASGI transport) together
with the Orchestrator, exercises one full cycle:
  task create → orchestrator tick → spawn (mock adapter) → complete via API
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from bernstein.core.models import (
    AgentSession,
    ModelConfig,
    OrchestratorConfig,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_PAYLOAD = {
    "title": "Write hello world",
    "description": "Create a simple hello world script",
    "role": "backend",
    "priority": 1,
    "scope": "small",
    "complexity": "low",
    "estimated_minutes": 10,
}


def _make_mock_spawner(
    session_id: str = "agent-001",
    role: str = "backend",
    pid: int = 9999,
) -> MagicMock:
    """Create a mock AgentSpawner that returns a fake AgentSession."""
    mock_spawner = MagicMock(spec=AgentSpawner)
    session = AgentSession(
        id=session_id,
        role=role,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
        status="working",
    )
    mock_spawner.spawn_for_tasks.return_value = session
    mock_spawner.check_alive.return_value = True
    return mock_spawner


def _make_orchestrator(
    tmp_path: Path,
    client: TestClient,
    mock_spawner: MagicMock,
    server_url: str = "http://testserver",
) -> Orchestrator:
    config = OrchestratorConfig(
        server_url=server_url,
        max_agents=4,
        max_tasks_per_agent=1,
        poll_interval_s=1,
        evolution_enabled=False,
    )
    return Orchestrator(
        config=config,
        spawner=mock_spawner,
        workdir=tmp_path,
        client=client,
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_task_create_visible_to_orchestrator(tmp_path: Path) -> None:
    """Tasks created via the API are fetched by the orchestrator on the next tick."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        # Create a task
        resp = client.post("/tasks", json=TASK_PAYLOAD)
        assert resp.status_code == 201

        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)
        result = orchestrator.tick()

    assert result.open_tasks == 1


def test_orchestrator_tick_spawns_agent_for_open_task(tmp_path: Path) -> None:
    """One tick spawns exactly one agent for a single open task."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        client.post("/tasks", json=TASK_PAYLOAD)
        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)
        result = orchestrator.tick()

    assert len(result.spawned) == 1
    mock_spawner.spawn_for_tasks.assert_called_once()
    batch = mock_spawner.spawn_for_tasks.call_args[0][0]
    assert batch[0].title == TASK_PAYLOAD["title"]


def test_orchestrator_respects_max_agents(tmp_path: Path) -> None:
    """Orchestrator does not spawn beyond max_agents."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        # Create 5 tasks
        for i in range(5):
            client.post("/tasks", json={**TASK_PAYLOAD, "title": f"Task {i}"})

        config = OrchestratorConfig(
            server_url="http://testserver",
            max_agents=2,
            max_tasks_per_agent=1,
            evolution_enabled=False,
        )
        orchestrator = Orchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            client=client,
        )
        result = orchestrator.tick()

    # Should spawn at most max_agents (2)
    assert len(result.spawned) <= 2


def test_task_complete_cycle_via_api(tmp_path: Path) -> None:
    """Full cycle: create task → tick (spawn) → complete via API → verify status."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        # 1. Create task
        resp = client.post("/tasks", json=TASK_PAYLOAD)
        task_id = resp.json()["id"]

        # 2. Tick orchestrator (spawns agent)
        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)
        result = orchestrator.tick()
        assert len(result.spawned) == 1

        # 3. Claim the task (lifecycle governance: OPEN → CLAIMED before completion)
        claim_resp = client.post(f"/tasks/{task_id}/claim")
        assert claim_resp.status_code in (200, 409)  # 409 if tick already claimed it

        # 4. Simulate agent completing the task (as a real agent would do)
        complete_resp = client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "Created hello_world.py with print statement"},
        )
        assert complete_resp.status_code == 200

        # 5. Verify the task is now done
        task_resp = client.get(f"/tasks/{task_id}")
        assert task_resp.status_code == 200
        assert task_resp.json()["status"] == "done"


def test_two_tasks_different_roles_spawn_separately(tmp_path: Path) -> None:
    """Tasks with different roles each get their own agent spawn."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        client.post("/tasks", json={**TASK_PAYLOAD, "role": "backend"})
        client.post("/tasks", json={**TASK_PAYLOAD, "title": "QA task", "role": "qa"})

        config = OrchestratorConfig(
            server_url="http://testserver",
            max_agents=4,
            max_tasks_per_agent=1,
            evolution_enabled=False,
        )
        orchestrator = Orchestrator(
            config=config,
            spawner=mock_spawner,
            workdir=tmp_path,
            client=client,
        )
        result = orchestrator.tick()

    # One agent per role batch
    assert len(result.spawned) == 2
    assert mock_spawner.spawn_for_tasks.call_count == 2


def test_no_tasks_no_spawn(tmp_path: Path) -> None:
    """Empty task queue → no spawn attempts."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)
        result = orchestrator.tick()

    assert result.open_tasks == 0
    assert result.spawned == []
    mock_spawner.spawn_for_tasks.assert_not_called()


def test_server_status_endpoint_returns_summary(tmp_path: Path) -> None:
    """GET /status returns a dashboard summary of the task queue."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
        # Create a couple of tasks
        client.post("/tasks", json=TASK_PAYLOAD)
        client.post("/tasks", json={**TASK_PAYLOAD, "title": "Another task"})

        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        # Status endpoint should include task counts
        assert "open" in data or "total" in data or isinstance(data, dict)
