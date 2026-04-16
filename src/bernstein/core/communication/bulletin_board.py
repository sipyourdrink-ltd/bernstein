"""Shared bulletin board for inter-agent communication."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


MessageType = Literal["info", "warning", "discovery", "coordination", "fact", "finding", "blocker", "pattern"]


@dataclass
class BulletinMessage:
    """Message posted to the bulletin board."""

    id: str
    sender_agent_id: str
    sender_task_id: str
    message_type: MessageType
    content: str
    timestamp: float
    tags: list[str] = field(default_factory=list[str])
    expires_at: float | None = None
    confidence: float = 0.5
    scope: list[str] = field(default_factory=list)
    source_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BulletinMessage:
        """Create from dictionary.

        Handles backward compatibility by supplying defaults for new fields.
        """
        return cls(
            id=data["id"],
            sender_agent_id=data["sender_agent_id"],
            sender_task_id=data["sender_task_id"],
            message_type=data["message_type"],
            content=data["content"],
            timestamp=data["timestamp"],
            tags=data.get("tags", []),
            expires_at=data.get("expires_at"),
            confidence=data.get("confidence", 0.5),
            scope=data.get("scope", []),
            source_model=data.get("source_model", ""),
        )

    def is_expired(self) -> bool:
        """Check if message has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class BulletinBoard:
    """Shared bulletin board for inter-agent communication.

    Agents can post messages that are visible to other agents.
    Useful for sharing discoveries, coordinating work, and avoiding duplication.

    Args:
        workdir: Project working directory.
        message_ttl_hours: Time-to-live for messages in hours.
    """

    def __init__(self, workdir: Path, message_ttl_hours: int = 24) -> None:
        """Initialize bulletin board.

        Args:
            workdir: Project working directory.
            message_ttl_hours: Time-to-live for messages in hours.
        """
        self._workdir = workdir
        self._board_file = workdir / ".sdd" / "runtime" / "bulletin_board.jsonl"
        self._board_file.parent.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = message_ttl_hours * 3600
        self._messages: dict[str, BulletinMessage] = {}

        # Load existing messages
        self._load_messages()

    def post(
        self,
        sender_agent_id: str,
        sender_task_id: str,
        content: str,
        message_type: MessageType = "info",
        tags: list[str] | None = None,
        ttl_hours: int | None = None,
    ) -> BulletinMessage:
        """Post a message to the bulletin board.

        Args:
            sender_agent_id: ID of the sending agent.
            sender_task_id: ID of the sender's task.
            content: Message content.
            message_type: Type of message.
            tags: Optional tags for filtering.
            ttl_hours: Optional custom TTL in hours.

        Returns:
            Posted BulletinMessage.
        """
        import uuid

        now = time.time()
        ttl = (ttl_hours or (self._ttl_seconds / 3600)) * 3600

        message = BulletinMessage(
            id=str(uuid.uuid4())[:8],
            sender_agent_id=sender_agent_id,
            sender_task_id=sender_task_id,
            message_type=message_type,
            content=content,
            timestamp=now,
            tags=tags or [],
            expires_at=now + ttl,
        )

        self._messages[message.id] = message
        self._save_message(message)

        logger.info(
            "Agent %s posted %s message: %s",
            sender_agent_id,
            message_type,
            content[:50],
        )

        return message

    def get_messages(
        self,
        agent_id: str | None = None,
        message_type: MessageType | None = None,
        tags: list[str] | None = None,
        exclude_expired: bool = True,
    ) -> list[BulletinMessage]:
        """Get messages from the bulletin board.

        Args:
            agent_id: Filter by sender agent ID.
            message_type: Filter by message type.
            tags: Filter by tags (must have all specified tags).
            include_expired: Include expired messages.

        Returns:
            List of matching BulletinMessage instances.
        """
        messages: list[BulletinMessage] = []

        for message in self._messages.values():
            # Skip expired unless requested
            if exclude_expired and message.is_expired():
                continue

            # Apply filters
            if agent_id and message.sender_agent_id != agent_id:
                continue

            if message_type and message.message_type != message_type:
                continue

            if tags and not all(tag in message.tags for tag in tags):
                continue

            messages.append(message)

        # Sort by timestamp (newest first)
        messages.sort(key=lambda m: m.timestamp, reverse=True)

        return messages

    def get_relevant_messages(
        self,
        agent_id: str,
        task_keywords: list[str] | None = None,
    ) -> list[BulletinMessage]:
        """Get messages relevant to an agent's current task.

        Excludes messages from the same agent.
        Filters by keywords in content or tags.

        Args:
            agent_id: Current agent ID.
            task_keywords: Keywords from current task.

        Returns:
            List of relevant BulletinMessage instances.
        """
        messages = self.get_messages(exclude_expired=True)

        relevant: list[BulletinMessage] = []
        for message in messages:
            # Skip own messages
            if message.sender_agent_id == agent_id:
                continue

            # Check keyword match
            if task_keywords:
                content_lower = message.content.lower()
                tags_lower = [t.lower() for t in message.tags]
                if not any(kw.lower() in content_lower or kw.lower() in tags_lower for kw in task_keywords):
                    continue

            relevant.append(message)

        return relevant

    def query(
        self,
        *,
        scope: str | None = None,
        message_type: MessageType | None = None,
        min_confidence: float = 0.0,
        since: float | None = None,
        limit: int = 50,
    ) -> list[BulletinMessage]:
        """Query messages with filtering.

        Args:
            scope: Filter by file path (exact or prefix match).
            message_type: Filter by message type.
            min_confidence: Minimum confidence threshold.
            since: Only return messages after this timestamp.
            limit: Maximum number of messages to return.

        Returns:
            List of matching BulletinMessage instances, newest first.
        """
        results: list[BulletinMessage] = []

        for message in self._messages.values():
            if message.is_expired():
                continue

            if message_type and message.message_type != message_type:
                continue

            if message.confidence < min_confidence:
                continue

            if since and message.timestamp < since:
                continue

            if (
                scope
                and message.scope
                and not any(s == scope or s.startswith(scope + "/") or scope.startswith(s + "/") for s in message.scope)
            ):
                continue
            # Messages with no scope are considered global — included by default

            results.append(message)

        results.sort(key=lambda m: m.timestamp, reverse=True)
        return results[:limit]

    def get_relevant_for_task(self, context_files: list[str], role: str) -> list[BulletinMessage]:
        """Get messages relevant to a specific task based on file scope and role.

        Args:
            context_files: File paths the task works with.
            role: Role of the agent (used for tag matching).

        Returns:
            List of relevant BulletinMessage instances, newest first.
        """
        results: list[BulletinMessage] = []

        for message in self._messages.values():
            if message.is_expired():
                continue

            # Check role match in tags
            role_match = role.lower() in [t.lower() for t in message.tags]

            # Check scope overlap with context files
            scope_match = False
            if not message.scope:
                # No scope means global — always relevant
                scope_match = True
            else:
                for msg_scope in message.scope:
                    for ctx_file in context_files:
                        if (
                            msg_scope == ctx_file
                            or ctx_file.startswith(msg_scope + "/")
                            or msg_scope.startswith(ctx_file + "/")
                        ):
                            scope_match = True
                            break
                    if scope_match:
                        break

            if role_match or scope_match:
                results.append(message)

        results.sort(key=lambda m: m.timestamp, reverse=True)
        return results

    def apply_confidence_decay(self, decay_rate: float = 0.95, min_confidence: float = 0.1) -> None:
        """Decay confidence of all messages over time.

        Multiplies each message's confidence by decay_rate, flooring at
        min_confidence. Call this periodically (e.g., each orchestrator tick).

        Args:
            decay_rate: Multiplier applied to each message's confidence.
            min_confidence: Floor below which confidence will not drop.
        """
        for message in self._messages.values():
            new_conf = message.confidence * decay_rate
            message.confidence = max(new_conf, min_confidence)

    def cleanup_expired(self) -> int:
        """Clean up expired messages.

        Returns:
            Number of messages removed.
        """
        expired = [msg_id for msg_id, msg in self._messages.items() if msg.is_expired()]

        for msg_id in expired:
            del self._messages[msg_id]

        # Rewrite file without expired messages
        self._rewrite_board()

        if expired:
            logger.info("Cleaned up %d expired bulletin messages", len(expired))

        return len(expired)

    def _load_messages(self) -> None:
        """Load messages from file."""
        if not self._board_file.exists():
            return

        try:
            for line in self._board_file.read_text().splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                message = BulletinMessage.from_dict(data)
                if not message.is_expired():
                    self._messages[message.id] = message
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load bulletin board: %s", exc)

    def _save_message(self, message: BulletinMessage) -> None:
        """Append a message to the board file."""
        with self._board_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict()) + "\n")

    def _rewrite_board(self) -> None:
        """Rewrite the board file without expired messages."""
        lines: list[str] = []
        for message in self._messages.values():
            if not message.is_expired():
                lines.append(json.dumps(message.to_dict()) + "\n")

        self._board_file.write_text("".join(lines))
