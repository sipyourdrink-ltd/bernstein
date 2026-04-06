"""Tests for ENT-010: Disaster recovery with cross-region WAL replication."""

from __future__ import annotations

from bernstein.core.wal_replication import (
    AckPolicy,
    FollowerHealth,
    ReplicableWALEntry,
    ReplicationConfig,
    WALReplicationManager,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(seq: int) -> ReplicableWALEntry:
    return ReplicableWALEntry(
        seq=seq,
        entry_hash=f"hash-{seq}",
        payload={"action": f"test-{seq}"},
        source_region="us-east",
    )


# ---------------------------------------------------------------------------
# Basic setup
# ---------------------------------------------------------------------------


class TestReplicationSetup:
    def test_initializes_followers(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast"),
        )
        mgr = WALReplicationManager(config, source_region="us-east")
        states = mgr.get_follower_states()
        assert "eu-west" in states
        assert "ap-southeast" in states
        assert states["eu-west"].health == FollowerHealth.UNKNOWN

    def test_source_region(self) -> None:
        mgr = WALReplicationManager(source_region="eu-west")
        assert mgr.source_region == "eu-west"


# ---------------------------------------------------------------------------
# Entry buffering
# ---------------------------------------------------------------------------


class TestEntryBuffering:
    def test_append_and_retrieve(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            batch_size=10,
        )
        mgr = WALReplicationManager(config)
        for i in range(5):
            mgr.append_entry(_make_entry(i))

        pending = mgr.get_pending_entries("eu-west")
        assert len(pending) == 5

    def test_batch_size_respected(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            batch_size=3,
        )
        mgr = WALReplicationManager(config)
        for i in range(10):
            mgr.append_entry(_make_entry(i))

        pending = mgr.get_pending_entries("eu-west")
        assert len(pending) == 3

    def test_unknown_region_returns_empty(self) -> None:
        mgr = WALReplicationManager()
        assert mgr.get_pending_entries("nonexistent") == []


# ---------------------------------------------------------------------------
# Acknowledgements
# ---------------------------------------------------------------------------


class TestAcknowledgements:
    def test_acknowledge_updates_state(self) -> None:
        config = ReplicationConfig(target_regions=("eu-west",))
        mgr = WALReplicationManager(config)
        for i in range(5):
            mgr.append_entry(_make_entry(i))

        mgr.acknowledge("eu-west", 3)
        states = mgr.get_follower_states()
        assert states["eu-west"].last_acked_seq == 3
        assert states["eu-west"].health == FollowerHealth.HEALTHY

    def test_pending_excludes_acked(self) -> None:
        config = ReplicationConfig(target_regions=("eu-west",))
        mgr = WALReplicationManager(config)
        for i in range(5):
            mgr.append_entry(_make_entry(i))

        mgr.acknowledge("eu-west", 2)
        pending = mgr.get_pending_entries("eu-west")
        # Should only return entries with seq > 2
        assert all(e.seq > 2 for e in pending)
        assert len(pending) == 2  # seq 3 and 4


# ---------------------------------------------------------------------------
# Failure tracking
# ---------------------------------------------------------------------------


class TestFailureTracking:
    def test_record_failure_increments_count(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            max_retries=3,
        )
        mgr = WALReplicationManager(config)
        mgr.record_failure("eu-west", "connection timeout")
        states = mgr.get_follower_states()
        assert states["eu-west"].consecutive_failures == 1
        assert states["eu-west"].health != FollowerHealth.UNREACHABLE

    def test_marks_unreachable_after_max_retries(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            max_retries=2,
        )
        mgr = WALReplicationManager(config)
        mgr.record_failure("eu-west")
        mgr.record_failure("eu-west")
        states = mgr.get_follower_states()
        assert states["eu-west"].health == FollowerHealth.UNREACHABLE

    def test_ack_resets_failures(self) -> None:
        config = ReplicationConfig(target_regions=("eu-west",))
        mgr = WALReplicationManager(config)
        mgr.record_failure("eu-west")
        mgr.acknowledge("eu-west", 0)
        states = mgr.get_follower_states()
        assert states["eu-west"].consecutive_failures == 0


# ---------------------------------------------------------------------------
# Lag tracking
# ---------------------------------------------------------------------------


class TestLagTracking:
    def test_lag_calculation(self) -> None:
        config = ReplicationConfig(target_regions=("eu-west",))
        mgr = WALReplicationManager(config)
        for i in range(10):
            mgr.append_entry(_make_entry(i))
        mgr.acknowledge("eu-west", 5)
        assert mgr.get_follower_lag("eu-west") == 4  # 9 - 5

    def test_unknown_region_returns_negative(self) -> None:
        mgr = WALReplicationManager()
        assert mgr.get_follower_lag("nonexistent") == -1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def test_healthy_when_caught_up(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            max_lag_entries=10,
        )
        mgr = WALReplicationManager(config)
        for i in range(5):
            mgr.append_entry(_make_entry(i))
        mgr.acknowledge("eu-west", 4)

        health = mgr.check_health()
        assert health["eu-west"] == FollowerHealth.HEALTHY

    def test_lagging_when_behind(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west",),
            max_lag_entries=5,
        )
        mgr = WALReplicationManager(config)
        for i in range(20):
            mgr.append_entry(_make_entry(i))

        health = mgr.check_health()
        assert health["eu-west"] == FollowerHealth.LAGGING


# ---------------------------------------------------------------------------
# Quorum
# ---------------------------------------------------------------------------


class TestQuorum:
    def test_leader_only_always_met(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast"),
            ack_policy=AckPolicy.LEADER_ONLY,
        )
        mgr = WALReplicationManager(config)
        assert mgr.is_quorum_met(999)

    def test_quorum_majority(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast", "us-west"),
            ack_policy=AckPolicy.QUORUM,
        )
        mgr = WALReplicationManager(config)
        mgr.append_entry(_make_entry(0))
        mgr.acknowledge("eu-west", 0)
        mgr.acknowledge("ap-southeast", 0)
        assert mgr.is_quorum_met(0)

    def test_quorum_not_met(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast", "us-west"),
            ack_policy=AckPolicy.QUORUM,
        )
        mgr = WALReplicationManager(config)
        mgr.append_entry(_make_entry(0))
        mgr.acknowledge("eu-west", 0)
        assert not mgr.is_quorum_met(0)

    def test_all_policy(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast"),
            ack_policy=AckPolicy.ALL,
        )
        mgr = WALReplicationManager(config)
        mgr.append_entry(_make_entry(0))
        mgr.acknowledge("eu-west", 0)
        assert not mgr.is_quorum_met(0)
        mgr.acknowledge("ap-southeast", 0)
        assert mgr.is_quorum_met(0)


# ---------------------------------------------------------------------------
# Buffer compaction
# ---------------------------------------------------------------------------


class TestBufferCompaction:
    def test_compact_removes_acked_entries(self) -> None:
        config = ReplicationConfig(target_regions=("eu-west",))
        mgr = WALReplicationManager(config)
        for i in range(10):
            mgr.append_entry(_make_entry(i))

        mgr.acknowledge("eu-west", 5)
        removed = mgr.compact_buffer()
        assert removed == 6  # seq 0-5 inclusive

    def test_compact_keeps_unacked(self) -> None:
        config = ReplicationConfig(
            target_regions=("eu-west", "ap-southeast"),
        )
        mgr = WALReplicationManager(config)
        for i in range(10):
            mgr.append_entry(_make_entry(i))

        # One follower at 3, another at 7 -> compact up to 3
        mgr.acknowledge("eu-west", 3)
        mgr.acknowledge("ap-southeast", 7)
        removed = mgr.compact_buffer()
        assert removed == 4  # seq 0-3

    def test_compact_no_followers(self) -> None:
        mgr = WALReplicationManager(ReplicationConfig(target_regions=()))
        for i in range(5):
            mgr.append_entry(_make_entry(i))
        removed = mgr.compact_buffer()
        assert removed == 5
