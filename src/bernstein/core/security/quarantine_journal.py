"""Journaled quarantine record: append-only audit trail for init failures (#5108).

The :class:`~bernstein.core.security.quarantine.QuarantineStore` overwrites a
single JSON file on every mutation. That gives a point-in-time snapshot of the
current quarantine state but loses the history: when was an entry created, how
many times was the threshold crossed, when was it released?

This module is a thin wrapper over
:class:`~bernstein.core.persistence.work_ledger.WorkLedger` -- the append-only,
hash-chained JSONL primitive this codebase already has, already reviewed,
already used for the work ledger's own crash-safe replay. An earlier version
of this module rolled its own chain (hashing four enumerated fields rather
than the whole entry, with no monotonic index and a ``read()`` that silently
stopped at the first unparseable line, discarding everything after it without
reporting an error). Wrapping ``WorkLedger`` instead of maintaining a second,
weaker implementation fixes all three: ``WorkLedger.append`` hashes the entire
payload dict (so ``reason`` is covered along with every other field, present
or future, with no field list to keep in sync), each entry carries a
monotonic ``seq``, and :meth:`QuarantineJournal.verify` reports every
unparseable row as a named error and keeps reading past it rather than
stopping silently.

What this does **not** claim: a bare hash chain with no external anchor
cannot prove it is the *complete* chain -- deleting the tail leaves a shorter
chain that still verifies, because nothing outside the file says how many
entries there are supposed to be. That is a structural property of a hash
chain alone, not a defect specific to this wrapper or to ``WorkLedger``; an
anchored head (recorded in a run manifest, or chained into the audit_chain
HMAC store) is what closes it, and is out of scope here.

The store and journal are independent: a caller that only wants the current
snapshot keeps using :class:`~bernstein.core.security.quarantine.QuarantineStore`
alone.

Typical use::

    from pathlib import Path
    from bernstein.core.security.quarantine_journal import QuarantineJournal

    journal = QuarantineJournal(Path(".sdd/runtime/quarantine-journal"))
    journal.record_quarantined("flaky-init-agent", reason="failed 3 times")
    journal.record_released("flaky-init-agent", reason="operator reset")
    errors = journal.verify()   # [] on a valid chain
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from bernstein.core.persistence.work_ledger import (
    GENESIS_HASH,
    LedgerReader,
    WorkLedger,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.persistence.work_ledger import LedgerEntry

__all__ = ["GENESIS_HASH", "QuarantineJournal", "QuarantineJournalEntry"]

#: Event types the journal records. Stored as the ledger kind, namespaced
#: under ``quarantine.`` so it reads clearly alongside the ledger's own
#: ``run.``/``task.`` kinds without colliding with their reserved vocabulary.
QuarantineEventType = Literal["quarantined", "released", "expired"]

_KIND_PREFIX = "quarantine."


def _kind_for(event_type: QuarantineEventType) -> str:
    return f"{_KIND_PREFIX}{event_type}"


def _event_type_from_kind(kind: str) -> str:
    return kind.removeprefix(_KIND_PREFIX)


@dataclass(frozen=True, slots=True)
class QuarantineJournalEntry:
    """One quarantine state-transition event, adapted from the underlying ledger entry.

    Attributes:
        event_type: The transition: ``quarantined``, ``released``, or ``expired``.
        task_title: Canonical task title the transition concerns.
        timestamp: Caller-supplied ISO 8601 timestamp of the transition
            (distinct from the ledger's own wall-clock ``ts``, which is
            metadata about when the row was written, not the semantic
            transition time -- see :meth:`QuarantineJournal.record_quarantined`).
        reason: Human-readable reason for the transition.
        prev_hash: Entry hash of the immediately preceding entry, or
            :data:`GENESIS_HASH` for the first entry.
        entry_hash: Hash chain value of this entry, over the whole payload.
        seq: Monotonic sequence number -- the index a gap or truncation
            would break.
    """

    event_type: str
    task_title: str
    timestamp: str
    reason: str
    prev_hash: str
    entry_hash: str
    seq: int

    @classmethod
    def _from_ledger_entry(cls, entry: LedgerEntry) -> QuarantineJournalEntry:
        payload = entry.payload
        return cls(
            event_type=_event_type_from_kind(entry.kind),
            task_title=str(payload.get("task_title", "")),
            timestamp=str(payload.get("timestamp", "")),
            reason=str(payload.get("reason", "")),
            prev_hash=entry.prev_hash,
            entry_hash=entry.entry_hash,
            seq=entry.seq,
        )


class QuarantineJournal:
    """Append-only, hash-chained journal for quarantine state transitions.

    A thin wrapper over :class:`~bernstein.core.persistence.work_ledger.WorkLedger`;
    see the module docstring for why. Thread-safety and crash-safety are
    ``WorkLedger``'s, not reimplemented here.

    Args:
        ledger_dir: Directory the underlying ledger bucket file lives in.
            Created on first write.
    """

    def __init__(self, ledger_dir: Path) -> None:
        self._ledger_dir = ledger_dir
        self._ledger: WorkLedger | None = None

    def _writer(self) -> WorkLedger:
        # Opened lazily so constructing a QuarantineJournal never creates the
        # directory or bucket file for a caller that only ever reads.
        if self._ledger is None:
            self._ledger = WorkLedger.open(self._ledger_dir)
        return self._ledger

    @property
    def path(self) -> Path:
        """Path to the underlying ledger bucket file (public, for tests and tooling)."""
        return LedgerReader(self._ledger_dir).bucket_path

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self) -> list[QuarantineJournalEntry]:
        """Return all journal entries in append order.

        An unparseable row is skipped here (mirroring
        :meth:`~bernstein.core.persistence.work_ledger.LedgerReader.entries`'s
        crash tolerance) so a torn trailing line never prevents the rest of
        the journal from being read. :meth:`verify` is the path that reports
        such a row as an error instead of silently omitting it.

        Returns:
            List of :class:`QuarantineJournalEntry` objects, oldest first.
        """
        reader = LedgerReader(self._ledger_dir)
        return [QuarantineJournalEntry._from_ledger_entry(entry) for entry in reader.entries()]

    def verify(self) -> list[str]:
        """Verify the hash chain and return a list of error strings.

        Unlike :meth:`read`, an unparseable row is reported here rather than
        silently skipped, and scanning continues past it -- a single
        corrupted line does not hide every error after it.

        Returns:
            Empty list when the chain is intact; one error string per
            problem otherwise (a broken link, a hash mismatch, or an
            unparseable row).
        """
        return LedgerReader(self._ledger_dir).verify().errors

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def _record(
        self, event_type: QuarantineEventType, task_title: str, timestamp: str, reason: str
    ) -> QuarantineJournalEntry:
        if not timestamp:
            from datetime import UTC, datetime

            timestamp = datetime.now(tz=UTC).isoformat()
        entry = self._writer().append(
            kind=_kind_for(event_type),
            payload={"task_title": task_title, "timestamp": timestamp, "reason": reason},
        )
        return QuarantineJournalEntry._from_ledger_entry(entry)

    def record_quarantined(self, task_title: str, *, reason: str, timestamp: str = "") -> QuarantineJournalEntry:
        """Append a ``quarantined`` event for *task_title*.

        Args:
            task_title: Title of the task being quarantined.
            reason: Human-readable reason (e.g. ``"failed 3 times"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`QuarantineJournalEntry`.
        """
        return self._record("quarantined", task_title, timestamp, reason)

    def record_released(self, task_title: str, *, reason: str, timestamp: str = "") -> QuarantineJournalEntry:
        """Append a ``released`` event for *task_title*.

        Args:
            task_title: Title of the task being released from quarantine.
            reason: Human-readable reason (e.g. ``"operator reset"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`QuarantineJournalEntry`.
        """
        return self._record("released", task_title, timestamp, reason)

    def record_expired(self, task_title: str, *, reason: str, timestamp: str = "") -> QuarantineJournalEntry:
        """Append an ``expired`` event for *task_title*.

        Args:
            task_title: Title of the task whose quarantine entry expired.
            reason: Human-readable reason (e.g. ``"7-day TTL elapsed"``).
            timestamp: ISO 8601 timestamp; auto-generated if empty.

        Returns:
            The appended :class:`QuarantineJournalEntry`.
        """
        return self._record("expired", task_title, timestamp, reason)
