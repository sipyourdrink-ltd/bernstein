"""Worktree inventory and garbage-collection helpers.

This subpackage hosts the classifier used by ``bernstein worktrees`` and the
TUI list pane to inspect every directory under ``.sdd/runtime/worktrees/``
(or, on older layouts, ``.sdd/worktrees/``), assign each worktree a
deterministic state, and reap the non-active ones.
"""

from __future__ import annotations

from bernstein.core.worktrees.classifier import (
    GC_LOCK_RELPATH,
    STALE_TRACE_AGE_S,
    WORKTREE_GC_LIFECYCLE_EVENT,
    ClassifiedWorktree,
    WorktreeState,
    classify_worktrees,
    format_size,
    iter_worktree_dirs,
    reap_worktree,
    worktrees_root,
)
from bernstein.core.worktrees.run_helpers import (
    RunHelper,
    capture_run_helpers,
    classify_run_helpers,
)

__all__ = [
    "GC_LOCK_RELPATH",
    "STALE_TRACE_AGE_S",
    "WORKTREE_GC_LIFECYCLE_EVENT",
    "ClassifiedWorktree",
    "RunHelper",
    "WorktreeState",
    "capture_run_helpers",
    "classify_run_helpers",
    "classify_worktrees",
    "format_size",
    "iter_worktree_dirs",
    "reap_worktree",
    "worktrees_root",
]
