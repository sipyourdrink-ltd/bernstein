# pyright: reportPrivateUsage=false
"""Task lifecycle: claim, spawn, complete, retry, decompose.

Re-export shim — all logic has been extracted to focused sub-modules:

- ``task_claim``       — claim_and_spawn_batches, file overlap, warm pool
- ``task_completion``  — process_completed_tasks, backlog tickets, priority decay
- ``task_retry``       — retry escalation, model/effort ladders
- ``task_spawn_bridge`` — auto-decompose, conflict resolution

Every public symbol is re-exported here so that existing imports like
``from bernstein.core.task_lifecycle import X`` continue to work unchanged.
"""

from __future__ import annotations

# -- task_claim.py --
from bernstein.core.task_claim import _batch_timeout_seconds as _batch_timeout_seconds
from bernstein.core.task_claim import _claim_file_ownership as _claim_file_ownership
from bernstein.core.task_claim import _get_active_agent_files as _get_active_agent_files
from bernstein.core.task_claim import _speculative_warm_pool_candidates as _speculative_warm_pool_candidates
from bernstein.core.task_claim import check_file_overlap as check_file_overlap
from bernstein.core.task_claim import claim_and_spawn_batches as claim_and_spawn_batches
from bernstein.core.task_claim import infer_affected_paths as infer_affected_paths
from bernstein.core.task_claim import prepare_speculative_warm_pool as prepare_speculative_warm_pool

# -- task_completion.py --
from bernstein.core.task_completion import MIN_PRIORITY as MIN_PRIORITY
from bernstein.core.task_completion import PRIORITY_DECAY_THRESHOLD_HOURS as PRIORITY_DECAY_THRESHOLD_HOURS
from bernstein.core.task_completion import _enqueue_paired_test_task as _enqueue_paired_test_task
from bernstein.core.task_completion import _get_changed_files_in_worktree as _get_changed_files_in_worktree
from bernstein.core.task_completion import _get_git_diff_line_count_in_worktree as _get_git_diff_line_count_in_worktree
from bernstein.core.task_completion import _move_backlog_ticket as _move_backlog_ticket
from bernstein.core.task_completion import collect_completion_data as collect_completion_data
from bernstein.core.task_completion import deprioritize_old_unclaimed_tasks as deprioritize_old_unclaimed_tasks
from bernstein.core.task_completion import handle_permission_denied_error as handle_permission_denied_error
from bernstein.core.task_completion import process_completed_tasks as process_completed_tasks

# -- task_retry.py --
from bernstein.core.task_retry import _EFFORT_LADDER as _EFFORT_LADDER
from bernstein.core.task_retry import _MODEL_LADDER as _MODEL_LADDER
from bernstein.core.task_retry import _SCOPE_TIMEOUT_SECONDS as _SCOPE_TIMEOUT_SECONDS
from bernstein.core.task_retry import _XL_ROLES as _XL_ROLES
from bernstein.core.task_retry import _XL_TIMEOUT_SECONDS as _XL_TIMEOUT_SECONDS
from bernstein.core.task_retry import _bump_effort as _bump_effort
from bernstein.core.task_retry import _choose_retry_escalation as _choose_retry_escalation
from bernstein.core.task_retry import _escalate_model as _escalate_model
from bernstein.core.task_retry import _extract_failure_context as _extract_failure_context
from bernstein.core.task_retry import maybe_retry_task as maybe_retry_task
from bernstein.core.task_retry import retry_or_fail_task as retry_or_fail_task

# -- task_spawn_bridge.py --
from bernstein.core.task_spawn_bridge import auto_decompose_task as auto_decompose_task
from bernstein.core.task_spawn_bridge import create_conflict_resolution_task as create_conflict_resolution_task
from bernstein.core.task_spawn_bridge import should_auto_decompose as should_auto_decompose

__all__ = [
    "auto_decompose_task",
    "check_file_overlap",
    "claim_and_spawn_batches",
    "collect_completion_data",
    "create_conflict_resolution_task",
    "deprioritize_old_unclaimed_tasks",
    "handle_permission_denied_error",
    "infer_affected_paths",
    "maybe_retry_task",
    "prepare_speculative_warm_pool",
    "process_completed_tasks",
    "retry_or_fail_task",
    "should_auto_decompose",
]
