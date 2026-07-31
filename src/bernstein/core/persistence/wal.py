"""Write-Ahead Log (WAL) for orchestrator decisions.

Provides crash-safe durability and execution fingerprinting for the
Bernstein orchestrator. Every orchestrator decision is appended to a
hash-chained JSONL file before the action executes.

Storage: .sdd/runtime/wal/<run-id>.wal.jsonl

Features:
- Hash-chained JSONL entries (tamper-evident, integrity-verifiable)
- fsync per entry (crash-safe durability guarantee)
- Execution fingerprinting (determinism proof across runs)
- Crash recovery via uncommitted entry detection
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinel prev_hash for the first entry in a WAL.
GENESIS_HASH: str = "0" * 64


class WALIntegrityError(Exception):
    """Raised when WAL hash chain integrity is violated."""


@dataclass(frozen=True)
class WALEntry:
    """A single WAL entry representing one orchestrator decision.

    All fields are immutable. ``committed=False`` signals that the
    corresponding action had not yet been confirmed when this entry
    was written - useful for crash-recovery inspection.
    """

    seq: int
    prev_hash: str
    entry_hash: str
    timestamp: float
    decision_type: str
    inputs: dict[str, Any]
    output: dict[str, Any]
    actor: str
    committed: bool = True


def _compute_entry_hash(payload: dict[str, Any]) -> str:
    """Return SHA-256 of the canonical JSON of *payload*.

    *payload* must NOT contain the ``entry_hash`` key - the hash is
    computed over all other fields so it can be stored alongside them.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# UncommittedIndex
# ---------------------------------------------------------------------------


class UncommittedIndex:
    """Sidecar index of uncommitted WAL entries across all runs.

    Without this index, :meth:`WALRecovery.scan_all_uncommitted` must read
    and JSON-parse every line of every ``*.wal.jsonl`` file on startup.
    With 200 runs x 500 entries that is 100 000 JSON parses per boot.

    The index is a JSONL file at ``.sdd/runtime/wal/uncommitted.idx.json``
    holding one row per uncommitted entry::

        {"run_id": "r-1", "seq": 3, "entry_hash": "ab12..."}

    The index is a *secondary cache*: if it is missing, truncated, or
    otherwise corrupt, callers must fall back to a full WAL scan and
    rebuild the index from the scan result. Loss of the index therefore
    only costs one slow boot - never correctness.

    All mutating operations ``fsync`` the file so a crash cannot leave
    the on-disk form diverging from the in-process state.
    """

    _FILENAME = "uncommitted.idx.json"

    def __init__(self, sdd_dir: Path) -> None:
        self._path = sdd_dir / "runtime" / "wal" / self._FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """Return the on-disk path of the index file."""
        return self._path

    # ------------------------------------------------------------------
    # Load / persist
    # ------------------------------------------------------------------

    def load(self) -> list[tuple[str, int, str]]:
        """Return every indexed ``(run_id, seq, entry_hash)`` tuple.

        Returns an empty list when the index file does not exist.

        Raises:
            ValueError: When the index file exists but is malformed.
                Callers that want to fall back to a full scan should
                catch this and trigger a rebuild.
        """
        if not self._path.exists():
            return []

        rows: list[tuple[str, int, str]] = []
        try:
            text = self._path.read_text()
        except OSError as exc:
            raise ValueError(f"uncommitted index unreadable: {exc}") from exc

        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                run_id = str(data["run_id"])
                seq = int(data["seq"])
                entry_hash = str(data["entry_hash"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"uncommitted index corrupt at line {lineno}: {exc}") from exc
            rows.append((run_id, seq, entry_hash))
        return rows

    def _write_all(self, rows: list[tuple[str, int, str]]) -> None:
        """Atomically rewrite the index with *rows* (fsync guaranteed)."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w") as f:
            for run_id, seq, entry_hash in rows:
                f.write(
                    json.dumps(
                        {"run_id": run_id, "seq": seq, "entry_hash": entry_hash},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(self, run_id: str, seq: int, entry_hash: str) -> None:
        """Append ``(run_id, seq, entry_hash)`` to the index.

        Duplicates are allowed on disk - :meth:`load` is tolerant of them
        as long as each row is individually well-formed.  Callers that
        care about uniqueness should use :meth:`remove` before re-adding.
        """
        with self._path.open("a") as f:
            f.write(
                json.dumps(
                    {"run_id": run_id, "seq": seq, "entry_hash": entry_hash},
                    separators=(",", ":"),
                )
                + "\n"
            )
            f.flush()
            os.fsync(f.fileno())

    def remove(self, run_id: str, seq: int) -> bool:
        """Remove every row matching ``(run_id, seq)`` from the index.

        Returns ``True`` when at least one row was removed.  Missing or
        corrupt indexes are treated as empty (no rows removed, no error).
        """
        try:
            rows = self.load()
        except ValueError:
            # Corrupt index: nothing to remove, let the next scan rebuild.
            return False
        kept = [r for r in rows if not (r[0] == run_id and r[1] == seq)]
        if len(kept) == len(rows):
            return False
        self._write_all(kept)
        return True

    def remove_run(self, run_id: str) -> int:
        """Remove every row whose ``run_id`` matches *run_id*.

        Returns the number of rows removed.  Called after a run's WAL is
        closed so that subsequent scans are not slowed by
        stale rows pointing at an already-recovered WAL.
        """
        try:
            rows = self.load()
        except ValueError:
            return 0
        kept = [r for r in rows if r[0] != run_id]
        removed = len(rows) - len(kept)
        if removed:
            self._write_all(kept)
        return removed

    def rebuild(self, rows: list[tuple[str, int, str]]) -> None:
        """Replace the index with *rows* (used after a fallback scan)."""
        self._write_all(rows)


# ---------------------------------------------------------------------------
# WALWriter
# ---------------------------------------------------------------------------


class WALWriter:
    """Append-only WAL writer with hash chaining and per-entry fsync.

    Each call to :meth:`append` writes a JSON line, fsyncs the file, and
    returns the completed :class:`WALEntry`. The hash chain starts from
    :data:`GENESIS_HASH` (all zeros) for a new WAL, or resumes from the
    last recorded ``entry_hash`` when continuing an existing WAL.
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._run_id = run_id
        self._sdd_dir = sdd_dir
        self._path = sdd_dir / "runtime" / "wal" / f"{run_id}.wal.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._seq, self._prev_hash = self._load_tail()
        # Sidecar index of uncommitted entries. Lazily instantiated
        # so tests that only exercise the reader do not create the index file.
        self._index: UncommittedIndex | None = None

    def _uncommitted_index(self) -> UncommittedIndex:
        """Return a lazily-instantiated :class:`UncommittedIndex`."""
        if self._index is None:
            self._index = UncommittedIndex(self._sdd_dir)
        return self._index

    def _load_tail(self) -> tuple[int, str]:
        """Return (last_seq, last_entry_hash) from an existing WAL file.

        Returns (-1, GENESIS_HASH) for a new or empty WAL.

        Implementation: seeks to end and reads backward in
        fixed-size chunks until a complete non-empty line is recovered
        (or the start of the file is reached). Avoids an O(N) full-file
        read on every construction of a ``WALWriter``.

        When the trailing line is torn (a write interrupted by a crash),
        the file is rescanned for the last self-consistent entry and the
        chain resumes from it - see :meth:`_scan_last_valid_entry`.
        """
        if not self._path.exists():
            return -1, GENESIS_HASH

        last_line = self._read_last_nonempty_line()
        if last_line is None:
            return -1, GENESIS_HASH

        try:
            data = json.loads(last_line)
            return int(data["seq"]), str(data["entry_hash"])
        except (KeyError, ValueError):
            logger.warning("WAL tail unreadable at %s; resuming from the last valid entry", self._path)
            return self._scan_last_valid_entry()

    def _scan_last_valid_entry(self) -> tuple[int, str]:
        """Return (seq, entry_hash) of the last self-consistent WAL line.

        Used when the trailing line is torn. Resuming from
        :data:`GENESIS_HASH` instead would make the next ``append`` chain
        off genesis rather than off its real predecessor, forking the
        hash chain at the truncation point. Only entries whose stored
        ``entry_hash`` matches the SHA-256 of their own payload are
        eligible, so a partially-written line can never become the anchor.

        Returns ``(-1, GENESIS_HASH)`` when no valid entry exists.
        """
        last_seq = -1
        last_hash = GENESIS_HASH
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        seq = int(data["seq"])
                        entry_hash = str(data["entry_hash"])
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                    payload = {k: v for k, v in data.items() if k != "entry_hash"}
                    if _compute_entry_hash(payload) != entry_hash:
                        continue
                    last_seq, last_hash = seq, entry_hash
        except OSError:
            return -1, GENESIS_HASH
        return last_seq, last_hash

    def _read_last_nonempty_line(self, chunk_size: int = 4096) -> str | None:
        """Return the last non-empty line of the WAL via backward seeking.

        Reads chunks from the end of the file until a newline precedes a
        non-empty trailing segment. Handles files that end mid-line (no
        trailing ``\\n``) by treating the unterminated tail as a candidate
        line. Returns ``None`` for an empty or whitespace-only file.
        """
        try:
            with self._path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                file_size = f.tell()
                if file_size == 0:
                    return None

                buffer = b""
                pos = file_size
                # Read chunks backward until we have at least one full line
                # (i.e. a newline before the accumulated buffer) or reach
                # the start of the file.
                while pos > 0:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    buffer = f.read(read_size) + buffer

                    # Strip trailing newlines/whitespace so we can look for
                    # the newline that *precedes* the last non-empty line.
                    stripped = buffer.rstrip(b"\r\n \t")
                    if not stripped:
                        # Entire tail so far is whitespace - keep reading back.
                        continue
                    nl = stripped.rfind(b"\n")
                    if nl != -1:
                        candidate = stripped[nl + 1 :]
                        text = candidate.decode("utf-8", errors="replace").strip()
                        return text or None
                    # No newline in what we've read yet - if we're already
                    # at the file start, the whole buffer is one line.
                    if pos == 0:
                        text = stripped.decode("utf-8", errors="replace").strip()
                        return text or None
        except OSError:
            return None
        return None

    def write_entry(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        actor: str,
        committed: bool = True,
    ) -> WALEntry:
        """Convenience alias for :meth:`append`."""
        return self.append(
            decision_type=decision_type,
            inputs=inputs,
            output=output,
            actor=actor,
            committed=committed,
        )

    def append(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
        actor: str,
        committed: bool = True,
    ) -> WALEntry:
        """Append a decision entry to the WAL.

        The file is fsynced before returning, guaranteeing durability even
        if the process crashes immediately after this call returns.

        Args:
            decision_type: Short label for the decision (e.g. "task_created").
            inputs: Inputs to the decision (must be JSON-serializable).
            output: Result of the decision (must be JSON-serializable).
            actor: Identity of the orchestrator component writing this entry.
            committed: ``True`` (default) if the action has been executed;
                ``False`` to mark a pre-execution intent for crash recovery.

        Returns:
            The completed, hash-chained :class:`WALEntry`.
        """
        seq = self._seq + 1
        timestamp = time.time()

        payload: dict[str, Any] = {
            "seq": seq,
            "prev_hash": self._prev_hash,
            "timestamp": timestamp,
            "decision_type": decision_type,
            "inputs": inputs,
            "output": output,
            "actor": actor,
            "committed": committed,
        }
        entry_hash = _compute_entry_hash(payload)

        record = payload | {"entry_hash": entry_hash}
        # Capture the pre-write length so a failed write/flush/fsync can be
        # rolled back. Without the rollback a durable line can outlive a
        # failed fsync while self._seq/_prev_hash stay unadvanced, and the
        # caller's retry then reuses this seq - two lines with the same seq
        # and a permanently inconsistent chain.
        try:
            pre_write_size = self._path.stat().st_size
        except OSError:
            pre_write_size = 0
        try:
            with self._path.open("a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            self._rollback_partial_append(pre_write_size)
            raise

        # update the sidecar index after the WAL line has been
        # durably written. Index corruption only degrades startup speed
        # (scan_all_uncommitted falls back to a full scan and rebuilds)
        # so we swallow the error rather than failing the append.
        if not committed:
            try:
                self._uncommitted_index().add(self._run_id, seq, entry_hash)
            except OSError:
                logger.warning("uncommitted index add failed; will rebuild on next scan", exc_info=True)

        entry = WALEntry(
            seq=seq,
            prev_hash=self._prev_hash,
            entry_hash=entry_hash,
            timestamp=timestamp,
            decision_type=decision_type,
            inputs=inputs,
            output=output,
            actor=actor,
            committed=committed,
        )
        self._seq = seq
        self._prev_hash = entry_hash
        return entry

    def _rollback_partial_append(self, size: int) -> None:
        """Truncate the WAL back to *size* after a failed append.

        Restores the on-disk state the writer's in-memory ``_seq`` and
        ``_prev_hash`` still describe, so a retry re-appends the same
        entry instead of adding a duplicate ``seq``. Best-effort: a
        failure here is logged, never raised, so the caller still sees
        the original write error.
        """
        try:
            with self._path.open("r+b") as f:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            logger.warning("WAL rollback after failed append did not complete at %s", self._path, exc_info=True)

    def mark_committed(self, seq: int) -> bool:
        """Remove ``(run_id, seq)`` from the uncommitted index.

        The hash-chained WAL is append-only, so the on-disk entry itself
        cannot be mutated from ``committed=False`` to ``committed=True``
        retroactively.  This method only updates the sidecar index used
        by :meth:`WALRecovery.scan_all_uncommitted` - it signals "a
        follow-up committed entry has been written, stop reporting this
        seq as uncommitted on boot".

        Returns ``True`` when a matching index row was removed, ``False``
        when the index had no such row (e.g. the seq was already
        committed, the index was rebuilt, or the entry was never written
        with ``committed=False``).
        """
        try:
            return self._uncommitted_index().remove(self._run_id, seq)
        except OSError:
            logger.warning("uncommitted index remove failed; will rebuild on next scan", exc_info=True)
            return False


# ---------------------------------------------------------------------------
# WALReader
# ---------------------------------------------------------------------------


class WALReader:
    """Read and verify a WAL file written by :class:`WALWriter`."""

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._path = sdd_dir / "runtime" / "wal" / f"{run_id}.wal.jsonl"

    def iter_entries(self) -> Iterator[WALEntry]:
        """Yield all :class:`WALEntry` objects in write order.

        Streams the WAL file line-by-line: entries are parsed
        lazily so memory usage is O(1) in the WAL size. A malformed
        trailing line (e.g. torn write after a crash) is logged and
        skipped rather than aborting the iteration.

        Raises:
            FileNotFoundError: If the WAL file does not exist.
        """
        for _, entry in self._iter_parsed():
            yield entry

    def _iter_parsed(self) -> Iterator[tuple[dict[str, Any], WALEntry]]:
        """Yield ``(raw_record, entry)`` for every well-formed WAL line.

        The raw record is handed back alongside the parsed entry so
        callers that need to recompute ``entry_hash`` verify it against
        the bytes as written rather than a re-serialised approximation.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"WAL file not found: {self._path}")

        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("WAL line unparseable at %s; skipping", self._path)
                    continue
                # Tampered or torn-write lines may parse as JSON but be
                # missing required fields. Catch the lookup/cast errors
                # and skip - verify_chain() reports a chain break via
                # the integrity hash anyway, so we should not crash the
                # iterator when a downstream caller (e.g. lineage
                # verifier) walks a corrupted WAL.
                try:
                    entry = WALEntry(
                        seq=int(data["seq"]),
                        prev_hash=str(data["prev_hash"]),
                        entry_hash=str(data["entry_hash"]),
                        timestamp=float(data["timestamp"]),
                        decision_type=str(data["decision_type"]),
                        inputs=dict(data["inputs"]),
                        output=dict(data["output"]),
                        actor=str(data["actor"]),
                        committed=bool(data.get("committed", True)),
                    )
                except (KeyError, TypeError, ValueError):
                    logger.warning("WAL line missing/malformed fields at %s; skipping", self._path)
                    continue
                yield data, entry

    def iter_verified_entries(self) -> Iterator[WALEntry]:
        """Yield only the entries whose hash chain checks out.

        Applies the same two checks as :meth:`verify_chain` - the stored
        ``entry_hash`` must equal the SHA-256 of the entry's own payload,
        and ``prev_hash`` must equal the preceding entry's ``entry_hash``
        - and drops any entry that fails either one.

        Callers that act on WAL contents without a separate
        :meth:`verify_chain` pass (crash recovery, orphan reclamation)
        use this so that a corrupted or foreign WAL cannot drive real
        actions. Entries are still streamed one at a time, and a single
        bad entry does not disqualify the ones that follow it - the
        running hash advances using the stored value, matching
        :meth:`verify_chain`.

        Raises:
            FileNotFoundError: If the WAL file does not exist.
        """
        prev_hash = GENESIS_HASH
        for data, entry in self._iter_parsed():
            payload = {k: v for k, v in data.items() if k != "entry_hash"}
            hash_ok = entry.entry_hash == _compute_entry_hash(payload)
            linked = entry.prev_hash == prev_hash
            prev_hash = entry.entry_hash
            if not hash_ok or not linked:
                logger.warning(
                    "WAL entry at %s seq %s failed chain verification; not trusted",
                    self._path,
                    entry.seq,
                )
                continue
            yield entry

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Verify hash chain integrity of the entire WAL.

        Checks that:
        1. Each entry's ``prev_hash`` equals the previous entry's ``entry_hash``.
        2. Each entry's ``entry_hash`` matches the SHA-256 of its payload.

        Streams the WAL line-by-line: only the running
        ``prev_hash`` and the collected error list are held in memory,
        so verification is O(1) in working set regardless of WAL size.

        Returns:
            ``(True, [])`` if the chain is intact; ``(False, errors)`` otherwise.

        Raises:
            FileNotFoundError: If the WAL file does not exist.
        """
        if not self._path.exists():
            raise FileNotFoundError(f"WAL file not found: {self._path}")

        errors: list[str] = []
        prev_hash = GENESIS_HASH

        with self._path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"Invalid JSON (seq unknown): {exc}")
                    continue

                seq = data.get("seq", "?")
                stored_hash = str(data.get("entry_hash", ""))

                # Check prev_hash linkage
                if data.get("prev_hash") != prev_hash:
                    errors.append(
                        f"Chain broken at seq {seq}: "
                        f"expected prev_hash {prev_hash[:8]}..., "
                        f"got {str(data.get('prev_hash', ''))[:8]}..."
                    )

                # Recompute entry_hash from payload (exclude the stored entry_hash)
                payload = {k: v for k, v in data.items() if k != "entry_hash"}
                expected_hash = _compute_entry_hash(payload)

                if stored_hash != expected_hash:
                    errors.append(
                        f"Hash mismatch at seq {seq}: expected {expected_hash[:8]}..., got {stored_hash[:8]}..."
                    )

                # Advance prev_hash using stored value to detect cascading errors
                prev_hash = stored_hash

        return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# WALRecovery
# ---------------------------------------------------------------------------


class WALRecovery:
    """Crash recovery helper: find entries not yet committed at crash time.

    Usage pattern for crash-safe orchestration::

        # Before executing action:
        entry = writer.append(..., committed=False)
        # Execute action
        writer.append(..., committed=True)  # or a commit marker

        # On restart:
        recovery = WALRecovery(run_id, sdd_dir)
        for entry in recovery.get_uncommitted_entries():
            # re-execute or quarantine
            ...
        WALRecovery.close_wal(run_id, sdd_dir, reason="recovered")

    Once ``close_wal`` has been called, subsequent scans (via
    :meth:`scan_all_uncommitted` / :meth:`find_orphaned_claims`) skip the
    WAL so the same uncommitted entries are not re-reported forever
    .
    """

    def __init__(self, run_id: str, sdd_dir: Path) -> None:
        self._reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)

    def get_uncommitted_entries(self) -> list[WALEntry]:
        """Return all entries with ``committed=False``.

        Only chain-verified entries are returned: recovery acts on these
        entries, so an entry whose ``entry_hash`` or ``prev_hash`` does
        not check out must not reach the caller.

        Returns an empty list if the WAL file does not exist (fresh start).
        """
        try:
            return [e for e in self._reader.iter_verified_entries() if not e.committed]
        except FileNotFoundError:
            return []

    # ------------------------------------------------------------------
    # Closed-WAL sidecar marker
    # ------------------------------------------------------------------

    @staticmethod
    def _closed_marker_path(run_id: str, sdd_dir: Path) -> Path:
        """Return the ``.closed`` sidecar marker path for *run_id*."""
        return sdd_dir / "runtime" / "wal" / f"{run_id}.wal.closed"

    @staticmethod
    def is_wal_closed(run_id: str, sdd_dir: Path) -> bool:
        """Return True when a ``.closed`` marker exists for *run_id*.

        A closed marker signals that a previous recovery cycle has
        already observed and handled every uncommitted entry in the
        corresponding WAL - future scans must skip it to prevent
        unbounded re-scanning of the same entries.
        """
        return WALRecovery._closed_marker_path(run_id, sdd_dir).exists()

    @staticmethod
    def close_wal(
        run_id: str,
        sdd_dir: Path,
        *,
        reason: str = "recovered",
        uncommitted_count: int = 0,
        orphaned_count: int = 0,
    ) -> Path:
        """Write a ``.closed`` sidecar marker next to ``{run_id}.wal.jsonl``.

        After this call, :meth:`scan_all_uncommitted` and
        :meth:`find_orphaned_claims` will skip ``run_id`` on every
        subsequent invocation.  The marker is a small JSON document
        recording when and why the WAL was closed so operators can audit
        recovery history.

        The write is ``fsync``'d to guarantee that a crash immediately
        after recovery cannot undo the close (which would reintroduce
        the unbounded re-scan bug).

        Args:
            run_id: Run ID whose WAL is being closed.
            sdd_dir: The ``.sdd`` directory root.
            reason: Free-form string recorded in the marker body.
            uncommitted_count: Number of uncommitted entries that were
                observed during recovery (for audit trail).
            orphaned_count: Number of orphaned claims that were observed
                during recovery (for audit trail).

        Returns:
            Path to the ``.closed`` marker.
        """
        marker = WALRecovery._closed_marker_path(run_id, sdd_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": run_id,
            "closed_at": time.time(),
            "reason": reason,
            "uncommitted_count": uncommitted_count,
            "orphaned_count": orphaned_count,
        }
        with marker.open("w") as f:
            f.write(json.dumps(payload, separators=(",", ":")))
            f.flush()
            os.fsync(f.fileno())

        # Fsyncing the marker's contents does not make its directory entry
        # durable: POSIX only guarantees a new dirent survives a crash once
        # the parent directory itself is fsynced. Without this the marker
        # can be invisible on the next boot and the WAL is re-scanned
        # forever - exactly what the marker exists to prevent.
        try:
            dir_fd = os.open(marker.parent, os.O_RDONLY)
        except OSError:
            logger.warning("could not open WAL directory to fsync close marker for %s", run_id, exc_info=True)
        else:
            try:
                os.fsync(dir_fd)
            except OSError:
                logger.warning("directory fsync for close marker failed for %s", run_id, exc_info=True)
            finally:
                os.close(dir_fd)

        # drop stale uncommitted-index rows for the now-closed
        # run so future scans do not have to filter them out.
        try:
            UncommittedIndex(sdd_dir).remove_run(run_id)
        except OSError:
            logger.warning("failed to prune uncommitted index for %s", run_id, exc_info=True)
        return marker

    @staticmethod
    def scan_all_uncommitted(
        sdd_dir: Path,
        *,
        exclude_run_id: str | None = None,
    ) -> list[tuple[str, WALEntry]]:
        """Scan all WAL files for uncommitted entries from previous runs.

        Iterates over every ``*.wal.jsonl`` file in the WAL directory, skipping
        *exclude_run_id* (typically the current run) and any WAL whose
        ``.closed`` sidecar marker is present ( - prevents
        unbounded re-scanning of already-recovered WALs). Returns a flat
        list of ``(run_id, WALEntry)`` pairs for every entry with
        ``committed=False``.

        Returns an empty list when the WAL directory does not exist (fresh
        project with no prior runs).

        Args:
            sdd_dir: The ``.sdd`` directory root.
            exclude_run_id: Run ID to skip (the in-progress run).

        Returns:
            List of (run_id, uncommitted_entry) tuples.
        """
        wal_dir = sdd_dir / "runtime" / "wal"
        if not wal_dir.is_dir():
            return []

        results: list[tuple[str, WALEntry]] = []
        for wal_file in sorted(wal_dir.glob("*.wal.jsonl")):
            if wal_file.is_symlink():
                continue
            run_id = wal_file.name.removesuffix(".wal.jsonl")
            if run_id == exclude_run_id:
                continue
            if WALRecovery.is_wal_closed(run_id, sdd_dir):
                continue
            recovery = WALRecovery(run_id=run_id, sdd_dir=sdd_dir)
            for entry in recovery.get_uncommitted_entries():
                results.append((run_id, entry))
        return results

    @staticmethod
    def find_orphaned_claims(
        sdd_dir: Path,
        *,
        exclude_run_id: str | None = None,
    ) -> list[tuple[str, WALEntry]]:
        """Return uncommitted ``task_claimed`` entries with no matching spawn.

        Scans each prior run's WAL for ``task_claimed`` entries written with
        ``committed=False`` that do NOT have a subsequent ``task_spawn_confirmed``
        entry for the same ``task_id`` in the same run.  These represent the
        work-loss window where the server moved a task to *claimed* but the
        orchestrator crashed before the agent was spawned -- on restart the
        task would otherwise sit in *claimed* forever (or be abandoned by
        ``_reconcile_claimed_tasks`` without a dedicated retry audit trail).

        WALs with a ``.closed`` sidecar marker are skipped so
        that orphans handled by a prior recovery are not retried forever.

        Entries are read through :meth:`WALReader.iter_verified_entries`,
        so a WAL whose ``entry_hash`` or ``prev_hash`` does not check out
        yields no claims to force-reclaim.

        Args:
            sdd_dir: The ``.sdd`` directory root.
            exclude_run_id: Run ID to skip (the in-progress run).

        Returns:
            List of ``(run_id, WALEntry)`` tuples for each orphaned claim.
        """
        wal_dir = sdd_dir / "runtime" / "wal"
        if not wal_dir.is_dir():
            return []

        orphans: list[tuple[str, WALEntry]] = []
        for wal_file in sorted(wal_dir.glob("*.wal.jsonl")):
            if wal_file.is_symlink():
                continue
            run_id = wal_file.name.removesuffix(".wal.jsonl")
            if run_id == exclude_run_id:
                continue
            if WALRecovery.is_wal_closed(run_id, sdd_dir):
                continue
            reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)
            try:
                entries = list(reader.iter_verified_entries())
            except FileNotFoundError:
                continue

            confirmed_task_ids: set[str] = {
                str(e.inputs.get("task_id", ""))
                for e in entries
                if e.decision_type == "task_spawn_confirmed" and e.committed
            }
            for entry in entries:
                if entry.decision_type != "task_claimed" or entry.committed:
                    continue
                task_id = str(entry.inputs.get("task_id", ""))
                if not task_id or task_id in confirmed_task_ids:
                    continue
                orphans.append((run_id, entry))
        return orphans


# ---------------------------------------------------------------------------
# ExecutionFingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WALEntryDigest:
    """Cumulative fingerprint state after one WAL entry.

    ``digest`` is the run's :class:`ExecutionFingerprint` *as of* this entry:
    ``sha256(state_i).hexdigest()``, the same transform :meth:`ExecutionFingerprint.compute`
    applies to the final state. Because the underlying state is a rolling
    hash, the first index at which two runs' digests differ is exactly the
    first decision at which they diverged - no second fingerprinting scheme
    is introduced, only a snapshot of the existing one after each entry.

    ``seq`` is the WAL entry's sequence number from the hash chain, so the
    reported divergence points at a concrete entry an operator can locate.
    """

    seq: int
    decision_type: str
    digest: str


def first_divergence(a: list[WALEntryDigest], b: list[WALEntryDigest]) -> int | None:
    """Return the first index at which two digest streams differ.

    Compares the cumulative per-entry digests of two runs in WAL order.
    Returns the zero-based index of the first diverging entry, or ``None``
    when the two streams are identical (same length, equal digests at every
    position). A length mismatch diverges at the first index present in one
    stream but not the other.
    """
    limit = min(len(a), len(b))
    for i in range(limit):
        if a[i].digest != b[i].digest:
            return i
    if len(a) != len(b):
        return limit
    return None


class ExecutionFingerprint:
    """Determinism fingerprint over an ordered sequence of orchestrator decisions.

    Two runs with the same fingerprint made identical decisions in identical
    order - a verifiable proof of determinism usable as a CI gate.

    The fingerprint is a SHA-256 computed iteratively over the sequence::

        state_0 = b""
        state_i = sha256(state_{i-1} || decision_type || ":" || inputs_hash || ":" || output_hash)
        fingerprint = sha256(state_n).hexdigest()

    where ``inputs_hash`` and ``output_hash`` are each the SHA-256 of the
    canonical JSON of the respective dict.
    """

    def __init__(self) -> None:
        self._state: bytes = b""

    def add_decision(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Convenience alias for :meth:`record`."""
        self.record(decision_type, inputs, output)

    def record(
        self,
        decision_type: str,
        inputs: dict[str, Any],
        output: dict[str, Any],
    ) -> None:
        """Accumulate one decision into the fingerprint state."""
        inputs_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        output_hash = hashlib.sha256(json.dumps(output, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        step = f"{decision_type}:{inputs_hash}:{output_hash}".encode()
        self._state = hashlib.sha256(self._state + step).digest()

    def compute(self) -> str:
        """Return the current fingerprint as a 64-character hex string."""
        return hashlib.sha256(self._state).hexdigest()

    def finalize(self) -> str:
        """Convenience alias for :meth:`compute`."""
        return self.compute()

    @classmethod
    def from_wal(cls, reader: WALReader) -> ExecutionFingerprint:
        """Build a fingerprint from all entries in *reader*.

        Args:
            reader: A :class:`WALReader` positioned at the start of a WAL.

        Returns:
            An :class:`ExecutionFingerprint` reflecting all decisions in the WAL.
        """
        fp = cls()
        for entry in reader.iter_entries():
            fp.record(entry.decision_type, entry.inputs, entry.output)
        return fp

    @classmethod
    def entry_digests(cls, reader: WALReader) -> list[WALEntryDigest]:
        """Return the cumulative fingerprint digest after each WAL entry.

        Walks the WAL exactly as :meth:`from_wal` does, but snapshots the
        rolling hash after every entry. The last element's ``digest`` equals
        ``ExecutionFingerprint.from_wal(reader).compute()`` for the same WAL,
        so the per-entry stream and the headline fingerprint are guaranteed
        consistent. Use with :func:`first_divergence` to locate where two
        runs' decision traces forked.

        Args:
            reader: A :class:`WALReader` positioned at the start of a WAL.

        Returns:
            One :class:`WALEntryDigest` per WAL entry, in write order.
        """
        fp = cls()
        digests: list[WALEntryDigest] = []
        for entry in reader.iter_entries():
            fp.record(entry.decision_type, entry.inputs, entry.output)
            digests.append(
                WALEntryDigest(
                    seq=entry.seq,
                    decision_type=entry.decision_type,
                    digest=fp.compute(),
                )
            )
        return digests
