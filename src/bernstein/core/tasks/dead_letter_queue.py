"""Dead letter queue for permanently failed tasks.

When a task exhausts its maximum retry count, it is moved to the DLQ
rather than silently dropped. The DLQ provides:

- Persistent storage (JSONL file in .sdd/runtime/)
- Querying by failure reason, role, or time range
- Manual replay to resubmit tasks for another attempt
- Metrics on permanent failure patterns

Usage::

    dlq = DeadLetterQueue(sdd_dir=Path(".sdd"))
    dlq.enqueue(task_id="T-001", reason="max retries exhausted", ...)
    entries = dlq.list_entries()
    dlq.replay(entry_id="...", client=httpx_client, server_url="...")
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

logger = logging.getLogger(__name__)


@dataclass
class DLQEntry:
    """A single dead letter queue entry.

    Attributes:
        id: Unique entry ID.
        task_id: Original task ID.
        title: Task title for display.
        role: Task role (e.g. ``"backend"``).
        reason: Why the task was permanently failed.
        retry_count: Number of retries attempted before DLQ.
        original_error: Last error message from the final attempt.
        created_at: Unix timestamp when the entry was created.
        metadata: Additional context (model, scope, etc.).
        replayed: Whether this entry has been replayed.
        replayed_at: Unix timestamp of replay, or 0.0.
    """

    id: str
    task_id: str
    title: str
    role: str
    reason: str
    retry_count: int = 0
    original_error: str = ""
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    replayed: bool = False
    replayed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary representation.
        """
        return {
            "id": self.id,
            "task_id": self.task_id,
            "title": self.title,
            "role": self.role,
            "reason": self.reason,
            "retry_count": self.retry_count,
            "original_error": self.original_error,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "replayed": self.replayed,
            "replayed_at": self.replayed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DLQEntry:
        """Deserialize from a dict.

        Args:
            data: Dictionary with DLQ entry fields.

        Returns:
            DLQEntry instance.
        """
        return cls(
            id=str(data.get("id", "")),
            task_id=str(data.get("task_id", "")),
            title=str(data.get("title", "")),
            role=str(data.get("role", "")),
            reason=str(data.get("reason", "")),
            retry_count=int(data.get("retry_count", 0)),
            original_error=str(data.get("original_error", "")),
            created_at=float(data.get("created_at", 0.0)),
            metadata=dict(data.get("metadata", {})),
            replayed=bool(data.get("replayed", False)),
            replayed_at=float(data.get("replayed_at", 0.0)),
        )


@dataclass(frozen=True)
class DLQStats:
    """Summary statistics for the dead letter queue.

    Attributes:
        total_entries: Total entries ever written.
        pending_entries: Entries not yet replayed.
        replayed_entries: Entries that have been replayed.
        by_role: Count of pending entries by role.
        by_reason: Count of pending entries by failure reason.
    """

    total_entries: int
    pending_entries: int
    replayed_entries: int
    by_role: dict[str, int]
    by_reason: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        """Serialize to JSON-compatible dict.

        Returns:
            Dictionary with stats.
        """
        return {
            "total_entries": self.total_entries,
            "pending_entries": self.pending_entries,
            "replayed_entries": self.replayed_entries,
            "by_role": self.by_role,
            "by_reason": self.by_reason,
        }


class DeadLetterQueue:
    """Persistent dead letter queue for permanently failed tasks.

    Stores entries in a JSONL file under ``.sdd/runtime/dlq.jsonl``.
    Thread-safe for single-process use (no cross-process locking).

    Args:
        sdd_dir: Path to the .sdd state directory.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._sdd_dir = sdd_dir
        self._dlq_path = sdd_dir / "runtime" / "dlq.jsonl"
        self._entries: list[DLQEntry] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Load entries from disk if not already loaded."""
        if self._loaded:
            return
        self._entries = []
        if self._dlq_path.exists():
            try:
                for line in self._dlq_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        self._entries.append(DLQEntry.from_dict(data))
                    except (json.JSONDecodeError, KeyError) as exc:
                        logger.debug("Skipping malformed DLQ line: %s", exc)
            except OSError as exc:
                logger.warning("Failed to read DLQ file: %s", exc)
        self._loaded = True

    def enqueue(
        self,
        *,
        task_id: str,
        title: str,
        role: str,
        reason: str,
        retry_count: int = 0,
        original_error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DLQEntry:
        """Add a permanently failed task to the dead letter queue.

        Args:
            task_id: Original task ID.
            title: Task title.
            role: Task role.
            reason: Why the task was permanently failed.
            retry_count: Number of retries before DLQ.
            original_error: Last error from the final attempt.
            metadata: Additional context.

        Returns:
            The created DLQ entry.
        """
        self._ensure_loaded()
        entry = DLQEntry(
            id=uuid.uuid4().hex[:16],
            task_id=task_id,
            title=title,
            role=role,
            reason=reason,
            retry_count=retry_count,
            original_error=original_error,
            metadata=metadata or {},
        )
        self._entries.append(entry)
        self._append_to_file(entry)

        logger.info(
            "DLQ: task %s (%s) added — %s (retries=%d)",
            task_id,
            title,
            reason,
            retry_count,
        )
        return entry

    def list_entries(
        self,
        *,
        pending_only: bool = False,
        role: str | None = None,
        limit: int = 100,
    ) -> list[DLQEntry]:
        """List DLQ entries with optional filters.

        Args:
            pending_only: If True, exclude replayed entries.
            role: Filter by task role.
            limit: Maximum entries to return.

        Returns:
            Filtered list of DLQ entries (newest first).
        """
        self._ensure_loaded()
        result = list(self._entries)
        if pending_only:
            result = [e for e in result if not e.replayed]
        if role is not None:
            result = [e for e in result if e.role == role]
        result.sort(key=lambda e: e.created_at, reverse=True)
        return result[:limit]

    def get_entry(self, entry_id: str) -> DLQEntry | None:
        """Retrieve a single DLQ entry by ID.

        Args:
            entry_id: DLQ entry ID.

        Returns:
            Matching entry or None.
        """
        self._ensure_loaded()
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def replay(
        self,
        entry_id: str,
        client: httpx.Client,
        server_url: str,
    ) -> bool:
        """Replay a DLQ entry by resubmitting the task to the server.

        Creates a new task with the same title, role, and description,
        and marks the DLQ entry as replayed.

        Args:
            entry_id: DLQ entry ID to replay.
            client: httpx client for server communication.
            server_url: Task server base URL.

        Returns:
            True if the task was successfully resubmitted.
        """
        self._ensure_loaded()
        entry = self.get_entry(entry_id)
        if entry is None:
            logger.warning("DLQ replay: entry %s not found", entry_id)
            return False
        if entry.replayed:
            logger.info("DLQ replay: entry %s already replayed", entry_id)
            return False

        task_payload: dict[str, Any] = {
            "title": entry.title,
            "role": entry.role,
            "priority": entry.metadata.get("priority", 2),
            "description": (
                f"Replayed from DLQ (original task: {entry.task_id}).\n"
                f"Previous failure: {entry.reason}\n"
                f"Error: {entry.original_error}"
            ),
        }
        # Forward scope/complexity if available
        if "scope" in entry.metadata:
            task_payload["scope"] = entry.metadata["scope"]
        if "complexity" in entry.metadata:
            task_payload["complexity"] = entry.metadata["complexity"]

        try:
            resp = client.post(f"{server_url}/tasks", json=task_payload)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("DLQ replay failed for %s: %s", entry_id, exc)
            return False

        entry.replayed = True
        entry.replayed_at = time.time()
        self._rewrite_file()

        logger.info("DLQ: replayed entry %s (task %s)", entry_id, entry.task_id)
        return True

    def stats(self) -> DLQStats:
        """Compute summary statistics for the DLQ.

        Returns:
            Aggregate statistics.
        """
        self._ensure_loaded()
        pending = [e for e in self._entries if not e.replayed]
        by_role: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        for entry in pending:
            by_role[entry.role] = by_role.get(entry.role, 0) + 1
            by_reason[entry.reason] = by_reason.get(entry.reason, 0) + 1

        return DLQStats(
            total_entries=len(self._entries),
            pending_entries=len(pending),
            replayed_entries=len(self._entries) - len(pending),
            by_role=by_role,
            by_reason=by_reason,
        )

    def _append_to_file(self, entry: DLQEntry) -> None:
        """Append a single entry to the JSONL file.

        Args:
            entry: Entry to append.
        """
        try:
            self._dlq_path.parent.mkdir(parents=True, exist_ok=True)
            with self._dlq_path.open("a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("Failed to write DLQ entry: %s", exc)

    def _rewrite_file(self) -> None:
        """Rewrite the entire JSONL file from memory."""
        try:
            self._dlq_path.parent.mkdir(parents=True, exist_ok=True)
            with self._dlq_path.open("w") as f:
                for entry in self._entries:
                    f.write(json.dumps(entry.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("Failed to rewrite DLQ file: %s", exc)
