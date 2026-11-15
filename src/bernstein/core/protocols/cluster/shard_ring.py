"""Consistent hash ring for sharding cluster nodes across multiple registries.

Supports deterministic node-to-shard mapping using consistent hashing with
virtual nodes. The implementation is language-portable: an edge relay in
JavaScript can reimplement the same hash function and achieve byte-identical
mappings.
"""

from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass


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
