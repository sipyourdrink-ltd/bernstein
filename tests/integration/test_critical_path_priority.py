"""Integration test: critical path priority handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from bernstein.core.models import Task
from httpx import Response

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_critical_path_priority(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create tasks
    # Task A: low priority, but dependency for B
    resp = test_client.post(
        "/tasks", json={"title": "Task A", "description": "Low priority dependency", "role": "backend", "priority": 3}
    )
    task_a_id = resp.json()["id"]

    # Task B: high priority, depends on A
    test_client.post(
        "/tasks",
        json={
            "title": "Task B",
            "description": "High priority critical path",
            "role": "backend",
            "priority": 1,
            "depends_on": [task_a_id],
        },
    )

    # Tasks C, D, E: low priority, independent
    for i in range(1, 4):
        test_client.post(
            "/tasks",
            json={"title": f"Task {i + 2}", "description": "Low priority background", "role": "backend", "priority": 3},
        )

    # 2. Run orchestrator with max_agents=1
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # FIX for loop order (same as in test_sequential_dependency)
    original_spawn = orch._spawner.spawn_for_tasks

    def fixed_spawn(tasks):
        resp = test_client.get("/tasks")
        done_tasks = [Task.from_dict(t) for t in resp.json() if t["status"] == "done"]
        from bernstein.core.orchestrator import TickResult
        from bernstein.core.task_lifecycle import process_completed_tasks

        process_completed_tasks(orch, done_tasks, TickResult())
        return original_spawn(tasks)

    orch._spawner.spawn_for_tasks = fixed_spawn

    spawned_titles = []
    original_spawn_for_tasks = orch._spawner.spawn_for_tasks

    def tracked_spawn(tasks):
        for t in tasks:
            spawned_titles.append(t.title)
        return original_spawn_for_tasks(tasks)

    orch._spawner.spawn_for_tasks = tracked_spawn

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                tasks_data = resp.json()
                for t in tasks_data:
                    # Auto-complete everything immediately for this test
                    if t["status"] == "claimed":
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                resp = test_client.get("/tasks")
                return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(20):
            orch.tick()
            resp = test_client.get("/tasks")
            if all(t["status"] == "done" for t in resp.json()):
                break
            await asyncio.sleep(0.1)

        # 3. Verify order
        # Expected order: Task A (to unblock B), Task B (highest priority), then others
        assert spawned_titles[0] == "Task A"
        assert spawned_titles[1] == "Task B"
        # Others follow
        assert len(spawned_titles) == 5
