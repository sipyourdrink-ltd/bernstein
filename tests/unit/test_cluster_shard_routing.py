"""Tests for cluster shard routing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from bernstein.core.models import ClusterConfig, ClusterTopology
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.protocols.cluster.cluster import (
    NodeHeartbeatClient,
    NodeRegistry,
    _compute_node_id_sharded,
    _find_owner_shard,
    _get_shard_url,
)
from bernstein.core.routes.task_cluster import router
from bernstein.core.server import (
    NodeCapacitySchema,
    NodeHeartbeatBatchRequest,
    NodeHeartbeatItem,
    NodeRegisterRequest,
)


def test_compute_node_id_sharded():
    """Test deterministic node ID generation."""
    node_id_1 = _compute_node_id_sharded("worker-1", "http://localhost:9000")
    node_id_2 = _compute_node_id_sharded("worker-1", "http://localhost:9000")
    node_id_3 = _compute_node_id_sharded("worker-2", "http://localhost:9000")

    assert node_id_1 == node_id_2
    assert node_id_1 != node_id_3
    assert len(node_id_1) == 12


def test_find_owner_shard():
    """Test shard ownership calculation."""
    shards = (("shard-0", "http://s0:8000"), ("shard-1", "http://s1:8000"))

    node_id_1 = _compute_node_id_sharded("w1", "http://w1:9000")
    owner_1 = _find_owner_shard(node_id_1, shards)
    owner_1_repeat = _find_owner_shard(node_id_1, shards)

    assert owner_1 is not None
    assert owner_1 == owner_1_repeat
    assert owner_1 in ("shard-0", "shard-1")

    assert _find_owner_shard(node_id_1, ()) is None


def test_get_shard_url():
    """Test shard URL lookup."""
    shards = (("shard-0", "http://s0:8000"), ("shard-1", "http://s1:8000"))

    assert _get_shard_url("shard-0", shards) == "http://s0:8000"
    assert _get_shard_url("shard-1", shards) == "http://s1:8000"
    assert _get_shard_url("shard-2", shards) is None


@pytest.fixture
def sharded_app():
    """Create a FastAPI app with shard routing configured."""
    app = FastAPI()
    app.include_router(router)

    config = ClusterConfig(
        enabled=True,
        topology=ClusterTopology.STAR,
        shard_id="shard-0",
        shards=(
            ("shard-0", "http://localhost:8000"),
            ("shard-1", "http://localhost:8001"),
        ),
    )
    registry = NodeRegistry(config)
    app.state.node_registry = registry
    app.state.store = MagicMock()

    return app


def test_register_misdirected(sharded_app):
    """Test registration of node owned by another shard returns 421."""
    client = TestClient(sharded_app)

    for i in range(100):
        test_name = f"worker-{i}"
        test_url = "http://test:9000"
        test_id = _compute_node_id_sharded(test_name, test_url)
        test_owner = _find_owner_shard(test_id, sharded_app.state.node_registry.config.shards)
        if test_owner == "shard-1":
            node_name = test_name
            node_url = test_url
            break

    payload = NodeRegisterRequest(
        name=node_name,
        url=node_url,
        capacity=NodeCapacitySchema(),
        labels={},
        cell_ids=[],
    )

    response = client.post("/cluster/nodes", json=payload.model_dump())

    assert response.status_code == 421
    data = response.json()
    assert data["shard_id"] == "shard-1"
    assert data["url"] == "http://localhost:8001"


def test_shard_map_endpoint(sharded_app):
    """Test GET /cluster/shard-map returns sharded config."""
    client = TestClient(sharded_app)

    response = client.get("/cluster/shard-map")

    assert response.status_code == 200
    data = response.json()
    assert data["shard_id"] == "shard-0"
    assert len(data["shards"]) == 2
    assert data["shards"][0]["id"] == "shard-0"
    assert data["shards"][1]["id"] == "shard-1"


def test_shard_map_unsharded():
    """Test GET /cluster/shard-map on unsharded cluster returns 404."""
    app = FastAPI()
    app.include_router(router)

    config = ClusterConfig(
        enabled=True,
        topology=ClusterTopology.STAR,
        shard_id=None,
        shards=(),
    )
    registry = NodeRegistry(config)
    app.state.node_registry = registry
    app.state.store = MagicMock()

    client = TestClient(app)
    response = client.get("/cluster/shard-map")

    assert response.status_code == 404


def test_batch_heartbeat_misdirected(sharded_app):
    """Test batch heartbeat splits owned and misdirected nodes."""
    client = TestClient(sharded_app)

    shard0_id = None
    shard1_id = None
    for i in range(100):
        test_id = _compute_node_id_sharded(f"w-{i}", "http://w:9000")
        owner = _find_owner_shard(test_id, sharded_app.state.node_registry.config.shards)
        if owner == "shard-0" and shard0_id is None:
            shard0_id = test_id
        if owner == "shard-1" and shard1_id is None:
            shard1_id = test_id
        if shard0_id and shard1_id:
            break

    batch = NodeHeartbeatBatchRequest(
        heartbeats=[
            NodeHeartbeatItem(node_id=shard0_id, capacity=None),
            NodeHeartbeatItem(node_id=shard1_id, capacity=None),
        ]
    )

    response = client.post("/cluster/nodes/heartbeats", json=batch.model_dump())

    assert response.status_code == 200
    data = response.json()
    assert shard1_id in data["misdirected"]
    assert data["misdirected"][shard1_id] == "http://localhost:8001"


def test_heartbeat_misdirected(sharded_app):
    """Test heartbeat for misdirected node returns 421."""
    client = TestClient(sharded_app)

    shard1_id = None
    for i in range(100):
        test_id = _compute_node_id_sharded(f"w-{i}", "http://w:9000")
        owner = _find_owner_shard(test_id, sharded_app.state.node_registry.config.shards)
        if owner == "shard-1":
            shard1_id = test_id
            break

    response = client.post(f"/cluster/nodes/{shard1_id}/heartbeat", json={})

    assert response.status_code == 421
    data = response.json()
    assert data["shard_id"] == "shard-1"
    assert data["url"] == "http://localhost:8001"


def test_heartbeat_client_retry_on_421():
    """Test NodeHeartbeatClient retries with new URL on 421."""
    call_count = 0

    def mock_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            resp = MagicMock()
            resp.status_code = 421
            resp.json.return_value = {
                "shard_id": "shard-1",
                "url": "http://localhost:8001",
            }
            return resp

        resp = MagicMock()
        resp.status_code = 201
        resp.json.return_value = {"id": "test-node-123"}
        return resp

    client_obj = NodeHeartbeatClient(
        server_url="http://localhost:8000",
        node_name="test-worker",
        node_url="http://worker:9000",
    )

    with patch.object(httpx.Client, "post", side_effect=mock_post):
        with httpx.Client() as http_client:
            result = client_obj._register(http_client)

    assert result is True
    assert client_obj._node_id == "test-node-123"
    assert client_obj._server_url == "http://localhost:8001"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_owner_matches_hash_ring_and_accepts_non_hex_ids() -> None:
    from bernstein.core.protocols.cluster.cluster import SHARD_VNODES, _find_owner_shard
    from bernstein.core.protocols.cluster.shard_ring import HashRing

    shards = (("a", "http://a"), ("b", "http://b"), ("c", "http://c"))
    ring = HashRing(("a", "b", "c"), SHARD_VNODES)
    for key in ("0123456789ab", "ffffffffffff", "not-hex", "node-7"):
        assert _find_owner_shard(key, shards) == ring.lookup(key)


def test_batch_heartbeat_persists_in_one_pass(tmp_path) -> None:
    from bernstein.core.models import ClusterConfig, NodeInfo

    from bernstein.core.protocols.cluster.cluster import NodeRegistry

    reg = NodeRegistry(ClusterConfig(), persist_path=tmp_path / "nodes.db")
    for i in range(3):
        reg.register(NodeInfo(id=f"n{i}", name=f"w{i}", url=f"http://w{i}"))
    results = reg.heartbeat_batch([("n0", None), ("n1", None), ("missing", None)])
    assert [r is not None for r in results] == [True, True, False]


def test_legacy_json_migrates_when_db_path_given(tmp_path) -> None:
    import json

    from bernstein.core.models import ClusterConfig

    from bernstein.core.protocols.cluster.cluster import NodeRegistry

    rows = [{"id": "x1", "name": "w", "url": "http://w"}]
    (tmp_path / "nodes.json").write_text(json.dumps(rows))
    reg = NodeRegistry(ClusterConfig(), persist_path=tmp_path / "nodes.db")
    assert reg.get("x1") is not None
