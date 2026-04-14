"""Chaos test: git index locked during merge."""

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
async def test_worktree_locked(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a task
    desc = (
        "```python\n"
        "# INTEGRATION-MOCK\n"
        "from pathlib import Path\n"
        "Path('chaos.txt').write_text('chaos')\n"
        "import subprocess\n"
        "subprocess.run(['git', 'add', 'chaos.txt'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'chaos'], check=True)\n"
        "runtime_dir = Path(__file__).parent\n"
        "(runtime_dir / 'DONE_chaos').write_text('done')\n"
        "import time\n"
        "time.sleep(2)\n"
        "```"
    )
    test_client.post("/tasks", json={"title": "Chaos Task", "description": desc, "role": "backend"})

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

        def _inject_lock_on_completion(tasks_data):
            """CHAOS: Lock the git index when tasks have DONE markers."""
            for t in tasks_data:
                slug = t["title"].lower().replace(" ", "-")
                marker = integration_sdd / "runtime" / f"DONE_{slug}"
                if marker.exists():
                    lock_file = integration_sdd.parent / ".git" / "index.lock"
                    lock_file.write_text("locked")

        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(
            test_client,
            integration_sdd,
            on_tasks_fetched=_inject_lock_on_completion,
        )
        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for _ in range(20):
            orch.tick()

            # Manually purge dead agents
            dead_ids = [sid for sid, s in orch._agents.items() if s.status == "dead"]
            for sid in dead_ids:
                del orch._agents[sid]

            resp = test_client.get("/tasks")
            if any(t["status"] == "done" for t in resp.json()):
                break
            await asyncio.sleep(0.5)

        # 3. Verify
        # The task should be done on server, but changes NOT on main because merge failed
        assert not (integration_sdd.parent / "chaos.txt").exists()

        # Cleanup lock so next tests can run
        lock_file = integration_sdd.parent / ".git" / "index.lock"
        if lock_file.exists():
            lock_file.unlink()
