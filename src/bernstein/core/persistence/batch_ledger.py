"""Per-item checkpoint ledger for a flat batch run (issue #5126).

:class:`~bernstein.core.persistence.work_ledger.WorkLedger` resumes a task
GRAPH by replaying a hash chain. A batch is a different shape: a flat list of
entities processed one at a time, where the only questions are "have I already
done this one" and "how many have I done today". Replaying a graph answers
neither, and nothing else in the tree maps an entity id to its last success.

Two properties, and they are the same property:

**Resume.** An item recorded as done is skipped on the next pass, so a crash
costs the item in flight and nothing before it.

**The cap.** The daily cap is derived from the ledger's own entries, never from
a counter in memory. An in-memory count resets to zero on restart -- which is
exactly the moment an operator most wants the cap to hold, because a process
that keeps dying and retrying is the one that would blow through it. Reading
the count back from the log means "what happened today" has one source of truth
rather than a counter that can drift from the record describing it.

Append-only JSONL, hash-chained with the same
:func:`~bernstein.core.persistence.work_ledger.compute_entry_hash` primitive the
work ledger uses, so an entry cannot be edited or reordered without breaking
every hash after it. Reads tolerate a torn final line: a process killed
mid-append leaves a partial record, and refusing to load the whole ledger over
its last byte would turn a crash into an outage.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.work_ledger import compute_entry_hash
from bernstein.core.security.path_containment import contained_path

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

#: The chain's anchor. Matches the work ledger's genesis convention.
GENESIS_HASH = "0" * 64

#: Entry kind, so a reader can tell these apart from anything else appended
#: alongside them later.
ENTRY_KIND = "batch.item"

_LEDGER_FILENAME = "batch-items.jsonl"


class BatchLedgerError(RuntimeError):
    """Raised for unrecoverable batch-ledger read/write errors."""


class DailyCapReached(RuntimeError):
    """Raised when today's entries already meet the cap.

    A distinct type rather than a boolean return, because "the cap held" is not
    a failure of the item -- a caller that treats it as one would retry forever.

    Attributes:
        cap: The cap that held.
        done_today: Entries already recorded for today.
    """

    def __init__(self, cap: int, done_today: int) -> None:
        super().__init__(f"daily cap reached: {done_today} of {cap} recorded today")
        self.cap = cap
        self.done_today = done_today


@dataclass(frozen=True)
class BatchItemRecord:
    """One successfully processed item.

    Attributes:
        entity_id: The item's identity, as the caller names it.
        at:        Unix timestamp of the success.
        day:       ``YYYY-MM-DD`` in UTC, the bucket the daily cap counts.
        entry_hash: This entry's hash, chaining to its predecessor.
        prev_hash:  The predecessor's hash, or :data:`GENESIS_HASH` for the first.
        detail:    Caller-supplied payload, carried verbatim.
    """

    entity_id: str
    at: float
    day: str
    entry_hash: str
    prev_hash: str
    detail: dict[str, Any]


def _utc_day(at: float) -> str:
    """The UTC calendar day an instant falls in.

    UTC, not local time: a batch resumed in a different timezone -- or across a
    DST boundary -- must not get a second day's worth of budget for the same
    afternoon.
    """
    return datetime.fromtimestamp(at, tz=UTC).strftime("%Y-%m-%d")


class BatchLedger:
    """An append-only, hash-chained record of the items a batch has completed.

    One instance per process; the append is guarded in-process by a lock and is
    a single ``O_APPEND`` write, which is atomic on POSIX for a line under
    ``PIPE_BUF``.
    """

    def __init__(self, ledger_dir: Path, *, filename: str = _LEDGER_FILENAME) -> None:
        """Open (or create on first write) a ledger under *ledger_dir*.

        Args:
            ledger_dir: Directory holding the ledger file.
            filename: Ledger basename. Containment-checked against
                *ledger_dir*, so a symlinked ledger file cannot redirect an
                append outside it -- the same barrier the work ledger applies
                to its bucket.
        """
        self._dir = ledger_dir
        self._path = contained_path(ledger_dir, filename, label="batch ledger")
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """The ledger file. It need not exist yet."""
        return self._path

    # -- reading ------------------------------------------------------------

    def entries(self) -> list[BatchItemRecord]:
        """Every recorded item, in append order. Empty when the ledger is absent."""
        return list(self._iter_entries())

    def _iter_entries(self) -> Iterator[BatchItemRecord]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BatchLedgerError(f"cannot read batch ledger {self._path}: {exc}") from exc

        lines = raw.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Only the LAST line may be torn: a process killed mid-append
                # leaves a partial record, and refusing to load the ledger over
                # its final byte would turn a crash into an outage. A break in
                # the middle is corruption and is reported.
                if index == len(lines) - 1:
                    logger.warning("Dropping torn final line of %s", self._path)
                    return
                raise BatchLedgerError(f"corrupt entry at line {index + 1} of {self._path}") from None
            if not isinstance(row, dict):
                raise BatchLedgerError(f"entry at line {index + 1} of {self._path} is not an object")
            raw_detail = row.get("detail")
            detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
            yield BatchItemRecord(
                entity_id=str(row.get("entity_id", "")),
                at=float(row.get("at", 0.0)),
                day=str(row.get("day", "")),
                entry_hash=str(row.get("entry_hash", "")),
                prev_hash=str(row.get("prev_hash", GENESIS_HASH)),
                detail=detail,
            )

    def done_ids(self) -> set[str]:
        """Entity ids already recorded. The resume set."""
        return {entry.entity_id for entry in self._iter_entries()}

    def last_success(self, entity_id: str) -> float | None:
        """The most recent success instant for one entity, or None.

        The LAST occurrence wins: an entity re-processed after a policy change
        has two entries, and the question a caller asks is when it last
        succeeded, not when it first did.
        """
        found: float | None = None
        for entry in self._iter_entries():
            if entry.entity_id == entity_id:
                found = entry.at
        return found

    def pending(self, entity_ids: list[str]) -> list[str]:
        """The subset of *entity_ids* not yet recorded, in the order given.

        Order is preserved because a batch's order is often deliberate -- a
        priority queue, or a dependency-respecting sequence -- and returning a
        set would quietly discard it.
        """
        done = self.done_ids()
        return [entity_id for entity_id in entity_ids if entity_id not in done]

    def done_today(self, *, now: float | None = None) -> int:
        """How many entries fall in the current UTC day.

        Read from the ledger every time, never cached: the count has to survive
        a restart, and a cached one is the thing being replaced.
        """
        today = _utc_day(datetime.now(tz=UTC).timestamp() if now is None else now)
        return sum(1 for entry in self._iter_entries() if entry.day == today)

    def remaining_today(self, cap: int, *, now: float | None = None) -> int:
        """How many more items today's cap allows. Never negative.

        A cap lowered below what a previous run already did leaves this at 0
        rather than a negative budget: the cap is a ceiling on new work, and
        work already recorded cannot be un-done by changing the number.
        """
        if cap <= 0:
            return 0
        return max(0, cap - self.done_today(now=now))

    def head_hash(self) -> str:
        """The last entry's hash, or :data:`GENESIS_HASH` for an empty ledger."""
        head = GENESIS_HASH
        for entry in self._iter_entries():
            head = entry.entry_hash
        return head

    def verify(self) -> None:
        """Recompute the chain and raise on the first entry that does not match.

        Raises:
            BatchLedgerError: An entry's recorded hash is not the hash of its
                content and predecessor -- the ledger was edited or reordered.
        """
        prev = GENESIS_HASH
        for index, entry in enumerate(self._iter_entries(), start=1):
            expected = self._entry_hash(entry.entity_id, entry.at, entry.day, entry.detail, prev)
            if entry.prev_hash != prev:
                raise BatchLedgerError(f"entry {index} of {self._path} does not chain to its predecessor")
            if entry.entry_hash != expected:
                raise BatchLedgerError(f"entry {index} of {self._path} has a hash its content does not produce")
            prev = entry.entry_hash

    # -- writing ------------------------------------------------------------

    @staticmethod
    def _entry_hash(
        entity_id: str,
        at: float,
        day: str,
        detail: dict[str, Any],
        prev_hash: str,
    ) -> str:
        return compute_entry_hash(
            prev_hash=prev_hash,
            kind=ENTRY_KIND,
            task_id=entity_id,
            payload={"at": at, "day": day, "detail": detail},
        )

    def record(
        self,
        entity_id: str,
        *,
        at: float | None = None,
        detail: dict[str, Any] | None = None,
        cap: int = 0,
    ) -> BatchItemRecord:
        """Record one item as done, and return the entry written.

        Args:
            entity_id: The item's identity.
            at: Success instant; defaults to now.
            detail: Payload carried verbatim into the entry.
            cap: When positive, the daily cap this record must not exceed. The
                check and the append are under one lock, so two threads cannot
                both read ``cap - 1`` and both write.

        Raises:
            DailyCapReached: Today's entries already meet *cap*.
            BatchLedgerError: The ledger could not be appended to.
        """
        moment = datetime.now(tz=UTC).timestamp() if at is None else at
        day = _utc_day(moment)
        payload = dict(detail or {})

        with self._lock:
            if cap > 0:
                done = self.done_today(now=moment)
                if done >= cap:
                    raise DailyCapReached(cap=cap, done_today=done)
            prev = self.head_hash()
            entry = BatchItemRecord(
                entity_id=entity_id,
                at=moment,
                day=day,
                entry_hash=self._entry_hash(entity_id, moment, day, payload, prev),
                prev_hash=prev,
                detail=payload,
            )
            self._append(entry)
        return entry

    def _append(self, entry: BatchItemRecord) -> None:
        line = json.dumps(
            {
                "kind": ENTRY_KIND,
                "entity_id": entry.entity_id,
                "at": entry.at,
                "day": entry.day,
                "prev_hash": entry.prev_hash,
                "entry_hash": entry.entry_hash,
                "detail": entry.detail,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            # O_APPEND, and fsync before returning: the caller is about to treat
            # this item as done, and a record still in the page cache when the
            # box dies would have it processed twice -- the one thing the ledger
            # exists to prevent.
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise BatchLedgerError(f"cannot append to batch ledger {self._path}: {exc}") from exc


def compact(ledger: BatchLedger, *, keep_days: int, now: float | None = None) -> int:
    """Drop entries older than *keep_days* whole days, rewriting the chain.

    Returns the number of entries removed. The chain is RE-DERIVED from the
    surviving entries rather than carried over, because a chain with a hole in
    it verifies as tampered -- which is the correct reading of a file somebody
    edited, and the wrong one for a retention policy the operator asked for.

    A no-op when nothing is old enough, so a scheduled compaction on a fresh
    ledger does not rewrite a file for no reason.
    """
    if keep_days < 0:
        raise ValueError("keep_days must not be negative")
    moment = datetime.now(tz=UTC).timestamp() if now is None else now
    cutoff = moment - keep_days * 86400.0
    entries = ledger.entries()
    kept = [entry for entry in entries if entry.at >= cutoff]
    if len(kept) == len(entries):
        return 0

    prev = GENESIS_HASH
    lines: list[str] = []
    for entry in kept:
        entry_hash = BatchLedger._entry_hash(entry.entity_id, entry.at, entry.day, entry.detail, prev)
        lines.append(
            json.dumps(
                {
                    "kind": ENTRY_KIND,
                    "entity_id": entry.entity_id,
                    "at": entry.at,
                    "day": entry.day,
                    "prev_hash": prev,
                    "entry_hash": entry_hash,
                    "detail": entry.detail,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        prev = entry_hash

    body = "".join(f"{line}\n" for line in lines)
    # Scratch sibling and rename: truncating the real file would, if the process
    # died mid-write, lose every resume point at once -- turning a retention
    # sweep into a full reprocess.
    fd, tmp_name = tempfile.mkstemp(dir=ledger.path.parent, prefix=f"{ledger.path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, ledger.path)
    except OSError as exc:
        raise BatchLedgerError(f"cannot compact batch ledger {ledger.path}: {exc}") from exc
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return len(entries) - len(kept)


__all__ = [
    "ENTRY_KIND",
    "GENESIS_HASH",
    "BatchItemRecord",
    "BatchLedger",
    "BatchLedgerError",
    "DailyCapReached",
    "compact",
]
