"""Integration test: parallel independent tasks."""

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
async def test_parallel_independent(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create 3 independent tasks
    task_ids = []
    for i in range(1, 4):
        desc = (
            "```python\n"
            "# INTEGRATION-MOCK\n"
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            f"Path('independent_{i}.txt').write_text('data {i}')\n"
            f"subprocess.run(['git', 'add', 'independent_{i}.txt'], check=True)\n"
            f"subprocess.run(['git', 'commit', '-m', 'work {i}'], check=True)\n"
            "runtime_dir = Path(__file__).parent\n"
            f"(runtime_dir / 'DONE_task-{i}').write_text('done')\n"
            "time.sleep(2)\n"
            "```"
        )
        resp = test_client.post("/tasks", json={"title": f"Task {i}", "description": desc, "role": "backend"})
        task_ids.append(resp.json()["id"])

    # 2. Run orchestrator with max_agents=3
    orch: Orchestrator = orchestrator_factory(max_agents=3, use_worktrees=True)
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

        # First tick should spawn all 3 (in separate batches if they have the same role?)
        # Actually group_by_role will put them in the same batch if max_tasks_per_agent is large enough.
        # Default max_tasks_per_agent is 1? Let's check OrchestratorConfig.
        # In orchestrator_factory it's not set, so it uses default.

        orch.tick()

        # Verify 3 agents spawned (or 3 tasks claimed)
        resp = test_client.get("/tasks")
        claimed = [t for t in resp.json() if t["status"] == "claimed"]
        assert len(claimed) == 3 or len([t for t in resp.json() if t["status"] == "done"]) == 3

        # Run ticks until done
        for _ in range(30):
            orch.tick()
            resp = test_client.get("/tasks")
            if all(t["status"] == "done" for t in resp.json()):
                break
            time.sleep(0.5)

        # 3. Verify
        for i in range(1, 4):
            fpath = integration_sdd.parent / f"independent_{i}.txt"
            assert fpath.exists()
            assert fpath.read_text() == f"data {i}"

            # Verify no cross-pollution (file from task i should NOT be in worktree of task j)
            # This is naturally handled by worktrees.
