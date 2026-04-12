# pyright: reportPrivateUsage=false
"""Task claiming and batch spawn bridge.

Thin re-export module -- all implementations live in
:mod:`bernstein.core.tasks.task_lifecycle`.  This module exists so that
imports of the form ``from bernstein.core.tasks.task_claim import ...``
continue to work after the code was consolidated.
"""

from bernstein.core.tasks.task_lifecycle import (
    _XL_ROLES,
    _batch_timeout_seconds,
    _claim_file_ownership,
    _get_active_agent_files,
    _get_changed_files_in_worktree,
    _speculative_warm_pool_candidates,
    check_file_overlap,
    claim_and_spawn_batches,
    infer_affected_paths,
    prepare_speculative_warm_pool,
)

__all__ = [
    "_XL_ROLES",
    "_batch_timeout_seconds",
    "_claim_file_ownership",
    "_get_active_agent_files",
    "_get_changed_files_in_worktree",
    "_speculative_warm_pool_candidates",
    "check_file_overlap",
    "claim_and_spawn_batches",
    "infer_affected_paths",
    "prepare_speculative_warm_pool",
]
