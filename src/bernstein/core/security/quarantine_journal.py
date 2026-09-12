"""Journaled quarantine record: append-only audit trail for init failures (#5108).

The :class:`~bernstein.core.security.quarantine.QuarantineStore` overwrites a
single JSON file on every mutation.  That gives a point-in-time snapshot of
the current quarantine state but loses the history: when was an entry
created, how many times was the threshold crossed, when was it released?

This module provides a complementary, append-only JSONL journal that sits next
to the QuarantineStore snapshot file.  Each journal entry captures a single
state transition (``quarantined``, ``released``, or ``expired``) with a
timestamp, a reason, and a hash chain linking it to its predecessor.  The
chain makes truncation detectable: a reader that sees an entry whose
``prev_hash`` does not match the previous entry's ``entry_hash`` knows the
journal has been tampered with or truncated.

The journal is write-only for normal operation; :meth:`QuarantineJournal.verify`
is the audit path.  The store and journal are independent: a caller that only
wants the current snapshot keeps using
:class:`~bernstein.core.security.quarantine.QuarantineStore` alone.

Typical use::

    from pathlib import Path
    from bernstein.core.security.quarantine_journal import QuarantineJournal

    journal = QuarantineJournal(Path(".sdd/runtime/quarantine-journal.jsonl"))
    journal.record_quarantined("flaky-init-agent", reason="failed 3 times")
    journal.record_released("flaky-init-agent", reason="operator reset")
    errors = journal.verify()   # [] on a valid chain
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

#: Event types the journal records.
QuarantineEventType = Literal["quarantined", "released", "expired"]

#: Sentinel prev_hash for the first journal entry.
GENESIS_HASH: str = "0" * 64


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _compute_entry_hash(prev_hash: str, event_type: str, task_title: str, timestamp: str) -> str:
    """Derive the chain hash for one journal entry."""
    payload = _canonical(
        {
            "event_type": event_type,
            "prev_hash": prev_hash,
            "task_title": task_title,
            "timestamp": timestamp,
        }
    )
    return _sha256_hex(payload)


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One quarantine state-transition event in the journal.

    Attributes:
        event_type: The transition: ``quarantined``, ``released``, or ``expired``.
        task_title: Canonical task title the transition concerns.
        timestamp: ISO 8601 timestamp of the transition.
        reason: Human-readable reason for the transition.
        prev_hash: Entry hash of the immediately preceding entry, or
            :data:`GENESIS_HASH` for the first entry.
        entry_hash: SHA-256 chain hash of this entry's payload.
    """

    event_type: QuarantineEventType
    task_title: str
    timestamp: str
    reason: str
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of this entry."""
        return {
            "event_type": self.event_type,
            "task_title": self.task_title,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JournalEntry:
        """Reconstruct a :class:`JournalEntry` from a parsed JSON object."""
        return cls(
            event_type=data["event_type"],
            task_title=str(data["task_title"]),
            timestamp=str(data["timestamp"]),
            reason=str(data.get("reason", "")),
            prev_hash=str(data["prev_hash"]),
            entry_hash=str(data["entry_hash"]),
        )


class QuarantineJournal:
    """Append-only, hash-chained JSONL journal for quarantine state transitions.

    Thread-safe: a per-instance lock serialises concurrent appends.

    Args:
        path: Path to the JSONL journal file.  Created on first write.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self) -> list[JournalEntry]:
        """Return all journal entries in append order.

        A torn trailing line (write interrupted mid-byte) is silently skipped
        so a crash during an append never prevents the journal from being read.

        Returns:
            List of :class:`JournalEntry` objects, oldest first.
        """
        if not self._path.exists():
            return []
        entries: list[JournalEntry] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(JournalEntry.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                break
        return entries

    def verify(self) -> list[str]:
        """Verify the hash chain and return a list of error strings.

        Returns:
            Empty list when the chain is intact; one error string per
            broken link otherwise.
        """
        entries = self.read()
        errors: list[str] = []
        prev_hash = GENESIS_HASH
        for index, entry in enumerate(entries):
            expected = _compute_entry_hash(prev_hash, entry.event_type, entry.task_title, entry.timestamp)
            if entry.prev_hash != prev_hash:
                errors.append(f"entry[{index}]: prev_hash mismatch (expected {prev_hash!r}, got {entry.prev_hash!r})")
            if entry.entry_hash != expected:
                errors.append(f"entry[{index}]: entry_hash mismatch (expected {expected!r}, got {entry.entry_hash!r})")
            prev_hash = entry.entry_hash
        return errors

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def _append(self, event_type: QuarantineEventType, task_title: str, timestamp: str, reason: str) -> JournalEntry:
        with self._lock:
            existing = self.read()
            prev_hash = existing[-1].entry_hash if existing else GENESIS_HASH
            entry_hash = _compute_entry_hash(prev_hash, event_type, task_title, timestamp)
            entry = JournalEntry(
                event_type=event_type,
                task_title=task_title,
                timestamp=timestamp,
                reason=reason,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True, separators=(",", ":")) + "\n")
        return entry

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def record_quarantined(self, task_title: str, *, reason: str, timestamp: str = "") -> JournalEntry:
        """Append a ``quarantined`` event for *task_title*.

        Args:
            task_title: Title of the task being quarantined.
            reason: Human-readable reason (e.g. ``"failed 3 times"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`JournalEntry`.
        """
        if not timestamp:
            from datetime import UTC, datetime

            timestamp = datetime.now(tz=UTC).isoformat()
        return self._append("quarantined", task_title, timestamp, reason)

    def record_released(self, task_title: str, *, reason: str, timestamp: str = "") -> JournalEntry:
        """Append a ``released`` event for *task_title*.

        Args:
            task_title: Title of the task being released from quarantine.
            reason: Human-readable reason (e.g. ``"operator reset"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`JournalEntry`.
        """
        if not timestamp:
            from datetime import UTC, datetime

            timestamp = datetime.now(tz=UTC).isoformat()
        return self._append("released", task_title, timestamp, reason)

    def record_expired(self, task_title: str, *, reason: str, timestamp: str = "") -> JournalEntry:
        """Append an ``expired`` event for *task_title*.

        Args:
            task_title: Title of the task whose quarantine entry expired.
            reason: Human-readable reason (e.g. ``"7-day TTL elapsed"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`JournalEntry`.
        """
        if not timestamp:
            from datetime import UTC, datetime

            timestamp = datetime.now(tz=UTC).isoformat()
        return self._append("expired", task_title, timestamp, reason)
