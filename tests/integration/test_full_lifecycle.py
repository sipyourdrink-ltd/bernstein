"""Integration test: full task lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_full_lifecycle(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a task via API
    task_payload = {
        "title": "Simple lifecycle test",
        "description": "Create a file named mock_output.txt with content 'success'",
        "role": "backend",
        "scope": "small",
        "model": "sonnet",
        "completion_signals": [{"type": "path_exists", "value": "mock_output.txt"}],
    }

    # We'll use respx to route orchestrator's httpx calls to the TestClient
    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        # Route all calls to our test_client
        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(
            test_client,
            integration_sdd,
            slug_fn=lambda t: t["id"],
            complete_statuses=frozenset({"working"}),
        )
        respx_mock.route().mock(side_effect=handler)

        # Create task
        resp = test_client.post("/tasks", json=task_payload)
        assert resp.status_code == 201
        task_id = resp.json()["id"]

        # 2. Run orchestrator for a few ticks
        orch: Orchestrator = orchestrator_factory(max_agents=1)

        # We need to give the mock agent time to work and the orchestrator time to process
        # Run 10 ticks (give more time)
        for i in range(10):
            # Intercept and auto-complete BEFORE tick
            resp = test_client.get("/tasks")
            tasks = resp.json()
            for t in tasks:
                if t["status"] in ("claimed", "working", "in_progress"):
                    marker = integration_sdd / "runtime" / f"DONE_{t['id']}"
                    if marker.exists():
                        print(f"Detected completion for {t['id']}, marking done via API")
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                        marker.unlink()

            orch.tick()
            await asyncio.sleep(0.5)
            # Log current task status
            resp = test_client.get(f"/tasks/{task_id}")
            status = resp.json()["status"]
            print(f"Tick {i}: Task status = {status}")
            if status == "done":
                break

        # 3. Verify task is done
        resp = test_client.get(f"/tasks/{task_id}")
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status != "done":
            print(f"FAILED! SDD path: {integration_sdd}")
            await asyncio.sleep(10)
        assert status == "done"

        # 4. Verify file was created and merged to main
        lifecycle_file = integration_sdd.parent / "mock_output.txt"
        assert lifecycle_file.exists()
        assert "completed" in lifecycle_file.read_text()
