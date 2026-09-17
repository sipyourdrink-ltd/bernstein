"""Spawn short-lived CLI agents for task batches.

This module re-exports everything from ``spawner_core`` (and helper
sub-modules) so that existing imports
(``from bernstein.core.spawner import AgentSpawner``) keep working
without duplicating ~2900 lines of implementation.
"""

from __future__ import annotations

# --- Core spawner class and prompt-rendering helpers -----------------------
from bernstein.core.agents.spawner_core import (
    AgentSpawner,
    _extract_tags_from_tasks,
    _health_check_interval,
    _inject_scheduled_tasks,
    _list_subdirs_cached,
    _read_cached,
    _render_auth_section,
    _render_batch_prompt,
    _render_fallback,
    _render_predecessor_context,
    _render_prompt,
    _render_signal_check,
    _sanitise_for_log,
    logger,
)

# --- Warm-pool helpers (routing / tool allowlist) --------------------------
from bernstein.core.agents.spawner_warm_pool import (
    _TOOL_ALLOWLIST_ENV_VAR,
    _load_role_config,
    _select_batch_config,
    _should_use_router,
    build_tool_allowlist_env,
    check_tool_allowed,
    parse_tool_allowlist_env,
)

__all__ = [
    "_TOOL_ALLOWLIST_ENV_VAR",
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
    "logger",
    "parse_tool_allowlist_env",
]
