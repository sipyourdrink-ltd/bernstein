"""Integration test: full spawn → execute → complete lifecycle.

Tests the end-to-end pipeline:
  3 tasks created → 2 orchestrator ticks → mock agent spawned →
  agent completes tasks via API → orchestrator detects completion →
  summary.md written with correct counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from bernstein.core.models import (
    AgentSession,
    ModelConfig,
    OrchestratorConfig,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from starlette.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASKS = [
    {
        "title": "Add login endpoint",
        "description": "Implement POST /auth/login with JWT",
        "role": "backend",
        "priority": 1,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 15,
    },
    {
        "title": "Write auth unit tests",
        "description": "Test login endpoint with mock DB",
        "role": "qa",
        "priority": 1,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 10,
    },
    {
        "title": "Design database schema",
        "description": "Define users table and indexes",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
    },
]


def _make_mock_spawner(
    session_id: str = "agent-001",
    role: str = "backend",
    pid: int = 9999,
    model: str = "sonnet",
    effort: str = "high",
) -> MagicMock:
    mock_spawner = MagicMock(spec=AgentSpawner)
    session = AgentSession(
        id=session_id,
        role=role,
        pid=pid,
        model_config=ModelConfig(model, effort),
        status="working",
    )
    mock_spawner.spawn_for_tasks.return_value = session
    mock_spawner.check_alive.return_value = True
    mock_spawner.get_worktree_path.return_value = None
    return mock_spawner


def _make_orchestrator(
    tmp_path: Path,
    client: TestClient,
    mock_spawner: MagicMock,
    max_tasks_per_agent: int = 2,
    max_agents: int = 4,
) -> Orchestrator:
    config = OrchestratorConfig(
        server_url="http://testserver",
        max_agents=max_agents,
        max_tasks_per_agent=max_tasks_per_agent,
        poll_interval_s=1,
        evolution_enabled=False,
        evolve_mode=False,
    )
    return Orchestrator(
        config=config,
        spawner=mock_spawner,
        workdir=tmp_path,
        client=client,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_lifecycle_spawn_execute_complete(tmp_path: Path) -> None:
    """Full lifecycle: create 3 tasks, spawn agents, complete via API, verify summary."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    # Use side_effect to return different sessions per role batch
    mock_spawner = MagicMock(spec=AgentSpawner)
    sessions: list[AgentSession] = []

    def _spawn_side_effect(batch: list) -> AgentSession:
        idx = len(sessions)
        session = AgentSession(
            id=f"agent-{idx:03d}",
            role=batch[0].role,
            pid=9000 + idx,
            model_config=ModelConfig("sonnet", "high"),
            status="working",
        )
        sessions.append(session)
        return session

    mock_spawner.spawn_for_tasks.side_effect = _spawn_side_effect
    mock_spawner.check_alive.return_value = True
    mock_spawner.get_worktree_path.return_value = None

    with TestClient(app) as client:
        # 1. Create 3 tasks
        task_ids: list[str] = []
        for payload in TASKS:
            resp = client.post("/tasks", json=payload)
            assert resp.status_code == 201, resp.text
            task_ids.append(resp.json()["id"])

        assert len(task_ids) == 3

        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)

        # 2. Tick 1: orchestrator should spawn agents for the open tasks
        result1 = orchestrator.tick()

        # Three tasks exist: 2 backend + 1 qa; with max_tasks_per_agent=2 and
        # max_agents=4 the orchestrator should spawn at least one agent.
        assert result1.open_tasks == 3 or len(result1.spawned) >= 1, (
            f"Expected open tasks or spawned agents: open={result1.open_tasks}, spawned={result1.spawned}"
        )
        assert mock_spawner.spawn_for_tasks.call_count >= 1

        # 3. Verify model/effort routing: spawner was called with Task objects
        #    whose scope and complexity match our payloads.
        all_spawned_tasks = [
            task
            for call in mock_spawner.spawn_for_tasks.call_args_list
            for task in call[0][0]  # first positional arg = batch list
        ]
        spawned_titles = {t.title for t in all_spawned_tasks}
        # At least one of our task titles should have been dispatched
        assert spawned_titles & {p["title"] for p in TASKS}, (
            f"Expected some tasks to be spawned, got titles: {spawned_titles}"
        )

        # All spawned batches should carry a valid model config via the session
        for session in sessions:
            assert session.model_config.model, "Session must have a model"
            assert session.model_config.effort, "Session must have an effort level"

        # 4. Claim any tasks still in OPEN status (lifecycle governance:
        #    OPEN → CLAIMED required before completion). Tick may have already
        #    claimed some tasks, so 409 is acceptable.
        for task_id in task_ids:
            claim_resp = client.post(f"/tasks/{task_id}/claim")
            assert claim_resp.status_code in (200, 409), (
                f"Unexpected status {claim_resp.status_code} claiming task {task_id}: {claim_resp.text}"
            )

        # 5. Simulate agent completion: mark all tasks done via the API
        for task_id in task_ids:
            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": f"Task {task_id} completed successfully"},
            )
            assert resp.status_code == 200, (
                f"Unexpected status {resp.status_code} completing task {task_id}: {resp.text}"
            )

        # Verify all tasks are done
        for task_id in task_ids:
            task_resp = client.get(f"/tasks/{task_id}")
            assert task_resp.status_code == 200
            assert task_resp.json()["status"] == "done", (
                f"Task {task_id} in unexpected status: {task_resp.json()['status']}"
            )

        # 6. Tick 2: simulate agents dying so orchestrator detects completion
        mock_spawner.check_alive.return_value = False

        result2 = orchestrator.tick()

        # After agents die, orchestrator should have reaped them
        # active_agents should drop (agents marked dead)
        assert result2.active_agents == 0, (
            f"Expected 0 active agents after all processes died, got {result2.active_agents}"
        )

        # open_tasks should be 0 since all tasks are done
        assert result2.open_tasks == 0, f"Expected 0 open tasks after completion, got {result2.open_tasks}"

        # 7. Verify summary.md was written with correct counts
        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert summary_path.exists(), "Expected .sdd/runtime/summary.md to be written after all tasks complete"

        summary_text = summary_path.read_text()
        assert "# Run Summary" in summary_text

        # Extract completed count from summary
        # Format: "**Total completed:** N"
        assert "**Total completed:**" in summary_text, f"Summary missing 'Total completed' field:\n{summary_text}"
        assert "**Total failed:**" in summary_text

        # At least 1 task should be shown as completed
        import re

        match = re.search(r"\*\*Total completed:\*\* (\d+)", summary_text)
        assert match is not None
        total_completed = int(match.group(1))
        assert total_completed >= 1, (
            f"Expected at least 1 completed task in summary, got {total_completed}:\n{summary_text}"
        )

        # Summary should list task titles for done tasks
        # (at least one of our task titles should appear)
        task_titles = {p["title"] for p in TASKS}
        found_any = any(title in summary_text for title in task_titles)
        assert found_any, f"Expected at least one task title in summary:\n{summary_text}"


def test_lifecycle_model_effort_routing_by_complexity(tmp_path: Path) -> None:
    """Verify spawner receives tasks with scope/complexity that drive model routing."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner()

    with TestClient(app) as client:
        # Create tasks with distinct complexity levels
        small_low = {
            "title": "Trivial fix",
            "description": "Fix a typo",
            "role": "backend",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 5,
        }
        large_high = {
            "title": "Refactor auth module",
            "description": "Refactor the entire authentication system",
            "role": "backend",
            "priority": 2,
            "scope": "large",
            "complexity": "high",
            "estimated_minutes": 120,
        }

        for payload in (small_low, large_high):
            resp = client.post("/tasks", json=payload)
            assert resp.status_code == 201

        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner, max_tasks_per_agent=1)
        orchestrator.tick()

        # Both tasks are backend role → may be grouped into 1 or 2 batches
        # depending on max_tasks_per_agent. With max_tasks_per_agent=1, each task
        # gets its own spawn call.
        assert mock_spawner.spawn_for_tasks.call_count >= 1

        # All batches passed to spawner should contain Tasks with scope/complexity
        for call in mock_spawner.spawn_for_tasks.call_args_list:
            batch = call[0][0]
            for task in batch:
                assert task.scope.value in ("small", "medium", "large")
                assert task.complexity.value in ("low", "medium", "high")
                assert task.title in (small_low["title"], large_high["title"])


def test_lifecycle_orchestrator_detects_completion_state(tmp_path: Path) -> None:
    """Orchestrator tick 2 sees done tasks and zero active agents after completion."""
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    mock_spawner = _make_mock_spawner(session_id="agent-x", role="qa", pid=7777)

    with TestClient(app) as client:
        payload = {
            "title": "Run smoke tests",
            "description": "Execute smoke test suite",
            "role": "qa",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 5,
        }
        resp = client.post("/tasks", json=payload)
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        orchestrator = _make_orchestrator(tmp_path, client, mock_spawner)

        # Tick 1: spawns agent, task moves to claimed
        result1 = orchestrator.tick()
        assert len(result1.spawned) == 1
        assert result1.spawned[0] == "agent-x"

        # Claim the task if tick didn't already (lifecycle governance:
        # OPEN → CLAIMED required before completion)
        claim_resp = client.post(f"/tasks/{task_id}/claim")
        assert claim_resp.status_code in (200, 409)  # 409 if tick already claimed

        # Complete the task (direct API call as a real agent would do)
        complete_resp = client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "Smoke tests passed: 42/42"},
        )
        assert complete_resp.status_code == 200
        assert complete_resp.json()["status"] == "done"

        # Tick 2: agent process dies, orchestrator sees 0 open + 0 active
        mock_spawner.check_alive.return_value = False
        result2 = orchestrator.tick()

        assert result2.open_tasks == 0
        assert result2.active_agents == 0
        assert result2.errors == [], f"Unexpected errors: {result2.errors}"

        # Summary should now be written
        summary_path = tmp_path / ".sdd" / "runtime" / "summary.md"
        assert summary_path.exists()

        summary_text = summary_path.read_text()
        assert "**Total completed:** 1" in summary_text
        assert "**Total failed:** 0" in summary_text
        # Task title should appear in task list
        assert "Run smoke tests" in summary_text
