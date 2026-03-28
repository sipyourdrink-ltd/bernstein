"""Append-only bulletin board for cross-agent communication.

The bulletin board is the shared communication channel between cells.
Agents post messages (alerts, blockers, findings, status updates, dependency
declarations) and read messages posted since their last check.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

MessageType = Literal["alert", "blocker", "finding", "status", "dependency"]


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
class BulletinBoard:
    """Append-only message log for cross-agent communication.

    Thread-safe. Messages are ordered by insertion time.  The board can
    be flushed to a JSONL file for persistence / debugging.
    """

    _messages: list[BulletinMessage] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def post(self, msg: BulletinMessage) -> BulletinMessage:
        """Append a message to the board.

        If the message timestamp is zero, the current time is used.

        Args:
            msg: Message to post.

        Returns:
            The stored message (with timestamp filled in).
        """
        if msg.timestamp == 0.0:
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
