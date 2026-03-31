"""Integration test: auto-decomposition of large tasks."""

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
async def test_auto_decomposition(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a large task
    test_client.post("/tasks", json={
        "title": "Large Feature",
        "description": "A very large task.",
        "role": "backend",
        "scope": "large"
    })

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=2, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False
    orch._config.force_parallel = True

    handled_decompose_ids = set()

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                tasks_data = resp.json()
                for t in tasks_data:
                    if t["title"].startswith("[DECOMPOSE]") and t["status"] == "claimed" and t["id"] not in handled_decompose_ids:
                        handled_decompose_ids.add(t["id"])
                        # Create subtasks with unique scripts to avoid conflicts
                        for i in range(1, 3):
                            desc = (
                                "```python\n"
                                "# INTEGRATION-MOCK\n"
                                "import os, subprocess, time\n"
                                "from pathlib import Path\n"
                                f"Path('sub_{i}.txt').write_text('done')\n"
                                f"subprocess.run(['git', 'add', 'sub_{i}.txt'], check=True)\n"
                                f"subprocess.run(['git', 'commit', '-m', 'sub {i}'], check=True)\n"
                                "runtime_dir = Path(__file__).parent\n"
                                f"(runtime_dir / 'DONE_subtask-{i}').write_text('done')\n"
                                "time.sleep(2)\n"
                                "```"
                            )
                            test_client.post("/tasks", json={
                                "title": f"Subtask {i}",
                                "description": desc,
                                "role": "backend",
                                "scope": "small"
                            })
                        # Complete the manager task
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "decomposed"})

                # Handle markers
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
        for _ in range(40):
            orch.tick()

            # WORKAROUND: Manually purge dead agents to avoid race condition
            dead_ids = [sid for sid, s in orch._agents.items() if s.status == "dead"]
            for sid in dead_ids:
                del orch._agents[sid]

            resp = test_client.get("/tasks")
            tasks = resp.json()
            subtasks = [t for t in tasks if "Subtask" in t["title"] and not t["title"].startswith("[CONFLICT]")]
            if subtasks and len(subtasks) == 2 and all(t["status"] == "done" for t in subtasks):
                break
            time.sleep(0.5)

        # 3. Verify
        resp = test_client.get("/tasks")
        tasks = resp.json()
        subtasks = [t for t in tasks if "Subtask" in t["title"] and not t["title"].startswith("[CONFLICT]")]
        assert len(subtasks) == 2
        assert all(t["status"] == "done" for t in subtasks)
