"""Chaos test: zombie agent process recycling."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_zombie_agent_process(test_client: TestClient, orchestrator_factory, integration_sdd: Path, monkeypatch):
    # 1. Create a task with a script that sleeps forever (zombie)
    desc = "```python\n# INTEGRATION-MOCK\nimport time\nprint('Zombie started')\ntime.sleep(60)\n```"
    resp = test_client.post("/tasks", json={"title": "Zombie Task", "description": desc, "role": "backend"})
    task_id = resp.json()["id"]

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # Speed up recycling
    import bernstein.core.agent_lifecycle

    monkeypatch.setattr(bernstein.core.agent_lifecycle, "_IDLE_GRACE_S", 1.0)

    # WORKAROUND: Monkeypatch adapter.kill to avoid killing the test process group
    from bernstein.adapters.registry import get_adapter

    adapter = get_adapter("integration-mock")

    def safe_kill(pid):
        import contextlib

        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    monkeypatch.setattr(adapter, "kill", safe_kill)

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

        # Tick 0: Spawn zombie
        orch.tick()

        session_id = next(iter(orch._agents.keys()))
        session = orch._agents[session_id]
        assert orch._spawner.check_alive(session)

        # 3. Mark task DONE manually on server
        test_client.post(f"/tasks/{task_id}/complete", json={"result_summary": "manual done"})

        # CHAOS: Break the mapping so process_completed_tasks skips it
        del orch._task_to_session[task_id]

        # Tick 1: Detect idle agent
        orch.tick()

        # Verify SHUTDOWN signal was sent
        assert session_id in orch._idle_shutdown_ts

        # Wait for grace period
        time.sleep(1.5)

        # Tick 2: Grace period elapsed, should KILL process
        orch.tick()

        # Verify process is dead
        assert not orch._spawner.check_alive(session)
        assert session_id not in orch._idle_shutdown_ts
        assert session.status == "dead"
