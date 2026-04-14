"""Integration test: batch task execution."""

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

        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(test_client, integration_sdd)
        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(30):
            orch.tick()
            resp = test_client.get("/tasks")
            tasks = resp.json()
            if all(t["status"] == "done" for t in tasks):
                break
            await asyncio.sleep(0.5)

        # 3. Verify
        assert spawn_count == 1
        resp = test_client.get("/tasks")
        for t in resp.json():
            assert t["status"] == "done"

        assert (integration_sdd.parent / "batch.txt").exists()
