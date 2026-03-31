"""Integration test: Plan execution."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
import yaml
from httpx import Response

from bernstein.core.manager_parsing import _resolve_depends_on
from bernstein.core.plan_loader import load_plan_from_yaml

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_plan_execution(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a plan file
    plan_path = integration_sdd.parent / "my_plan.yaml"
    plan_content = {
        "name": "Multi-stage Plan",
        "stages": [
            {
                "name": "Stage1",
                "steps": [
                    {"goal": "Step 1.1", "role": "backend", "scope": "small"},
                ],
            },
            {
                "name": "Stage2",
                "depends_on": ["Stage1"],
                "steps": [
                    {"goal": "Step 2.1", "role": "backend", "scope": "small"},
                ],
            },
        ],
    }
    plan_path.write_text(yaml.dump(plan_content))

    # 2. Load tasks from plan
    tasks = load_plan_from_yaml(plan_path)
    _resolve_depends_on(tasks)
    assert len(tasks) == 2
    assert tasks[1].depends_on == [tasks[0].id]

    # 3. Bootstrap (inject tasks into server)
    # We'll use a global respx mock to handle all orchestrator calls
    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            # Auto-complete logic for tick: when orchestrator polls, we check if any agent is 'working' and finished
            if method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                ts = resp.json()
                for t in ts:
                    if t["status"] == "working":
                        marker = integration_sdd / "runtime" / f"DONE_{t['id']}"
                        if marker.exists():
                            print(f"Handler: Detected completion for {t['id']}, marking done")
                            test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                            marker.unlink()
                # Re-fetch after possible completions
                resp = test_client.get("/tasks")
                return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        id_map = {}
        for t in tasks:
            t.depends_on = [id_map.get(dep, dep) for dep in t.depends_on]
            old_id = t.id

            payload = {
                "title": t.title,
                "description": t.description,
                "role": t.role,
                "scope": t.scope.value,
                "depends_on": t.depends_on,
                "model": "sonnet",  # Force mock
            }
            resp = test_client.post("/tasks", json=payload)
            assert resp.status_code == 201
            server_id = resp.json()["id"]
            id_map[old_id] = server_id

        # 4. Run orchestrator
        orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=False)

        # Run ticks until both are done
        for i in range(30):
            # Check for completion markers and update server BEFORE tick
            # This is more reliable than respx handler for state transitions
            resp = test_client.get("/tasks")
            ts = resp.json()
            for t in ts:
                if t["status"] in ("claimed", "working", "in_progress"):
                    marker = integration_sdd / "runtime" / f"DONE_{t['id']}"
                    if marker.exists():
                        print(f"Test Loop: Detected completion for {t['id']}, marking done")
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                        marker.unlink()

            orch.tick()
            time.sleep(0.5)

            resp = test_client.get("/tasks")
            all_tasks = resp.json()
            done_count = sum(1 for t in all_tasks if t["status"] == "done")
            print(f"Tick {i}: Done {done_count}/2")
            if done_count == 2:
                break

        resp = test_client.get("/tasks")
        assert sum(1 for t in resp.json() if t["status"] == "done") == 2
