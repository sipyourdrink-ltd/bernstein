"""Tests for cluster node heartbeat health telemetry endpoints."""

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event

import pytest
from bernstein.core.models import ClusterConfig, NodeInfo
from fastapi.testclient import TestClient

from bernstein.core.protocols.cluster.cluster import NodeRegistry
from bernstein.core.server.server import create_app


@pytest.fixture()
def cluster_app():
    """Create a cluster-mode app for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Keep the store under .sdd/runtime so the app's sdd_dir resolves
        # inside the tempdir; a bare tmpdir makes it the tempdir's parent
        # (e.g. /tmp), where the audit chain then collides with unrelated
        # state.
        jsonl_path = Path(tmp_dir) / ".sdd" / "runtime" / "tasks.jsonl"
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


@pytest.mark.parametrize("same_worker", [False, True], ids=["two-workers", "one-worker-partial-reports"])
def test_concurrent_heartbeats_survive_restart(tmp_path, monkeypatch, same_worker):
    cluster_app = create_app(
        jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl", cluster_config=ClusterConfig(enabled=True)
    )
    registry = cluster_app.state.node_registry
    with TestClient(cluster_app) as client:

        def register(name):
            response = client.post("/cluster/nodes", json={"name": name, "url": f"http://{name}:9000"})
            assert response.status_code == 201
            return response.json()["id"]

        first = register("first")
        second = first if same_worker else register("second")
        paused, release, second_entered = Event(), Event(), Event()
        heartbeat = registry.heartbeat

        def observe_entry(node_id, capacity):
            if capacity.mem_used_pct is not None:
                second_entered.set()
            return heartbeat(node_id, capacity)

        monkeypatch.setattr(registry, "heartbeat", observe_entry)

        def pause_first():
            paused.set()
            assert release.wait(10)

        if same_worker:
            # Pause the read/modify/write after missing fields were filled from the previous reading.
            assign = NodeInfo.__setattr__

            def assign_capacity(node, name, value):
                if name == "capacity" and value.disk_free_mb == 4096 and not paused.is_set():
                    pause_first()
                assign(node, name, value)

            monkeypatch.setattr(NodeInfo, "__setattr__", assign_capacity)
        else:
            # Pause after snapshot creation and before publication to disk.
            encode = json.dumps

            def encode_snapshot(value, **kwargs):
                if isinstance(value, list) and value and value[0].get("disk_free_mb") == 4096 and not paused.is_set():
                    pause_first()
                return encode(value, **kwargs)

            monkeypatch.setattr(json, "dumps", encode_snapshot)

        def report(node, reading):
            response = client.post(f"/cluster/nodes/{node}/heartbeat", json={"capacity": reading})
            assert response.status_code == 200, response.text

        with ThreadPoolExecutor(max_workers=2) as pool:
            older = pool.submit(report, first, {"disk_free_mb": 4096})
            try:
                assert paused.wait(10)
                newer = pool.submit(report, second, {"mem_used_pct": 91})
                assert second_entered.wait(10)
                # An unprotected second writer can finish while the older writer is paused.
                wait([newer], timeout=0.5)
            finally:
                release.set()
            older.result(timeout=10)
            newer.result(timeout=10)

    restored = NodeRegistry(registry.config, persist_path=tmp_path / ".sdd" / "runtime" / "nodes.json")
    assert restored.get(first).capacity.disk_free_mb == 4096
    assert restored.get(second).capacity.mem_used_pct == 91


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


# ---------------------------------------------------------------------------
# #7853: the by-ID claim path gates on registered node health too.
# ---------------------------------------------------------------------------


def _register_node(client, name="orch-node"):
    """Register a node through the cluster-registry URL and return its ID."""
    response = client.post(
        "http://cluster.test/cluster/nodes",
        json={"name": name, "url": f"http://{name}:9000"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_task(client, title):
    """Create a task through the task-server URL and return its ID."""
    response = client.post(
        "http://tasks.test/tasks",
        json={"title": title, "description": "fixture", "role": "backend"},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["id"]


def test_orchestrator_claim_by_id_waits_for_node_recovery(cluster_app):
    """An unhealthy registered node is refused a by-ID claim until it recovers.

    The orchestrator reports capacity to the cluster registry under one
    URL but claims through the task server under another, naming its
    parent session as the claim owner. The registry assigned the node a
    different ID, so the claim carries it explicitly as ``node_id``;
    before #7853 this path never consulted the registry and an
    unhealthy node claimed specific tasks unimpeded.
    """
    client = TestClient(cluster_app)
    parent_session = "parent-session-7853"

    # Registration goes to the cluster-registry URL; claims go to the
    # separate task-server URL. The two base URLs never coincide, so the
    # resolution under test cannot key on the request URL.
    node_id = _register_node(client)
    assert node_id != parent_session

    def claim(task_id):
        return client.post(
            f"http://tasks.test/tasks/{task_id}/claim",
            params={"claimed_by_session": parent_session, "node_id": node_id},
        )

    first = _create_task(client, "first task")
    granted = claim(first)
    assert granted.status_code == 200, granted.text
    # The parent session -- not the registry node ID -- is the recorded owner.
    assert granted.json()["claimed_by_session"] == parent_session

    second = _create_task(client, "second task")
    heartbeat_path = f"http://cluster.test/cluster/nodes/{node_id}/heartbeat"
    assert client.post(heartbeat_path, json={"capacity": {"disk_free_mb": 1}}).status_code == 200

    refused = claim(second)
    assert refused.status_code == 409, refused.text
    assert node_id in refused.json()["detail"]
    # The refused task stays open and unowned, ready for a healthy claimant.
    task_after_refusal = client.get(f"http://tasks.test/tasks/{second}").json()
    assert task_after_refusal["status"] == "open"
    assert task_after_refusal["claimed_by_session"] is None

    # Recovery reported through the registry URL re-opens claiming.
    assert client.post(heartbeat_path, json={"capacity": {"disk_free_mb": 4096}}).status_code == 200
    recovered = claim(second)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["claimed_by_session"] == parent_session


def test_claim_by_id_gates_on_cordoned_node(cluster_app):
    """Operator intent (cordon) also closes the by-ID path for a named node."""
    client = TestClient(cluster_app)
    node_id = _register_node(client, name="cordoned-node")
    task_id = _create_task(client, "cordoned task")
    assert client.post(f"/cluster/nodes/{node_id}/cordon").status_code == 200

    refused = client.post(
        f"/tasks/{task_id}/claim",
        params={"claimed_by_session": "parent-session", "node_id": node_id},
    )
    assert refused.status_code == 409, refused.text

    assert client.post(f"/cluster/nodes/{node_id}/uncordon").status_code == 200
    granted = client.post(
        f"/tasks/{task_id}/claim",
        params={"claimed_by_session": "parent-session", "node_id": node_id},
    )
    assert granted.status_code == 200, granted.text


def test_claim_by_id_without_registered_node_still_claims(cluster_app):
    """Older clients and single-host runs are not gated by the registry.

    A claimant the task server's registry does not know -- no
    registration at all (single-host), a bare parent session (older
    client), or a node ID from a separate registry deployment -- claims
    exactly as before #7853.
    """
    client = TestClient(cluster_app)

    single_host = _create_task(client, "single-host task")
    granted = client.post(f"/tasks/{single_host}/claim")
    assert granted.status_code == 200, granted.text

    older_client = _create_task(client, "older-client task")
    granted = client.post(
        f"/tasks/{older_client}/claim",
        params={"claimed_by_session": "session-never-registered"},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["claimed_by_session"] == "session-never-registered"

    separate_registry = _create_task(client, "separate-registry task")
    granted = client.post(
        f"/tasks/{separate_registry}/claim",
        params={"claimed_by_session": "session-elsewhere", "node_id": "node-from-another-registry"},
    )
    assert granted.status_code == 200, granted.text


def test_next_task_with_explicit_node_id_waits_for_recovery(cluster_app):
    """The pull path resolves the registered node from ``node_id`` as well."""
    client = TestClient(cluster_app)
    node_id = _register_node(client, name="pull-node")
    _create_task(client, "pull task")
    heartbeat_path = f"/cluster/nodes/{node_id}/heartbeat"
    params = {"claimed_by_session": "parent-session", "node_id": node_id}

    assert client.post(heartbeat_path, json={"capacity": {"disk_free_mb": 1}}).status_code == 200
    assert client.get("/tasks/next/backend", params=params).status_code == 404

    assert client.post(heartbeat_path, json={"capacity": {"disk_free_mb": 4096}}).status_code == 200
    claimed = client.get("/tasks/next/backend", params=params)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["claimed_by_session"] == "parent-session"
