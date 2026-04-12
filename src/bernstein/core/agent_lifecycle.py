"""Agent lifecycle: tracking, heartbeat, crash detection, reaping.

This module is a **re-export shim** — the implementation is split across
``agent_reaping``, ``agent_recycling``, and ``agent_state_refresh``.
All public names are re-exported here so that existing
``from bernstein.core.agent_lifecycle import X`` continues to work.
"""

# -- agent_reaping: death handling, orphaned tasks, metrics --
from bernstein.core.agent_reaping import _has_git_commits_on_branch as _has_git_commits_on_branch
from bernstein.core.agent_reaping import _maybe_preserve_worktree as _maybe_preserve_worktree
from bernstein.core.agent_reaping import _propagate_abort_to_children as _propagate_abort_to_children
from bernstein.core.agent_reaping import _release_file_ownership as _release_file_ownership
from bernstein.core.agent_reaping import _release_task_to_session as _release_task_to_session
from bernstein.core.agent_reaping import _requeue_rate_limited_task as _requeue_rate_limited_task
from bernstein.core.agent_reaping import _save_partial_work as _save_partial_work
from bernstein.core.agent_reaping import emit_orphan_metrics as emit_orphan_metrics
from bernstein.core.agent_reaping import handle_orphaned_task as handle_orphaned_task
from bernstein.core.agent_reaping import purge_dead_agents as purge_dead_agents
from bernstein.core.agent_reaping import reap_dead_agents as reap_dead_agents

# -- agent_recycling: idle detection, signals, stale/stall checks --
from bernstein.core.agent_recycling import _IDLE_GRACE_S as _IDLE_GRACE_S
from bernstein.core.agent_recycling import _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S as _IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S
from bernstein.core.agent_recycling import _IDLE_HEARTBEAT_THRESHOLD_S as _IDLE_HEARTBEAT_THRESHOLD_S
from bernstein.core.agent_recycling import _IDLE_LIVENESS_EXTENSION_S as _IDLE_LIVENESS_EXTENSION_S
from bernstein.core.agent_recycling import _is_process_alive as _is_process_alive
from bernstein.core.agent_recycling import check_kill_signals as check_kill_signals
from bernstein.core.agent_recycling import check_loops_and_deadlocks as check_loops_and_deadlocks
from bernstein.core.agent_recycling import check_stale_agents as check_stale_agents
from bernstein.core.agent_recycling import check_stalled_tasks as check_stalled_tasks
from bernstein.core.agent_recycling import recycle_idle_agents as recycle_idle_agents
from bernstein.core.agent_recycling import send_shutdown_signals as send_shutdown_signals

# -- agent_state_refresh: state refresh, abort chains, compaction retry --
from bernstein.core.agent_state_refresh import _COMPACT_MAX_RETRIES as _COMPACT_MAX_RETRIES
from bernstein.core.agent_state_refresh import _COMPACT_RETRY_META as _COMPACT_RETRY_META
from bernstein.core.agent_state_refresh import _abort_siblings as _abort_siblings
from bernstein.core.agent_state_refresh import _patch_retry_with_compaction as _patch_retry_with_compaction
from bernstein.core.agent_state_refresh import _try_compact_and_retry as _try_compact_and_retry
from bernstein.core.agent_state_refresh import classify_agent_abort_reason as classify_agent_abort_reason
from bernstein.core.agent_state_refresh import refresh_agent_states as refresh_agent_states

__all__ = [
    "_COMPACT_MAX_RETRIES",
    "_COMPACT_RETRY_META",
    "_IDLE_GRACE_S",
    "_IDLE_HEARTBEAT_THRESHOLD_EVOLVE_S",
    "_IDLE_HEARTBEAT_THRESHOLD_S",
    "_IDLE_LIVENESS_EXTENSION_S",
    "_abort_siblings",
    "_has_git_commits_on_branch",
    "_is_process_alive",
    "_maybe_preserve_worktree",
    "_propagate_abort_to_children",
    "_release_file_ownership",
    "_release_task_to_session",
    "_patch_retry_with_compaction",
    "_requeue_rate_limited_task",
    "_save_partial_work",
    "_try_compact_and_retry",
    "check_kill_signals",
    "check_loops_and_deadlocks",
    "check_stale_agents",
    "check_stalled_tasks",
    "classify_agent_abort_reason",
    "emit_orphan_metrics",
    "handle_orphaned_task",
    "purge_dead_agents",
    "reap_dead_agents",
    "recycle_idle_agents",
    "refresh_agent_states",
    "send_shutdown_signals",
]
