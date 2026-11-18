"""Opt-in registry ownership exercised through the real application routes."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from bernstein.core.config.config_schema import ClusterSchema
from bernstein.core.config.seed_parser import SeedError, _parse_cluster
from bernstein.core.protocols.cluster.cluster import NodeRegistry
from bernstein.core.protocols.cluster.shard_ring import HashRing, NodeShardStorage, ShardMap
from bernstein.core.server import create_app
from bernstein.core.tasks.models import ClusterConfig, NodeCapacity, NodeInfo, NodeStatus

SHARDS = (("alpha", "http://central-a:8080"), ("beta", "https://central-b/cluster"), ("gamma", "http://central-c"))
TOKEN = "test-registry-shard-token"  # NOSONAR - test fixture
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def test_config_round_trip_and_defaults() -> None:
    raw = {"shards": dict(SHARDS), "shard_vnodes": 64}
    parsed = _parse_cluster(raw)
    assert parsed is not None
    schema = ClusterSchema.model_validate(raw)
    assert parsed.shards == tuple(schema.shards.items()) == SHARDS
    assert parsed.shard_vnodes == schema.shard_vnodes == 64
    default = _parse_cluster({})
    assert default is not None
    assert default.shards == ClusterConfig().shards == ()
    assert default.shard_vnodes == ClusterSchema().shard_vnodes == 128


@pytest.mark.parametrize("value", [True, False, 0, -1, 1025, 1.5, "128", None])
def test_vnodes_rejected_consistently(value: object) -> None:
    with pytest.raises(ValueError, match="shard_vnodes"):
        ClusterConfig(shard_vnodes=value)  # type: ignore[arg-type]
    with pytest.raises(SeedError, match="shard_vnodes"):
        _parse_cluster({"shard_vnodes": value})
    with pytest.raises(ValidationError, match="shard_vnodes"):
        ClusterSchema.model_validate({"shard_vnodes": value})


@pytest.mark.parametrize(
    ("shard_id", "url"),
    [
        ("", "https://central"),
        ("a b", "https://central"),
        ("é", "https://central"),
        ("x" * 65, "https://central"),
        ("a", "central"),
        ("a", "ftp://central"),
        ("a", "http:///missing"),
        ("a", "https://user:pass@central"),
        ("a", "https://central:0"),
        ("a", "https://central:70000"),
        ("a", "https://central:bad"),
        ("a", "https://central?"),
        ("a", "https://central#"),
        ("a", "https://central/path\n"),
        ("a", 123),
        (123, "https://central"),
    ],
)
def test_shard_entries_rejected_consistently(shard_id: object, url: object) -> None:
    with pytest.raises(ValueError, match="shards"):
        ClusterConfig(shards=((shard_id, url),))  # type: ignore[arg-type]
    raw = {"shards": {shard_id: url}}
    with pytest.raises(SeedError, match="shards"):
        _parse_cluster(raw)
    with pytest.raises(ValidationError, match="shards"):
        ClusterSchema.model_validate(raw)


@pytest.mark.parametrize("value", [[], None, "alpha", 12])
def test_seed_and_schema_reject_non_mapping_shards(value: object) -> None:
    with pytest.raises(SeedError, match="shards"):
        _parse_cluster({"shards": value})
    with pytest.raises(ValidationError, match="shards"):
        ClusterSchema.model_validate({"shards": value})


def test_config_duplicate_ids_and_partition_limit() -> None:
    with pytest.raises(ValueError, match="unique"):
        ClusterConfig(shards=(SHARDS[0], SHARDS[0]))
    raw = {f"s{i}": "https://central" for i in range(257)}
    with pytest.raises(ValueError, match="256"):
        ClusterConfig(shards=tuple(raw.items()))
    with pytest.raises(SeedError, match="256"):
        _parse_cluster({"shards": raw})
    with pytest.raises(ValidationError, match="shards"):
        ClusterSchema.model_validate({"shards": raw})


@pytest.mark.parametrize("shards", [(), SHARDS])
def test_routes_use_owner_for_registration_heartbeat_and_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shards: tuple[tuple[str, str], ...]
) -> None:
    config = ClusterConfig(enabled=True, auth_token=TOKEN, shards=shards)
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl", auth_token=TOKEN, cluster_config=config)
    registry = app.state.node_registry
    storage = registry._nodes
    if shards:
        assert isinstance(storage, NodeShardStorage)
    else:
        assert type(storage) is dict
    ring = HashRing(tuple(dict(SHARDS)), config.shard_vnodes)
    # Choose one deterministic server-minted node ID for each ring owner.
    ids: dict[str, str] = {}
    for number in range(1000):
        node_id = uuid.UUID(int=number << 80).hex[:12]
        ids.setdefault(ring.lookup(node_id), node_id)
        if len(ids) == len(SHARDS):
            break
    assert set(ids) == set(dict(SHARDS))
    with TestClient(app) as client:
        for index, (owner, node_id) in enumerate(ids.items()):
            # Patch only while the registration handler mints its NodeInfo ID.
            with monkeypatch.context() as patch:
                patch.setattr(uuid, "uuid4", lambda node_id=node_id: uuid.UUID(hex=node_id + "0" * 20))
                body = {"name": f"worker-{index}", "url": f"http://worker-{index}:8052"}
                response = client.post("/cluster/nodes", json=body, headers=HEADERS)
            assert response.status_code == 201, response.text
            assert response.json()["id"] == node_id
            node = registry.get(node_id)
            registered_at = node.registered_at
            if shards:
                assert storage._partitions[owner][node_id] is node
                assert storage.ring.lookup(node_id) == owner
                assert storage.shard_map.get_url(owner) == ShardMap.from_config(config.shards).get_url(owner)
                assert all(node_id not in p for s, p in storage._partitions.items() if s != owner)
            assert client.post(f"/cluster/nodes/{node_id}/cordon", headers=HEADERS).status_code == 200
            again = client.post("/cluster/nodes", json=body, headers=HEADERS)
            assert again.status_code == 201
            assert again.json()["id"] == node_id
            assert registry.get(node_id) is node
            assert node.registered_at == registered_at
            assert node.status is NodeStatus.CORDONED
            beat = client.post(
                f"/cluster/nodes/{node_id}/heartbeat",
                json={"capacity": {"max_agents": 9, "available_slots": 7, "active_agents": 2}},
                headers=HEADERS,
            )
            assert beat.status_code == 200, beat.text
            assert node.capacity.available_slots == 7
            assert node.status is NodeStatus.CORDONED
            assert client.post(f"/cluster/nodes/{node_id}/uncordon", headers=HEADERS).status_code == 200
            assert node.status is NodeStatus.ONLINE
            assert client.post(f"/cluster/nodes/{node_id}/drain", headers=HEADERS).status_code == 200
            assert node.status is NodeStatus.DRAINING
        assert len(client.get("/cluster/nodes", headers=HEADERS).json()) == 3
        assert client.get("/cluster/status", headers=HEADERS).json()["total_nodes"] == 3
        for node_id in ids.values():
            assert client.delete(f"/cluster/nodes/{node_id}", headers=HEADERS).status_code == 204
            assert registry.get(node_id) is None
        assert not registry.list_nodes()
        assert client.post("/cluster/nodes/unknown/heartbeat", json={}, headers=HEADERS).status_code == 410
        assert client.delete("/cluster/nodes/unknown", headers=HEADERS).status_code == 404


def test_partition_reload_aggregation_and_stale(tmp_path: Path) -> None:
    config = ClusterConfig(shards=SHARDS, node_timeout_s=1)
    path = tmp_path / "nodes.json"
    registry = NodeRegistry(config, persist_path=path)
    for number in range(30):
        registry.register(NodeInfo(id=f"node-{number}", name=f"worker-{number}", url=f"http://worker-{number}"))
    assert registry.online_count() == 30
    assert registry.total_capacity() == 180
    assert registry.cluster_summary()["total_nodes"] == 30
    assert isinstance(json.loads(path.read_text()), list)
    restored = NodeRegistry(config, persist_path=path)
    assert len(restored.list_nodes(NodeStatus.OFFLINE)) == 30
    assert isinstance(restored._nodes, NodeShardStorage)
    assert all(restored._nodes._partitions.values())
    for node in restored.list_nodes():
        assert restored.find_by_identity(node.name, node.url) is node
        assert restored.heartbeat(node.id, NodeCapacity(available_slots=4)) is node
    assert restored.total_capacity() == 120
    best = restored.best_node_for_task()
    assert best is not None
    for node in restored.list_nodes():
        node.last_heartbeat = 0
    assert len(restored.mark_stale()) == 30
    assert restored.online_count() == 0
    assert restored.best_node_for_task() is None


def test_default_server_loads_selected_seed_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    from bernstein.core.server import server_app

    seed_path = tmp_path / "selected.yaml"
    seed_path.write_text(yaml.safe_dump({"goal": "Registry test", "cluster": {"shards": dict(SHARDS)}}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(seed_path))
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(server_app, "_default_cluster_config", ClusterConfig(enabled=True, auth_token=TOKEN))
    app = server_app.get_app()
    storage = app.state.node_registry._nodes
    assert isinstance(storage, NodeShardStorage)
    assert storage.shard_map.shard_map == dict(SHARDS)
    assert app.state.node_registry.config.auth_token == TOKEN
    with TestClient(app) as client:
        response = client.post("/cluster/nodes", json={"name": "seed-worker", "url": "http://worker"}, headers=HEADERS)
        assert response.status_code == 201, response.text
        node_id = response.json()["id"]
        assert node_id in storage._partitions[storage.ring.lookup(node_id)]
        # Seed reload is metadata-only: ring membership is fixed until restart.
        seed_path.write_text(
            yaml.safe_dump({"goal": "Registry test", "cluster": {"shards": {"other": "https://other"}}})
        )
        app.state.reload_seed_config()
        assert app.state.node_registry._nodes is storage
        assert storage.shard_map.shard_map == dict(SHARDS)


def test_default_server_rejects_invalid_seed_shards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.core.server import server_app

    seed_path = tmp_path / "selected.yaml"
    seed_path.write_text("goal: Registry test\ncluster:\n  shard_vnodes: false\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_SEED_PATH", str(seed_path))
    with pytest.raises(SeedError, match="shard_vnodes"):
        server_app.get_app()
