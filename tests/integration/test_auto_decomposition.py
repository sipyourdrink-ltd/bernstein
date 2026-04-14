"""Integration test: auto-decomposition of large tasks."""

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
async def test_auto_decomposition(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a large task
    test_client.post(
        "/tasks",
        json={"title": "Large Feature", "description": "A very large task.", "role": "backend", "scope": "large"},
    )

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=2, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False
    orch._config.force_parallel = True

    handled_decompose_ids = set()

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def _handle_decompose(tasks_data):
            for t in tasks_data:
                if (
                    t["title"].startswith("[DECOMPOSE]")
                    and t["status"] == "claimed"
                    and t["id"] not in handled_decompose_ids
                ):
                    handled_decompose_ids.add(t["id"])
                    for i in range(1, 3):
                        desc = (
                            "```python\n# INTEGRATION-MOCK\nimport os, subprocess, time\nfrom pathlib import Path\n"
                            f"Path('sub_{i}.txt').write_text('done')\n"
                            f"subprocess.run(['git', 'add', 'sub_{i}.txt'], check=True)\n"
                            f"subprocess.run(['git', 'commit', '-m', 'sub {i}'], check=True)\n"
                            f"runtime_dir = Path(__file__).parent\n(runtime_dir / 'DONE_subtask-{i}').write_text('done')\n"
                            "time.sleep(2)\n```"
                        )
                        test_client.post(
                            "/tasks",
                            json={"title": f"Subtask {i}", "description": desc, "role": "backend", "scope": "small"},
                        )
                    test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "decomposed"})

        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(test_client, integration_sdd, on_tasks_fetched=_handle_decompose)
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
            await asyncio.sleep(0.5)

        # 3. Verify
        resp = test_client.get("/tasks")
        tasks = resp.json()
        subtasks = [t for t in tasks if "Subtask" in t["title"] and not t["title"].startswith("[CONFLICT]")]
        assert len(subtasks) == 2
        assert all(t["status"] == "done" for t in subtasks)
