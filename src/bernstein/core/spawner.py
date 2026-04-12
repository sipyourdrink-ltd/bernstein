"""Spawn short-lived CLI agents for task batches.

This module is a re-export shim.  The implementation has been decomposed
into focused sub-modules:

- ``spawner_core`` — AgentSpawner class, prompt rendering, caches
- ``spawner_merge`` — merge, push retry, trace finalization, reap helpers
- ``spawner_worktree`` — worktree lifecycle (create / cleanup / prune)
- ``spawner_warm_pool`` — routing helpers, role config, tool allowlist
"""

from __future__ import annotations

# -- Module-level caches (re-exported for backward compat) --------------------
from bernstein.core.spawner_core import _DIR_CACHE as _DIR_CACHE
from bernstein.core.spawner_core import _FILE_CACHE as _FILE_CACHE

# -- Core class and prompt utilities ------------------------------------------
from bernstein.core.spawner_core import AgentSpawner as AgentSpawner
from bernstein.core.spawner_core import _extract_tags_from_tasks as _extract_tags_from_tasks
from bernstein.core.spawner_core import _health_check_interval as _health_check_interval
from bernstein.core.spawner_core import _inject_scheduled_tasks as _inject_scheduled_tasks
from bernstein.core.spawner_core import _list_subdirs_cached as _list_subdirs_cached
from bernstein.core.spawner_core import _read_cached as _read_cached
from bernstein.core.spawner_core import _render_auth_section as _render_auth_section
from bernstein.core.spawner_core import _render_batch_prompt as _render_batch_prompt
from bernstein.core.spawner_core import _render_fallback as _render_fallback
from bernstein.core.spawner_core import _render_predecessor_context as _render_predecessor_context
from bernstein.core.spawner_core import _render_prompt as _render_prompt
from bernstein.core.spawner_core import _render_signal_check as _render_signal_check
from bernstein.core.spawner_core import _sanitise_for_log as _sanitise_for_log

# -- Warm pool, routing, and tool allowlist -----------------------------------
from bernstein.core.spawner_warm_pool import _load_role_config as _load_role_config
from bernstein.core.spawner_warm_pool import _select_batch_config as _select_batch_config
from bernstein.core.spawner_warm_pool import _should_use_router as _should_use_router
from bernstein.core.spawner_warm_pool import build_tool_allowlist_env as build_tool_allowlist_env
from bernstein.core.spawner_warm_pool import check_tool_allowed as check_tool_allowed
from bernstein.core.spawner_warm_pool import parse_tool_allowlist_env as parse_tool_allowlist_env

__all__ = [
    "_DIR_CACHE",
    "_FILE_CACHE",
    "AgentSpawner",
    "_extract_tags_from_tasks",
    "_health_check_interval",
    "_inject_scheduled_tasks",
    "_list_subdirs_cached",
    "_load_role_config",
    "_read_cached",
    "_render_auth_section",
    "_render_batch_prompt",
    "_render_fallback",
    "_render_predecessor_context",
    "_render_prompt",
    "_render_signal_check",
    "_sanitise_for_log",
    "_select_batch_config",
    "_should_use_router",
    "build_tool_allowlist_env",
    "check_tool_allowed",
    "parse_tool_allowlist_env",
]
