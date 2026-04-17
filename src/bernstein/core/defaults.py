"""Centralized default values for the Bernstein orchestrator.

All magic numbers, timeouts, thresholds, and tuning parameters live here.
Override via bernstein.yaml ``tuning:`` section or environment variables.

Usage::

    from bernstein.core.defaults import ORCHESTRATOR, SPAWN, TASK, AGENT
    timeout = ORCHESTRATOR.drain_timeout_s

To override at runtime (e.g., from parsed bernstein.yaml)::

    from bernstein.core.defaults import override
    override("orchestrator", {"drain_timeout_s": 120.0})
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Orchestrator defaults
# ---------------------------------------------------------------------------


@dataclass
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

    replenish_cooldown_s: float = 60.0
    replenish_max_tasks: int = 5
    max_ingest_per_tick: int = 50

    evolve_backoff_max: int = 8  # max 8x backoff for empty evolve cycles


# ---------------------------------------------------------------------------
# Spawn / Agent defaults
# ---------------------------------------------------------------------------


@dataclass
class SpawnDefaults:
    """Agent spawning, process management, worktree lifecycle."""

    disk_free_threshold_gb: float = 1.0
    spawn_failure_cooldown_s: float = 300.0  # 5 min
    in_process_wait_timeout_s: float = 5.0
    in_process_poll_timeout_s: float = 0.1
    lesson_cache_ttl_s: float = 300.0  # 5 min


@dataclass
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


@dataclass
class TaskDefaults:
    """Timeouts, retry, priority, batch sizing."""

    scope_timeout_s: dict[str, float] = field(
        default_factory=lambda: {
            "small": 15 * 60,  # 900s  (15 min)
            "medium": 30 * 60,  # 1800s (30 min)
            "large": 60 * 60,  # 3600s (60 min)
        }
    )
    xl_timeout_s: float = 120 * 60  # 7200s (2 hours)

    priority_decay_threshold_hours: float = 24.0
    min_priority: int = 3

    retry_base_delay_s: float = 30.0
    retry_max_backoff_s: float = 300.0  # 5 min
    transient_max_retries: int = 3
    fatal_max_retries: int = 0

    subtask_wait_timeout_s: float = 30 * 60  # 30 min
    max_combined_estimated_minutes: int = 60
    max_tasks_per_compacted_batch: int = 5
    min_batch_size: int = 3

    max_io_retries: int = 3


# ---------------------------------------------------------------------------
# Token / Context defaults
# ---------------------------------------------------------------------------


@dataclass
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

    api_timeout_s: float = 5.0
    api_max_chars: int = 500_000

    high_io_ratio_threshold: float = 10.0
    efficiency_ratio_threshold: float = 3.0
    minimal_output_threshold: int = 100


# ---------------------------------------------------------------------------
# Cost defaults
# ---------------------------------------------------------------------------


@dataclass
class CostDefaults:
    """Budget caps, scope budgets, effort→turns mapping."""

    scope_budget_usd: dict[str, float] = field(
        default_factory=lambda: {
            "small": 2.0,
            "medium": 5.0,
            "large": 15.0,
        }
    )
    scope_multipliers: dict[str, float] = field(
        default_factory=lambda: {
            "small": 1.0,
            "medium": 1.5,
            "large": 2.0,
        }
    )
    effort_base_turns: dict[str, int] = field(
        default_factory=lambda: {
            "max": 100,
            "high": 50,
            "medium": 30,
            "normal": 25,
            "low": 15,
        }
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


@dataclass
class GateDefaults:
    """Quality gate thresholds and timeouts."""

    subprocess_timeout_s: float = 120.0
    intent_max_diff_chars: int = 8_000
    intent_max_tokens: int = 256
    fork_context_max_chars: int = 4_000
    review_max_diff_chars: int = 10_000
    review_max_tokens: int = 1_024


# ---------------------------------------------------------------------------
# Adaptive parallelism defaults
# ---------------------------------------------------------------------------


@dataclass
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


@dataclass
class ApprovalDefaults:
    """Human-in-the-loop approval gate."""

    poll_interval_s: float = 5.0
    max_wait_s: float = 3600.0  # 1 hour


# ---------------------------------------------------------------------------
# Protocol defaults
# ---------------------------------------------------------------------------


@dataclass
class ProtocolDefaults:
    """MCP, cluster, WebSocket protocol tuning."""

    mcp_probe_interval_s: float = 30.0
    mcp_max_restarts: int = 5
    mcp_max_backoff_s: float = 30.0
    mcp_backoff_multiplier: float = 2.0
    mcp_refresh_cooldown_s: float = 60.0
    mcp_readiness_timeout_s: float = 10.0
    mcp_readiness_poll_s: float = 0.5

    cluster_autoscale_cooldown_s: float = 120.0
    cluster_min_nodes: int = 1
    cluster_max_nodes: int = 20
    cluster_steal_threshold: int = 3
    cluster_steal_cooldown_s: float = 10.0

    ws_ping_interval_s: float = 15.0
    ws_max_buffer: int = 256

    sse_read_timeout_s: float = 60.0


# ---------------------------------------------------------------------------
# Plan / Risk defaults
# ---------------------------------------------------------------------------


@dataclass
class PlanDefaults:
    """Planning, risk assessment, cost estimation."""

    tokens_by_scope: dict[str, int] = field(
        default_factory=lambda: {
            "small": 30_000,
            "medium": 80_000,
            "large": 200_000,
        }
    )
    model_by_complexity: dict[str, str] = field(
        default_factory=lambda: {
            "low": "haiku",
            "medium": "sonnet",
            "high": "opus",
        }
    )
    free_adapters: tuple[str, ...] = ("qwen", "gemini", "ollama")


# ---------------------------------------------------------------------------
# Trigger defaults
# ---------------------------------------------------------------------------


@dataclass
class TriggerDefaults:
    """Trigger rate limits and file watching."""

    max_tasks_per_minute: int = 20
    max_tasks_per_trigger_per_hour: int = 50
    file_watch_max_queue: int = 10_000
    dependency_scan_interval_s: float = 7 * 24 * 60 * 60  # weekly


# ---------------------------------------------------------------------------
# Singletons (mutable via override())
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

_SECTION_MAP: dict[str, Any] = {
    "orchestrator": ORCHESTRATOR,
    "spawn": SPAWN,
    "agent": AGENT,
    "task": TASK,
    "token": TOKEN,
    "cost": COST,
    "gate": GATE,
    "parallelism": PARALLELISM,
    "approval": APPROVAL,
    "protocol": PROTOCOL,
    "plan": PLAN,
    "trigger": TRIGGER,
}


def override(section: str, overrides: dict[str, Any]) -> None:
    """Apply runtime overrides from bernstein.yaml ``tuning:`` section.

    Args:
        section: One of the section names (e.g., ``"orchestrator"``).
        overrides: Mapping of field names to new values.

    Raises:
        KeyError: If *section* is not recognized.
        AttributeError: If a field name does not exist on the target dataclass.
    """
    target = _SECTION_MAP[section]
    for key, value in overrides.items():
        if not hasattr(target, key):
            raise AttributeError(
                f"{type(target).__name__} has no field {key!r}. Valid fields: {list(target.__dataclass_fields__)}"
            )
        current = getattr(target, key)
        # Merge dicts instead of replacing
        if isinstance(current, dict) and isinstance(value, dict):
            merged = copy.copy(current)
            merged.update(value)
            object.__setattr__(target, key, merged)
        else:
            object.__setattr__(target, key, value)


def reset() -> None:
    """Reset all sections to their default values (for testing)."""
    global ORCHESTRATOR, SPAWN, AGENT, TASK, TOKEN, COST, GATE
    global PARALLELISM, APPROVAL, PROTOCOL, PLAN, TRIGGER
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
    _SECTION_MAP.update(
        {
            "orchestrator": ORCHESTRATOR,
            "spawn": SPAWN,
            "agent": AGENT,
            "task": TASK,
            "token": TOKEN,
            "cost": COST,
            "gate": GATE,
            "parallelism": PARALLELISM,
            "approval": APPROVAL,
            "protocol": PROTOCOL,
            "plan": PLAN,
            "trigger": TRIGGER,
        }
    )
