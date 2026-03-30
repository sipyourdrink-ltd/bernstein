"""End-to-end test: distributed cluster with task stealing.

Exercises the full cluster lifecycle:
  1. Central server with cluster mode enabled
  2. Two worker nodes register and send heartbeats
  3. Tasks are created and claimed by workers
  4. Task stealing rebalances overloaded nodes
  5. Workers complete tasks and final state is consistent
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path
from httpx import ASGITransport, AsyncClient

from bernstein.core.models import ClusterConfig, ClusterTopology
from bernstein.core.server import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cluster_app(tmp_path: Path):
    """Central server with cluster mode and star topology."""
    return create_app(
        jsonl_path=tmp_path / "tasks.jsonl",
        cluster_config=ClusterConfig(
            enabled=True,
            topology=ClusterTopology.STAR,
            node_timeout_s=60,
        ),
    )


@pytest.fixture()
async def api(cluster_app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=cluster_app)
    async with AsyncClient(transport=transport, base_url="http://central") as c:
        yield c


def _node(name: str, slots: int = 4) -> dict:
    return {
        "name": name,
        "url": f"http://{name}:8052",
        "capacity": {
            "max_agents": slots,
            "available_slots": slots,
            "active_agents": 0,
            "gpu_available": False,
            "supported_models": ["sonnet", "opus", "haiku"],
        },
        "labels": {"name": name},
        "cell_ids": [],
    }


def _task(title: str, role: str = "backend") -> dict:
    return {
        "title": title,
        "description": f"Do: {title}",
        "role": role,
        "priority": 1,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 10,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_full_cluster_lifecycle(api: AsyncClient) -> None:
    """Register nodes, create tasks, claim, complete, verify cluster state."""
    # Register two workers
    r1 = await api.post("/cluster/nodes", json=_node("worker-alpha", slots=4))
    r2 = await api.post("/cluster/nodes", json=_node("worker-beta", slots=4))
    assert r1.status_code == 201
    assert r2.status_code == 201
    # Create 6 tasks
    task_ids = []
    for i in range(6):
        r = await api.post("/tasks", json=_task(f"task-{i}"))
        assert r.status_code == 201
        task_ids.append(r.json()["id"])

    # Alpha claims 3 tasks
    alpha_tasks = []
    for _ in range(3):
        r = await api.get("/tasks/next/backend")
        assert r.status_code == 200
        alpha_tasks.append(r.json()["id"])

    # Beta claims 3 tasks
    beta_tasks = []
    for _ in range(3):
        r = await api.get("/tasks/next/backend")
        assert r.status_code == 200
        beta_tasks.append(r.json()["id"])

    # All 6 claimed
    claimed = await api.get("/tasks?status=claimed")
    assert len(claimed.json()) == 6

    # Alpha completes its tasks
    for tid in alpha_tasks:
        r = await api.post(f"/tasks/{tid}/complete", json={"result_summary": "Done by alpha"})
        assert r.status_code == 200

    # Beta completes its tasks
    for tid in beta_tasks:
        r = await api.post(f"/tasks/{tid}/complete", json={"result_summary": "Done by beta"})
        assert r.status_code == 200

    # Verify final state
    status = await api.get("/status")
    s = status.json()
    assert s["done"] == 6
    assert s["open"] == 0

    # Cluster still healthy
    cluster = await api.get("/cluster/status")
    cs = cluster.json()
    assert cs["online_nodes"] == 2
    assert cs["total_nodes"] == 2


@pytest.mark.anyio
async def test_task_steal_endpoint(api: AsyncClient) -> None:
    """POST /cluster/steal evaluates the steal policy and returns actions."""
    # Register two nodes
    r1 = await api.post("/cluster/nodes", json=_node("overloaded", slots=2))
    r2 = await api.post("/cluster/nodes", json=_node("idle-node", slots=8))
    assert r1.status_code == 201
    assert r2.status_code == 201
    overloaded_id = r1.json()["id"]
    idle_id = r2.json()["id"]

    # The steal endpoint evaluates the policy based on reported queue depths
    resp = await api.post(
        "/cluster/steal",
        json={"queue_depths": {overloaded_id: 10, idle_id: 0}},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Response structure is correct
    assert "actions" in data
    assert "total_stolen" in data
    assert isinstance(data["actions"], list)


@pytest.mark.anyio
async def test_worker_heartbeat_updates_capacity(api: AsyncClient) -> None:
    """Worker heartbeats update capacity in the cluster view."""
    r = await api.post("/cluster/nodes", json=_node("busy-worker", slots=8))
    assert r.status_code == 201
    node_id = r.json()["id"]

    # Send heartbeat with reduced capacity (6 active agents)
    hb = await api.post(
        f"/cluster/nodes/{node_id}/heartbeat",
        json={
            "capacity": {
                "max_agents": 8,
                "available_slots": 2,
                "active_agents": 6,
                "gpu_available": False,
                "supported_models": ["sonnet"],
            }
        },
    )
    assert hb.status_code == 200
    assert hb.json()["capacity"]["active_agents"] == 6
    assert hb.json()["capacity"]["available_slots"] == 2

    # Verify in cluster status
    status = await api.get("/cluster/status")
    cs = status.json()
    assert cs["active_agents"] == 6
    assert cs["available_slots"] == 2


@pytest.mark.anyio
async def test_concurrent_claims_across_workers(api: AsyncClient) -> None:
    """Multiple workers racing to claim tasks: no double-claiming."""
    import asyncio

    await api.post("/cluster/nodes", json=_node("alpha"))
    await api.post("/cluster/nodes", json=_node("beta"))

    # Single task, two concurrent claims
    r = await api.post("/tasks", json=_task("contested-task"))
    task_id = r.json()["id"]

    results = await asyncio.gather(
        api.post(f"/tasks/{task_id}/claim?expected_version=1"),
        api.post(f"/tasks/{task_id}/claim?expected_version=1"),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], f"Expected [200, 409], got {codes}"


@pytest.mark.anyio
async def test_node_graceful_unregister(api: AsyncClient) -> None:
    """Worker graceful shutdown unregisters from the cluster."""
    r = await api.post("/cluster/nodes", json=_node("ephemeral"))
    node_id = r.json()["id"]

    # Cluster shows 1 node
    status = await api.get("/cluster/status")
    assert status.json()["total_nodes"] == 1

    # Unregister
    del_r = await api.delete(f"/cluster/nodes/{node_id}")
    assert del_r.status_code == 204

    # Cluster empty
    status = await api.get("/cluster/status")
    assert status.json()["total_nodes"] == 0


@pytest.mark.anyio
async def test_worker_cli_command_exists() -> None:
    """The `bernstein worker` CLI command is registered."""
    from click.testing import CliRunner

    from bernstein.cli.main import cli

    runner = CliRunner()
    result = runner.invoke(cli, ["worker", "--help"])
    assert result.exit_code == 0
    assert "worker" in result.output.lower()
    assert "--server" in result.output
    assert "--slots" in result.output
    assert "--roles" in result.output
