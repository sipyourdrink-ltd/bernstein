"""Deterministic selection of local partitions inside one NodeRegistry process.

The registry opts in through nonempty shard configuration; the default single
dictionary is unchanged. Consistent hashing with virtual nodes maps node IDs
to local partitions. Configured URLs are metadata only.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import ItemsView, Iterator, KeysView, MutableMapping, ValuesView
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, overload

if TYPE_CHECKING:
    from bernstein.core.tasks.models import NodeInfo


_POP_MISSING = object()


@dataclass(frozen=True)
class HashRing:
    """Consistent hash ring with virtual nodes for local partition selection.

    Virtual nodes define the token positions used to assign keys to partitions.
    Registry membership is fixed until restart. Hash = first 8 bytes of
    sha256(key) as a big-endian unsigned int.
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
    """Mapping from local partition ID to configured URL metadata."""

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
    URLs are partition metadata and do not initiate network traffic.
    """

    def __init__(self, shards: tuple[tuple[str, str], ...], vnodes: int) -> None:
        self.shard_map = ShardMap.from_config(shards)
        self.ring = HashRing(tuple(self.shard_map.shard_map), vnodes)
        self._partitions: dict[str, dict[str, NodeInfo]] = {shard: {} for shard in self.shard_map.shard_map}
        self._lock = RLock()

    def __getitem__(self, key: str) -> NodeInfo:
        with self._lock:
            return self._partitions[self.ring.lookup(key)][key]

    def __setitem__(self, key: str, value: NodeInfo) -> None:
        with self._lock:
            self._partitions[self.ring.lookup(key)][key] = value

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._partitions[self.ring.lookup(key)][key]

    @overload
    def pop(self, key: str) -> NodeInfo: ...

    @overload
    def pop[T](self, key: str, default: T) -> NodeInfo | T: ...

    def pop(self, key: str, default: object = _POP_MISSING) -> object:
        # MutableMapping.pop separates lookup from deletion. Keep duplicate
        # unregister calls atomic like the registry's original dict.pop.
        with self._lock:
            partition = self._partitions[self.ring.lookup(key)]
            if default is _POP_MISSING:
                return partition.pop(key)
            return partition.pop(key, default)

    def _snapshot(self) -> dict[str, NodeInfo]:
        # Freeze membership while holding the same lock as structural writers.
        # NodeInfo objects remain shared, as they were in the original registry.
        with self._lock:
            snapshot: dict[str, NodeInfo] = {}
            for partition in self._partitions.values():
                snapshot.update(partition)
            return snapshot

    def values(self) -> ValuesView[NodeInfo]:
        return self._snapshot().values()

    def items(self) -> ItemsView[str, NodeInfo]:
        return self._snapshot().items()

    def keys(self) -> KeysView[str]:
        return self._snapshot().keys()

    def __iter__(self) -> Iterator[str]:
        return iter(self._snapshot())

    def __len__(self) -> int:
        with self._lock:
            return sum(len(partition) for partition in self._partitions.values())
