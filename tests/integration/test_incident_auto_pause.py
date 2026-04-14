"""Integration test: incident auto-pause on high failure rate."""

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
async def test_incident_auto_pause(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create 5 tasks
    task_ids = []
    for i in range(1, 6):
        title = f"Task {i}"
        # Task 1 will fail
        if i == 1:
            desc = "```python\n# INTEGRATION-MOCK\nimport sys\nsys.exit(1)\n```"
        else:
            slug = title.lower().replace(" ", "-")
            desc = f"```python\n# INTEGRATION-MOCK\nfrom pathlib import Path\nruntime_dir = Path(__file__).parent\n(runtime_dir / 'DONE_{slug}').write_text('done')\n```"

        resp = test_client.post("/tasks", json={"title": title, "description": desc, "role": "backend"})
        task_ids.append(resp.json()["id"])

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=5, use_worktrees=True)
    # Ensure auto_pause IS ENABLED (default is True, but orchestrator_factory might have changed it?)
    # Actually orchestrator_factory in conftest doesn't touch it.
    orch._incident_manager.auto_pause = True

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(test_client, integration_sdd)
        respx_mock.route().mock(side_effect=handler)

        # Run ticks until Tick 5 (where incident check happens)
        # Note: we need enough ticks for tasks to complete and be reaped.
        for tick_idx in range(10):
            orch.tick()

            # Manually purge dead agents to avoid the race condition found in previous tests
            dead_ids = [sid for sid, s in orch._agents.items() if s.status == "dead"]
            for sid in dead_ids:
                del orch._agents[sid]

            print(f"Tick {tick_idx}: pause={orch._incident_manager.should_pause}")
            if orch._incident_manager.should_pause:
                break
            await asyncio.sleep(0.2)

        # 3. Verify
        assert orch._incident_manager.should_pause
        assert len(orch._incident_manager.incidents) > 0

        # Verify orch.tick() now returns early
        # We can check if any NEW agents are spawned even if there are open tasks.
        # But wait, if it's paused, it should skip Step 3 (spawning).

        pre_tick_spawned = len(orch._agents)
        orch.tick()
        assert len(orch._agents) == pre_tick_spawned
