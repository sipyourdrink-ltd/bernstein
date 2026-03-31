"""Chaos test: adapter spawn error (mock timeout)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

from bernstein.adapters.base import SpawnError

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_adapter_timeout(test_client: TestClient, orchestrator_factory, integration_sdd: Path, monkeypatch):
    # 1. Create a task
    test_client.post("/tasks", json={"title": "Timeout Task", "description": "Fail my spawn", "role": "backend"})

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # Speed up failure
    orch._MAX_SPAWN_FAILURES = 1

    # CHAOS: Mock adapter spawn failure
    from bernstein.adapters.registry import get_adapter
    adapter = get_adapter("integration-mock")

    def failing_spawn(*args, **kwargs):
        raise SpawnError("Mock adapter timeout")

    monkeypatch.setattr(adapter, "spawn", failing_spawn)

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(10):
            orch.tick()

            resp = test_client.get("/tasks")
            tasks = resp.json()
            task = next(t for t in tasks if t["title"] == "Timeout Task")
            if task["status"] == "failed":
                break
            time.sleep(0.1)

        # 3. Verify
        resp = test_client.get("/tasks")
        task = next(t for t in resp.json() if t["title"] == "Timeout Task")
        assert task["status"] == "failed"
        assert "Mock adapter timeout" in task["result_summary"]
