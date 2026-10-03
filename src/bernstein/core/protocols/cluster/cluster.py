"""Cluster coordination: node registration, heartbeats, topology management.

Manages a registry of Bernstein nodes for distributed multi-instance
coordination. The central server tracks which nodes are alive, their
capacity, and routes tasks accordingly.

Also provides NodeHeartbeatClient: a thread-safe client that a worker node
uses to auto-register itself and send periodic heartbeats to the central
server.
"""

from __future__ import annotations

import functools
import logging
import operator
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - used at runtime
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.models import (
    ClusterConfig,
    NodeCapacity,
    NodeInfo,
    NodeStatus,
)
from bernstein.core.observability import prometheus as _metrics
from bernstein.core.protocols.cluster import cluster_audit as _audit
from bernstein.core.protocols.cluster.cluster_tls import (
    TLSConfig,
    build_httpx_client_kwargs,
)
from bernstein.core.protocols.cluster.shard_ring import HashRing
from bernstein.core.protocols.cluster.sqlite_node_store import SQLiteNodeStore

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shard routing — deterministic node assignment for scale-out.
# ---------------------------------------------------------------------------


def _compute_node_id_sharded(name: str, url: str) -> str:
    """Compute deterministic node ID from name and url when sharded."""
    import hashlib

    identity = f"{name}|{url}"
    return hashlib.sha256(identity.encode()).hexdigest()[:12]


SHARD_VNODES = 100
_HEARTBEAT_PERSIST_INTERVAL_S = 30.0


def _find_owner_shard(node_id: str, shards: tuple[tuple[str, str], ...]) -> str | None:
    """Find owner shard using consistent hashing.

    Returns shard_id of the owner, or None if no shards configured.
    """
    if not shards:
        return None
    return _ring_for(tuple(sid for sid, _ in shards)).lookup(node_id)


@functools.lru_cache(maxsize=8)
def _ring_for(shard_ids: tuple[str, ...]) -> HashRing:
    return HashRing(shard_ids, SHARD_VNODES)


def _get_shard_url(shard_id: str, shards: tuple[tuple[str, str], ...]) -> str | None:
    """Lookup the URL for a shard ID."""
    for sid, url in shards:
        if sid == shard_id:
            return url
    return None


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
        self._store: SQLiteNodeStore | None = None
        self._last_persisted: dict[str, float] = {}
        self._heartbeat_seen: set[str] = set()
        if persist_path is not None:
            self._open_store(persist_path)
            self._load_persisted()

    @property
    def config(self) -> ClusterConfig:
        return self._config

    def _open_store(self, persist_path: Path) -> None:
        db_path = persist_path.with_suffix(".db") if persist_path.suffix == ".json" else persist_path
        legacy = db_path.with_suffix(".json")
        try:
            self._store = SQLiteNodeStore(db_path)
            if legacy.exists() and self._store.count() == 0:
                self._store.migrate_from_json(legacy)
        except Exception as exc:
            logger.warning("Failed to open node store: %s", exc)
            self._store = None

    def _load_persisted(self) -> None:
        """Load nodes from the store; operator-set statuses survive, the rest start OFFLINE."""
        if self._store is None:
            return
        try:
            for node in self._store.list_nodes():
                if node.status not in _OPERATOR_INTENT_STATUSES:
                    node.status = NodeStatus.OFFLINE
                node.last_heartbeat = 0.0
                self._nodes[node.id] = node
            logger.info("Loaded %d persisted nodes", len(self._nodes))
        except Exception as exc:
            logger.warning("Failed to load persisted nodes: %s", exc)

    def _persist(self, node: NodeInfo) -> None:
        """Write one node row."""
        if self._store is None:
            return
        try:
            self._store.upsert(node)
            self._last_persisted[node.id] = time.time()
        except Exception as exc:
            logger.warning("Failed to persist node %s: %s", node.id, exc)

    def _persist_delete(self, node_id: str) -> None:
        if self._store is None:
            return
        try:
            self._store.delete(node_id)
        except Exception as exc:
            logger.warning("Failed to delete node %s: %s", node_id, exc)

    def register(self, node: NodeInfo) -> NodeInfo:
        """Register or re-register a node.

        If the node ID already exists, update its info (preserving
        registered_at, and any status the operator set). Otherwise create a new
        entry, which starts ``ONLINE``.
        """
        existing = self._nodes.get(node.id)
        if existing is not None:
            existing.name = node.name or existing.name
            existing.url = node.url or existing.url
            existing.capacity = node.capacity
            # Not unconditionally ONLINE: a cordon is an operator's instruction
            # and a restart is not an answer to it. See
            # _status_after_reregistration.
            existing.status = _status_after_reregistration(existing.status, existing)
            existing.last_heartbeat = time.time()
            existing.labels = node.labels or existing.labels
            existing.cell_ids = node.cell_ids or existing.cell_ids
            logger.info("Re-registered node %s (%s)", node.id, node.name)
            self._persist(existing)
            self._sync_node_count_metrics()
            return existing

        node.registered_at = time.time()
        node.last_heartbeat = time.time()
        node.status = NodeStatus.ONLINE
        self._nodes[node.id] = node
        logger.info("Registered new node %s (%s) at %s", node.id, node.name, node.url)
        self._persist(node)
        self._sync_node_count_metrics()
        _audit.record_node_registered(
            node.id,
            role=node.labels.get("role", "worker"),
            registered_at=node.registered_at,
            initial_capacity=node.capacity.max_agents,
        )
        return node

    def heartbeat(
        self,
        node_id: str,
        capacity: NodeCapacity | None = None,
        *,
        disk_free_mb: int | None = None,
        mem_used_pct: float | None = None,
        mesh_rtt_ms: float | None = None,
        platform: str | None = None,
        persist: bool = True,
    ) -> NodeInfo | None:
        """Record a heartbeat from a node. Returns None if node is unknown.

        The row is rewritten only when status or health changed, or the last
        write is older than ``_HEARTBEAT_PERSIST_INTERVAL_S``. With
        ``persist=False`` the caller writes the row itself.
        """
        node = self._nodes.get(node_id)
        if node is None:
            _metrics.record_heartbeat("rejected_unknown_node")
            return None
        node.last_heartbeat = time.time()
        status_flipped = False
        # Don't override CORDONED/DRAINING status
        if node.status == NodeStatus.OFFLINE:
            node.status = NodeStatus.ONLINE
            status_flipped = True
        if capacity is not None:
            node.capacity = capacity
        cap = node.capacity
        if disk_free_mb is not None:
            cap.disk_free_mb = disk_free_mb
        if mem_used_pct is not None:
            cap.mem_used_pct = mem_used_pct
        if mesh_rtt_ms is not None:
            cap.mesh_rtt_ms = mesh_rtt_ms
        if platform is not None:
            cap.platform = platform
        verdict = health_verdict(
            disk_free_mb=cap.disk_free_mb,
            mem_used_pct=cap.mem_used_pct,
            mesh_rtt_ms=cap.mesh_rtt_ms,
        )
        health_changed = verdict != node.health
        if verdict != "ok" and node.health == "ok":
            node.unhealthy_since = node.last_heartbeat
        elif verdict == "ok":
            node.unhealthy_since = None
        node.health = verdict
        first_beat = node.id not in self._heartbeat_seen
        self._heartbeat_seen.add(node.id)
        if persist and (
            first_beat
            or status_flipped
            or health_changed
            or node.last_heartbeat - self._last_persisted.get(node.id, 0.0) >= _HEARTBEAT_PERSIST_INTERVAL_S
        ):
            self._persist(node)
        if status_flipped:
            self._sync_node_count_metrics()
        _metrics.record_heartbeat("accepted")
        return node

    def heartbeat_batch(self, items: list[tuple[str, NodeCapacity | None]]) -> list[NodeInfo | None]:
        """Record several heartbeats and write the touched rows in one transaction."""
        results = [self.heartbeat(node_id, capacity, persist=False) for node_id, capacity in items]
        self._persist_many([n for n in results if n is not None])
        return results

    def _persist_many(self, nodes: list[NodeInfo]) -> None:
        if self._store is None or not nodes:
            return
        try:
            self._store.upsert_many(nodes)
            now = time.time()
            for node in nodes:
                self._last_persisted[node.id] = now
        except Exception as exc:
            logger.warning("Failed to persist %d nodes: %s", len(nodes), exc)

    def unregister(self, node_id: str) -> bool:
        """Remove a node from the registry."""
        removed = self._nodes.pop(node_id, None) is not None
        if removed:
            self._persist_delete(node_id)
            self._sync_node_count_metrics()
            _audit.record_node_left(node_id, reason="unregistered")
        return removed

    def cordon(self, node_id: str) -> NodeInfo | None:
        """Cordon a node -- exclude from scheduling but keep accepting heartbeats."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.status = NodeStatus.CORDONED
        logger.info("Cordoned node %s (%s)", node.id, node.name)
        self._persist(node)
        self._sync_node_count_metrics()
        _audit.record_node_cordoned(node.id)
        return node

    def uncordon(self, node_id: str) -> NodeInfo | None:
        """Uncordon a node -- resume accepting tasks."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        if node.status in (NodeStatus.CORDONED, NodeStatus.DRAINING):
            node.status = NodeStatus.ONLINE
            logger.info("Uncordoned node %s (%s)", node.id, node.name)
            self._persist(node)
            self._sync_node_count_metrics()
        return node

    def start_drain(self, node_id: str) -> NodeInfo | None:
        """Start draining a node -- cordon + mark as draining."""
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.status = NodeStatus.DRAINING
        logger.info("Started draining node %s (%s)", node.id, node.name)
        self._persist(node)
        self._sync_node_count_metrics()
        _audit.record_node_drained(node.id)
        return node

    def get(self, node_id: str) -> NodeInfo | None:
        """Look up a node by ID."""
        return self._nodes.get(node_id)

    def find_by_identity(self, name: str, url: str) -> NodeInfo | None:
        """Look up a node by its operator-visible identity (name + url).

        ``register`` keys on the node id, which a caller that mints a fresh
        ``NodeInfo`` per attempt does not carry across a restart. Registration
        surfaces use this to resolve the id an already-known worker was given,
        so a restarting worker updates its entry instead of adding one.

        A blank ``name`` or ``url`` never matches: those carry no identity, so
        collapsing them would merge unrelated anonymous nodes into one entry.
        """
        if not name or not url:
            return None
        return next((n for n in self._nodes.values() if n.name == name and n.url == url), None)

    def list_nodes(self, status: NodeStatus | None = None) -> list[NodeInfo]:
        """List all nodes, optionally filtered by status."""
        nodes = list(self._nodes.values())
        if status is not None:
            nodes = [n for n in nodes if n.status == status]
        return nodes

    def mark_stale(self, timeout_s: float | None = None) -> list[NodeInfo]:
        """Mark nodes that haven't heartbeated within timeout as offline.

        Returns the list of nodes that were marked offline.
        """
        time.time()
        timeout = self._config.node_timeout_s if timeout_s is None else timeout_s
        stale: list[NodeInfo] = []
        for node in self._nodes.values():
            if node.status == NodeStatus.ONLINE and not node.is_alive(timeout):
                node.status = NodeStatus.OFFLINE
                stale.append(node)
                logger.warning("Node %s (%s) marked offline - no heartbeat for %ds", node.id, node.name, timeout)
                _audit.record_node_left(node.id, reason="timeout")
        self._persist_many(stale)
        if stale:
            self._sync_node_count_metrics()
        return stale

    # ------------------------------------------------------------------
    # Observability - keep the cluster_nodes_total gauge in sync with the
    # registry's authoritative view.  Called from every mutation path.
    # ------------------------------------------------------------------

    def _sync_node_count_metrics(self) -> None:
        """Set the per-status node-count gauge from the current registry."""
        counts: dict[str, int] = {s.value: 0 for s in NodeStatus}
        for node in self._nodes.values():
            counts[node.status.value] = counts.get(node.status.value, 0) + 1
        for status, count in counts.items():
            _metrics.set_node_count(status, count)

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
            n
            for n in self._nodes.values()
            if n.status == NodeStatus.ONLINE and n.health == "ok" and n.capacity.available_slots > 0
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

        return max(candidates, key=score)

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


#: Statuses that record what an *operator* decided, as opposed to what the
#: server last observed. Only these survive a re-registration.
#:
#: ``CORDONED`` and ``DRAINING`` are instructions -- keep work off this node --
#: and the node restarting is not an answer to either. ``DEGRADED`` and
#: ``OFFLINE`` are observations about a process, and the process re-registering
#: is direct evidence they no longer hold, so those are right to reset.
_OPERATOR_INTENT_STATUSES: frozenset[NodeStatus] = frozenset({NodeStatus.CORDONED, NodeStatus.DRAINING})


def _status_after_reregistration(current: NodeStatus, node: NodeInfo) -> NodeStatus:
    """What a re-registering node's status becomes: intent survives, health resets.

    ``register`` used to set ``ONLINE`` unconditionally on this path, so a node
    an operator had cordoned returned to ``ONLINE`` the moment it restarted and
    its slots re-entered ``total_capacity``.

    That inverts the property a cordon exists for. Cordoning is usually a
    response to a node misbehaving, and a misbehaving node restarts more often
    than a healthy one -- so the worse a worker behaved, the more reliably it
    escaped its cordon. The domain convention agrees: a kubelet restart does not
    clear ``spec.unschedulable``.

    Args:
        current: The status on the existing registry record.
        node: The re-registering node, for the log line only.

    Returns:
        ``current`` when it records operator intent, else ``ONLINE``.
    """
    if current not in _OPERATOR_INTENT_STATUSES:
        return NodeStatus.ONLINE
    # Nothing said either way before this, so an operator learned a cordon had
    # been cleared by watching work get scheduled onto the node. Say it plainly.
    logger.info(
        "Node %s (%s) re-registered; keeping operator-set status %s",
        node.id,
        node.name,
        current.value,
    )
    return current


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
        donors.sort(key=operator.itemgetter(1), reverse=True)
        receivers.sort(key=operator.itemgetter(1), reverse=True)

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
        tls: TLSConfig | None = None,
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
        self._tls = tls

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
            if resp.status_code == 421:
                try:
                    data = resp.json()
                    new_url = data.get("url", "")
                    if new_url and new_url != self._server_url:
                        self._server_url = new_url
                        logger.info("Misdirected to shard %s; retrying registration", data.get("shard_id", "?"))
                        return self._register(client)
                except (ValueError, KeyError):
                    pass
            logger.warning("Node registration failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Node registration error: %s", exc)
        return False

    def _send_heartbeat(self, client: httpx.Client) -> bool:
        """Send a heartbeat to the central server. Returns True on success."""
        if self._node_id is None:
            return False

        capacity = self._capacity_fn() if self._capacity_fn else self._capacity
        payload: dict[str, Any] = {
            "capacity": {
                "max_agents": capacity.max_agents,
                "available_slots": capacity.available_slots,
                "active_agents": capacity.active_agents,
                "gpu_available": capacity.gpu_available,
                "supported_models": capacity.supported_models,
            },
        }
        if capacity.disk_free_mb is not None:
            payload["disk_free_mb"] = capacity.disk_free_mb
        if capacity.mem_used_pct is not None:
            payload["mem_used_pct"] = capacity.mem_used_pct
        if capacity.mesh_rtt_ms is not None:
            payload["mesh_rtt_ms"] = capacity.mesh_rtt_ms
        if capacity.platform is not None:
            payload["platform"] = capacity.platform
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
                logger.warning("Node %s not found on server; will re-register", self._node_id)
                self._node_id = None
                self._registered.clear()
                return False
            if resp.status_code == 421:
                try:
                    data = resp.json()
                    new_url = data.get("url", "")
                    if new_url and new_url != self._server_url:
                        self._server_url = new_url
                        logger.info("Misdirected to shard %s; retrying heartbeat", data.get("shard_id", "?"))
                        return self._send_heartbeat(client)
                except (ValueError, KeyError):
                    pass
            logger.warning("Heartbeat failed: %d %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            logger.warning("Heartbeat error: %s", exc)
        return False

    def _run(self) -> None:
        """Main loop for the heartbeat daemon thread."""
        with httpx.Client(**build_httpx_client_kwargs(self._tls)) as client:
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
            with suppress(httpx.HTTPError), httpx.Client(**build_httpx_client_kwargs(self._tls)) as client:
                client.delete(
                    f"{self._server_url}/cluster/nodes/{self._node_id}",
                    headers=self._headers(),
                    timeout=5.0,
                )
                logger.info("Unregistered node %s from central server", self._node_id)
            self._node_id = None
            self._registered.clear()

    def wait_registered(self, timeout_s: float = 30.0) -> bool:
        """Block until the node is registered, or timeout. Returns True if registered."""
        return self._registered.wait(timeout=timeout_s)

    def update_capacity(self, capacity: NodeCapacity) -> None:
        """Update the locally cached capacity (sent on next heartbeat)."""
        self._capacity = capacity


# ---------------------------------------------------------------------------
# Health verdict — evaluates worker-reported telemetry against thresholds.
# ---------------------------------------------------------------------------


@dataclass
class HealthThresholds:
    """Configurable thresholds for worker health evaluation."""

    min_disk_free_mb: int = 512
    max_mem_used_pct: float = 95.0
    max_mesh_rtt_ms: float = 5000.0


def health_verdict(
    *,
    disk_free_mb: int | None = None,
    mem_used_pct: float | None = None,
    mesh_rtt_ms: float | None = None,
    thresholds: HealthThresholds | None = None,
) -> str:
    """Return ``"ok"`` or ``"unhealthy:<reason>[,<reason>]"``."""
    t = thresholds or HealthThresholds()
    reasons: list[str] = []
    if disk_free_mb is not None and disk_free_mb < t.min_disk_free_mb:
        reasons.append("disk")
    if mem_used_pct is not None and mem_used_pct > t.max_mem_used_pct:
        reasons.append("memory")
    if mesh_rtt_ms is not None and mesh_rtt_ms > t.max_mesh_rtt_ms:
        reasons.append("mesh-rtt")
    return "ok" if not reasons else "unhealthy:" + ",".join(reasons)


# ---------------------------------------------------------------------------
# Batch task assignment — assign N tasks to the best N workers in one call.
# ---------------------------------------------------------------------------


def batch_assign(
    registry: NodeRegistry,
    tasks: list[dict[str, Any]],
    *,
    preferred_labels: dict[str, str] | None = None,
) -> list[tuple[dict[str, Any], NodeInfo | None]]:
    """Assign a batch of tasks to available workers.

    Each task dict may carry ``required_model`` and ``require_gpu``.
    Returns ``(task, assigned_node_or_None)`` pairs. Slots are
    decremented in-memory between picks so the same node is not
    over-assigned within a single batch.

    Uses power-of-two-choices when the pool is large enough:
    pick two random candidates and assign to the one with more slots.
    """
    import random

    online = [n for n in registry.list_nodes(NodeStatus.ONLINE) if n.capacity.available_slots > 0 and n.health == "ok"]
    if not online:
        return [(t, None) for t in tasks]

    # Track remaining slots to avoid over-assignment
    remaining: dict[str, int] = {n.id: n.capacity.available_slots for n in online}
    node_map: dict[str, NodeInfo] = {n.id: n for n in online}

    results: list[tuple[dict[str, Any], NodeInfo | None]] = []
    for task in tasks:
        req_model = task.get("required_model")
        req_gpu = task.get("require_gpu", False)

        candidates = [
            nid
            for nid, slots in remaining.items()
            if slots > 0
            and (not req_model or req_model in node_map[nid].capacity.supported_models)
            and (not req_gpu or node_map[nid].capacity.gpu_available)
        ]

        if not candidates:
            results.append((task, None))
            continue

        # Power-of-two-choices
        if len(candidates) >= 2:
            a, b = random.sample(candidates, 2)
            pick = a if remaining[a] >= remaining[b] else b
        else:
            pick = candidates[0]

        remaining[pick] -= 1
        results.append((task, node_map[pick]))

    return results
