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
    node = cluster_app.state.node_registry.get(node_id)
    assert node.capacity.disk_free_mb == 5000
    assert node.capacity.mem_used_pct == 65.5
    assert node.capacity.mesh_rtt_ms == 12.5
    assert node.capacity.platform == "linux-x86_64"


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


def test_heartbeat_client_serializes_telemetry_inside_capacity():
    import json

    import httpx
    from bernstein.core.models import NodeCapacity

    from bernstein.core.protocols.cluster.cluster import NodeHeartbeatClient

    heartbeat = NodeHeartbeatClient(
        "http://central.test",
        capacity=NodeCapacity(disk_free_mb=1024, mem_used_pct=70.0, mesh_rtt_ms=5.0, platform="linux"),
    )
    heartbeat._node_id = "worker"

    def respond(request):
        payload = json.loads(request.content)
        assert "disk_free_mb" not in payload
        assert payload["capacity"]["disk_free_mb"] == 1024
        assert payload["capacity"]["mem_used_pct"] == 70.0
        assert payload["capacity"]["mesh_rtt_ms"] == 5.0
        assert payload["capacity"]["platform"] == "linux"
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert heartbeat._send_heartbeat(client)
