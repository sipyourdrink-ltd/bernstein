"""Tests for deterministic node-key selection of local registry partitions."""

from __future__ import annotations

import statistics

import pytest

from bernstein.core.protocols.cluster.shard_ring import HashRing, ShardMap


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
        """Verify 2000 keys map to 50 local partitions within ±50% of mean."""
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

        # Local partition key counts should be within ±50% of mean
        for count in values:
            deviation_pct = abs(count - mean) / mean * 100
            assert deviation_pct <= 50, f"Deviation {deviation_pct}% exceeds 50%"

    def test_rebalancing_one_shard_added(self) -> None:
        """Compare two fixed rings: one extra partition remaps at most ~1/(N+1)+5% of keys."""
        num_nodes = 2000
        num_shards_initial = 50
        shard_ids_initial = tuple(f"shard-{i}" for i in range(num_shards_initial))
        ring_initial = HashRing(shard_ids=shard_ids_initial, vnodes=128)

        # Record initial assignments
        initial_assignments = {}
        for i in range(num_nodes):
            key = f"node-{i}"
            initial_assignments[key] = ring_initial.lookup(key)

        # Build a separate ring with one extra local partition
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
