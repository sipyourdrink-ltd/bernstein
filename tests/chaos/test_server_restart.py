"""Chaos test: server restart resilience."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import ConnectError, Response

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_server_restart_resilience(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a task
    task_payload = {
        "title": "B1: Restart test",
        "description": "Long-running task",
        "role": "backend",
        "scope": "small",
        "model": "sonnet",
        "completion_signals": [{"type": "path_exists", "value": "mock_output.txt"}],
    }

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        outage_active = [False]

        def chaos_handler(request):
            if outage_active[0]:
                raise ConnectError("Server is restarting (simulated chaos)...")
            return handler(request)

        respx_mock.route().mock(side_effect=chaos_handler)

        resp = test_client.post("/tasks", json=task_payload)
        task_id = resp.json()["id"]

        # 2. Run orchestrator for 1 tick to claim
        orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=False)
        orch.tick()

        resp = test_client.get(f"/tasks/{task_id}")
        assert resp.json()["status"] == "claimed"

        # 3. Simulate Server Restart
        print("Simulating server outage...")
        outage_active[0] = True

        # Ticks during outage - orchestrator should log errors but stay alive
        for i in range(3):
            orch.tick()
            print(f"Outage tick {i} done")
            await asyncio.sleep(0.1)

        # 4. Recover and finish
        print("Server back online")
        outage_active[0] = False

        for i in range(15):
            # Check for completion marker BEFORE tick
            marker = integration_sdd / "runtime" / f"DONE_{task_id}"
            if marker.exists():
                print(f"Chaos Test: Detected completion for {task_id}, marking done")
                test_client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"})
                marker.unlink()

            orch.tick()
            await asyncio.sleep(0.5)
            resp = test_client.get(f"/tasks/{task_id}")
            status = resp.json()["status"]
            print(f"Recovery tick {i}: status={status}")
            if status == "done":
                break

        assert resp.json()["status"] == "done"
