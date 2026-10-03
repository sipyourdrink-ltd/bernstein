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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import NodeInfo, NodeStatus


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


class ShardedNodeRegistry:
    """Registry facade that routes per-node ops by ring and scatter-gathers aggregate ops.

    Holds N NodeRegistry instances (one per shard), routed by consistent hash
    of node_id. Single-node operations (register, heartbeat) go to one shard;
    aggregate operations (list_nodes, online_count) gather from all shards.
    """

    def __init__(
        self,
        shards: Sequence[str],
        registries: dict[str, Any],
        ring: HashRing,
    ) -> None:
        self._ring = ring
        self._registries = registries
        self._shard_ids = tuple(shards)

    def _route_shard(self, node_id: str) -> str:
        return self._ring.lookup(node_id)

    def _get_registry(self, shard_id: str) -> Any:
        return self._registries[shard_id]

    def register(self, node: NodeInfo) -> NodeInfo:
        shard_id = self._route_shard(node.id)
        return self._get_registry(shard_id).register(node)

    def heartbeat(
        self,
        node_id: str,
        capacity: Any | None = None,
        health_fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        shard_id = self._route_shard(node_id)
        registry = self._get_registry(shard_id)
        if capacity is not None:
            node = registry.get(node_id)
            if node is not None:
                node.capacity = capacity
                node.last_heartbeat = __import__("time").time()
                registry._persist(node)
                registry._sync_node_count_metrics()
        if health_fields:
            node = registry.get(node_id)
            if node is not None:
                for k, v in health_fields.items():
                    if hasattr(node.capacity, k):
                        setattr(node.capacity, k, v)
                registry._persist(node)
        return {"status": "ok"}

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        result = []
        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            result.extend(registry.list_nodes(status))
        return result

    def online_count(self) -> int:
        return sum(self._get_registry(s).online_count() for s in self._shard_ids)

    def total_capacity(self) -> dict[str, int]:
        total_max_agents = 0
        total_available = 0
        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            capacity = registry.total_capacity()
            total_max_agents += capacity.get("max_agents", 0)
            total_available += capacity.get("available_slots", 0)
        return {
            "max_agents": total_max_agents,
            "available_slots": total_available,
        }

    def cluster_summary(self) -> dict[str, Any]:
        shards: dict[str, Any] = {}
        total_online = 0
        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            shards[shard_id] = registry.cluster_summary()
            total_online += registry.online_count()
        return {
            "shards": shards,
            "total_online": total_online,
            "total_capacity": self.total_capacity(),
        }

    def mark_stale(self, timeout_s: float) -> list[str]:
        result: list[str] = []
        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            stale_nodes = registry.mark_stale(timeout_s)
            result.extend(n.id for n in stale_nodes)
        return result

    def find_by_identity(self, name: str, url: str) -> NodeInfo | None:
        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            node = registry.find_by_identity(name, url)
            if node is not None:
                return node
        return None

    def get(self, node_id: str) -> NodeInfo | None:
        shard_id = self._route_shard(node_id)
        return self._get_registry(shard_id).get(node_id)

    def best_node_for_task(
        self,
        task: Any,
        health_ok_only: bool = False,
        labels_required: dict[str, str] | None = None,
    ) -> Any | None:
        best_overall = None
        best_score = (-1, -1)

        for shard_id in self._shard_ids:
            registry = self._get_registry(shard_id)
            shard_best = registry.best_node_for_task(task, health_ok_only, labels_required)
            if shard_best is None:
                continue

            score = self._score_node(shard_best)
            if score > best_score:
                best_score = score
                best_overall = shard_best

        return best_overall

    @staticmethod
    def _score_node(node: NodeInfo) -> tuple[int, int]:
        return (node.capacity.available_slots, -node.capacity.active_agents)
