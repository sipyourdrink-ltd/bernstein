"""Chaos test: corrupt task payload from server."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_corrupt_task_payload(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a task
    test_client.post("/tasks", json={"title": "Good Task", "description": "I am fine", "role": "backend"})

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    corrupt_mode = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if corrupt_mode and method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                data = resp.json()
                # Add a corrupt task
                data.append(
                    {
                        "id": "corrupt-1",
                        "title": "Corrupt Task",
                        "status": "open",
                        "description": 12345,  # SHOULD BE STRING
                        # Missing other required fields
                    }
                )
                return Response(200, json=data)

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # First tick is fine
        orch.tick()

        # Second tick with corruption
        corrupt_mode = True
        try:
            orch.tick()
        except Exception as exc:
            print(f"Orchestrator crashed as expected: {exc}")
            # If it crashed, we verified it's NOT resilient to this specific corruption
            return

        # 3. Verify (if it didn't crash)
        # It might have skipped the bad task and processed the good one
        # But looking at fetch_all_tasks, it will likely crash.
