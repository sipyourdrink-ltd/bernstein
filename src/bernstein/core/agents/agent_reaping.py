"""Agent death handling and orphaned task recovery.

Thin re-export module -- all implementations live in
:mod:`bernstein.core.agents.agent_lifecycle`.  This module exists so that
imports of the form ``from bernstein.core.agents.agent_reaping import ...``
continue to work after the code was consolidated.
"""

from bernstein.core.agents.agent_lifecycle import (
    _has_git_commits_on_branch,
    _maybe_preserve_worktree,
    _propagate_abort_to_children,
    _release_file_ownership,
    _release_task_to_session,
    _requeue_rate_limited_task,
    _save_partial_work,
    emit_orphan_metrics,
    handle_orphaned_task,
    purge_dead_agents,
    reap_dead_agents,
)

__all__ = [
    "_has_git_commits_on_branch",
    "_maybe_preserve_worktree",
    "_propagate_abort_to_children",
    "_release_file_ownership",
    "_release_task_to_session",
    "_requeue_rate_limited_task",
    "_save_partial_work",
    "emit_orphan_metrics",
    "handle_orphaned_task",
    "purge_dead_agents",
    "reap_dead_agents",
]
