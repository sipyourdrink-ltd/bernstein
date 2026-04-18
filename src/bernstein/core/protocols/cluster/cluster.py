"""Cluster coordination: node registration, heartbeats, topology management.

Manages a registry of Bernstein nodes for distributed multi-instance
coordination. The central server tracks which nodes are alive, their
capacity, and routes tasks accordingly.

Also provides NodeHeartbeatClient: a thread-safe client that a worker node
uses to auto-register itself and send periodic heartbeats to the central
server.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path  # noqa: TC003 — used at runtime in _load_persisted/_save
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.models import (
    ClusterConfig,
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class NodeRegistry:
    """Registry of cluster nodes with optional disk persistence.

    Thread-safe via the caller holding the FastAPI request lifecycle
    (single async event loop). When ``persist_path`` is provided, the
    registry survives server restarts (nodes are loaded as OFFLINE and
    transition to ONLINE on first heartbeat).
    """

    def __init__(self, config: ClusterConfig, persist_path: Path | None = None) -> None:
        self._nodes: dict[str, NodeInfo] = {}
        self._config = config
        self._persist_path = persist_path
        if persist_path is not None:
            self._load_persisted()

    @property
    def config(self) -> ClusterConfig:
        return self._config

    def _load_persisted(self) -> None:
        """Load nodes from disk, marking all as OFFLINE until heartbeat."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for entry in data:
                node = NodeInfo(
                    id=entry["id"],
                    name=entry.get("name", ""),
                    url=entry.get("url", ""),
                    capacity=NodeCapacity(
                        max_agents=entry.get("max_agents", 6),
                        supported_models=entry.get("supported_models", ["sonnet", "opus", "haiku"]),
                    ),
                    status=NodeStatus.OFFLINE,  # Always start offline
                    last_heartbeat=0.0,
                    registered_at=entry.get("registered_at", 0.0),
                    labels=entry.get("labels", {}),
                    cell_ids=entry.get("cell_ids", []),
                )
                self._nodes[node.id] = node
            logger.info("Loaded %d persisted nodes (all marked OFFLINE)", len(self._nodes))
        except Exception as exc:
            logger.warning("Failed to load persisted nodes: %s", exc)

    def _save(self) -> None:
        """Persist current node registry to disk."""
        if self._persist_path is None:
            return
        try:
            data: list[dict[str, Any]] = []
            for node in self._nodes.values():
                data.append(
                    {
                        "id": node.id,
                        "name": node.name,
                        "url": node.url,
                        "max_agents": node.capacity.max_agents,
                        "supported_models": node.capacity.supported_models,
                        "registered_at": node.registered_at,
                        "labels": node.labels,
                        "cell_ids": node.cell_ids,
                    }
                )
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Failed to persist node registry: %s", exc)

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
            self._save()
            return existing

        node.registered_at = time.time()
        node.last_heartbeat = time.time()
        node.status = NodeStatus.ONLINE
        self._nodes[node.id] = node
        logger.info("Registered new node %s (%s) at %s", node.id, node.name, node.url)
        self._save()
        return node

    def heartbeat(self, node_id: str, capacity: NodeCapacity | None = None) -> NodeInfo | None:
        """Record a heartbeat from a node. Returns None if node is unknown."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.last_heartbeat = time.time()
        # Don't override CORDONED/DRAINING status
        if node.status == NodeStatus.OFFLINE:
            node.status = NodeStatus.ONLINE
            self._save()
        if capacity is not None:
            node.capacity = capacity
        return node

    def unregister(self, node_id: str) -> bool:
        """Remove a node from the registry."""
        removed = self._nodes.pop(node_id, None) is not None
        if removed:
            self._save()
        return removed

    def cordon(self, node_id: str) -> NodeInfo | None:
        """Cordon a node -- exclude from scheduling but keep accepting heartbeats."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.status = NodeStatus.CORDONED
        logger.info("Cordoned node %s (%s)", node.id, node.name)
        self._save()
        return node

    def uncordon(self, node_id: str) -> NodeInfo | None:
        """Uncordon a node -- resume accepting tasks."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        if node.status in (NodeStatus.CORDONED, NodeStatus.DRAINING):
            node.status = NodeStatus.ONLINE
            logger.info("Uncordoned node %s (%s)", node.id, node.name)
            self._save()
        return node

    def start_drain(self, node_id: str) -> NodeInfo | None:
        """Start draining a node -- cordon + mark as draining."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.status = NodeStatus.DRAINING
        logger.info("Started draining node %s (%s)", node.id, node.name)
        self._save()
        return node

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
        time.time()
        timeout = self._config.node_timeout_s
        stale: list[NodeInfo] = []
        for node in self._nodes.values():
            if node.status == NodeStatus.ONLINE and not node.is_alive(timeout):
                node.status = NodeStatus.OFFLINE
                stale.append(node)
                logger.warning("Node %s (%s) marked offline — no heartbeat for %ds", node.id, node.name, timeout)
        return stale

    def online_count(self) -> int:
        """Number of online nodes."""
        return sum(1 for n in self._nodes.values() if n.status == NodeStatus.ONLINE)

    def total_capacity(self) -> int:
        """Total available agent slots across all online nodes."""
        return sum(n.capacity.available_slots for n in self._nodes.values() if n.status == NodeStatus.ONLINE)

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
            n for n in self._nodes.values() if n.status == NodeStatus.ONLINE and n.capacity.available_slots > 0
        ]
        if not candidates:
            return None

        if required_model:
            candidates = [n for n in candidates if required_model in n.capacity.supported_models]
        if require_gpu:
            candidates = [n for n in candidates if n.capacity.gpu_available]
        if not candidates:
            return None

        # Score by label affinity + available capacity
        def score(node: NodeInfo) -> tuple[int, int]:
            affinity = 0
            if preferred_labels:
                affinity = sum(1 for k, v in preferred_labels.items() if node.labels.get(k) == v)
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


def _match_steal_pairs(
    donors: list[tuple[str, int]],
    receivers: list[tuple[str, int]],
    overload_threshold: int,
    max_steal_per_tick: int,
) -> list[tuple[str, str, int]]:
    """Match donor nodes to receiver nodes, returning (donor, receiver, count) tuples."""
    pairs: list[tuple[str, str, int]] = []
    for donor_id, depth in donors:
        excess = depth - overload_threshold
        for i, (recv_id, recv_slots) in enumerate(receivers):
            if recv_slots <= 0 or excess <= 0:
                continue
            steal_count = min(excess, recv_slots, max_steal_per_tick)
            if steal_count <= 0:
                continue
            pairs.append((donor_id, recv_id, steal_count))
            excess -= steal_count
            receivers[i] = (recv_id, recv_slots - steal_count)
    return pairs


class TaskStealPolicy:
    """Policy for when and how to steal tasks between nodes.

    A node is eligible to *donate* tasks when its queue depth exceeds
    ``overload_threshold``.  A node is eligible to *receive* stolen tasks
    when its available slots are above ``idle_threshold``.

    The central server evaluates this policy; individual workers just
    report their queue depth on each heartbeat.
    """

    def __init__(
        self,
        overload_threshold: int = 5,
        idle_threshold: int = 2,
        max_steal_per_tick: int = 3,
    ) -> None:
        self.overload_threshold = overload_threshold
        self.idle_threshold = idle_threshold
        self.max_steal_per_tick = max_steal_per_tick

    def find_steal_pairs(
        self,
        registry: NodeRegistry,
        queue_depths: dict[str, int],
    ) -> list[tuple[str, str, int]]:
        """Identify (donor_node_id, receiver_node_id, count) steal actions.

        Args:
            registry: The cluster node registry.
            queue_depths: Mapping of node_id → number of queued/claimed tasks.

        Returns:
            List of (donor_id, receiver_id, steal_count) tuples.
        """
        online = registry.list_nodes(NodeStatus.ONLINE)
        if len(online) < 2:
            return []

        donors: list[tuple[str, int]] = []
        receivers: list[tuple[str, int]] = []

        for node in online:
            depth = queue_depths.get(node.id, 0)
            if depth > self.overload_threshold:
                donors.append((node.id, depth))
            elif node.capacity.available_slots >= self.idle_threshold:
                receivers.append((node.id, node.capacity.available_slots))

        # Sort donors by most overloaded first, receivers by most idle first
        donors.sort(key=lambda x: x[1], reverse=True)
        receivers.sort(key=lambda x: x[1], reverse=True)

        return _match_steal_pairs(donors, receivers, self.overload_threshold, self.max_steal_per_tick)


class NodeHeartbeatClient:
    """Background heartbeat client for worker nodes.

    Runs a daemon thread that periodically sends heartbeats to the
    central server. On first call, registers the node; subsequent
    calls update capacity and confirm liveness.

    Thread-safe: start/stop can be called from any thread.
    """

    def __init__(
        self,
        server_url: str,
        node_name: str | None = None,
        node_url: str | None = None,
        capacity: NodeCapacity | None = None,
        labels: dict[str, str] | None = None,
        cell_ids: list[str] | None = None,
        interval_s: int = 15,
        auth_token: str | None = None,
        capacity_fn: Callable[[], NodeCapacity] | None = None,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._node_name = node_name or socket.gethostname()
        self._node_url = node_url or ""
        self._capacity = capacity or NodeCapacity()
        self._labels = labels or {}
        self._cell_ids = cell_ids or []
        self._interval_s = interval_s
        self._auth_token = auth_token
        self._capacity_fn = capacity_fn

        self._node_id: str | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered = threading.Event()

    @property
    def node_id(self) -> str | None:
        """The ID assigned by the central server after registration."""
        return self._node_id

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _register(self, client: httpx.Client) -> bool:
        """Register this node with the central server. Returns True on success."""
        payload = {
            "name": self._node_name,
            "url": self._node_url,
            "capacity": {
                "max_agents": self._capacity.max_agents,
                "available_slots": self._capacity.available_slots,
                "active_agents": self._capacity.active_agents,
                "gpu_available": self._capacity.gpu_available,
                "supported_models": self._capacity.supported_models,
            },
            "labels": self._labels,
            "cell_ids": self._cell_ids,
        }
        try:
            resp = client.post(
                f"{self._server_url}/cluster/nodes",
                json=payload,
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 201:
                data = resp.json()
                self._node_id = data.get("id")
                self._registered.set()
                logger.info("Registered as node %s with central server %s", self._node_id, self._server_url)
                return True
            logger.warning("Node registration failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Node registration error: %s", exc)
        return False

    def _send_heartbeat(self, client: httpx.Client) -> bool:
        """Send a heartbeat to the central server. Returns True on success."""
        if self._node_id is None:
            return False

        capacity = self._capacity_fn() if self._capacity_fn else self._capacity
        payload = {
            "capacity": {
                "max_agents": capacity.max_agents,
                "available_slots": capacity.available_slots,
                "active_agents": capacity.active_agents,
                "gpu_available": capacity.gpu_available,
                "supported_models": capacity.supported_models,
            },
        }
        try:
            resp = client.post(
                f"{self._server_url}/cluster/nodes/{self._node_id}/heartbeat",
                json=payload,
                headers=self._headers(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 404:
                # Node was evicted; re-register on next cycle
                logger.warning("Node %s not found on server; will re-register", self._node_id)
                self._node_id = None
                self._registered.clear()
                return False
            logger.warning("Heartbeat failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Heartbeat error: %s", exc)
        return False

    def _run(self) -> None:
        """Main loop for the heartbeat daemon thread."""
        with httpx.Client() as client:
            while not self._stop_event.is_set():
                if self._node_id is None and not self._register(client):
                    # Retry registration after interval
                    self._stop_event.wait(self._interval_s)
                    continue

                self._send_heartbeat(client)
                self._stop_event.wait(self._interval_s)

    def start(self) -> None:
        """Start the heartbeat daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="bernstein-node-heartbeat",
            daemon=True,
        )
        self._thread.start()
        logger.info("Node heartbeat client started (interval=%ds, server=%s)", self._interval_s, self._server_url)

    def stop(self, timeout_s: float = 5.0) -> None:
        """Stop the heartbeat daemon and unregister from the central server."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None

        # Best-effort unregister
        if self._node_id is not None:
            try:
                with httpx.Client() as client:
                    client.delete(
                        f"{self._server_url}/cluster/nodes/{self._node_id}",
                        headers=self._headers(),
                        timeout=5.0,
                    )
                    logger.info("Unregistered node %s from central server", self._node_id)
            except httpx.HTTPError:
                pass
            self._node_id = None
            self._registered.clear()

    def wait_registered(self, timeout_s: float = 30.0) -> bool:
        """Block until the node is registered, or timeout. Returns True if registered."""
        return self._registered.wait(timeout=timeout_s)

    def update_capacity(self, capacity: NodeCapacity) -> None:
        """Update the locally cached capacity (sent on next heartbeat)."""
        self._capacity = capacity
