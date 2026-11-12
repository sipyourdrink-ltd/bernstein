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
async def test_worktree_locked(
    test_client: TestClient, orchestrator_factory, integration_sdd: Path, caplog: pytest.LogCaptureFixture
):
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
        "(runtime_dir / 'DONE_chaos-task').write_text('done')\n"
        "import time\n"
        "time.sleep(2)\n"
        "```"
    )
    created = test_client.post("/tasks", json={"title": "Chaos Task", "description": desc, "role": "backend"})
    assert created.status_code in (200, 201), created.text
    task_id = created.json()["id"]

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    lock_file = integration_sdd.parent / ".git" / "index.lock"
    owns_lock = False

    try:
        with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:

            def _inject_lock_on_completion(tasks_data):
                """CHAOS: Lock the git index when tasks have DONE markers."""
                nonlocal owns_lock
                if owns_lock:
                    return
                for t in tasks_data:
                    if t["id"] != task_id:
                        continue
                    slug = t["title"].lower().replace(" ", "-")
                    marker = integration_sdd / "runtime" / f"DONE_{slug}"
                    if marker.exists():
                        with lock_file.open("x", encoding="utf-8") as handle:
                            owns_lock = True
                            handle.write("locked")
                        break

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
                task = next(t for t in resp.json() if t["id"] == task_id)
                if task["status"] in {"done", "failed"}:
                    break
                await asyncio.sleep(0.5)
            else:
                pytest.fail("the lock test task did not reach a terminal state")

            # 3. Verify
            assert owns_lock, "the test never injected its index lock"
            assert task["status"] == "failed"
            assert any(
                f"Merge-back failed for task {task_id}" in message and str(lock_file) in message
                for message in caplog.messages
            ), "the task failed without hitting the injected index lock"
            assert not (integration_sdd.parent / "chaos.txt").exists()

    finally:
        if owns_lock:
            lock_file.unlink(missing_ok=True)
