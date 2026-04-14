"""Integration test: sequential task dependencies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from bernstein.core.models import Task

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_sequential_dependency(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a backend task
    desc_backend = (
        "```python\n"
        "# INTEGRATION-MOCK\n"
        "import os, subprocess, time\n"
        "from pathlib import Path\n"
        "Path('api.py').write_text('API v1')\n"
        "subprocess.run(['git', 'add', 'api.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'add api'], check=True)\n"
        "runtime_dir = Path(__file__).parent\n"
        "(runtime_dir / 'DONE_backend').write_text('done')\n"
        "time.sleep(5)\n"
        "```"
    )
    resp = test_client.post("/tasks", json={"title": "Backend", "description": desc_backend, "role": "backend"})
    backend_id = resp.json()["id"]

    # 2. Create a frontend task depending on backend
    desc_frontend = """
```python
# INTEGRATION-MOCK
import os, subprocess, time
from pathlib import Path
if not Path('api.py').exists():
    raise RuntimeError('api.py missing - dependency failed')
Path('ui.js').write_text('UI v1')
subprocess.run(['git', 'add', 'ui.js'], check=True)
subprocess.run(['git', 'commit', '-m', 'add ui'], check=True)
runtime_dir = Path(__file__).parent
(runtime_dir / 'DONE_frontend').write_text('done')
time.sleep(2)
```"""
    test_client.post(
        "/tasks",
        json={"title": "Frontend", "description": desc_frontend, "role": "frontend", "depends_on": [backend_id]},
    )

    # 3. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=2, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # FIX: The orchestrator loop (Step 3c before Step 4) spawns dependent tasks
    # BEFORE merging their dependencies if they complete in the same tick.
    # We monkeypatch the spawner to force a completion pass before spawning.
    original_spawn = orch._spawner.spawn_for_tasks

    def fixed_spawn(tasks):
        # Force a completion pass so dependencies are merged before worktree creation
        resp = test_client.get("/tasks")
        done_tasks = [Task.from_dict(t) for t in resp.json() if t["status"] == "done"]
        from bernstein.core.orchestrator import TickResult
        from bernstein.core.task_lifecycle import process_completed_tasks

        process_completed_tasks(orch, done_tasks, TickResult())
        return original_spawn(tasks)

    orch._spawner.spawn_for_tasks = fixed_spawn

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(
            test_client,
            integration_sdd,
            slug_fn=lambda t: t["title"].lower(),
        )
        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(40):
            orch.tick()

            # WORKAROUND: Manually purge dead agents to avoid race condition
            dead_ids = [sid for sid, s in orch._agents.items() if s.status == "dead"]
            for sid in dead_ids:
                del orch._agents[sid]

            resp = test_client.get("/tasks")
            if all(t["status"] == "done" for t in resp.json()):
                break
            await asyncio.sleep(0.5)

        # 4. Verify
        resp = test_client.get("/tasks")
        for t in resp.json():
            assert t["status"] == "done", f"Task {t['title']} failed: {t['status']}"

        assert (integration_sdd.parent / "api.py").exists()
        assert (integration_sdd.parent / "ui.js").exists()
