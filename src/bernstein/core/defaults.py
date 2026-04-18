"""Centralized default values for the Bernstein orchestrator.

All magic numbers, timeouts, thresholds, and tuning parameters live here.
Override via bernstein.yaml ``tuning:`` section or environment variables.

Usage::

    from bernstein.core.defaults import ORCHESTRATOR, SPAWN, TASK, AGENT
    timeout = ORCHESTRATOR.drain_timeout_s

To override at runtime (e.g., from parsed bernstein.yaml)::

    from bernstein.core.defaults import override
    override("orchestrator", {"drain_timeout_s": 120.0})

Safety model (audit-155)
------------------------
All ``*Defaults`` dataclasses are ``frozen=True`` — direct attribute mutation
(``COST.foo = 1``) raises :class:`dataclasses.FrozenInstanceError`.  Dict
default-factory fields are wrapped in :class:`types.MappingProxyType`, so
inner-item mutation (``COST.effort_base_turns['max'] = 0``) raises
:class:`TypeError`.

:func:`override` and :func:`reset` never mutate in place.  They build a new
instance via :func:`dataclasses.replace` and rebind the module-level singleton
(``setattr(module, SECTION_UPPER, new)``) atomically.  Consumers that read
defaults through the module (``_defaults.ORCHESTRATOR.tick_interval_s``) see
the new value immediately; consumers that captured a reference via
``from bernstein.core.defaults import X`` keep the snapshot they imported.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

# ---------------------------------------------------------------------------
# Orchestrator defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorDefaults:
    """Run loop, tick scheduling, drain, and convergence."""

    tick_interval_s: float = 3.0
    normal_tick_phase: int = 6  # run normal ops every N ticks
    slow_tick_phase: int = 30  # run slow ops every N ticks

    max_consecutive_failures: int = 10  # tick failures before abort
    max_spawn_failures: int = 3  # consecutive spawn failures → mark failed
    spawn_backoff_base_s: float = 30.0
    spawn_backoff_max_s: float = 300.0

    drain_timeout_s: float = 60.0
    server_failure_threshold: int = 12  # ticks of server unreachability → stop
    server_failure_warn: int = 3

    stale_claim_timeout_s: float = 900.0  # 15 min
    deadline_warning_window_s: float = 300.0  # 5 min warning before deadline

    max_dead_agents_kept: int = 20
    max_processed_done: int = 500

    manager_review_completion_threshold: int = 7
    manager_review_stall_s: float = 900.0  # 15 min


# ---------------------------------------------------------------------------
# Spawn / Agent defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnDefaults:
    """Agent spawning, process management, worktree lifecycle."""

    disk_free_threshold_gb: float = 1.0
    spawn_failure_cooldown_s: float = 300.0  # 5 min
    lesson_cache_ttl_s: float = 300.0  # 5 min


@dataclass(frozen=True)
class AgentDefaults:
    """Heartbeat, idle detection, escalation tiers."""

    heartbeat_stale_s: float = 120.0  # 2 min
    idle_log_age_threshold_s: float = 180.0  # 3 min

    # Escalation tiers (seconds of heartbeat silence)
    escalation_warn_s: float = 60.0
    escalation_sigusr1_s: float = 90.0
    escalation_sigterm_s: float = 120.0
    escalation_sigkill_s: float = 150.0

    # Escalation count thresholds
    escalation_kill_count: int = 7
    escalation_high_count: int = 5
    escalation_med_count: int = 3

    zombie_pid_max_age_s: float = 7 * 24 * 3600  # 7 days


# ---------------------------------------------------------------------------
# Task defaults
# ---------------------------------------------------------------------------


def _freeze_dict_str_float(mapping: dict[str, float]) -> Mapping[str, float]:
    """Return a read-only view over a fresh copy of *mapping*.

    Using :class:`types.MappingProxyType` blocks in-place item mutation so that
    ``TASK.scope_timeout_s['small'] = 1`` raises :class:`TypeError`.
    """
    return MappingProxyType(dict(mapping))


def _freeze_dict_str_int(mapping: dict[str, int]) -> Mapping[str, int]:
    """Read-only view for ``Mapping[str, int]`` default factories."""
    return MappingProxyType(dict(mapping))


def _freeze_dict_str_str(mapping: dict[str, str]) -> Mapping[str, str]:
    """Read-only view for ``Mapping[str, str]`` default factories."""
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class TaskDefaults:
    """Timeouts, retry, priority, batch sizing."""

    scope_timeout_s: Mapping[str, float] = field(
        default_factory=lambda: _freeze_dict_str_float(
            {
                "small": 15 * 60,  # 900s  (15 min)
                "medium": 30 * 60,  # 1800s (30 min)
                "large": 60 * 60,  # 3600s (60 min)
            }
        )
    )
    xl_timeout_s: float = 120 * 60  # 7200s (2 hours)

    priority_decay_threshold_hours: float = 24.0
    min_priority: int = 3

    subtask_wait_timeout_s: float = 30 * 60  # 30 min
    max_combined_estimated_minutes: int = 60
    max_tasks_per_compacted_batch: int = 5
    min_batch_size: int = 3

    max_io_retries: int = 3


# ---------------------------------------------------------------------------
# Token / Context defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenDefaults:
    """Token monitoring, compaction, context management."""

    kill_threshold: int = 50_000
    min_samples_for_growth_check: int = 3
    quadratic_ratio: float = 2.0
    sample_interval_s: float = 30.0

    compact_threshold_pct: float = 90.0
    compact_max_failures: int = 3
    compact_cooldown_s: float = 120.0
    nudge_threshold_pct: float = 80.0

    truncation_threshold_pct: float = 80.0
    rejection_threshold_pct: float = 95.0

    code_block_max_lines: int = 100
    file_listing_max_entries: int = 50

    oversized_interval_tokens: int = 20_000
    min_loop_samples: int = 3


# ---------------------------------------------------------------------------
# Cost defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostDefaults:
    """Budget caps, scope budgets, effort→turns mapping."""

    scope_budget_usd: Mapping[str, float] = field(
        default_factory=lambda: _freeze_dict_str_float(
            {
                "small": 2.0,
                "medium": 5.0,
                "large": 15.0,
            }
        )
    )
    scope_multipliers: Mapping[str, float] = field(
        default_factory=lambda: _freeze_dict_str_float(
            {
                "small": 1.0,
                "medium": 1.5,
                "large": 2.0,
            }
        )
    )
    effort_base_turns: Mapping[str, int] = field(
        default_factory=lambda: _freeze_dict_str_int(
            {
                "max": 100,
                "high": 50,
                "medium": 30,
                "normal": 25,
                "low": 15,
            }
        )
    )
    opus_budget_multiplier: float = 2.0
    batch_max_turns: int = 200
    rate_limit_cooldown_s: float = 300.0  # 5 min
    rate_limit_cache_ttl_s: float = 180.0  # 3 min
    rate_limit_probe_timeout_s: float = 15.0
    fallback_cost_per_1k_tokens: float = 0.005


# ---------------------------------------------------------------------------
# Quality gate defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDefaults:
    """Quality gate thresholds and timeouts."""

    intent_max_diff_chars: int = 8_000
    intent_max_tokens: int = 256
    fork_context_max_chars: int = 4_000
    review_max_diff_chars: int = 10_000
    review_max_tokens: int = 1_024


# ---------------------------------------------------------------------------
# Adaptive parallelism defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParallelismDefaults:
    """CPU-aware spawn throttling and error-rate windows."""

    error_rate_high: float = 0.20  # 20%
    error_rate_low: float = 0.05  # 5%
    low_error_sustain_s: float = 120.0  # 2 min
    cpu_pause_threshold: float = 300.0  # 3 cores pinned
    window_s: float = 600.0  # 10 min


# ---------------------------------------------------------------------------
# Approval defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalDefaults:
    """Human-in-the-loop approval gate."""

    poll_interval_s: float = 5.0
    max_wait_s: float = 3600.0  # 1 hour


# ---------------------------------------------------------------------------
# Protocol defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolDefaults:
    """MCP, cluster, WebSocket protocol tuning."""

    mcp_probe_interval_s: float = 30.0
    mcp_max_restarts: int = 5
    mcp_max_backoff_s: float = 30.0
    mcp_backoff_multiplier: float = 2.0

    cluster_autoscale_cooldown_s: float = 120.0
    cluster_min_nodes: int = 1
    cluster_max_nodes: int = 20
    cluster_steal_threshold: int = 3
    cluster_steal_cooldown_s: float = 10.0


# ---------------------------------------------------------------------------
# Plan / Risk defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanDefaults:
    """Planning, risk assessment, cost estimation."""

    tokens_by_scope: Mapping[str, int] = field(
        default_factory=lambda: _freeze_dict_str_int(
            {
                "small": 30_000,
                "medium": 80_000,
                "large": 200_000,
            }
        )
    )
    model_by_complexity: Mapping[str, str] = field(
        default_factory=lambda: _freeze_dict_str_str(
            {
                "low": "haiku",
                "medium": "sonnet",
                "high": "opus",
            }
        )
    )
    free_adapters: tuple[str, ...] = ("qwen", "gemini", "ollama")


# ---------------------------------------------------------------------------
# Trigger defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerDefaults:
    """Trigger rate limits and file watching."""

    max_tasks_per_minute: int = 20
    max_tasks_per_trigger_per_hour: int = 50


# ---------------------------------------------------------------------------
# Janitor / retention defaults (audit-081)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JanitorDefaults:
    """Disk retention policy for long-running orchestrator artifacts.

    Controls both JSONL append-log rotation thresholds and directory-level
    pruning of per-run artifacts. See audit-081.
    """

    # Per-run directory retention
    run_retention_count: int = 20
    # Per-run WAL file retention under .sdd/runtime/wal/
    wal_retention_count: int = 50

    # Rotation thresholds for append-only JSONL files (bytes).
    bridge_lineage_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    task_notifications_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    idempotency_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    file_health_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    file_health_touches_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    replay_rotate_bytes: int = 50 * 1024 * 1024  # 50 MiB per run


# ---------------------------------------------------------------------------
# Singletons (rebindable via override()/reset())
# ---------------------------------------------------------------------------

ORCHESTRATOR = OrchestratorDefaults()
SPAWN = SpawnDefaults()
AGENT = AgentDefaults()
TASK = TaskDefaults()
TOKEN = TokenDefaults()
COST = CostDefaults()
GATE = GateDefaults()
PARALLELISM = ParallelismDefaults()
APPROVAL = ApprovalDefaults()
PROTOCOL = ProtocolDefaults()
PLAN = PlanDefaults()
TRIGGER = TriggerDefaults()
JANITOR = JanitorDefaults()


# Mapping of section name (as used in bernstein.yaml ``tuning:`` blocks) to the
# module-level attribute that stores the singleton.  We rebind the attribute
# rather than mutate in place so the frozen dataclass invariant holds.
_SECTION_TO_ATTR: Mapping[str, str] = MappingProxyType(
    {
        "orchestrator": "ORCHESTRATOR",
        "spawn": "SPAWN",
        "agent": "AGENT",
        "task": "TASK",
        "token": "TOKEN",
        "cost": "COST",
        "gate": "GATE",
        "parallelism": "PARALLELISM",
        "approval": "APPROVAL",
        "protocol": "PROTOCOL",
        "plan": "PLAN",
        "trigger": "TRIGGER",
        "janitor": "JANITOR",
    }
)


# Mapping of module attribute name → dataclass factory used by :func:`reset`.
_ATTR_TO_FACTORY: Mapping[str, type[Any]] = MappingProxyType(
    {
        "ORCHESTRATOR": OrchestratorDefaults,
        "SPAWN": SpawnDefaults,
        "AGENT": AgentDefaults,
        "TASK": TaskDefaults,
        "TOKEN": TokenDefaults,
        "COST": CostDefaults,
        "GATE": GateDefaults,
        "PARALLELISM": ParallelismDefaults,
        "APPROVAL": ApprovalDefaults,
        "PROTOCOL": ProtocolDefaults,
        "PLAN": PlanDefaults,
        "TRIGGER": TriggerDefaults,
        "JANITOR": JanitorDefaults,
    }
)


def _freeze_mapping(value: Any) -> Any:
    """Wrap plain ``dict`` values in :class:`MappingProxyType`.

    Used by :func:`override` so that a caller passing a fresh dict for a
    mapping field cannot retain a live mutable handle to the defaults.
    """
    if isinstance(value, dict):
        clone: dict[Any, Any] = dict(value)  # type: ignore[arg-type]
        return MappingProxyType(clone)
    return value


def override(section: str, overrides: dict[str, Any]) -> None:
    """Apply runtime overrides from bernstein.yaml ``tuning:`` section.

    The targeted singleton is rebuilt via :func:`dataclasses.replace` and the
    module-level attribute is rebound atomically — no mutation of the existing
    frozen instance occurs.  For mapping fields, the override payload is merged
    with the current view (new keys win, omitted keys are preserved) and the
    merged result is re-wrapped in :class:`MappingProxyType` to keep the
    read-only invariant.

    Args:
        section: One of the section names (e.g., ``"orchestrator"``).
        overrides: Mapping of field names to new values.

    Raises:
        KeyError: If *section* is not recognized.
        AttributeError: If a field name does not exist on the target dataclass.
    """
    try:
        attr_name = _SECTION_TO_ATTR[section]
    except KeyError:
        raise KeyError(section) from None

    module = sys.modules[__name__]
    current: Any = getattr(module, attr_name)
    fields = current.__dataclass_fields__

    changes: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in fields:
            raise AttributeError(f"{type(current).__name__} has no field {key!r}. Valid fields: {list(fields)}")
        existing: Any = getattr(current, key)
        # Merge mapping fields rather than replacing, matching legacy
        # behaviour (callers pass partial dicts from bernstein.yaml).
        if isinstance(existing, Mapping) and isinstance(value, dict):
            merged: dict[Any, Any] = dict(existing)  # type: ignore[arg-type]
            merged.update(value)  # type: ignore[arg-type]
            changes[key] = MappingProxyType(merged)
        else:
            changes[key] = _freeze_mapping(value)

    new_instance = replace(current, **changes)
    setattr(module, attr_name, new_instance)


def reset() -> None:
    """Reset all sections to their default values (for testing).

    Rebuilds each singleton from its dataclass factory and rebinds the
    module-level attribute.  After :func:`reset`, any caller looking up
    ``bernstein.core.defaults.<SECTION>`` via attribute access sees the
    fresh instance.
    """
    module = sys.modules[__name__]
    for attr_name, factory in _ATTR_TO_FACTORY.items():
        setattr(module, attr_name, factory())
