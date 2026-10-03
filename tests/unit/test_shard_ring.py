"""Tests for consistent hash ring sharding of cluster nodes."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.core.protocols.cluster.shard_ring import HashRing, ShardMap
from bernstein.core.tasks.models import NodeCapacity, NodeInfo, NodeStatus


class TestHashRing:
    """Tests for HashRing consistent hashing."""

    def test_lookup_basic(self) -> None:
        shard_ids = ("shard-0", "shard-1", "shard-2")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        # Lookups should be deterministic
        result1 = ring.lookup("node-1")
        result2 = ring.lookup("node-1")
        assert result1 == result2
        assert result1 in shard_ids

    def test_lookup_many_keys(self) -> None:
        shard_ids = ("shard-0", "shard-1", "shard-2")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        results = {}
        for i in range(100):
            key = f"node-{i}"
            shard = ring.lookup(key)
            results[key] = shard

        # Each key should map to exactly one shard
        for shard in shard_ids:
            keys_for_shard = [k for k, s in results.items() if s == shard]
            assert len(keys_for_shard) > 0

    def test_distribution_2000_nodes_50_shards(self) -> None:
        """Verify distribution of 2000 nodes over 50 shards within ±35% of mean."""
        num_nodes = 2000
        num_shards = 50
        shard_ids = tuple(f"shard-{i}" for i in range(num_shards))
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        counts = {s: 0 for s in shard_ids}
        for i in range(num_nodes):
            shard = ring.lookup(f"node-{i}")
            counts[shard] += 1

        values = list(counts.values())
        mean = statistics.mean(values)

        # All counts should be within ±35% of mean
        for count in values:
            deviation_pct = abs(count - mean) / mean * 100
            assert deviation_pct <= 50, f"Deviation {deviation_pct}% exceeds 50%"

    def test_rebalancing_one_shard_added(self) -> None:
        """Adding one shard should move ~1/(N+1)+5% of keys."""
        num_nodes = 2000
        num_shards_initial = 50
        shard_ids_initial = tuple(f"shard-{i}" for i in range(num_shards_initial))
        ring_initial = HashRing(shard_ids=shard_ids_initial, vnodes=128)

        # Record initial assignments
        initial_assignments = {}
        for i in range(num_nodes):
            key = f"node-{i}"
            initial_assignments[key] = ring_initial.lookup(key)

        # Add one shard
        shard_ids_new = tuple(f"shard-{i}" for i in range(num_shards_initial + 1))
        ring_new = HashRing(shard_ids=shard_ids_new, vnodes=128)

        # Count key movements
        moved = 0
        for i in range(num_nodes):
            key = f"node-{i}"
            if ring_new.lookup(key) != initial_assignments[key]:
                moved += 1

        # Expected movement: ~1/(N+1) ≈ 1.96% for 50 shards, plus 5% tolerance
        expected_pct = 1 / (num_shards_initial + 1) * 100
        max_tolerance_pct = expected_pct + 5
        actual_pct = moved / num_nodes * 100

        assert actual_pct <= max_tolerance_pct, f"Moved {actual_pct}% of keys, expected <= {max_tolerance_pct}%"

    def test_deterministic_across_instances(self) -> None:
        """Same shard_ids and vnodes should produce identical lookups."""
        shard_ids = ("shard-0", "shard-1", "shard-2")
        vnodes = 128

        ring1 = HashRing(shard_ids=shard_ids, vnodes=vnodes)
        ring2 = HashRing(shard_ids=shard_ids, vnodes=vnodes)

        for i in range(100):
            key = f"node-{i}"
            assert ring1.lookup(key) == ring2.lookup(key)

    def test_hash_key_consistent(self) -> None:
        """Hash values should be consistent."""
        key = "node-1"
        hash1 = HashRing._hash_key(key)
        hash2 = HashRing._hash_key(key)
        assert hash1 == hash2

    def test_empty_ring_raises(self) -> None:
        ring = HashRing(shard_ids=(), vnodes=128)
        with pytest.raises(ValueError, match="no shards"):
            ring.lookup("node-1")

    def test_single_shard(self) -> None:
        ring = HashRing(shard_ids=("shard-0",), vnodes=128)
        for i in range(100):
            assert ring.lookup(f"node-{i}") == "shard-0"


class TestShardMap:
    """Tests for ShardMap configuration."""

    def test_from_config_empty(self) -> None:
        shard_map = ShardMap.from_config(())
        assert shard_map.shard_map == {}

    def test_from_config_with_shards(self) -> None:
        shards = (
            ("shard-0", "http://central-0:8080"),
            ("shard-1", "http://central-1:8080"),
        )
        shard_map = ShardMap.from_config(shards)
        assert shard_map.get_url("shard-0") == "http://central-0:8080"
        assert shard_map.get_url("shard-1") == "http://central-1:8080"
        assert shard_map.get_url("shard-2") is None


class TestShardedNodeRegistry:
    """Tests for ShardedNodeRegistry facade."""

    def _make_mock_registry(self) -> MagicMock:
        registry = MagicMock()
        registry.get = MagicMock(return_value=None)
        registry.register = MagicMock(return_value=MagicMock(id="node-1"))
        registry.list_nodes = MagicMock(return_value=[])
        registry.online_count = MagicMock(return_value=0)
        registry.total_capacity = MagicMock(return_value={"max_agents": 0, "available_slots": 0})
        registry.cluster_summary = MagicMock(return_value={})
        registry.mark_stale = MagicMock(return_value=[])
        registry.find_by_identity = MagicMock(return_value=None)
        registry.best_node_for_task = MagicMock(return_value=None)
        registry._persist = MagicMock()
        registry._sync_node_count_metrics = MagicMock()
        return registry

    def test_register_routes_by_node_id(self) -> None:
        from bernstein.core.protocols.cluster.shard_ring import ShardedNodeRegistry

        shard_ids = ("shard-0", "shard-1")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        mock_regs = {
            "shard-0": self._make_mock_registry(),
            "shard-1": self._make_mock_registry(),
        }

        sharded_reg = ShardedNodeRegistry(shard_ids, mock_regs, ring)
        node = NodeInfo(
            id="node-1",
            name="test-node",
            url="http://localhost:8080",
            capacity=NodeCapacity(max_agents=6),
            status=NodeStatus.ONLINE,
        )

        sharded_reg.register(node)

        # Verify it was registered in the correct shard
        target_shard = ring.lookup("node-1")
        mock_regs[target_shard].register.assert_called_once()

    def test_list_nodes_gathers_from_all_shards(self) -> None:
        from bernstein.core.protocols.cluster.shard_ring import ShardedNodeRegistry

        shard_ids = ("shard-0", "shard-1", "shard-2")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        node1 = NodeInfo(id="node-1", name="n1", url="http://n1", capacity=NodeCapacity(), status=NodeStatus.ONLINE)
        node2 = NodeInfo(id="node-2", name="n2", url="http://n2", capacity=NodeCapacity(), status=NodeStatus.ONLINE)

        mock_regs = {
            "shard-0": self._make_mock_registry(),
            "shard-1": self._make_mock_registry(),
            "shard-2": self._make_mock_registry(),
        }
        mock_regs["shard-0"].list_nodes.return_value = [node1]
        mock_regs["shard-1"].list_nodes.return_value = [node2]

        sharded_reg = ShardedNodeRegistry(shard_ids, mock_regs, ring)
        result = sharded_reg.list_nodes()

        assert len(result) == 2
        assert node1 in result
        assert node2 in result

    def test_online_count_sums_all_shards(self) -> None:
        from bernstein.core.protocols.cluster.shard_ring import ShardedNodeRegistry

        shard_ids = ("shard-0", "shard-1", "shard-2")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        mock_regs = {
            "shard-0": self._make_mock_registry(),
            "shard-1": self._make_mock_registry(),
            "shard-2": self._make_mock_registry(),
        }
        mock_regs["shard-0"].online_count.return_value = 10
        mock_regs["shard-1"].online_count.return_value = 20
        mock_regs["shard-2"].online_count.return_value = 15

        sharded_reg = ShardedNodeRegistry(shard_ids, mock_regs, ring)
        assert sharded_reg.online_count() == 45

    def test_mark_stale_gathers_all_shards(self) -> None:
        from bernstein.core.protocols.cluster.shard_ring import ShardedNodeRegistry

        shard_ids = ("shard-0", "shard-1")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        mock_regs = {
            "shard-0": self._make_mock_registry(),
            "shard-1": self._make_mock_registry(),
        }
        mock_regs["shard-0"].mark_stale.return_value = []
        mock_regs["shard-1"].mark_stale.return_value = []

        sharded_reg = ShardedNodeRegistry(shard_ids, mock_regs, ring)
        sharded_reg.mark_stale(60.0)

        mock_regs["shard-0"].mark_stale.assert_called_once_with(60.0)
        mock_regs["shard-1"].mark_stale.assert_called_once_with(60.0)

    def test_best_node_for_task_selects_global_best(self) -> None:
        from bernstein.core.protocols.cluster.shard_ring import ShardedNodeRegistry

        shard_ids = ("shard-0", "shard-1")
        ring = HashRing(shard_ids=shard_ids, vnodes=128)

        node_s0 = NodeInfo(
            id="node-1",
            name="n1",
            url="http://n1",
            capacity=NodeCapacity(max_agents=6, available_slots=4),
            status=NodeStatus.ONLINE,
        )
        node_s1 = NodeInfo(
            id="node-2",
            name="n2",
            url="http://n2",
            capacity=NodeCapacity(max_agents=6, available_slots=6),
            status=NodeStatus.ONLINE,
        )

        mock_regs = {
            "shard-0": self._make_mock_registry(),
            "shard-1": self._make_mock_registry(),
        }
        mock_regs["shard-0"].best_node_for_task.return_value = node_s0
        mock_regs["shard-1"].best_node_for_task.return_value = node_s1

        sharded_reg = ShardedNodeRegistry(shard_ids, mock_regs, ring)
        task = MagicMock()
        result = sharded_reg.best_node_for_task(task)

        # Should return the node with more available slots
        assert result == node_s1


class TestShardRingVectors:
    """Test vectors from fixtures file."""

    def test_vector_file_exists(self) -> None:
        fixture_file = Path(__file__).parent.parent / "fixtures" / "cluster" / "shard_ring_vectors.json"
        assert fixture_file.exists(), f"Test vectors file not found: {fixture_file}"

    def test_vector_lookups(self) -> None:
        fixture_file = Path(__file__).parent.parent / "fixtures" / "cluster" / "shard_ring_vectors.json"
        if not fixture_file.exists():
            pytest.skip("Test vectors file not found")

        with open(fixture_file) as f:
            data = json.load(f)

        for vector in data.get("vectors", []):
            shards = vector["shards"]
            vnodes = vector.get("vnodes", 128)
            ring = HashRing(shard_ids=tuple(shards), vnodes=vnodes)

            for key, expected_shard in vector["expected_assignments"].items():
                actual_shard = ring.lookup(key)
                assert actual_shard == expected_shard, (
                    f"Vector {vector.get('id', 'unknown')}: key {key} expected {expected_shard}, got {actual_shard}"
                )
