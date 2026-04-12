"""ENT-010: Disaster recovery with cross-region WAL replication.

Replicates Write-Ahead Log (WAL) entries to one or more remote regions
for disaster recovery.  Uses a pull-based replication model where follower
nodes request entries from the leader by sequence number.

Features:
- Ordered, at-least-once delivery of WAL entries
- Follower lag tracking and alerting
- Replication health monitoring
- Configurable replication factor and acknowledgement policy
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class AckPolicy(StrEnum):
    """Replication acknowledgement policy."""

    LEADER_ONLY = "leader_only"  # Ack after local write
    QUORUM = "quorum"  # Ack after majority replicas confirm
    ALL = "all"  # Ack after all replicas confirm


@dataclass(frozen=True)
class ReplicationConfig:
    """Configuration for cross-region WAL replication.

    Attributes:
        enabled: Whether replication is active.
        target_regions: Regions to replicate to.
        ack_policy: When to acknowledge writes.
        max_lag_entries: Alert threshold for follower lag.
        batch_size: Max entries per replication batch.
        retry_interval_s: Seconds between retry attempts.
        max_retries: Maximum retries per batch.
    """

    enabled: bool = True
    target_regions: tuple[str, ...] = ()
    ack_policy: AckPolicy = AckPolicy.LEADER_ONLY
    max_lag_entries: int = 100
    batch_size: int = 50
    retry_interval_s: float = 5.0
    max_retries: int = 3


# ---------------------------------------------------------------------------
# WAL entry for replication
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicableWALEntry:
    """A WAL entry prepared for replication.

    Attributes:
        seq: Sequence number in the WAL.
        entry_hash: Content hash for integrity verification.
        payload: Serialized entry data.
        source_region: Region that originated the entry.
        timestamp: When the entry was created.
    """

    seq: int = 0
    entry_hash: str = ""
    payload: dict[str, Any] = field(default_factory=dict[str, Any])
    source_region: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Follower state
# ---------------------------------------------------------------------------


class FollowerHealth(StrEnum):
    """Health status of a replication follower."""

    HEALTHY = "healthy"
    LAGGING = "lagging"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass
class FollowerState:
    """Tracks the replication state of a follower region.

    Attributes:
        region: Follower region identifier.
        last_acked_seq: Last sequence number acknowledged.
        last_ack_time: Timestamp of last acknowledgement.
        health: Current health status.
        consecutive_failures: Number of consecutive replication failures.
        total_replicated: Total entries successfully replicated.
    """

    region: str = ""
    last_acked_seq: int = -1
    last_ack_time: float = 0.0
    health: FollowerHealth = FollowerHealth.UNKNOWN
    consecutive_failures: int = 0
    total_replicated: int = 0


# ---------------------------------------------------------------------------
# Replication result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicationResult:
    """Result of a replication batch attempt.

    Attributes:
        region: Target region.
        entries_sent: Number of entries sent.
        entries_acked: Number of entries acknowledged.
        success: Whether the batch was fully replicated.
        error: Error message if failed.
        duration_s: Time taken in seconds.
    """

    region: str = ""
    entries_sent: int = 0
    entries_acked: int = 0
    success: bool = True
    error: str = ""
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
# Replication manager
# ---------------------------------------------------------------------------


class WALReplicationManager:
    """Manages cross-region WAL replication for disaster recovery.

    Tracks follower state and provides methods for preparing replication
    batches and processing acknowledgements.

    Args:
        config: Replication configuration.
        source_region: This node's region identifier.
    """

    def __init__(
        self,
        config: ReplicationConfig | None = None,
        source_region: str = "us-east",
    ) -> None:
        self._config = config or ReplicationConfig()
        self._source_region = source_region
        self._followers: dict[str, FollowerState] = {}
        self._wal_buffer: list[ReplicableWALEntry] = []
        self._leader_seq: int = -1

        # Initialize follower state for target regions
        for region in self._config.target_regions:
            self._followers[region] = FollowerState(region=region)

    @property
    def config(self) -> ReplicationConfig:
        """Return the replication configuration."""
        return self._config

    @property
    def source_region(self) -> str:
        """Return the source region identifier."""
        return self._source_region

    def append_entry(self, entry: ReplicableWALEntry) -> None:
        """Buffer a WAL entry for replication.

        Args:
            entry: Entry to replicate.
        """
        self._wal_buffer.append(entry)
        if entry.seq > self._leader_seq:
            self._leader_seq = entry.seq

    def get_pending_entries(
        self,
        region: str,
    ) -> list[ReplicableWALEntry]:
        """Get entries that need to be replicated to a follower region.

        Args:
            region: Target follower region.

        Returns:
            List of entries not yet acknowledged by the follower.
        """
        follower = self._followers.get(region)
        if follower is None:
            return []

        pending = [e for e in self._wal_buffer if e.seq > follower.last_acked_seq]
        return pending[: self._config.batch_size]

    def acknowledge(self, region: str, seq: int) -> None:
        """Record that a follower has acknowledged entries up to seq.

        Args:
            region: Follower region.
            seq: Highest acknowledged sequence number.
        """
        follower = self._followers.get(region)
        if follower is None:
            return

        entries_acked = seq - follower.last_acked_seq
        follower.last_acked_seq = seq
        follower.last_ack_time = time.time()
        follower.consecutive_failures = 0
        follower.total_replicated += max(0, entries_acked)
        follower.health = FollowerHealth.HEALTHY
        logger.debug(
            "Follower %s acked up to seq %d (%d new entries)",
            region,
            seq,
            entries_acked,
        )

    def record_failure(self, region: str, error: str = "") -> None:
        """Record a replication failure for a follower.

        Args:
            region: Follower region.
            error: Error description.
        """
        follower = self._followers.get(region)
        if follower is None:
            return

        follower.consecutive_failures += 1
        if follower.consecutive_failures >= self._config.max_retries:
            follower.health = FollowerHealth.UNREACHABLE
        logger.warning(
            "Replication failure for %s (attempt %d): %s",
            region,
            follower.consecutive_failures,
            error,
        )

    def get_follower_lag(self, region: str) -> int:
        """Get the replication lag in entries for a follower.

        Args:
            region: Follower region.

        Returns:
            Number of entries the follower is behind. -1 if unknown.
        """
        follower = self._followers.get(region)
        if follower is None:
            return -1
        return max(0, self._leader_seq - follower.last_acked_seq)

    def check_health(self) -> dict[str, FollowerHealth]:
        """Check replication health for all followers.

        Updates health status based on lag thresholds.

        Returns:
            Mapping of region -> health status.
        """
        result: dict[str, FollowerHealth] = {}
        for region, follower in self._followers.items():
            lag = self.get_follower_lag(region)
            if lag > self._config.max_lag_entries:
                follower.health = FollowerHealth.LAGGING
            result[region] = follower.health
        return result

    def get_follower_states(self) -> dict[str, FollowerState]:
        """Return a snapshot of all follower states.

        Returns:
            Copy of the follower state dictionary.
        """
        return dict(self._followers)

    def is_quorum_met(self, seq: int) -> bool:
        """Check if a quorum of followers have acknowledged a sequence.

        Args:
            seq: Sequence number to check.

        Returns:
            True if enough followers have acknowledged for the ack policy.
        """
        if self._config.ack_policy == AckPolicy.LEADER_ONLY:
            return True

        acked_count = sum(1 for f in self._followers.values() if f.last_acked_seq >= seq)

        total = len(self._followers)
        if total == 0:
            return True

        if self._config.ack_policy == AckPolicy.ALL:
            return acked_count >= total

        # Quorum: majority
        quorum_size = (total // 2) + 1
        return acked_count >= quorum_size

    def compact_buffer(self) -> int:
        """Remove entries from the buffer that all followers have acked.

        Returns:
            Number of entries removed.
        """
        if not self._followers:
            count = len(self._wal_buffer)
            self._wal_buffer.clear()
            return count

        min_acked = min(
            (f.last_acked_seq for f in self._followers.values()),
            default=-1,
        )
        before = len(self._wal_buffer)
        self._wal_buffer = [e for e in self._wal_buffer if e.seq > min_acked]
        removed = before - len(self._wal_buffer)
        if removed:
            logger.debug("Compacted %d replicated entries from buffer", removed)
        return removed
