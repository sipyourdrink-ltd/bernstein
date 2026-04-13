"""Chaos test: concurrent task claim conflict (CAS)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_concurrent_claim_conflict(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a task
    resp = test_client.post("/tasks", json={"title": "Shared Task", "description": "Claim me", "role": "backend"})
    task_id = resp.json()["id"]
    initial_version = resp.json()["version"]

    # 2. Create two orchestrator instances
    orch1: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch2: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)

    orch1._approval_gate = None
    orch2._approval_gate = None
    orch1._incident_manager.auto_pause = False
    orch2._incident_manager.auto_pause = False

    stale_mode = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if stale_mode and method == "GET" and api_path == "/tasks":
                # CHAOS: Return the task as OPEN even though it's claimed, with INITIAL version
                resp = test_client.get("/tasks")
                data = resp.json()
                for t in data:
                    if t["id"] == task_id:
                        t["status"] = "open"
                        t["assigned_agent"] = None
                        t["version"] = initial_version
                return Response(200, json=data)

            content = request.read()
            headers = dict(request.headers)
            # CRITICAL: Pass query params (like expected_version) to test_client
            resp = test_client.request(
                method, api_path, content=content, headers=headers, params=dict(request.url.params)
            )
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # 3. Simulate concurrency
        # Instance 1 claims the task
        orch1.tick()

        resp = test_client.get(f"/tasks/{task_id}")
        assert resp.json()["status"] == "claimed"
        version_after_claim1 = resp.json()["version"]
        assert version_after_claim1 > initial_version

        # Instance 2 tries to claim the same task with STALE info
        stale_mode = True
        orch2.tick()

        # 4. Verify orch2 didn't spawn anything for this task
        # (It should have received 409 and skipped)
        assert task_id not in [tid for s in orch2._agents.values() for tid in s.task_ids]

        # Check server state - still claimed, version should not have increased again
        resp = test_client.get(f"/tasks/{task_id}")
        assert resp.json()["status"] == "claimed"
        assert resp.json()["version"] == version_after_claim1
