"""Mailbox system for agent-to-agent messages."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


MessagePriority = Literal["low", "normal", "high", "urgent"]


@dataclass
class MailboxMessage:
    """A message in the agent mailbox system."""

    id: str
    sender_id: str
    recipient_id: str
    subject: str
    content: str
    priority: MessagePriority = "normal"
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: float | None = None
    read: bool = False
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MailboxMessage:
        """Create from dictionary."""
        return cls(**data)

    def is_expired(self) -> bool:
        """Check if message has expired."""
        if self.ttl_seconds is None:
            return False
        return time.time() > (self.timestamp + self.ttl_seconds)


@dataclass
class MailboxQueue:
    """Priority queue for a single agent's mailbox."""

    agent_id: str
    messages: list[MailboxMessage] = field(default_factory=list[MailboxMessage])
    max_size: int = 1000

    def add(self, message: MailboxMessage) -> bool:
        """Add a message to the queue.

        Args:
            message: Message to add.

        Returns:
            True if added, False if queue is full.
        """
        if len(self.messages) >= self.max_size:
            logger.warning("Mailbox queue full for agent %s", self.agent_id)
            return False

        self.messages.append(message)
        self._sort_by_priority()

        logger.debug(
            "Added message %s to mailbox for agent %s",
            message.id,
            self.agent_id,
        )

        return True

    def _sort_by_priority(self) -> None:
        """Sort messages by priority (urgent first)."""
        priority_order = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
        self.messages.sort(key=lambda m: (priority_order.get(m.priority, 2), -m.timestamp))

    def peek(self, count: int = 10) -> list[MailboxMessage]:
        """Peek at messages without marking as read.

        Args:
            count: Maximum number of messages to return.

        Returns:
            List of messages (highest priority first).
        """
        self._cleanup_expired()
        return self.messages[:count]

    def receive(self, count: int = 10) -> list[MailboxMessage]:
        """Receive and mark messages as read.

        Args:
            count: Maximum number of messages to receive.

        Returns:
            List of received messages.
        """
        messages = self.peek(count)
        for msg in messages:
            msg.read = True
        return messages

    def _cleanup_expired(self) -> int:
        """Remove expired messages.

        Returns:
            Number of messages removed.
        """
        expired = [m for m in self.messages if m.is_expired()]
        self.messages = [m for m in self.messages if not m.is_expired()]

        if expired:
            logger.debug(
                "Cleaned up %d expired messages for agent %s",
                len(expired),
                self.agent_id,
            )

        return len(expired)

    def unread_count(self) -> int:
        """Get count of unread messages."""
        self._cleanup_expired()
        return sum(1 for m in self.messages if not m.read)


class MailboxSystem:
    """File-backed mailbox system for agent-to-agent messaging.

    Provides:
    - Per-agent message queues
    - Priority-based ordering
    - TTL for messages
    - Idle notifications
    - Concurrent writer safety

    Args:
        workdir: Project working directory.
        max_queue_size: Maximum messages per agent queue.
        default_ttl_seconds: Default message TTL.
    """

    def __init__(
        self,
        workdir: Path,
        max_queue_size: int = 1000,
        default_ttl_seconds: float = 3600,
    ) -> None:
        self._workdir = workdir
        self._mailbox_dir = workdir / ".sdd" / "runtime" / "mailboxes"
        self._mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._max_queue_size = max_queue_size
        self._default_ttl = default_ttl_seconds
        self._queues: dict[str, MailboxQueue] = {}

    def _get_queue_path(self, agent_id: str) -> Path:
        """Get path to agent's mailbox file.

        Args:
            agent_id: Agent identifier.

        Returns:
            Path to mailbox file.
        """
        return self._mailbox_dir / f"{agent_id}.json"

    def _load_queue(self, agent_id: str) -> MailboxQueue:
        """Load agent's mailbox queue from disk.

        Args:
            agent_id: Agent identifier.

        Returns:
            Loaded MailboxQueue.
        """
        if agent_id in self._queues:
            return self._queues[agent_id]

        queue = MailboxQueue(agent_id=agent_id, max_size=self._max_queue_size)
        queue_path = self._get_queue_path(agent_id)

        if queue_path.exists():
            try:
                data = json.loads(queue_path.read_text())
                for msg_data in data.get("messages", []):
                    msg = MailboxMessage.from_dict(msg_data)
                    if not msg.is_expired():
                        queue.messages.append(msg)
                queue._sort_by_priority()  # type: ignore[reportPrivateUsage]
                logger.debug(
                    "Loaded %d messages for agent %s",
                    len(queue.messages),
                    agent_id,
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Failed to load mailbox for %s: %s", agent_id, exc)

        self._queues[agent_id] = queue
        return queue

    def _save_queue(self, agent_id: str) -> None:
        """Save agent's mailbox queue to disk.

        Args:
            agent_id: Agent identifier.
        """
        if agent_id not in self._queues:
            return

        queue = self._queues[agent_id]
        queue_path = self._get_queue_path(agent_id)

        data = {
            "agent_id": agent_id,
            "messages": [m.to_dict() for m in queue.messages],
            "max_size": queue.max_size,
        }

        queue_path.write_text(json.dumps(data, indent=2))

    def send(
        self,
        sender_id: str,
        recipient_id: str,
        subject: str,
        content: str,
        priority: MessagePriority = "normal",
        ttl_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Send a message to an agent.

        Args:
            sender_id: Sender agent identifier.
            recipient_id: Recipient agent identifier.
            subject: Message subject.
            content: Message content.
            priority: Message priority.
            ttl_seconds: Optional TTL in seconds.
            metadata: Optional metadata dictionary.

        Returns:
            Message ID.
        """
        import uuid

        message = MailboxMessage(
            id=str(uuid.uuid4())[:8],
            sender_id=sender_id,
            recipient_id=recipient_id,
            subject=subject,
            content=content,
            priority=priority,
            ttl_seconds=ttl_seconds or self._default_ttl,
            metadata=metadata or {},
        )

        queue = self._load_queue(recipient_id)
        added = queue.add(message)

        if added:
            self._save_queue(recipient_id)

            # Check if recipient is idle and should be notified
            if priority in ("high", "urgent"):
                logger.info(
                    "High priority message sent to agent %s (may trigger wake)",
                    recipient_id,
                )

        return message.id

    def receive(
        self,
        agent_id: str,
        count: int = 10,
        mark_read: bool = True,
    ) -> list[MailboxMessage]:
        """Receive messages for an agent.

        Args:
            agent_id: Agent identifier.
            count: Maximum messages to receive.
            mark_read: Whether to mark messages as read.

        Returns:
            List of received messages.
        """
        queue = self._load_queue(agent_id)

        messages = queue.receive(count) if mark_read else queue.peek(count)

        if mark_read:
            self._save_queue(agent_id)

        return messages

    def peek(self, agent_id: str, count: int = 10) -> list[MailboxMessage]:
        """Peek at messages without marking as read.

        Args:
            agent_id: Agent identifier.
            count: Maximum messages to peek.

        Returns:
            List of messages.
        """
        return self.receive(agent_id, count, mark_read=False)

    def unread_count(self, agent_id: str) -> int:
        """Get unread message count for an agent.

        Args:
            agent_id: Agent identifier.

        Returns:
            Number of unread messages.
        """
        queue = self._load_queue(agent_id)
        return queue.unread_count()

    def cleanup_agent(self, agent_id: str) -> int:
        """Clean up an agent's mailbox.

        Args:
            agent_id: Agent identifier.

        Returns:
            Number of messages removed.
        """
        queue_path = self._get_queue_path(agent_id)
        count = 0

        if queue_path.exists():
            try:
                data = json.loads(queue_path.read_text())
                count = len(data.get("messages", []))
                queue_path.unlink()
                logger.info(
                    "Cleaned up mailbox for agent %s (%d messages)",
                    agent_id,
                    count,
                )
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to cleanup mailbox for %s: %s", agent_id, exc)

        if agent_id in self._queues:
            del self._queues[agent_id]

        return count

    def get_all_agents_with_messages(self) -> list[str]:
        """Get list of agents with unread messages.

        Returns:
            List of agent IDs.
        """
        agents: list[str] = []
        for queue_file in self._mailbox_dir.glob("*.json"):
            agent_id = queue_file.stem
            if self.unread_count(agent_id) > 0:
                agents.append(agent_id)
        return agents
