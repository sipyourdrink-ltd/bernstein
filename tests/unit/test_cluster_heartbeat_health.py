"""Tests for cluster node heartbeat health telemetry endpoints."""

import tempfile
from pathlib import Path

import pytest
from bernstein.core.models import ClusterConfig
from fastapi.testclient import TestClient

from bernstein.core.server.server import create_app


@pytest.fixture()
def cluster_app():
    """Create a cluster-mode app for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        jsonl_path = Path(tmp_dir) / "tasks.jsonl"
        app = create_app(
            jsonl_path=jsonl_path,
            cluster_config=ClusterConfig(enabled=True),
        )
        app.state.node_registry._nodes.clear()
        yield app


def test_single_heartbeat_with_health_fields(cluster_app):
    """Single heartbeat stores health fields and returns 200 with status."""
    client = TestClient(cluster_app)

    # Register a node first
    reg_resp = client.post(
        "/cluster/nodes",
        json={
            "name": "test-node",
            "url": "http://localhost:9000",
            "capacity": {"max_agents": 4, "available_slots": 4},
        },
    )
    assert reg_resp.status_code == 201
    node_id = reg_resp.json()["id"]

    # Send heartbeat with health fields
    heartbeat_resp = client.post(
        f"/cluster/nodes/{node_id}/heartbeat",
        json={
            "capacity": {
                "max_agents": 4,
                "available_slots": 2,
                "active_agents": 2,
                "gpu_available": False,
                "disk_free_mb": 5000,
                "mem_used_pct": 65.5,
                "mesh_rtt_ms": 12.5,
                "platform": "linux-x86_64",
            }
        },
    )
    assert heartbeat_resp.status_code == 200
    body = heartbeat_resp.json()
    assert body["id"] == node_id
    assert body["status"] == "online"


def test_batch_heartbeat_accepted_and_unknown(cluster_app):
    """Batch heartbeat splits correctly between accepted and unknown nodes."""
    client = TestClient(cluster_app)

    # Register two nodes
    resp1 = client.post(
        "/cluster/nodes",
        json={"name": "node1", "url": "http://localhost:9001"},
    )
    node1_id = resp1.json()["id"]

    resp2 = client.post(
        "/cluster/nodes",
        json={"name": "node2", "url": "http://localhost:9002"},
    )
    node2_id = resp2.json()["id"]

    # Send batch with 2 known + 1 unknown
    batch_resp = client.post(
        "/cluster/nodes/heartbeats",
        json={
            "heartbeats": [
                {
                    "node_id": node1_id,
                    "capacity": {
                        "max_agents": 4,
                        "disk_free_mb": 4000,
                        "mem_used_pct": 50.0,
                    },
                },
                {
                    "node_id": node2_id,
                    "capacity": {"max_agents": 6, "disk_free_mb": 8000},
                },
                {"node_id": "unknown-node-xyz"},
            ]
        },
    )
    assert batch_resp.status_code == 200
    body = batch_resp.json()
    assert set(body["accepted"]) == {node1_id, node2_id}
    assert body["unknown"] == ["unknown-node-xyz"]


def test_batch_heartbeat_max_500_items(cluster_app):
    """Batch with >500 items returns 422."""
    client = TestClient(cluster_app)

    heartbeats = [{"node_id": f"node-{i}"} for i in range(501)]
    batch_resp = client.post(
        "/cluster/nodes/heartbeats",
        json={"heartbeats": heartbeats},
    )
    assert batch_resp.status_code == 422


def test_backward_compat_heartbeat_without_health_fields(cluster_app):
    """Old payload without health fields still works (200)."""
    client = TestClient(cluster_app)

    # Register a node
    reg_resp = client.post(
        "/cluster/nodes",
        json={"name": "old-node", "url": "http://localhost:9003"},
    )
    node_id = reg_resp.json()["id"]

    # Send heartbeat with old schema (no health fields)
    heartbeat_resp = client.post(
        f"/cluster/nodes/{node_id}/heartbeat",
        json={
            "capacity": {
                "max_agents": 6,
                "available_slots": 3,
                "active_agents": 3,
            }
        },
    )
    assert heartbeat_resp.status_code == 200
