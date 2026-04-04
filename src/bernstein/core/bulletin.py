"""Append-only bulletin board for cross-agent communication.

The bulletin board is the shared communication channel between cells.
Agents post messages (alerts, blockers, findings, status updates, dependency
declarations) and read messages posted since their last check.

Also provides the MessageBoard for agent-to-agent delegation — structured
requests where one agent asks another (by role/capability) to perform work
and optionally waits for a response.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

MessageType = Literal["alert", "blocker", "finding", "status", "dependency"]


# ---------------------------------------------------------------------------
# Task/notification protocol for agent terminal status reports
# ---------------------------------------------------------------------------


@dataclass
class AgentStatusNotification:
    """Structured terminal status report from an agent.

    Posted when an agent reaches a terminal state (completed, failed, killed)
    so the orchestrator lifecycle and observability layers stay consistent.

    Attributes:
        agent_id: Unique agent session identifier.
        task_id: Task the agent was working on.
        status: Terminal status — "completed", "failed", or "killed".
        summary: Human-readable outcome text.
        result: Optional machine-readable result payload (JSON serialisable).
        usage_tokens: Total tokens consumed during the run.
        usage_cost_usd: Estimated cost in USD.
        timestamp: Unix seconds when the notification was posted.
    """

    agent_id: str
    task_id: str
    status: str
    summary: str = ""
    result: dict[str, object] | None = None
    usage_tokens: int = 0
    usage_cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "status": self.status,
            "summary": self.summary,
            "result": self.result,
            "usage_tokens": self.usage_tokens,
            "usage_cost_usd": self.usage_cost_usd,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> AgentStatusNotification:
        """Deserialise from a dict."""
        return cls(
            agent_id=str(d.get("agent_id", "")),
            task_id=str(d.get("task_id", "")),
            status=str(d.get("status", "")),
            summary=str(d.get("summary", "")),
            result=cast("dict[str, object] | None", d.get("result")),
            usage_tokens=int(d.get("usage_tokens", 0) or 0),
            usage_cost_usd=float(d.get("usage_cost_usd", 0.0) or 0.0),
            timestamp=float(d.get("timestamp", 0.0) or 0.0),
        )


# ---------------------------------------------------------------------------
# Agent-to-agent delegation
# ---------------------------------------------------------------------------


class DelegationStatus(Enum):
    """Lifecycle states for a delegation request."""

    PENDING = "pending"  # Waiting for a capable agent to claim
    CLAIMED = "claimed"  # An agent has accepted the delegation
    COMPLETED = "completed"  # Result posted by the handler
    EXPIRED = "expired"  # Deadline passed without completion


@dataclass
class Delegation:
    """A structured request from one agent to another.

    Attributes:
        id: Unique delegation identifier.
        origin_agent: ID of the agent making the request.
        target_role: Role or capability required to handle this (e.g. "reviewer", "qa").
        description: What needs to be done.
        deadline: Unix timestamp after which the delegation expires.
        status: Current lifecycle state.
        claimed_by: Agent ID that accepted the delegation, if any.
        result: Response posted by the handling agent, if completed.
        created_at: Unix timestamp when the delegation was created.
        cell_id: Optional cell scope for the delegation.
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    origin_agent: str = ""
    target_role: str = ""
    description: str = ""
    deadline: float = 0.0
    status: DelegationStatus = DelegationStatus.PENDING
    claimed_by: str | None = None
    result: str | None = None
    created_at: float = field(default_factory=time.time)
    cell_id: str | None = None

    def is_expired(self) -> bool:
        """Check if the delegation has passed its deadline."""
        return self.deadline > 0 and time.time() > self.deadline

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "id": self.id,
            "origin_agent": self.origin_agent,
            "target_role": self.target_role,
            "description": self.description,
            "deadline": self.deadline,
            "status": self.status.value,
            "claimed_by": self.claimed_by,
            "result": self.result,
            "created_at": self.created_at,
            "cell_id": self.cell_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> Delegation:
        """Deserialise from a dict."""
        status_val = str(d.get("status", "pending"))
        try:
            status = DelegationStatus(status_val)
        except ValueError:
            status = DelegationStatus.PENDING
        return cls(
            id=str(d.get("id", uuid.uuid4().hex[:12])),
            origin_agent=str(d.get("origin_agent", "")),
            target_role=str(d.get("target_role", "")),
            description=str(d.get("description", "")),
            deadline=float(d.get("deadline", 0.0) or 0.0),
            status=status,
            claimed_by=cast("str | None", d.get("claimed_by")),
            result=cast("str | None", d.get("result")),
            created_at=float(d.get("created_at", 0.0) or 0.0),
            cell_id=cast("str | None", d.get("cell_id")),
        )


class MessageBoard:
    """Agent-to-agent delegation board.

    Stores delegation requests indexed by target role/capability.
    Agents post delegation requests specifying a target role, and agents
    with matching capabilities can claim and respond to them.

    Thread-safe. Stale delegations are cleaned up on access.
    """

    def __init__(self) -> None:
        self._delegations: dict[str, Delegation] = {}  # id -> Delegation
        self._by_role: dict[str, list[str]] = {}  # role -> [delegation_ids]
        self._status_notifications: list[AgentStatusNotification] = []
        self._lock = threading.Lock()

    def post_delegation(
        self,
        origin_agent: str,
        target_role: str,
        description: str,
        deadline: float = 0.0,
        cell_id: str | None = None,
    ) -> Delegation:
        """Create a new delegation request.

        Args:
            origin_agent: ID of the requesting agent.
            target_role: Role or capability needed (e.g. "reviewer").
            description: What the target agent should do.
            deadline: Unix timestamp for expiry (0 = no deadline).
            cell_id: Optional cell scope.

        Returns:
            The created Delegation.
        """
        d = Delegation(
            origin_agent=origin_agent,
            target_role=target_role,
            description=description,
            deadline=deadline,
            cell_id=cell_id,
        )
        with self._lock:
            self._delegations[d.id] = d
            self._by_role.setdefault(target_role, []).append(d.id)
        return d

    def query_by_role(self, role: str) -> list[Delegation]:
        """Find pending delegations that match a role/capability.

        Args:
            role: The role to search for.

        Returns:
            List of pending delegations for this role.
        """
        self._cleanup_expired()
        with self._lock:
            ids = self._by_role.get(role, [])
            return [
                self._delegations[did]
                for did in ids
                if did in self._delegations and self._delegations[did].status == DelegationStatus.PENDING
            ]

    def claim(self, delegation_id: str, agent_id: str) -> Delegation | None:
        """Claim a pending delegation.

        Args:
            delegation_id: ID of the delegation to claim.
            agent_id: ID of the agent claiming it.

        Returns:
            The updated Delegation, or None if not found/already claimed.
        """
        with self._lock:
            d = self._delegations.get(delegation_id)
            if d is None or d.status != DelegationStatus.PENDING:
                return None
            if d.is_expired():
                d.status = DelegationStatus.EXPIRED
                return None
            d.status = DelegationStatus.CLAIMED
            d.claimed_by = agent_id
            return d

    def post_result(self, delegation_id: str, agent_id: str, result: str) -> Delegation | None:
        """Post a result for a claimed delegation.

        Args:
            delegation_id: ID of the delegation.
            agent_id: ID of the agent posting the result (must be the claimer).
            result: The result text.

        Returns:
            The updated Delegation, or None if not found or wrong agent.
        """
        with self._lock:
            d = self._delegations.get(delegation_id)
            if d is None or d.claimed_by != agent_id:
                return None
            d.status = DelegationStatus.COMPLETED
            d.result = result
            return d

    def get_delegation(self, delegation_id: str) -> Delegation | None:
        """Get a single delegation by ID.

        Args:
            delegation_id: The delegation identifier.

        Returns:
            The Delegation, or None if not found.
        """
        with self._lock:
            return self._delegations.get(delegation_id)

    def get_by_origin(self, agent_id: str) -> list[Delegation]:
        """Get all delegations created by a specific agent.

        Args:
            agent_id: The origin agent's ID.

        Returns:
            List of delegations from this agent.
        """
        with self._lock:
            return [d for d in self._delegations.values() if d.origin_agent == agent_id]

    @property
    def count(self) -> int:
        """Total number of delegations on the board."""
        with self._lock:
            return len(self._delegations)

    def _cleanup_expired(self) -> int:
        """Mark expired delegations and return count cleaned.

        Returns:
            Number of delegations marked as expired.
        """
        cleaned = 0
        with self._lock:
            for d in self._delegations.values():
                if d.status == DelegationStatus.PENDING and d.is_expired():
                    d.status = DelegationStatus.EXPIRED
                    cleaned += 1
        return cleaned

    def flush_to_disk(self, path: Path) -> int:
        """Append all delegations to a JSONL file.

        Args:
            path: JSONL file path to write to.

        Returns:
            Number of delegations written.
        """
        with self._lock:
            delegations = list(self._delegations.values())

        if not delegations:
            return 0

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for d in delegations:
                f.write(json.dumps(d.to_dict(), default=str) + "\n")
        return len(delegations)

    def load_from_disk(self, path: Path) -> int:
        """Load delegations from a JSONL file.

        Args:
            path: JSONL file to read from.

        Returns:
            Number of new delegations loaded.
        """
        if not path.exists():
            return 0

        loaded = 0
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data: dict[str, object] = json.loads(line)
            except json.JSONDecodeError:
                continue
            did = str(data.get("id", ""))
            with self._lock:
                if did in self._delegations:
                    continue
            d = Delegation.from_dict(data)
            with self._lock:
                self._delegations[d.id] = d
                self._by_role.setdefault(d.target_role, []).append(d.id)
            loaded += 1
        return loaded


@dataclass(frozen=True)
class BulletinMessage:
    """A single message on the bulletin board.

    Args:
        agent_id: ID of the posting agent.
        type: Category of message.
        content: Free-text message body.
        timestamp: Unix epoch when posted (auto-filled if zero).
        cell_id: Optional cell the message pertains to.
    """

    agent_id: str
    type: MessageType
    content: str
    timestamp: float = 0.0
    cell_id: str | None = None


@dataclass
class AgentActivitySummary:
    """Activity summary broadcast by an agent for cross-agent visibility.

    Attributes:
        agent_id: Unique agent session identifier.
        summary: 3-5 word description of current activity state.
        timestamp: Unix seconds when the summary was posted.
    """

    agent_id: str
    summary: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-safe dict."""
        return {
            "agent_id": self.agent_id,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> AgentActivitySummary:
        """Deserialise from a dict."""
        return cls(
            agent_id=str(d.get("agent_id", "")),
            summary=str(d.get("summary", "")),
            timestamp=float(d.get("timestamp", 0.0) or 0.0),
        )


@dataclass
class BulletinBoard:
    """Append-only message log for cross-agent communication.

    Thread-safe. Messages are ordered by insertion time.  The board can
    be flushed to a JSONL file for persistence / debugging.
    """

    _messages: list[BulletinMessage] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _status_notifications: list[AgentStatusNotification] = field(default_factory=list)
    _activity_summaries: dict[str, AgentActivitySummary] = field(default_factory=dict)

    def post(self, msg: BulletinMessage) -> BulletinMessage:
        """Append a message to the board.

        If the message timestamp is zero, the current time is used.

        Args:
            msg: Message to post.

        Returns:
            The stored message (with timestamp filled in).
        """
        if msg.timestamp == 0:
            msg = BulletinMessage(
                agent_id=msg.agent_id,
                type=msg.type,
                content=msg.content,
                timestamp=time.time(),
                cell_id=msg.cell_id,
            )
        with self._lock:
            self._messages.append(msg)
        return msg

    def read_since(self, ts: float) -> list[BulletinMessage]:
        """Return all messages with timestamp strictly greater than *ts*.

        Args:
            ts: Unix epoch lower bound (exclusive).

        Returns:
            List of messages posted after *ts*, in insertion order.
        """
        with self._lock:
            return [m for m in self._messages if m.timestamp > ts]

    def read_by_type(self, msg_type: MessageType) -> list[BulletinMessage]:
        """Return all messages of a specific type.

        Args:
            msg_type: Message category to filter on.

        Returns:
            All matching messages in insertion order.
        """
        with self._lock:
            return [m for m in self._messages if m.type == msg_type]

    def read_by_cell(self, cell_id: str) -> list[BulletinMessage]:
        """Return all messages for a specific cell.

        Args:
            cell_id: Cell identifier to filter on.

        Returns:
            All matching messages in insertion order.
        """
        with self._lock:
            return [m for m in self._messages if m.cell_id == cell_id]

    @property
    def count(self) -> int:
        """Total number of messages on the board."""
        with self._lock:
            return len(self._messages)

    def flush_to_disk(self, path: Path) -> int:
        """Append all messages to a JSONL file on disk.

        Each message becomes one JSON line. The file is opened in append
        mode so previous flushes are preserved.

        Args:
            path: JSONL file path to write to.

        Returns:
            Number of messages written.
        """
        with self._lock:
            messages = list(self._messages)

        if not messages:
            return 0

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(asdict(msg), default=str) + "\n")
        return len(messages)

    def summary(self, limit: int = 10) -> str:
        """Return the last *limit* messages as a human-readable string.

        Useful for injecting recent team activity into an agent's prompt.

        Args:
            limit: Maximum number of messages to include (most recent first).

        Returns:
            Multi-line string, one message per line, or empty string if the
            board is empty.
        """
        with self._lock:
            recent = self._messages[-limit:]
        if not recent:
            return ""
        lines = [f"- {m.agent_id}: {m.content}" for m in recent]
        return "\n".join(lines)

    def post_file_created(
        self,
        agent_id: str,
        file_path: str,
        classes: list[str] | None = None,
    ) -> BulletinMessage:
        """Post a status message announcing a newly created file.

        Args:
            agent_id: ID of the agent that created the file.
            file_path: Path of the file relative to the project root.
            classes: Optional list of top-level class/function names defined.

        Returns:
            The stored BulletinMessage.
        """
        content = f"created {file_path} with classes: {', '.join(classes)}" if classes else f"created {file_path}"
        return self.post(BulletinMessage(agent_id=agent_id, type="status", content=content))

    def post_api_endpoint(
        self,
        agent_id: str,
        method: str,
        route: str,
        response: str | None = None,
    ) -> BulletinMessage:
        """Post a finding message announcing a new API endpoint definition.

        Args:
            agent_id: ID of the agent that defined the endpoint.
            method: HTTP method (GET, POST, etc.).
            route: URL path (e.g. "/auth/login").
            response: Optional description of the response shape.

        Returns:
            The stored BulletinMessage.
        """
        content = f"added {method} {route} returning {response}" if response else f"added {method} {route}"
        return self.post(BulletinMessage(agent_id=agent_id, type="finding", content=content))

    def load_from_disk(self, path: Path) -> int:
        """Load messages from a JSONL file, adding them to the board.

        Duplicate-safe: skips messages whose timestamp already exists in
        the board (exact float match).

        Args:
            path: JSONL file to read from.

        Returns:
            Number of new messages loaded.
        """
        if not path.exists():
            return 0

        existing_ts: set[float] = set()
        with self._lock:
            existing_ts = {m.timestamp for m in self._messages}

        loaded = 0
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                data: dict[str, object] = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(cast("float", data.get("timestamp", 0.0)))
            if ts in existing_ts:
                continue
            msg = BulletinMessage(
                agent_id=str(data.get("agent_id", "")),
                type=cast("MessageType", data.get("type", "status")),
                content=str(data.get("content", "")),
                timestamp=ts,
                cell_id=cast("str | None", data.get("cell_id")),
            )
            with self._lock:
                self._messages.append(msg)
            existing_ts.add(ts)
            loaded += 1
        return loaded

    # -- Agent status notifications --------------------------------------------

    def post_status_notification(self, notification: AgentStatusNotification) -> None:
        """Record a structured terminal status report from an agent.

        Args:
            notification: The typed status notification from the agent.
        """
        with self._lock:
            self._status_notifications.append(notification)

    def consume_status_notifications(self) -> list[AgentStatusNotification]:
        """Drain all pending status notifications.

        Returns:
            List of AgentStatusNotification objects (FIFO).
            The internal list is cleared after this call.
        """
        with self._lock:
            result = list(self._status_notifications)
            self._status_notifications.clear()
        return result

    # -- Agent activity summaries ---------------------------------------------

    def post_activity_summary(self, activity_summary: AgentActivitySummary) -> None:
        """Record the latest activity summary for an agent.

        Only the most-recent summary per agent_id is retained.

        Args:
            activity_summary: The summary to record.
        """
        with self._lock:
            self._activity_summaries[activity_summary.agent_id] = activity_summary

    def get_latest_activity_summary(self, agent_id: str) -> AgentActivitySummary | None:
        """Return the latest activity summary for a specific agent.

        Args:
            agent_id: The agent whose summary to retrieve.

        Returns:
            The most recent AgentActivitySummary, or None if not found.
        """
        with self._lock:
            return self._activity_summaries.get(agent_id)

    def get_all_activity_summaries(self) -> dict[str, AgentActivitySummary]:
        """Return the latest activity summary for every agent that has posted one.

        Returns:
            Mapping of agent_id -> AgentActivitySummary.
        """
        with self._lock:
            return dict(self._activity_summaries)
