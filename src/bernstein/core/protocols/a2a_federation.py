"""MCP-009: A2A protocol federation.

Exchange tasks with external orchestrators via A2A protocol. Builds on
the existing :mod:`bernstein.core.a2a` module to add:

- Peer registry for known external orchestrators.
- Outbound task delegation (send a task to an external peer).
- Inbound task acceptance (receive and track federated tasks).
- Status synchronisation between local and remote task states.

Usage::

    from bernstein.core.protocols.a2a_federation import A2AFederation

    fed = A2AFederation(local_handler=handler)
    fed.register_peer("design-team", "http://design.local:8052")
    task = fed.delegate_task("design-team", "Create wireframes for login page")
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class PeerState(StrEnum):
    """Connection state for a federation peer."""

    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    DEREGISTERED = "deregistered"


@dataclass
class FederationPeer:
    """A known external A2A-compatible orchestrator.

    Attributes:
        name: Human-readable peer name.
        endpoint: Base URL of the peer's A2A endpoint.
        state: Current connection state.
        capabilities: Capability tags the peer advertises.
        last_seen: Unix timestamp of last successful communication.
        task_count: Number of tasks delegated to this peer.
    """

    name: str
    endpoint: str
    state: PeerState = PeerState.ACTIVE
    capabilities: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    last_seen: float = field(default_factory=time.time)
    task_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "name": self.name,
            "endpoint": self.endpoint,
            "state": self.state.value,
            "capabilities": list(self.capabilities),
            "last_seen": self.last_seen,
            "task_count": self.task_count,
        }


class FederatedTaskStatus(StrEnum):
    """Status of a federated (delegated) task."""

    PENDING = "pending"
    SENT = "sent"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass
class FederatedTask:
    """A task delegated to or received from a federation peer.

    Attributes:
        id: Local federation task ID.
        peer_name: Name of the remote peer.
        remote_task_id: Task ID on the remote peer (set after delegation).
        local_task_id: Corresponding local Bernstein task ID, if any.
        message: Task description.
        role: Role hint for task routing.
        direction: "outbound" (we delegated) or "inbound" (peer delegated to us).
        status: Current federation status.
        created_at: Unix timestamp.
        updated_at: Unix timestamp of last update.
        result: Result data from the remote peer.
    """

    id: str
    peer_name: str
    message: str
    role: str = "backend"
    remote_task_id: str = ""
    local_task_id: str = ""
    direction: str = "outbound"
    status: FederatedTaskStatus = FederatedTaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "id": self.id,
            "peer_name": self.peer_name,
            "remote_task_id": self.remote_task_id,
            "local_task_id": self.local_task_id,
            "message": self.message,
            "role": self.role,
            "direction": self.direction,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class A2AFederation:
    """A2A protocol federation for exchanging tasks with external orchestrators.

    Args:
        local_endpoint: This orchestrator's A2A endpoint URL.
    """

    def __init__(self, local_endpoint: str = "http://localhost:8052") -> None:
        self._local_endpoint = local_endpoint
        self._peers: dict[str, FederationPeer] = {}
        self._tasks: dict[str, FederatedTask] = {}
        self._by_peer: dict[str, list[str]] = {}

    def register_peer(
        self,
        name: str,
        endpoint: str,
        capabilities: list[str] | None = None,
    ) -> FederationPeer:
        """Register a federation peer.

        Args:
            name: Peer name.
            endpoint: Peer's A2A endpoint URL.
            capabilities: Optional capability tags.

        Returns:
            The registered peer.
        """
        peer = FederationPeer(
            name=name,
            endpoint=endpoint.rstrip("/"),
            capabilities=list(capabilities) if capabilities else [],
        )
        self._peers[name] = peer
        self._by_peer.setdefault(name, [])
        logger.info("Registered federation peer '%s' at %s", name, endpoint)
        return peer

    def deregister_peer(self, name: str) -> bool:
        """Deregister a federation peer.

        Args:
            name: Peer name.

        Returns:
            True if the peer was found and deregistered.
        """
        peer = self._peers.get(name)
        if peer is None:
            return False
        peer.state = PeerState.DEREGISTERED
        logger.info("Deregistered federation peer '%s'", name)
        return True

    def get_peer(self, name: str) -> FederationPeer | None:
        """Look up a peer by name."""
        return self._peers.get(name)

    def list_peers(self) -> list[FederationPeer]:
        """Return all registered peers."""
        return list(self._peers.values())

    def delegate_task(
        self,
        peer_name: str,
        message: str,
        role: str = "backend",
    ) -> FederatedTask | None:
        """Delegate a task to an external peer.

        Creates a FederatedTask in PENDING state. The actual HTTP send
        is handled by :meth:`mark_sent` after the caller performs the
        network request.

        Args:
            peer_name: Name of the target peer.
            message: Task description.
            role: Role hint for routing.

        Returns:
            The created FederatedTask, or None if the peer is unknown
            or deregistered.
        """
        peer = self._peers.get(peer_name)
        if peer is None or peer.state == PeerState.DEREGISTERED:
            logger.warning("Cannot delegate to peer '%s': not available", peer_name)
            return None

        task = FederatedTask(
            id=uuid.uuid4().hex[:12],
            peer_name=peer_name,
            message=message,
            role=role,
            direction="outbound",
        )
        self._tasks[task.id] = task
        self._by_peer.setdefault(peer_name, []).append(task.id)
        peer.task_count += 1
        logger.info("Delegated task '%s' to peer '%s'", task.id, peer_name)
        return task

    def accept_inbound_task(
        self,
        peer_name: str,
        remote_task_id: str,
        message: str,
        role: str = "backend",
    ) -> FederatedTask:
        """Accept an inbound task from an external peer.

        Args:
            peer_name: Name of the sending peer.
            remote_task_id: Task ID on the remote peer.
            message: Task description.
            role: Role hint.

        Returns:
            The created inbound FederatedTask.
        """
        task = FederatedTask(
            id=uuid.uuid4().hex[:12],
            peer_name=peer_name,
            remote_task_id=remote_task_id,
            message=message,
            role=role,
            direction="inbound",
            status=FederatedTaskStatus.ACCEPTED,
        )
        self._tasks[task.id] = task
        self._by_peer.setdefault(peer_name, []).append(task.id)
        logger.info("Accepted inbound task '%s' from peer '%s'", task.id, peer_name)
        return task

    def mark_sent(self, task_id: str, remote_task_id: str) -> bool:
        """Mark an outbound task as sent, recording the remote task ID.

        Args:
            task_id: Local federation task ID.
            remote_task_id: ID assigned by the remote peer.

        Returns:
            True if the task was updated.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.remote_task_id = remote_task_id
        task.status = FederatedTaskStatus.SENT
        task.updated_at = time.time()
        return True

    def update_status(self, task_id: str, status: FederatedTaskStatus, result: str = "") -> bool:
        """Update the status of a federated task.

        Args:
            task_id: Federation task ID.
            status: New status.
            result: Optional result text.

        Returns:
            True if the task was updated.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.status = status
        task.updated_at = time.time()
        if result:
            task.result = result
        return True

    def link_local_task(self, task_id: str, local_task_id: str) -> bool:
        """Link a federated task to a local Bernstein task.

        Args:
            task_id: Federation task ID.
            local_task_id: Local Bernstein task ID.

        Returns:
            True if linked.
        """
        task = self._tasks.get(task_id)
        if task is None:
            return False
        task.local_task_id = local_task_id
        return True

    def get_task(self, task_id: str) -> FederatedTask | None:
        """Look up a federated task by ID."""
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        peer_name: str | None = None,
        direction: str | None = None,
    ) -> list[FederatedTask]:
        """List federated tasks with optional filtering.

        Args:
            peer_name: Filter by peer name.
            direction: Filter by direction ("inbound" or "outbound").

        Returns:
            List of matching federated tasks.
        """
        tasks = list(self._tasks.values())
        if peer_name is not None:
            tasks = [t for t in tasks if t.peer_name == peer_name]
        if direction is not None:
            tasks = [t for t in tasks if t.direction == direction]
        return tasks

    def to_dict(self) -> dict[str, Any]:
        """Serialize federation state to a JSON-compatible dict."""
        return {
            "local_endpoint": self._local_endpoint,
            "peers": {n: p.to_dict() for n, p in self._peers.items()},
            "task_count": len(self._tasks),
        }
