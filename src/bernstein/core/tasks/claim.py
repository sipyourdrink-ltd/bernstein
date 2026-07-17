"""Atomic claim primitive for race-safe shared task backlogs.

The primitive is intentionally small: a JSON backlog file plus a sibling
advisory lock file. Every claim reloads the backlog under both an in-process
thread lock and an OS-level file lock, flips one eligible row to
``in_progress``, stamps the claimer, and writes the document back with
``os.replace`` via :func:`write_atomic_json`.

This is for same-host shared backlogs. Network filesystems may weaken advisory
locking semantics; distributed deployments should use the database-backed task
store paths.
"""

from __future__ import annotations

import collections.abc as cabc
import json
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.persistence.file_locks import _cross_process_lock
from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

_OPEN_STATUS = "open"
_CLAIMED_STATUS = "in_progress"
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _empty_str_set() -> set[str]:
    return set()


def _empty_str_list() -> list[str]:
    return []


def _empty_metadata() -> dict[str, Any]:
    return {}


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    """Return the per-lock-path thread mutex used before the OS lock."""
    key = lock_path.absolute()
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _backlog_lock(lock_path: Path) -> Generator[None, None, None]:
    """Acquire the cross-thread and cross-process lock for *lock_path*."""
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock, _cross_process_lock(lock_path):
        yield


@dataclass(frozen=True)
class ClaimFilter:
    """Eligibility constraints for :func:`claim_next`.

    Attributes:
        project: Optional project/backlog namespace.
        role: Optional role to claim, such as ``"reviewer"``.
        capability: Optional capability required on the row.
        completed_ids: Task ids whose dependencies are satisfied.
        max_attempts: Global retry ceiling. Rows with attempts greater than or
            equal to this value are skipped.
        admits: Optional admission predicate over the entry's declared tags
            (#2544). Returns ``True`` when every declared tag is currently
            admissible (AND-of-all under its pool/tag limit). Supplied by the
            admission engine's projection so the claim path is the one
            admission gate. ``None`` disables the tag gate (default), keeping
            every legacy claim unchanged.
    """

    project: str | None = None
    role: str | None = None
    capability: str | None = None
    completed_ids: frozenset[str] = field(default_factory=frozenset)
    max_attempts: int | None = None
    admits: Callable[[frozenset[str]], bool] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed_ids", frozenset(self.completed_ids))

    def allows(self, entry: BacklogEntry) -> bool:
        """Return True when *entry* satisfies every claim predicate."""
        if entry.status != _OPEN_STATUS or entry.claimer is not None:
            return False
        if self.project is not None and entry.project != self.project:
            return False
        if self.role is not None and entry.role != self.role:
            return False
        if self.capability is not None and self.capability not in entry.capabilities:
            return False
        attempts_limit = self._attempts_limit(entry)
        if attempts_limit is not None and entry.attempts >= attempts_limit:
            return False
        if self.admits is not None and entry.tags and not self.admits(frozenset(entry.tags)):
            return False
        return all(dep in self.completed_ids for dep in entry.depends_on)

    def _attempts_limit(self, entry: BacklogEntry) -> int | None:
        limits = [limit for limit in (self.max_attempts, entry.max_attempts) if limit is not None]
        return min(limits) if limits else None


@dataclass
class BacklogEntry:
    """One task row in a shared backlog file."""

    id: str
    claimer: str | None = None
    status: str = _OPEN_STATUS
    role: str | None = None
    project: str | None = None
    capabilities: list[str] = field(default_factory=_empty_str_list)
    depends_on: list[str] = field(default_factory=_empty_str_list)
    tags: list[str] = field(default_factory=_empty_str_list)  # #2544 admission tags
    attempts: int = 0
    max_attempts: int | None = None
    claimed_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BacklogEntry:
        """Hydrate an entry from a JSON row."""
        task_id = raw.get("id")
        if task_id is None:
            raise ValueError("backlog entry is missing required 'id'")
        status = raw.get("status")
        claimer = raw.get("claimer")
        if status is None:
            status = _CLAIMED_STATUS if claimer is not None else _OPEN_STATUS
        raw_capabilities = raw.get("capabilities")
        if raw_capabilities is None:
            capability_values: list[Any] = []
        elif isinstance(raw_capabilities, list):
            capability_values = cast("list[Any]", raw_capabilities)
        else:
            raise ValueError(f"backlog entry {task_id!r} has non-list capabilities")

        raw_depends_on = raw.get("depends_on")
        if raw_depends_on is None:
            dependency_values: list[Any] = []
        elif isinstance(raw_depends_on, list):
            dependency_values = cast("list[Any]", raw_depends_on)
        else:
            raise ValueError(f"backlog entry {task_id!r} has non-list depends_on")

        raw_tags = raw.get("tags")
        if raw_tags is None:
            tag_values: list[Any] = []
        elif isinstance(raw_tags, list):
            tag_values = cast("list[Any]", raw_tags)
        else:
            raise ValueError(f"backlog entry {task_id!r} has non-list tags")

        raw_metadata = raw.get("metadata")
        if raw_metadata is None:
            metadata_values: dict[str, Any] = {}
        elif isinstance(raw_metadata, dict):
            metadata_values = cast("dict[str, Any]", raw_metadata)
        else:
            raise ValueError(f"backlog entry {task_id!r} has non-object metadata")
        return cls(
            id=str(task_id),
            claimer=str(claimer) if claimer is not None else None,
            status=str(status),
            role=str(raw["role"]) if raw.get("role") is not None else None,
            project=str(raw["project"]) if raw.get("project") is not None else None,
            capabilities=[str(capability) for capability in capability_values],
            depends_on=[str(dep) for dep in dependency_values],
            tags=[str(tag) for tag in tag_values],
            attempts=int(raw.get("attempts") or 0),
            max_attempts=int(raw["max_attempts"]) if raw.get("max_attempts") is not None else None,
            claimed_at=float(raw["claimed_at"]) if raw.get("claimed_at") is not None else None,
            metadata=metadata_values.copy(),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise the row to a compact JSON object."""
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "claimer": self.claimer,
        }
        if self.role is not None:
            data["role"] = self.role
        if self.project is not None:
            data["project"] = self.project
        if self.capabilities:
            data["capabilities"] = self.capabilities.copy()
        if self.depends_on:
            data["depends_on"] = self.depends_on.copy()
        if self.tags:
            data["tags"] = self.tags.copy()
        if self.attempts:
            data["attempts"] = self.attempts
        if self.max_attempts is not None:
            data["max_attempts"] = self.max_attempts
        if self.claimed_at is not None:
            data["claimed_at"] = self.claimed_at
        if self.metadata:
            data["metadata"] = self.metadata.copy()
        return data

    def claim(self, claimer_id: str, now: float | None = None) -> None:
        """Mark this entry as owned by *claimer_id*."""
        self.status = _CLAIMED_STATUS
        self.claimer = claimer_id
        self.claimed_at = time.time() if now is None else now
        self.attempts += 1


def _empty_backlog_entries() -> list[BacklogEntry]:
    return []


@dataclass
class Backlog:
    """In-memory view of an ordered JSON backlog."""

    path: Path
    entries: list[BacklogEntry] = field(default_factory=_empty_backlog_entries)

    @property
    def lock_path(self) -> Path:
        """Path to the sibling lock file used by this backlog."""
        return self.path.with_suffix(self.path.suffix + ".lock")

    @classmethod
    def load(cls, path: Path) -> Backlog:
        """Load *path*, treating a missing file as an empty backlog."""
        if not path.exists():
            return cls(path=path, entries=[])
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"backlog at {path} must be a JSON array, got {type(raw).__name__}")
        raw_rows = cast("list[Any]", raw)
        rows: list[Mapping[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, cabc.Mapping):
                raise ValueError(f"backlog at {path} contains non-object row: {type(row).__name__}")
            rows.append(cast("Mapping[str, Any]", row))
        entries = [BacklogEntry.from_dict(row) for row in rows]
        cls._validate_unique_ids(entries, path)
        return cls(path=path, entries=entries)

    @classmethod
    def write(
        cls,
        path: Path,
        entries: Iterable[str | BacklogEntry | Mapping[str, Any]],
        *,
        role: str | None = None,
        project: str | None = None,
    ) -> Backlog:
        """Overwrite *path* with an ordered backlog."""
        backlog_entries = [cls._coerce_entry(entry, role=role, project=project) for entry in entries]
        cls._validate_unique_ids(backlog_entries, path)
        backlog = cls(path=path, entries=backlog_entries)
        backlog.save()
        return backlog

    @staticmethod
    def _coerce_entry(
        entry: str | BacklogEntry | Mapping[str, Any],
        *,
        role: str | None,
        project: str | None,
    ) -> BacklogEntry:
        if isinstance(entry, BacklogEntry):
            return entry
        if isinstance(entry, str):
            return BacklogEntry(id=entry, role=role, project=project)
        return BacklogEntry.from_dict(entry)

    @staticmethod
    def _validate_unique_ids(entries: list[BacklogEntry], path: Path) -> None:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for entry in entries:
            if entry.id in seen:
                duplicates.add(entry.id)
            seen.add(entry.id)
        if duplicates:
            names = ", ".join(sorted(duplicates))
            raise ValueError(f"backlog at {path} contains duplicate task id(s): {names}")

    def save(self) -> None:
        """Persist this backlog with an atomic replace."""
        write_atomic_json(self.path, [entry.to_dict() for entry in self.entries])


def claim_next_entry(
    backlog_path: Path,
    claimer_id: str,
    filter: ClaimFilter | None = None,
) -> BacklogEntry | None:
    """Atomically claim and return the next eligible backlog entry."""
    claim_filter = filter or ClaimFilter()
    backlog = Backlog(path=backlog_path)
    with _backlog_lock(backlog.lock_path):
        backlog = Backlog.load(backlog_path)
        now = time.time()
        for entry in backlog.entries:
            if claim_filter.allows(entry):
                entry.claim(claimer_id, now=now)
                backlog.save()
                # claimer_id is caller-supplied (it reaches here from the
                # claim-receipt HTTP route); strip CR/LF before logging so it
                # cannot forge additional log lines.
                logger.debug(
                    "claim_next: %s -> %s (backlog=%s)",
                    entry.id,
                    sanitize_log(claimer_id),
                    backlog_path,
                )
                return entry
    return None


def claim_next(
    backlog_path: Path,
    claimer_id: str,
    filter: ClaimFilter | None = None,
) -> str | None:
    """Atomically claim the next eligible task id from *backlog_path*."""
    claimed = claim_next_entry(backlog_path, claimer_id=claimer_id, filter=filter)
    return claimed.id if claimed is not None else None


__all__ = [
    "Backlog",
    "BacklogEntry",
    "ClaimFilter",
    "claim_next",
    "claim_next_entry",
]
