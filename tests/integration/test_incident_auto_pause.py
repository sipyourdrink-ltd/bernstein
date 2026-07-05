"""Integration test: incident auto-pause on high failure rate.

Auto-pause is driven by the incident manager, which only requests a pause
for SEV1/SEV2 incidents:

- SEV1: >75% failure rate across 10+ tasks
- SEV2: error budget depleted (>= max(3, 10% of tasks) failures) across 5+ tasks

Incident detection runs on the orchestrator's slow tick (every
``ORCHESTRATOR.slow_tick_phase`` ticks), and a dead-looking agent is only
failed after the orphan liveness grace window elapses. This test pins both
knobs so a 5-task all-failing run deterministically depletes the error
budget and requests a pause within a handful of ticks.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_incident_auto_pause(
    test_client: TestClient,
    orchestrator_factory,
    integration_sdd: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    # 1. Create 5 tasks that all fail immediately. The error budget floor
    #    allows 3 failures, so 5 failed tasks out of 5 depletes the budget
    #    and raises a SEV2 incident (which requests the pause).
    task_ids = []
    for i in range(1, 6):
        desc = "```python\n# INTEGRATION-MOCK\nimport sys\nsys.exit(1)\n```"
        resp = test_client.post("/tasks", json={"title": f"Task {i}", "description": desc, "role": "backend"})
        task_ids.append(resp.json()["id"])

    # 2. Run orchestrator with auto-pause enabled.
    orch: Orchestrator = orchestrator_factory(max_agents=5, use_worktrees=True)
    orch._incident_manager.auto_pause = True

    # The mock adapter runs the agent script as a direct child process, so
    # its tracked PID is ground truth. Skip the orphan liveness grace that
    # defers death judgment while worktree/heartbeat files look fresh
    # (it exists for double-forking runners, which the mock is not).
    from bernstein.core.agents import agent_lifecycle

    monkeypatch.setattr(agent_lifecycle, "_ORPHAN_LIVENESS_GRACE_S", 0.0)

    # Incident detection is gated behind the slow tick; run it every tick
    # so the pause request lands within the test's tick budget.
    from bernstein.core.orchestration import orchestrator as orchestrator_module

    monkeypatch.setattr(
        orchestrator_module,
        "ORCHESTRATOR",
        dataclasses.replace(orchestrator_module.ORCHESTRATOR, slow_tick_phase=1),
    )

    # The slow tick also triggers the manager queue review on failures,
    # which calls an LLM; keep it out of this test.
    monkeypatch.setattr(orch, "_run_manager_queue_review", lambda: None)

    # The error budget is computed from the process-global metrics
    # collector; reset it so task counts from other tests in the same
    # process do not skew the budget.
    from bernstein.core.observability.metric_collector import get_collector

    get_collector().reset_task_metrics()

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        from tests.integration.conftest import make_proxy_handler

        handler = make_proxy_handler(test_client, integration_sdd)
        respx_mock.route().mock(side_effect=handler)

        for tick_idx in range(15):
            orch.tick()

            # Manually purge dead agents to avoid the race condition found in previous tests
            dead_ids = [sid for sid, s in orch._agents.items() if s.status == "dead"]
            for sid in dead_ids:
                del orch._agents[sid]

            print(f"Tick {tick_idx}: pause={orch._incident_manager.should_pause}")
            if orch._incident_manager.should_pause:
                break
            await asyncio.sleep(0.2)

        # 3. Verify: pause requested and at least one incident recorded.
        assert orch._incident_manager.should_pause
        assert len(orch._incident_manager.incidents) > 0

        # A paused orchestrator must not spawn new agents.
        pre_tick_spawned = len(orch._agents)
        orch.tick()
        assert len(orch._agents) == pre_tick_spawned
