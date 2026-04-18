"""WAL replay logic for crash recovery.

On startup, the orchestrator checks for uncommitted WAL entries from
previous runs and replays them to restore consistent state.  Each
entry is checked for idempotency before replay to avoid double-execution.

The replay pipeline:

1. **Scan** — find all uncommitted entries across WAL files.
2. **Filter** — check each entry against an idempotency store to skip
   already-executed actions.
3. **Replay** — re-execute the delta (entries not yet committed).
4. **Commit** — mark replayed entries as committed in the current WAL.

The idempotency store maps ``(decision_type, entry_hash)`` to a boolean
indicating whether the action was successfully executed.  This prevents
double-spawns, double-completions, and other duplicate side effects.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 (runtime use in Path.glob / dir traversal)
from typing import Any

from bernstein.core.defaults import JANITOR
from bernstein.core.persistence.runtime_state import rotate_log_file
from bernstein.core.persistence.wal import WALEntry, WALRecovery, WALWriter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayResult:
    """Result of replaying a single WAL entry.

    Attributes:
        entry: The WAL entry that was replayed.
        run_id: The run ID the entry came from.
        replayed: Whether the entry was actually replayed (vs skipped).
        skipped_reason: Reason for skipping, if applicable.
        error: Error message if replay failed.
    """

    entry: WALEntry
    run_id: str
    replayed: bool
    skipped_reason: str = ""
    error: str = ""


@dataclass
class ReplaySummary:
    """Summary of a complete WAL replay operation.

    Attributes:
        total_uncommitted: Total uncommitted entries found.
        replayed: Number of entries successfully replayed.
        skipped_idempotent: Number skipped due to idempotency check.
        skipped_stale: Number skipped due to staleness.
        failed: Number of entries that failed to replay.
        results: Individual replay results.
        duration_s: Total replay duration in seconds.
    """

    total_uncommitted: int = 0
    replayed: int = 0
    skipped_idempotent: int = 0
    skipped_stale: int = 0
    failed: int = 0
    results: list[ReplayResult] = field(default_factory=lambda: list[ReplayResult]())
    duration_s: float = 0.0


class IdempotencyStore:
    """Track which WAL entries have been successfully executed.

    Persisted to disk as a JSONL file in the WAL directory so it
    survives crashes.

    Args:
        sdd_dir: The ``.sdd`` directory root.
    """

    def __init__(self, sdd_dir: Path) -> None:
        self._path = sdd_dir / "runtime" / "wal" / "idempotency.jsonl"
        self._executed: set[str] = set()
        self._load()

    def _load(self) -> None:
        """Load previously recorded execution markers from disk.

        Scans the live ``idempotency.jsonl`` plus any rotated ``.N`` backups
        (audit-081) so that rotation never silently forgets a marker.
        """
        candidates: list[Path] = []
        if self._path.exists():
            candidates.append(self._path)
        backup_parent = self._path.parent
        if backup_parent.exists():
            candidates.extend(sorted(backup_parent.glob(f"{self._path.name}.*")))
        if not candidates:
            return
        for candidate in candidates:
            try:
                for line in candidate.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = str(data.get("key", ""))
                    if key:
                        self._executed.add(key)
            except OSError as exc:
                logger.warning("Failed to load idempotency markers from %s: %s", candidate, exc)

    def _make_key(self, entry: WALEntry) -> str:
        """Create a unique key for an entry.

        Args:
            entry: WAL entry.

        Returns:
            Unique string key combining decision type and entry hash.
        """
        return f"{entry.decision_type}:{entry.entry_hash}"

    def is_executed(self, entry: WALEntry) -> bool:
        """Check if an entry has already been executed.

        Args:
            entry: WAL entry to check.

        Returns:
            True if the entry was previously executed.
        """
        return self._make_key(entry) in self._executed

    def mark_executed(self, entry: WALEntry) -> None:
        """Mark an entry as successfully executed.

        Args:
            entry: WAL entry to mark.
        """
        key = self._make_key(entry)
        if key in self._executed:
            return

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # audit-081: cap unbounded idempotency.jsonl. _load() scans
            # rotated .N backups as well so rotation does not lose markers.
            rotate_log_file(self._path, max_bytes=JANITOR.idempotency_rotate_bytes)
            with self._path.open("a", encoding="utf-8") as f:
                record = {
                    "key": key,
                    "decision_type": entry.decision_type,
                    "entry_hash": entry.entry_hash,
                    "marked_at": time.time(),
                }
                f.write(json.dumps(record) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            logger.warning("Failed to persist idempotency marker: %s", exc)
            return
        self._executed.add(key)

    def clear(self) -> None:
        """Clear all execution markers (for testing)."""
        self._executed.clear()
        if self._path.exists():
            self._path.unlink(missing_ok=True)


# Decision types that are safe to skip during replay (informational only)
_SKIP_DECISION_TYPES: frozenset[str] = frozenset(
    {
        "tick_start",
        "degraded_mode_state",
        "degraded_mode_enter",
        "degraded_mode_exit",
    }
)

# Decision types that must be replayed (state-changing)
_REPLAY_DECISION_TYPES: frozenset[str] = frozenset(
    {
        "task_created",
        "task_claimed",
        "task_completed",
        "task_failed",
        "agent_spawned",
        "agent_killed",
    }
)

# Maximum age in seconds for entries to be considered for replay
_MAX_REPLAY_AGE_S: float = 3600.0  # 1 hour


class WALReplayEngine:
    """Replay uncommitted WAL entries on crash recovery.

    Args:
        sdd_dir: The ``.sdd`` directory root.
        current_run_id: The current run's ID (excluded from scanning).
        wal_writer: WAL writer for the current run (to record replay results).
        max_replay_age_s: Maximum entry age for replay eligibility.
    """

    def __init__(
        self,
        sdd_dir: Path,
        current_run_id: str,
        wal_writer: WALWriter | None = None,
        max_replay_age_s: float = _MAX_REPLAY_AGE_S,
    ) -> None:
        self._sdd_dir = sdd_dir
        self._current_run_id = current_run_id
        self._wal_writer = wal_writer
        self._max_replay_age_s = max_replay_age_s
        self._idempotency = IdempotencyStore(sdd_dir)

    @property
    def idempotency_store(self) -> IdempotencyStore:
        """The idempotency store used by this engine."""
        return self._idempotency

    def scan_and_replay(
        self,
        replay_handler: Any | None = None,
    ) -> ReplaySummary:
        """Scan for uncommitted entries and replay them.

        Args:
            replay_handler: Optional callable ``(entry: WALEntry) -> bool``
                that executes the actual replay action.  Returns True on
                success.  If None, entries are marked as replayed without
                executing anything (dry-run mode).

        Returns:
            Summary of the replay operation.
        """
        start = time.monotonic()
        summary = ReplaySummary()

        # 1. Scan all WAL files for uncommitted entries
        uncommitted = WALRecovery.scan_all_uncommitted(
            self._sdd_dir,
            exclude_run_id=self._current_run_id,
        )
        summary.total_uncommitted = len(uncommitted)

        if not uncommitted:
            logger.info("WAL replay: no uncommitted entries found")
            summary.duration_s = time.monotonic() - start
            return summary

        logger.info(
            "WAL replay: found %d uncommitted entries from %d previous run(s)",
            len(uncommitted),
            len({run_id for run_id, _ in uncommitted}),
        )

        # 2. Process each entry
        now = time.time()
        for run_id, entry in uncommitted:
            result = self._process_entry(run_id, entry, now, replay_handler)
            summary.results.append(result)

            if result.replayed:
                summary.replayed += 1
            elif result.skipped_reason == "idempotent":
                summary.skipped_idempotent += 1
            elif result.skipped_reason == "stale":
                summary.skipped_stale += 1
            elif result.error:
                summary.failed += 1

        # 3. Record replay summary in current WAL
        if self._wal_writer is not None:
            try:
                self._wal_writer.write_entry(
                    decision_type="wal_replay_completed",
                    inputs={
                        "total_uncommitted": summary.total_uncommitted,
                        "replayed": summary.replayed,
                        "skipped_idempotent": summary.skipped_idempotent,
                        "skipped_stale": summary.skipped_stale,
                        "failed": summary.failed,
                    },
                    output={},
                    actor="wal_replay_engine",
                )
            except OSError:
                logger.debug("WAL write failed for replay summary")

        summary.duration_s = time.monotonic() - start
        logger.info(
            "WAL replay completed in %.2fs: %d replayed, %d skipped (idempotent), %d skipped (stale), %d failed",
            summary.duration_s,
            summary.replayed,
            summary.skipped_idempotent,
            summary.skipped_stale,
            summary.failed,
        )
        return summary

    def _process_entry(
        self,
        run_id: str,
        entry: WALEntry,
        now: float,
        replay_handler: Any | None,
    ) -> ReplayResult:
        """Process a single uncommitted WAL entry.

        Args:
            run_id: The run ID this entry belongs to.
            entry: The WAL entry.
            now: Current wall-clock time.
            replay_handler: Optional replay callable.

        Returns:
            Result of processing this entry.
        """
        # Skip informational entries
        if entry.decision_type in _SKIP_DECISION_TYPES:
            return ReplayResult(
                entry=entry,
                run_id=run_id,
                replayed=False,
                skipped_reason="informational",
            )

        # Check staleness
        age = now - entry.timestamp
        if age > self._max_replay_age_s:
            logger.info(
                "WAL replay: skipping stale entry (age=%.0fs, type=%s, seq=%d)",
                age,
                entry.decision_type,
                entry.seq,
            )
            return ReplayResult(
                entry=entry,
                run_id=run_id,
                replayed=False,
                skipped_reason="stale",
            )

        # Check idempotency
        if self._idempotency.is_executed(entry):
            logger.debug(
                "WAL replay: skipping idempotent entry (type=%s, seq=%d)",
                entry.decision_type,
                entry.seq,
            )
            return ReplayResult(
                entry=entry,
                run_id=run_id,
                replayed=False,
                skipped_reason="idempotent",
            )

        # Execute replay
        if replay_handler is not None:
            try:
                success = replay_handler(entry)
                if success:
                    self._idempotency.mark_executed(entry)
                    return ReplayResult(
                        entry=entry,
                        run_id=run_id,
                        replayed=True,
                    )
                return ReplayResult(
                    entry=entry,
                    run_id=run_id,
                    replayed=False,
                    error="replay_handler returned False",
                )
            except Exception as exc:
                logger.warning(
                    "WAL replay failed for entry (type=%s, seq=%d): %s",
                    entry.decision_type,
                    entry.seq,
                    exc,
                )
                return ReplayResult(
                    entry=entry,
                    run_id=run_id,
                    replayed=False,
                    error=str(exc),
                )
        else:
            # Dry-run: mark as executed without doing anything
            self._idempotency.mark_executed(entry)
            return ReplayResult(
                entry=entry,
                run_id=run_id,
                replayed=True,
            )
