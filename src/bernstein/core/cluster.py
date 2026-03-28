"""Cluster coordination: node registration, heartbeats, topology management.

Manages a registry of Bernstein nodes for distributed multi-instance
coordination. The central server tracks which nodes are alive, their
capacity, and routes tasks accordingly.

Also provides NodeHeartbeatClient: a thread-safe client that a worker node
uses to auto-register itself and send periodic heartbeats to the central
server.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.models import (
    ClusterConfig,
    ClusterTopology,
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class NodeRegistry:
    """In-memory registry of cluster nodes.

    Thread-safe via the caller holding the FastAPI request lifecycle
    (single async event loop). For multi-process deployments, this
    would need to be backed by a shared store.
    """

    def __init__(self, config: ClusterConfig) -> None:
        self._nodes: dict[str, NodeInfo] = {}
        self._config = config

    @property
    def config(self) -> ClusterConfig:
        return self._config

    def register(self, node: NodeInfo) -> NodeInfo:
        """Register or re-register a node.

        If the node ID already exists, update its info (preserving
        registered_at). Otherwise create a new entry.
        """
        existing = self._nodes.get(node.id)
        if existing is not None:
            existing.name = node.name or existing.name
            existing.url = node.url or existing.url
            existing.capacity = node.capacity
            existing.status = NodeStatus.ONLINE
            existing.last_heartbeat = time.time()
            existing.labels = node.labels or existing.labels
            existing.cell_ids = node.cell_ids or existing.cell_ids
            logger.info("Re-registered node %s (%s)", node.id, node.name)
            return existing

        node.registered_at = time.time()
        node.last_heartbeat = time.time()
        node.status = NodeStatus.ONLINE
        self._nodes[node.id] = node
        logger.info("Registered new node %s (%s) at %s", node.id, node.name, node.url)
        return node

    def heartbeat(self, node_id: str, capacity: NodeCapacity | None = None) -> NodeInfo | None:
        """Record a heartbeat from a node. Returns None if node is unknown."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.last_heartbeat = time.time()
        node.status = NodeStatus.ONLINE
        if capacity is not None:
            node.capacity = capacity
        return node

    def unregister(self, node_id: str) -> bool:
        """Remove a node from the registry."""
        return self._nodes.pop(node_id, None) is not None

    def get(self, node_id: str) -> NodeInfo | None:
        """Look up a node by ID."""
        return self._nodes.get(node_id)

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        """List all nodes, optionally filtered by status."""
        nodes = list(self._nodes.values())
        if status is not None:
            nodes = [n for n in nodes if n.status == status]
        return nodes

    def mark_stale(self) -> list[NodeInfo]:
        """Mark nodes that haven't heartbeated within timeout as offline.

        Returns the list of nodes that were marked offline.
        """
        now = time.time()
        timeout = self._config.node_timeout_s
        stale: list[NodeInfo] = []
        for node in self._nodes.values():
            if node.status == NodeStatus.ONLINE and not node.is_alive(timeout):
                node.status = NodeStatus.OFFLINE
                stale.append(node)
                logger.warning("Node %s (%s) marked offline — no heartbeat for %ds",
                               node.id, node.name, timeout)
        return stale

    def online_count(self) -> int:
        """Number of online nodes."""
        return sum(1 for n in self._nodes.values() if n.status == NodeStatus.ONLINE)

    def total_capacity(self) -> int:
        """Total available agent slots across all online nodes."""
        return sum(
            n.capacity.available_slots
            for n in self._nodes.values()
            if n.status == NodeStatus.ONLINE
        )

    def best_node_for_task(
        self,
        required_model: str | None = None,
        require_gpu: bool = False,
        preferred_labels: dict[str, str] | None = None,
    ) -> NodeInfo | None:
        """Select the best node for a task based on capacity and affinity.

        Selection criteria (in order):
        1. Must be online with available slots
        2. Must support required model (if specified)
        3. Must have GPU (if required)
        4. Prefer nodes matching label affinities
        5. Among remaining, pick the one with most available slots
        """
        candidates = [
            n for n in self._nodes.values()
            if n.status == NodeStatus.ONLINE and n.capacity.available_slots > 0
        ]
        if not candidates:
            return None

        if required_model:
            candidates = [
                n for n in candidates
                if required_model in n.capacity.supported_models
            ]
        if require_gpu:
            candidates = [n for n in candidates if n.capacity.gpu_available]
        if not candidates:
            return None

        # Score by label affinity + available capacity
        def score(node: NodeInfo) -> tuple[int, int]:
            affinity = 0
            if preferred_labels:
                affinity = sum(
                    1 for k, v in preferred_labels.items()
                    if node.labels.get(k) == v
                )
            return (affinity, node.capacity.available_slots)

        candidates.sort(key=score, reverse=True)
        return candidates[0]

    def cluster_summary(self) -> dict[str, Any]:
        """Build a summary of the cluster state."""
        nodes = list(self._nodes.values())
        online = [n for n in nodes if n.status == NodeStatus.ONLINE]
        return {
            "topology": self._config.topology.value,
            "total_nodes": len(nodes),
            "online_nodes": len(online),
            "offline_nodes": len(nodes) - len(online),
            "total_capacity": sum(n.capacity.max_agents for n in online),
            "available_slots": sum(n.capacity.available_slots for n in online),
            "active_agents": sum(n.capacity.active_agents for n in online),
            "nodes": [_node_to_dict(n) for n in nodes],
        }


def _node_to_dict(node: NodeInfo) -> dict[str, Any]:
    """Serialize a NodeInfo to a JSON-compatible dict."""
    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "status": node.status.value,
        "capacity": {
            "max_agents": node.capacity.max_agents,
            "available_slots": node.capacity.available_slots,
            "active_agents": node.capacity.active_agents,
            "gpu_available": node.capacity.gpu_available,
            "supported_models": node.capacity.supported_models,
        },
        "last_heartbeat": node.last_heartbeat,
        "registered_at": node.registered_at,
        "labels": node.labels,
        "cell_ids": node.cell_ids,
    }


def node_from_dict(raw: dict[str, Any]) -> NodeInfo:
    """Deserialize a dict to a NodeInfo."""
    cap_raw = raw.get("capacity", {})
    capacity = NodeCapacity(
        max_agents=cap_raw.get("max_agents", 6),
        available_slots=cap_raw.get("available_slots", 6),
        active_agents=cap_raw.get("active_agents", 0),
        gpu_available=cap_raw.get("gpu_available", False),
        supported_models=cap_raw.get("supported_models", ["sonnet", "opus", "haiku"]),
    )
    return NodeInfo(
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        url=raw.get("url", ""),
        capacity=capacity,
        status=NodeStatus(raw.get("status", "online")),
        last_heartbeat=raw.get("last_heartbeat", time.time()),
        registered_at=raw.get("registered_at", time.time()),
        labels=raw.get("labels", {}),
        cell_ids=raw.get("cell_ids", []),
    )
