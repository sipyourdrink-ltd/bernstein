"""Integration test: batch task execution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_batch_execution(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create 2 tasks with same role
    # Task 1 contains the script that handles BOTH tasks
    desc_1 = (
        "```python\n"
        "# INTEGRATION-MOCK\n"
        "import os, subprocess, time\n"
        "from pathlib import Path\n"
        "Path('batch.txt').write_text('batched')\n"
        "subprocess.run(['git', 'add', 'batch.txt'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'work batch'], check=True)\n"
        "runtime_dir = Path(__file__).parent\n"
        "(runtime_dir / 'DONE_task-1').write_text('done')\n"
        "(runtime_dir / 'DONE_task-2').write_text('done')\n"
        "time.sleep(2)\n"
        "```"
    )
    test_client.post("/tasks", json={"title": "Task 1", "description": desc_1, "role": "backend"})
    test_client.post("/tasks", json={"title": "Task 2", "description": "Part of batch 1", "role": "backend"})

    # 2. Run orchestrator with max_tasks_per_agent=2
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._config.max_tasks_per_agent = 2
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        spawn_count = 0
        original_spawn = orch._spawner.spawn_for_tasks

        def counted_spawn(tasks):
            nonlocal spawn_count
            spawn_count += 1
            return original_spawn(tasks)

        orch._spawner.spawn_for_tasks = counted_spawn

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                tasks_data = resp.json()
                for t in tasks_data:
                    slug = t["title"].lower().replace(" ", "-")
                    marker = integration_sdd / "runtime" / f"DONE_{slug}"
                    if marker.exists():
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                        marker.unlink()
                resp = test_client.get("/tasks")
                return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(30):
            orch.tick()
            resp = test_client.get("/tasks")
            tasks = resp.json()
            if all(t["status"] == "done" for t in tasks):
                break
            time.sleep(0.5)

        # 3. Verify
        assert spawn_count == 1
        resp = test_client.get("/tasks")
        for t in resp.json():
            assert t["status"] == "done"

        assert (integration_sdd.parent / "batch.txt").exists()
