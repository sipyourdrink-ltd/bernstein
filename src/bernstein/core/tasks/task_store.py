"""Thread-safe in-memory task store with JSONL persistence.

All task mutations go through this class so the JSONL log stays consistent.

This module re-exports everything from ``task_store_core`` so that existing
imports (``from bernstein.core.task_store import TaskStore``) keep working
without duplicating ~1800 lines of implementation.
"""

from __future__ import annotations

# Re-export the complete public (and semi-private) API so every existing
# import path keeps working unchanged.
from bernstein.core.tasks.task_store_core import (
    DEFAULT_ARCHIVE_PATH,
    PANEL_GRACE_MS,
    ArchiveRecord,
    EmptyCompletionError,
    ProgressEntry,
    SnapshotEntry,
    TaskCreateRequest,
    TaskRecord,
    TaskStore,
    _CompletionSignalRequest,
    _parse_upgrade_dict,
    _retry_io,
)

__all__ = [
    "DEFAULT_ARCHIVE_PATH",
    "PANEL_GRACE_MS",
    "ArchiveRecord",
    "EmptyCompletionError",
    "ProgressEntry",
    "SnapshotEntry",
    "TaskCreateRequest",
    "TaskRecord",
    "TaskStore",
    "_CompletionSignalRequest",
    "_parse_upgrade_dict",
    "_retry_io",
]
