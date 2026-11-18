"""Consistent hash ring for sharding cluster nodes across multiple registries.

Supports deterministic node-to-shard mapping using consistent hashing with
virtual nodes. The implementation is language-portable: an edge relay in
JavaScript can reimplement the same hash function and achieve byte-identical
mappings.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.tasks.models import NodeInfo


@dataclass(frozen=True)
class HashRing:
    """Consistent hash ring with virtual nodes for shard selection.

    Virtual nodes improve distribution uniformity and reduce key movement
    when shards are added/removed. Hash = first 8 bytes of sha256(key) as
    big-endian unsigned int.
    """

    shard_ids: tuple[str, ...]
    vnodes: int
    _ring: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        ring: list[tuple[int, str]] = []
        for shard_id in self.shard_ids:
            for i in range(self.vnodes):
                vnode_key = f"{shard_id}#{i}"
                hash_val = self._hash_key(vnode_key)
                ring.append((hash_val, shard_id))
        ring.sort()
        object.__setattr__(self, "_ring", tuple(ring))

    @staticmethod
    def _hash_key(key: str) -> int:
        h = hashlib.sha256(key.encode("utf-8")).digest()
        return int.from_bytes(h[:8], "big")

    def lookup(self, key: str) -> str:
        if not self._ring:
            raise ValueError("no shards in ring")
        hash_val = self._hash_key(key)
        idx = bisect.bisect_left(self._ring, (hash_val,))
        if idx >= len(self._ring):
            idx = 0
        return self._ring[idx][1]


@dataclass(frozen=True)
class ShardMap:
    """Mapping from shard ID to central base URL."""

    shard_map: dict[str, str]

    @classmethod
    def from_config(cls, shards: tuple[tuple[str, str], ...]) -> ShardMap:
        """Build ShardMap from ClusterConfig.shards tuple of (id, url) pairs."""
        return cls(shard_map=dict(shards))

    def get_url(self, shard_id: str) -> str | None:
        return self.shard_map.get(shard_id)


class NodeShardStorage(MutableMapping[str, "NodeInfo"]):
    """Authoritative local node partitions, selected by stable node ID.

    All point operations use the ring; iteration spans the partitions for
    identity lookup, persistence, scheduling and cluster-wide summaries.
    URLs describe owners but do not initiate network traffic.
    """

    def __init__(self, shards: tuple[tuple[str, str], ...], vnodes: int) -> None:
        self.shard_map = ShardMap.from_config(shards)
        self.ring = HashRing(tuple(self.shard_map.shard_map), vnodes)
        self._partitions: dict[str, dict[str, NodeInfo]] = {shard: {} for shard in self.shard_map.shard_map}

    def __getitem__(self, key: str) -> NodeInfo:
        return self._partitions[self.ring.lookup(key)][key]

    def __setitem__(self, key: str, value: NodeInfo) -> None:
        self._partitions[self.ring.lookup(key)][key] = value

    def __delitem__(self, key: str) -> None:
        del self._partitions[self.ring.lookup(key)][key]

    def __iter__(self) -> Iterator[str]:
        for partition in self._partitions.values():
            yield from partition

    def __len__(self) -> int:
        return sum(len(partition) for partition in self._partitions.values())
