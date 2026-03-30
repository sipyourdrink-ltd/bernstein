"""Integration test: two Bernstein instances coordinating on shared tasks.

Simulates a star-topology cluster:
  - One central task server (in-process ASGI)
  - Two worker nodes registering themselves and competing to claim tasks
  - Verifies that tasks are distributed without double-claiming via CAS
  - Verifies the cluster status view shows all nodes
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.models import ClusterConfig, ClusterTopology
from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def central_app(tmp_path: Path):
    """Central server with cluster mode enabled (star topology)."""
    return create_app(
        jsonl_path=tmp_path / "tasks.jsonl",
        cluster_config=ClusterConfig(
            enabled=True,
            topology=ClusterTopology.STAR,
            node_timeout_s=60,
        ),
    )


@pytest.fixture()
async def central(central_app) -> AsyncClient:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=central_app)
    async with AsyncClient(transport=transport, base_url="http://central") as c:
        yield c


def _node_payload(name: str, url: str, slots: int = 4) -> dict:
    return {
        "name": name,
        "url": url,
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


def _task_payload(title: str, role: str = "backend", priority: int = 1) -> dict:
    return {
        "title": title,
        "description": f"Description for {title}",
        "role": role,
        "priority": priority,
        "scope": "small",
        "complexity": "low",
        "estimated_minutes": 10,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_two_nodes_register_and_appear_in_status(central: AsyncClient) -> None:
    """Both nodes register and appear in /cluster/status."""
    r1 = await central.post("/cluster/nodes", json=_node_payload("node-alpha", "http://alpha:8052"))
    r2 = await central.post("/cluster/nodes", json=_node_payload("node-beta", "http://beta:8052"))

    assert r1.status_code == 201
    assert r2.status_code == 201

    resp = await central.get("/cluster/status")
    assert resp.status_code == 200
    summary = resp.json()

    assert summary["total_nodes"] == 2
    assert summary["online_nodes"] == 2
    assert summary["topology"] == "star"
    names = {n["name"] for n in summary["nodes"]}
    assert names == {"node-alpha", "node-beta"}


@pytest.mark.anyio
async def test_task_claimed_by_exactly_one_node(central: AsyncClient) -> None:
    """When two nodes race to claim the same task, only one wins.

    Uses the version-based CAS: both POST to /tasks/{id}/claim with the same
    expected_version=1; only the first wins (HTTP 200), the second gets 409.
    """
    # Register two nodes
    await central.post("/cluster/nodes", json=_node_payload("alpha", "http://alpha:8052"))
    await central.post("/cluster/nodes", json=_node_payload("beta", "http://beta:8052"))

    # Post a task — freshly created tasks start at version=1
    resp = await central.post("/tasks", json=_task_payload("shared-task"))
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    # Both nodes attempt to claim the same task concurrently with the same expected_version.
    # asyncio.gather schedules both in the same event loop; the store's asyncio.Lock
    # serialises them so exactly one wins and one gets a 409.
    results = await asyncio.gather(
        central.post(f"/tasks/{task_id}/claim?expected_version=1"),
        central.post(f"/tasks/{task_id}/claim?expected_version=1"),
        return_exceptions=False,
    )
    status_codes = [r.status_code for r in results]

    # Exactly one should succeed (200), the other should conflict (409)
    assert 200 in status_codes, f"Expected one success; got {status_codes}"
    assert 409 in status_codes, f"Expected one conflict; got {status_codes}"


@pytest.mark.anyio
async def test_tasks_distributed_across_nodes(central: AsyncClient) -> None:
    """Multiple tasks are claimable by different nodes without collision."""
    # Register two nodes
    await central.post("/cluster/nodes", json=_node_payload("alpha", "http://alpha:8052"))
    await central.post("/cluster/nodes", json=_node_payload("beta", "http://beta:8052"))

    # Post 4 tasks
    task_ids = []
    for i in range(4):
        r = await central.post("/tasks", json=_task_payload(f"task-{i}"))
        assert r.status_code == 201
        task_ids.append(r.json()["id"])

    # Node alpha claims tasks 0 and 1 via GET /tasks/next/backend
    for _ in range(2):
        r = await central.get("/tasks/next/backend")
        assert r.status_code == 200

    # Node beta claims tasks 2 and 3 via GET /tasks/next/backend
    for _ in range(2):
        r = await central.get("/tasks/next/backend")
        assert r.status_code == 200

    # All 4 tasks should now be claimed
    resp = await central.get("/tasks?status=claimed")
    assert resp.status_code == 200
    claimed = resp.json()
    assert len(claimed) == 4


@pytest.mark.anyio
async def test_node_heartbeat_keeps_node_online(central: AsyncClient) -> None:
    """A node sending heartbeats stays online; capacity updates propagate."""
    resp = await central.post("/cluster/nodes", json=_node_payload("heartbeat-node", "http://hb:8052"))
    assert resp.status_code == 201
    node_id = resp.json()["id"]

    # Send a heartbeat with updated capacity
    hb = await central.post(
        f"/cluster/nodes/{node_id}/heartbeat",
        json={
            "capacity": {
                "max_agents": 4,
                "available_slots": 3,
                "active_agents": 1,
                "gpu_available": False,
                "supported_models": ["sonnet"],
            }
        },
    )
    assert hb.status_code == 200
    data = hb.json()
    assert data["capacity"]["available_slots"] == 3
    assert data["status"] == "online"


@pytest.mark.anyio
async def test_node_unregister_removes_from_cluster(central: AsyncClient) -> None:
    """Unregistering a node removes it from the cluster view."""
    r1 = await central.post("/cluster/nodes", json=_node_payload("temp-node", "http://tmp:8052"))
    node_id = r1.json()["id"]

    del_resp = await central.delete(f"/cluster/nodes/{node_id}")
    assert del_resp.status_code == 204

    status = await central.get("/cluster/status")
    assert status.json()["total_nodes"] == 0


@pytest.mark.anyio
async def test_complete_task_flow_two_nodes(central: AsyncClient) -> None:
    """Full flow: two nodes claim tasks, complete them, cluster stays consistent.

    This is the canonical two-instance coordination scenario:
    - Node alpha claims task A and completes it
    - Node beta claims task B and completes it
    - Final status shows 2 done, 0 open
    """
    await central.post("/cluster/nodes", json=_node_payload("alpha", "http://alpha:8052"))
    await central.post("/cluster/nodes", json=_node_payload("beta", "http://beta:8052"))

    # Create two tasks
    task_a_id = (await central.post("/tasks", json=_task_payload("task-A"))).json()["id"]
    task_b_id = (await central.post("/tasks", json=_task_payload("task-B"))).json()["id"]

    # Each node claims one task (sequential to avoid races in test assertions)
    ca = await central.post(f"/tasks/{task_a_id}/claim")
    cb = await central.post(f"/tasks/{task_b_id}/claim")
    assert ca.status_code == 200
    assert cb.status_code == 200

    # Both complete their tasks
    r_a = await central.post(f"/tasks/{task_a_id}/complete", json={"result_summary": "Task A done by alpha"})
    r_b = await central.post(f"/tasks/{task_b_id}/complete", json={"result_summary": "Task B done by beta"})

    assert r_a.status_code == 200
    assert r_b.status_code == 200

    # Verify final state via /status
    status_resp = await central.get("/status")
    assert status_resp.status_code == 200
    s = status_resp.json()
    assert s["done"] == 2
    assert s["open"] == 0

    # Cluster still shows both nodes
    cluster_resp = await central.get("/cluster/status")
    assert cluster_resp.json()["online_nodes"] == 2
