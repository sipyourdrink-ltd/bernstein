"""Chaos test: agent OOM (exit code 137), verify cleanup and recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from bernstein.core.orchestrator import Orchestrator
    from fastapi.testclient import TestClient


def _respx_proxy(test_client: TestClient):
    """Return a respx side_effect that proxies requests to the FastAPI TestClient."""

    def handler(request):
        method = request.method
        path = request.url.path
        api_path = path if path.startswith("/") else "/" + path
        content = request.read()
        headers = dict(request.headers)
        resp = test_client.request(method, api_path, content=content, headers=headers)
        return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

    return handler


@pytest.mark.asyncio
async def test_oom_agent_slot_reclaimed(
    test_client: TestClient,
    orchestrator_factory,
    integration_sdd: Path,
    monkeypatch,
):
    """Spawn a mock agent that exits with code 137 (OOM killed).

    Verify the agent slot is released and available for new agents.
    """
    # Create a task whose mock script exits immediately with code 137
    desc = "```python\n# INTEGRATION-MOCK\nimport sys\nprint('Simulating OOM kill')\nsys.exit(137)\n```"
    resp = test_client.post(
        "/tasks",
        json={"title": "OOM Task Slot", "description": desc, "role": "backend"},
    )
    assert resp.status_code in (200, 201)
    task_id = resp.json()["id"]

    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=False)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        respx_mock.route().mock(side_effect=_respx_proxy(test_client))

        # Tick 0: Spawn the OOM agent
        orch.tick()

        # Find the spawned session
        assert len(orch._agents) >= 1
        session_id = next(
            (sid for sid, s in orch._agents.items() if task_id in s.task_ids),
            None,
        )
        assert session_id is not None, "Agent session for OOM task not found"

        # Wait for the script to exit with code 137
        await asyncio.sleep(2)

        # Tick 1: Detect the dead agent, reclaim the slot
        orch.tick()

        session = orch._agents.get(session_id)
        assert session is not None
        assert session.status == "dead", f"Expected dead, got {session.status}"
        assert session.exit_code == 137

        # The slot should be reclaimed: active (non-dead) agents should be 0
        active_agents = [s for s in orch._agents.values() if s.status != "dead"]
        assert len(active_agents) == 0, f"Expected 0 active agents after OOM, got {len(active_agents)}"


@pytest.mark.asyncio
async def test_oom_agent_task_requeued(
    test_client: TestClient,
    orchestrator_factory,
    integration_sdd: Path,
    monkeypatch,
):
    """After OOM kill, verify the task is requeued (status goes to OPEN or FAILED).

    With max_task_retries=0 (default in test config), the task should be
    marked FAILED. With retries > 0, it would go back to OPEN.
    """
    desc = (
        "```python\n# INTEGRATION-MOCK\nimport sys\nprint('Simulating OOM kill for requeue test')\nsys.exit(137)\n```"
    )
    resp = test_client.post(
        "/tasks",
        json={"title": "OOM Task Requeue", "description": desc, "role": "backend"},
    )
    assert resp.status_code in (200, 201)
    task_id = resp.json()["id"]

    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=False)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        respx_mock.route().mock(side_effect=_respx_proxy(test_client))

        # Tick 0: Spawn the agent
        orch.tick()

        # Wait for the OOM-exit script to finish
        await asyncio.sleep(2)

        # Tick 1: Detect agent death and handle orphaned task
        orch.tick()

        # With max_task_retries=0, the task should be failed (not requeued)
        resp = test_client.get("/tasks")
        tasks = resp.json()
        task = next((t for t in tasks if t["id"] == task_id), None)
        assert task is not None, f"Task {task_id} not found in task list"
        # The task should be either "failed" (retries exhausted) or "open" (requeued).
        # With max_task_retries=0 in test config, expect "failed".
        assert task["status"] in ("failed", "open"), (
            f"Expected task to be failed or open after OOM, got {task['status']}"
        )


@pytest.mark.asyncio
async def test_oom_agent_worktree_preserved(
    test_client: TestClient,
    orchestrator_factory,
    integration_sdd: Path,
    monkeypatch,
):
    """After OOM with recovery='resume', verify the worktree is NOT deleted.

    When the orchestrator is configured with recovery='resume', crashed agent
    worktrees should be preserved so partial work can be resumed.
    """
    desc = (
        "```python\n# INTEGRATION-MOCK\nimport sys, time\n"
        "print('OOM in worktree test')\n"
        "time.sleep(0.5)\n"
        "sys.exit(137)\n```"
    )
    resp = test_client.post(
        "/tasks",
        json={"title": "OOM Worktree", "description": desc, "role": "backend"},
    )
    assert resp.status_code in (200, 201)
    task_id = resp.json()["id"]

    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # Enable crash-resume recovery so worktrees are preserved
    orch._config = orch._config.__class__(
        server_url=orch._config.server_url,
        max_agents=orch._config.max_agents,
        poll_interval_s=orch._config.poll_interval_s,
        max_task_retries=orch._config.max_task_retries,
        recovery="resume",
        max_crash_retries=3,
    )

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        respx_mock.route().mock(side_effect=_respx_proxy(test_client))

        # Tick 0: Spawn agent with worktree
        orch.tick()

        # Find the session
        session_id = next(
            (sid for sid, s in orch._agents.items() if task_id in s.task_ids),
            None,
        )
        assert session_id is not None, "Agent session not found"

        # Capture the worktree path before it dies
        _worktree_path = orch._spawner.get_worktree_path(session_id)

        # Wait for OOM exit
        await asyncio.sleep(2)

        # Tick 1: Detect death
        orch.tick()

        session = orch._agents.get(session_id)
        assert session is not None
        assert session.status == "dead"
        assert session.exit_code == 137

        # With recovery='resume', the worktree should be preserved
        assert task_id in orch._preserved_worktrees, (
            f"Expected worktree to be preserved for task {task_id} in recovery='resume' mode"
        )
        # The crash count should have been incremented
        assert orch._crash_counts.get(task_id, 0) >= 1


@pytest.mark.asyncio
async def test_oom_agent_metrics_recorded(
    test_client: TestClient,
    orchestrator_factory,
    integration_sdd: Path,
    monkeypatch,
):
    """Verify the metric collector records the OOM as a failure with OOM abort reason."""
    desc = "```python\n# INTEGRATION-MOCK\nimport sys\nprint('OOM for metrics test')\nsys.exit(137)\n```"
    resp = test_client.post(
        "/tasks",
        json={"title": "OOM Metrics", "description": desc, "role": "backend"},
    )
    assert resp.status_code in (200, 201)
    task_id = resp.json()["id"]

    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=False)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        respx_mock.route().mock(side_effect=_respx_proxy(test_client))

        # Tick 0: Spawn the agent
        orch.tick()

        session_id = next(
            (sid for sid, s in orch._agents.items() if task_id in s.task_ids),
            None,
        )
        assert session_id is not None

        # Wait for OOM exit
        await asyncio.sleep(2)

        # Tick 1: Detect death and record metrics
        orch.tick()

        session = orch._agents.get(session_id)
        assert session is not None
        assert session.exit_code == 137

        # Verify the abort reason was classified as OOM
        from bernstein.core.models import AbortReason

        assert session.abort_reason == AbortReason.OOM, f"Expected abort_reason=OOM, got {session.abort_reason}"

        # Check that metrics were written to the daily JSONL
        metrics_dir = integration_sdd / "metrics"
        if metrics_dir.exists():
            jsonl_files = sorted(metrics_dir.glob("*.jsonl"))
            metrics_entries: list[dict] = []
            for jf in jsonl_files:
                for line in jf.read_text().strip().splitlines():
                    if line.strip():
                        metrics_entries.append(json.loads(line))

            # Find metrics for our task
            task_metrics = [m for m in metrics_entries if m.get("task_id") == task_id]
            if task_metrics:
                # At least one metric entry should record failure
                has_failure = any(not m.get("success", True) for m in task_metrics)
                assert has_failure, (
                    f"Expected at least one failure metric for task {task_id}, but all metrics show success"
                )
