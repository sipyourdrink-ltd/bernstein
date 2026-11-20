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


@pytest.mark.parametrize("read_path", [None, "/cluster/nodes", "/cluster/status"])
def test_single_heartbeat_with_health_fields(cluster_app, read_path):
    """Heartbeat telemetry is retained in each HTTP node response."""
    client = TestClient(cluster_app)
    telemetry = {
        "disk_free_mb": 5000,
        "mem_used_pct": 65.5,
        "mesh_rtt_ms": 12.5,
        "platform": "linux-x86_64",
    }

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
                **telemetry,
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

    if read_path is not None:
        read_resp = client.get(read_path)
        assert read_resp.status_code == 200
        nodes = read_resp.json()
        if read_path == "/cluster/status":
            nodes = nodes["nodes"]
        body = next(node for node in nodes if node["id"] == node_id)
    assert {field: body["capacity"][field] for field in telemetry} == telemetry


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
    capacity = heartbeat_resp.json()["capacity"]
    for field in ("disk_free_mb", "mem_used_pct", "mesh_rtt_ms", "platform"):
        assert capacity[field] is None


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


def test_worker_waits_for_recovery_before_claiming_through_task_server(cluster_app, tmp_path):
    from bernstein.cli.commands.worker_cmd import WorkerLoop

    worker = WorkerLoop(server_url="http://testserver", name="health-worker", adapter="claude", workdir=tmp_path)
    with TestClient(cluster_app) as client:
        node_id = worker._register(client)
        assert node_id is not None
        worker._heartbeat(client, node_id)
        node = cluster_app.state.node_registry.get(node_id)
        assert node.capacity.disk_free_mb is not None
        assert node.capacity.platform
        created = client.post("/tasks", json={"title": "check health", "description": "fixture", "role": "backend"})
        assert created.status_code in (200, 201), created.text
        path = f"/cluster/nodes/{node_id}/heartbeat"
        assert client.post(path, json={"capacity": {"disk_free_mb": 1}}).status_code == 200
        assert worker._claim_task(client, "backend", node_id) is None
        assert client.post(path, json={"capacity": {"active_agents": 0}}).status_code == 200
        assert worker._claim_task(client, "backend", node_id) is None
        # The worker has just freed a slot locally and polls before its next capacity heartbeat.
        assert client.post(path, json={"capacity": {"disk_free_mb": 4096, "available_slots": 0}}).status_code == 200
        assert worker._claim_task(client, "backend", node_id)["id"] == created.json()["id"]
