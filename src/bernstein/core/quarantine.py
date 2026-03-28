"""Cross-run task quarantine — track repeatedly-failing tasks across Bernstein runs.

Tasks that fail ``QUARANTINE_THRESHOLD`` times are quarantined so the orchestrator
can skip them on future runs instead of burning tokens on known-bad work.

Quarantine state is persisted in ``.sdd/runtime/quarantine.json``.  Entries expire
automatically after ``QUARANTINE_EXPIRY_DAYS`` days so transient failures don't
permanently block a task.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

QUARANTINE_THRESHOLD = 3
"""Number of cross-run failures before a task is quarantined."""

QUARANTINE_EXPIRY_DAYS = 7
"""Days after which a quarantine entry is automatically expired."""


@dataclass
class QuarantineEntry:
    """A single quarantine record for a task.

    Attributes:
        task_title: Canonical task title (used as the lookup key).
        fail_count: Total number of failures recorded across runs.
        last_failure: ISO date string of the most recent failure (YYYY-MM-DD).
        reason: Human-readable reason for the most recent failure.
        action: What the orchestrator should do: "skip" or "decompose".
    """

    task_title: str
    fail_count: int
    last_failure: str
    reason: str
    action: Literal["skip", "decompose"] = "skip"


class QuarantineStore:
    """Persistent CRUD store for task quarantine entries.

    All mutations are immediately persisted to ``path`` so state survives
    across orchestrator restarts.

    Args:
        path: Full path to the quarantine JSON file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load(self) -> list[QuarantineEntry]:
        """Return all stored entries (including expired ones).

        Returns:
            List of QuarantineEntry objects, or empty list if the file
            does not exist or is unreadable.
        """
        if not self._path.exists():
            return []
        try:
            raw: list[dict[str, object]] = json.loads(self._path.read_text())
            return [
                QuarantineEntry(
                    task_title=str(item["task_title"]),
                    fail_count=int(str(item["fail_count"])),
                    last_failure=str(item["last_failure"]),
                    reason=str(item["reason"]),
                    action=item.get("action", "skip"),  # type: ignore[arg-type]
                )
                for item in raw
            ]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("quarantine: failed to load %s: %s", self._path, exc)
            return []

    def get_entry(self, task_title: str) -> QuarantineEntry | None:
        """Return the quarantine entry for *task_title*, or None.

        Expired entries are not returned.

        Args:
            task_title: Exact task title to look up.

        Returns:
            QuarantineEntry if found and not expired, else None.
        """
        for entry in self.load():
            if entry.task_title == task_title and not self._is_expired(entry):
                return entry
        return None

    def get_all(self) -> list[QuarantineEntry]:
        """Return only active (non-expired) quarantine entries.

        Returns:
            List of non-expired QuarantineEntry objects.
        """
        return [e for e in self.load() if not self._is_expired(e)]

    def is_quarantined(self, task_title: str) -> bool:
        """Return True if *task_title* is actively quarantined.

        A task is quarantined when its fail_count is at or above
        ``QUARANTINE_THRESHOLD`` and the entry has not expired.

        Args:
            task_title: Exact task title to check.

        Returns:
            True if the task should be skipped/handled specially.
        """
        entry = self.get_entry(task_title)
        if entry is None:
            return False
        return entry.fail_count >= QUARANTINE_THRESHOLD

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, entries: list[QuarantineEntry]) -> None:
        """Persist *entries* to the quarantine file.

        Creates parent directories as needed.

        Args:
            entries: Full list of entries to write (replaces current file).
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps([asdict(e) for e in entries], indent=2))

    def record_failure(self, task_title: str, reason: str) -> None:
        """Record a failure for *task_title*, incrementing its fail count.

        Creates a new entry if one does not exist.  Always updates
        ``last_failure`` to today and ``reason`` to the most recent failure.
        Persists immediately.

        Args:
            task_title: Title of the failed task.
            reason: Human-readable failure reason.
        """
        entries = self.load()
        today = date.today().isoformat()

        for entry in entries:
            if entry.task_title == task_title:
                entry.fail_count += 1
                entry.last_failure = today
                entry.reason = reason
                self.save(entries)
                logger.info(
                    "quarantine: task %r now has %d failure(s)",
                    task_title,
                    entry.fail_count,
                )
                return

        # First time seeing this task
        entries.append(
            QuarantineEntry(
                task_title=task_title,
                fail_count=1,
                last_failure=today,
                reason=reason,
                action="skip",
            )
        )
        self.save(entries)
        logger.debug("quarantine: started tracking failures for %r", task_title)

    def clear(self, task_title: str | None = None) -> None:
        """Remove quarantine entries.

        Args:
            task_title: If given, remove only the entry matching this title.
                If None, clear all entries.
        """
        if task_title is None:
            self.save([])
            logger.info("quarantine: cleared all entries")
            return

        entries = self.load()
        filtered = [e for e in entries if e.task_title != task_title]
        self.save(filtered)
        if len(filtered) < len(entries):
            logger.info("quarantine: cleared entry for %r", task_title)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _is_expired(entry: QuarantineEntry) -> bool:
        """Return True if the entry is older than QUARANTINE_EXPIRY_DAYS."""
        try:
            last = date.fromisoformat(entry.last_failure)
        except ValueError:
            return False
        return (date.today() - last) > timedelta(days=QUARANTINE_EXPIRY_DAYS)
