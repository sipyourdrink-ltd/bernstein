"""Thread-safe in-memory task store with JSONL persistence.

All task mutations go through this class so the JSONL log stays consistent.

This module is a **re-export shim** — the implementation lives in
``task_store_core``.  All public names are re-exported here so that
existing ``from bernstein.core.task_store import X`` continues to work.
"""

from bernstein.core.task_store_core import _MAX_IO_RETRIES as _MAX_IO_RETRIES
from bernstein.core.task_store_core import DEFAULT_ARCHIVE_PATH as DEFAULT_ARCHIVE_PATH
from bernstein.core.task_store_core import PANEL_GRACE_MS as PANEL_GRACE_MS
from bernstein.core.task_store_core import ArchiveRecord as ArchiveRecord
from bernstein.core.task_store_core import ProgressEntry as ProgressEntry
from bernstein.core.task_store_core import SnapshotEntry as SnapshotEntry
from bernstein.core.task_store_core import TaskCreateRequest as TaskCreateRequest
from bernstein.core.task_store_core import TaskRecord as TaskRecord
from bernstein.core.task_store_core import TaskStore as TaskStore
from bernstein.core.task_store_core import _parse_upgrade_dict as _parse_upgrade_dict
from bernstein.core.task_store_core import _retry_io as _retry_io

__all__ = [
    "DEFAULT_ARCHIVE_PATH",
    "PANEL_GRACE_MS",
    "_MAX_IO_RETRIES",
    "ArchiveRecord",
    "ProgressEntry",
    "SnapshotEntry",
    "TaskCreateRequest",
    "TaskRecord",
    "TaskStore",
    "_parse_upgrade_dict",
    "_retry_io",
]
