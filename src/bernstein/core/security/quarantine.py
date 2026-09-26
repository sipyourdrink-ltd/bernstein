"""Cross-run task quarantine - track repeatedly-failing tasks across Bernstein runs.

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

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

QUARANTINE_THRESHOLD = 3
"""Number of cross-run failures before a task is quarantined."""

QUARANTINE_EXPIRY_DAYS = 7
"""Days after which a quarantine entry is automatically expired."""

_TRANSIENT_HOST_FAILURE_MARKERS = (
    "disk space critical",
    "no space left on device",
    "out of memory",
    "cannot allocate memory",
    "too many open files",
)
"""Host exhaustion strings a spawn exception can carry: the spawner's own
refusal (``Disk space critical`` from ``spawner_core.py``'s pre-spawn floor
check) plus the POSIX errno texts the same condition surfaces as. Scope:
Windows mid-spawn exhaustion (``WinError 112``/``1455``, ``EDQUOT``) is not
matched and still records, and ``out of memory`` also matches per-process
limits (e.g. a V8 heap cap) that are not host-wide."""


def is_transient_host_failure(spawn_error_text: str) -> bool:
    """Return True when a spawn exception's text is host resource exhaustion.

    Only the batch spawn loop in ``task_lifecycle.py`` calls this, on the
    exception it caught: agents never author that text, so the classification
    is trusted where ``task.result_summary`` never was. The stored summary an
    agent can write through ``POST /tasks/{id}/fail`` never reaches this
    predicate; the exemption crosses to the tick loop only as the store's
    excused marker (see :meth:`QuarantineStore.excuse_failure`).
    """
    lowered = spawn_error_text.lower()
    return any(marker in lowered for marker in _TRANSIENT_HOST_FAILURE_MARKERS)


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
        self._excused_path = path.with_suffix(".excused.json")

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
            # Degrading to "nothing is quarantined" is the permissive answer:
            # every repeatedly-failing task becomes eligible again. Kept, so a
            # damaged file cannot stop the orchestrator, but logged as an
            # error saying what was lost - a WARNING understated it.
            logger.error(
                "quarantine: %s is unreadable (%s); treating every task as not quarantined "
                "until the file is repaired or removed",
                self._path,
                exc,
            )
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

    def is_excused(self, task_id: str) -> bool:
        """Return True when *task_id* carries a current excused marker.

        The spawn loop excuses the tasks it gave up spawning after the host
        ran out of resources; a marker expires on the same boundary as an
        entry (``_is_expired``), so the excuse always outlives the entry it
        excuses and there is no day where a live entry records again with
        its marker already gone. Keyed by task
        id, not title: only the tasks the spawn loop actually failed are
        excused, so a later task under the same title that fails for its own
        reasons still counts toward quarantine.

        Args:
            task_id: Exact task id to check.

        Returns:
            True while a current marker exists for the task id.
        """
        record = self._load_excused().get(task_id)
        if record is None:
            return False
        try:
            recorded = date.fromisoformat(str(record.get("recorded_at") or ""))
        except ValueError:
            return False
        return (date.today() - recorded) <= timedelta(days=QUARANTINE_EXPIRY_DAYS)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save(self, entries: list[QuarantineEntry]) -> None:
        """Persist *entries* to the quarantine file.

        Creates parent directories as needed.

        Written through :func:`write_atomic_json` rather than in place. A
        plain write truncates the destination before it writes, so the window
        between those two steps is one where the file on disk holds no
        entries - and :meth:`load` reads an unparseable file as "nothing is
        quarantined", which is the permissive answer. A crash in that window
        therefore releases every quarantined task back into the next run's
        schedule, silently. Writing to a temporary file and renaming means a
        reader sees either the previous set or the new one, never neither.

        Args:
            entries: Full list of entries to write (replaces current file).
        """
        write_atomic_json(self._path, [asdict(e) for e in entries])

    def excuse_failure(self, task_id: str, reason: str) -> None:
        """Mark *task_id* excused from cross-run failure recording.

        Written only by the spawn loop's give-up branch, after it classified
        the exception it caught as transient host resource exhaustion
        (:func:`is_transient_host_failure`). Agents can write
        ``task.result_summary`` through ``POST /tasks/{id}/fail`` but cannot
        write this marker, so the exemption cannot be forged from task text.
        Keyed by task id and expiring after ``QUARANTINE_EXPIRY_DAYS``: only
        the tasks the spawn loop failed are excused, never a later task that
        shares the title.

        Args:
            task_id: Task id whose spawn gave up on host exhaustion.
            reason: The orchestrator-observed exception text, for operators.
        """
        excused = self._load_excused()
        excused[task_id] = {
            "reason": reason,
            "recorded_at": date.today().isoformat(),
        }
        write_atomic_json(self._excused_path, excused)
        logger.warning(
            "quarantine: task %s excused from cross-run recording after host resource exhaustion: %s",
            task_id,
            reason,
        )

    def _load_excused(self) -> dict[str, dict[str, str]]:
        """Load excused markers, treating an absent or damaged file as empty."""
        if not self._excused_path.exists():
            return {}
        try:
            raw: dict[str, dict[str, object]] = json.loads(self._excused_path.read_text())
            return {
                str(title): {
                    "reason": str(item.get("reason") or ""),
                    "recorded_at": str(item.get("recorded_at") or ""),
                }
                for title, item in raw.items()
                if isinstance(item, dict)
            }
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(
                "quarantine: %s is unreadable (%s); treating no task as excused until the file is repaired or removed",
                self._excused_path,
                exc,
            )
            return {}

    def record_failure(self, task_title: str, reason: str, *, task_id: str = "") -> bool:
        """Record a failure for *task_title*, incrementing its fail count.

        Creates a new entry if one does not exist.  Always updates
        ``last_failure`` to today and ``reason`` to the most recent failure.
        Persists immediately.  A task whose id carries a current excused
        marker (the spawn loop gave up spawning it after host resource
        exhaustion) is refused: a full disk is not a fact about that task.
        A later task under the same title still records normally.

        Args:
            task_title: Title of the failed task.
            reason: Human-readable failure reason.
            task_id: Id of the failed task; checked against the excused
                markers the spawn loop wrote.

        Returns:
            True when the failure was recorded, False when it was excused.
        """
        if task_id and self.is_excused(task_id):
            logger.warning(
                "quarantine: not recording task %s (%r) failure: its spawn was "
                "excused after transient host resource exhaustion",
                task_id,
                task_title,
            )
            return False
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
                return True

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
        return True

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
