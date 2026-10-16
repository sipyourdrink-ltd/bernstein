"""Tests for the SQLite WAL-backed node store."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from bernstein.core.protocols.cluster.sqlite_node_store import SQLiteNodeStore
from bernstein.core.tasks.models import NodeCapacity, NodeInfo, NodeStatus


@pytest.fixture
def store(tmp_path: Path) -> SQLiteNodeStore:
    return SQLiteNodeStore(tmp_path / "nodes.db")


@pytest.fixture
def sample_node() -> NodeInfo:
    return NodeInfo(
        id="node-001",
        name="worker-1",
        url="http://10.0.0.1:8080",
        capacity=NodeCapacity(max_agents=8, available_slots=5, active_agents=3),
        status=NodeStatus.ONLINE,
        labels={"region": "us-east", "gpu": "true"},
    )


class TestBasicCRUD:
    def test_upsert_and_get(self, store: SQLiteNodeStore, sample_node: NodeInfo) -> None:
        store.upsert(sample_node)
        got = store.get("node-001")
        assert got is not None
        assert got.id == "node-001"
        assert got.name == "worker-1"
        assert got.capacity.max_agents == 8
        assert got.labels == {"region": "us-east", "gpu": "true"}

    def test_get_unknown(self, store: SQLiteNodeStore) -> None:
        assert store.get("nonexistent") is None

    def test_delete(self, store: SQLiteNodeStore, sample_node: NodeInfo) -> None:
        store.upsert(sample_node)
        assert store.delete("node-001")
        assert store.get("node-001") is None
        assert not store.delete("node-001")

    def test_upsert_updates(self, store: SQLiteNodeStore, sample_node: NodeInfo) -> None:
        store.upsert(sample_node)
        sample_node.name = "worker-1-updated"
        sample_node.capacity.max_agents = 16
        store.upsert(sample_node)
        got = store.get("node-001")
        assert got is not None
        assert got.name == "worker-1-updated"
        assert got.capacity.max_agents == 16

    def test_count(self, store: SQLiteNodeStore) -> None:
        assert store.count() == 0
        for i in range(5):
            store.upsert(NodeInfo(id=f"n{i}", status=NodeStatus.ONLINE))
        assert store.count() == 5
        assert store.count(NodeStatus.ONLINE) == 5
        assert store.count(NodeStatus.OFFLINE) == 0


class TestHeartbeat:
    def test_update_heartbeat_known(self, store: SQLiteNodeStore, sample_node: NodeInfo) -> None:
        store.upsert(sample_node)
        ok = store.update_heartbeat("node-001", disk_free_mb=1024, mem_used_pct=45.0)
        assert ok
        health = store.get_health("node-001")
        assert health is not None
        assert health["disk_free_mb"] == 1024
        assert health["mem_used_pct"] == 45.0
        assert health["health"] == "ok"

    def test_update_heartbeat_unknown(self, store: SQLiteNodeStore) -> None:
        assert not store.update_heartbeat("ghost")

    def test_heartbeat_revives_offline(self, store: SQLiteNodeStore) -> None:
        node = NodeInfo(id="off1", status=NodeStatus.OFFLINE)
        store.upsert(node)
        store.update_heartbeat("off1")
        got = store.get("off1")
        assert got is not None
        assert got.status == NodeStatus.ONLINE

    def test_heartbeat_preserves_cordoned(self, store: SQLiteNodeStore) -> None:
        node = NodeInfo(id="cord1", status=NodeStatus.CORDONED)
        store.upsert(node)
        store.update_heartbeat("cord1")
        got = store.get("cord1")
        assert got is not None
        assert got.status == NodeStatus.CORDONED

    def test_unhealthy_since_set_once(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="h1"))
        store.update_heartbeat("h1", health="unhealthy:disk")
        h1 = store.get_health("h1")
        assert h1 is not None
        ts1 = h1["unhealthy_since"]
        assert ts1 is not None

        store.update_heartbeat("h1", health="unhealthy:disk")
        h2 = store.get_health("h1")
        assert h2["unhealthy_since"] == ts1  # not updated again

    def test_batch_heartbeat(self, store: SQLiteNodeStore) -> None:
        for i in range(10):
            store.upsert(NodeInfo(id=f"b{i}"))
        results = store.batch_heartbeat([{"node_id": f"b{i}", "disk_free_mb": 500 + i} for i in range(10)])
        assert all(results.values())
        assert len(results) == 10
        results_ghost = store.batch_heartbeat([{"node_id": "ghost"}])
        assert not results_ghost["ghost"]


class TestListAndFilter:
    def test_list_all(self, store: SQLiteNodeStore) -> None:
        for i in range(3):
            store.upsert(NodeInfo(id=f"l{i}", status=NodeStatus.ONLINE))
        store.upsert(NodeInfo(id="off", status=NodeStatus.OFFLINE))
        assert len(store.list_nodes()) == 4
        assert len(store.list_nodes(NodeStatus.ONLINE)) == 3

    def test_find_by_identity(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="fi1", name="w1", url="http://a"))
        assert store.find_by_identity("w1", "http://a") is not None
        assert store.find_by_identity("w1", "http://b") is None
        assert store.find_by_identity("", "") is None

    def test_status_counts(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="a", status=NodeStatus.ONLINE))
        store.upsert(NodeInfo(id="b", status=NodeStatus.ONLINE))
        store.upsert(NodeInfo(id="c", status=NodeStatus.OFFLINE))
        counts = store.status_counts()
        assert counts["online"] == 2
        assert counts["offline"] == 1

    def test_mark_stale(self, store: SQLiteNodeStore) -> None:
        old = NodeInfo(id="old", status=NodeStatus.ONLINE)
        old.last_heartbeat = time.time() - 120
        store.upsert(old)
        fresh = NodeInfo(id="fresh", status=NodeStatus.ONLINE)
        store.upsert(fresh)
        stale = store.mark_stale(timeout_s=60)
        assert stale == ["old"]
        assert store.get("old").status == NodeStatus.OFFLINE
        assert store.get("fresh").status == NodeStatus.ONLINE


class TestBestNodes:
    def test_basic_selection(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="a", status=NodeStatus.ONLINE, capacity=NodeCapacity(available_slots=2)))
        store.upsert(NodeInfo(id="b", status=NodeStatus.ONLINE, capacity=NodeCapacity(available_slots=5)))
        best = store.best_nodes_for_task()
        assert len(best) == 1
        assert best[0].id == "b"

    def test_model_filter(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="a", status=NodeStatus.ONLINE, capacity=NodeCapacity(supported_models=["sonnet"])))
        store.upsert(NodeInfo(id="b", status=NodeStatus.ONLINE, capacity=NodeCapacity(supported_models=["opus"])))
        best = store.best_nodes_for_task(required_model="opus")
        assert len(best) == 1
        assert best[0].id == "b"

    def test_skips_unhealthy(self, store: SQLiteNodeStore) -> None:
        store.upsert(NodeInfo(id="sick", status=NodeStatus.ONLINE, capacity=NodeCapacity(available_slots=10)))
        store.update_heartbeat("sick", health="unhealthy:disk")
        best = store.best_nodes_for_task()
        assert len(best) == 0


class TestMigration:
    def test_migrate_from_json(self, store: SQLiteNodeStore, tmp_path: Path) -> None:
        json_path = tmp_path / "nodes.json"
        json_path.write_text(
            json.dumps(
                [
                    {"id": "m1", "name": "w1", "max_agents": 4, "labels": {"zone": "a"}},
                    {"id": "m2", "name": "w2", "registered_at": 1000.0},
                ]
            )
        )
        count = store.migrate_from_json(json_path)
        assert count == 2
        assert store.get("m1") is not None
        assert store.get("m1").labels == {"zone": "a"}
        assert store.get("m2").registered_at == 1000.0
        assert json_path.with_suffix(".json.migrated").exists()
        assert not json_path.exists()


class TestScale:
    """Verify the store handles 2000+ nodes without blowing up."""

    def test_2000_nodes(self, store: SQLiteNodeStore) -> None:
        for i in range(2000):
            store.upsert(
                NodeInfo(
                    id=f"w{i:04d}",
                    name=f"worker-{i}",
                    url=f"http://10.0.{i // 256}.{i % 256}:8080",
                    status=NodeStatus.ONLINE,
                    capacity=NodeCapacity(max_agents=6, available_slots=3),
                )
            )
        assert store.count() == 2000
        assert store.count(NodeStatus.ONLINE) == 2000

        results = store.batch_heartbeat(
            [{"node_id": f"w{i:04d}", "disk_free_mb": 1000, "mem_used_pct": 50.0} for i in range(2000)]
        )
        assert sum(results.values()) == 2000

        best = store.best_nodes_for_task(limit=10)
        assert len(best) == 10
