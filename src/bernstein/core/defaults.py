"""Centralized default values for the Bernstein orchestrator.

All magic numbers, timeouts, thresholds, and tuning parameters live here.
Override via bernstein.yaml ``tuning:`` section or environment variables.

Usage::

    from bernstein.core.defaults import ORCHESTRATOR, SPAWN, TASK, AGENT
    timeout = ORCHESTRATOR.drain_timeout_s

To override at runtime (e.g., from parsed bernstein.yaml)::

    from bernstein.core.defaults import override
    override("orchestrator", {"drain_timeout_s": 120.0})

Safety model
------------------------
All ``*Defaults`` dataclasses are ``frozen=True`` - direct attribute mutation
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
from typing import Any, Final, Literal

PY_IDENTIFIER_RE_FRAGMENT = r"[A-Z_][A-Z0-9_]*"
"""Regex fragment for Python identifiers when compiled with ``re.IGNORECASE``."""

SKILLS_AUTO_ROUTE_ENV: Final[str] = "BERNSTEIN_SKILLS_AUTO_ROUTE"
"""Environment variable enabling deterministic skill auto-routing."""

SKILLS_AUTO_ROUTE_DEFAULT_LIMIT: Final[int] = 2
"""Default number of auto-routed skill templates to inject."""

COPILOT_DEFAULT_MODEL: Final[str] = "auto"
"""Copilot model used when no operator-pinned model reaches the adapter; ``auto``
lets Copilot's own router pick the best available model."""

COPILOT_CLAUDE_TIER_MODELS: Final[frozenset[str]] = frozenset({"opus", "sonnet", "haiku"})
"""Claude cascade tier names that are not valid Copilot model ids; any that reach
the Copilot adapter are remapped to ``COPILOT_DEFAULT_MODEL``."""


@dataclass(frozen=True)
class DashboardStaticAsset:
    """Packaged dashboard static asset metadata."""

    file_name: str
    media_type: str


DASHBOARD_STATIC_ASSETS: Mapping[str, DashboardStaticAsset] = MappingProxyType(
    {
        "tailwind-3.4.17.min.js": DashboardStaticAsset(
            file_name="tailwind-3.4.17.min.js",
            media_type="application/javascript",
        ),
        "alpinejs-3.14.8.min.js": DashboardStaticAsset(
            file_name="alpinejs-3.14.8.min.js",
            media_type="application/javascript",
        ),
    }
)

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
    spawn_backoff_max_s: float = 300.0  # cap exponential backoff at 5 min

    drain_timeout_s: float = 60.0
    server_failure_threshold: int = 12  # ticks of server unreachability → stop
    server_failure_warn: int = 3  # warn after N consecutive server failures

    stale_claim_timeout_s: float = 900.0  # 15 min
    deadline_warning_window_s: float = 300.0  # 5 min warning before deadline

    max_dead_agents_kept: int = 20  # bounded dead-agent history for debugging
    max_processed_done: int = 500  # bounded done-task cache to limit memory

    manager_review_completion_threshold: int = 7  # trigger review every 7 done
    manager_review_stall_s: float = 900.0  # 15 min

    # How long a manager session may run with zero child tasks before
    # ``core.orchestration.stalled_manager`` declares a stall and aborts the
    # run. Tunable via ``tuning.orchestrator.stalled_manager_threshold_s`` in
    # bernstein.yaml, or the ``BERNSTEIN_STALL_THRESHOLD_S`` env var (checked
    # first). See ``stalled_manager.py`` module docstring for the detection
    # logic and its relationship to ``AGENT.idle_log_age_threshold_s``.
    stalled_manager_threshold_s: float = 170.0

    # Starting wall-clock kill deadline for a spawned agent (OrchestratorConfig.
    # max_agent_runtime_s). Self-extends +600s/tick up to a 5400s hard cap while
    # the agent is heartbeating (see core/agents/agent_lifecycle.py); this is
    # only the initial value before any extension. Tunable via
    # ``tuning.orchestrator.max_agent_runtime_s``.
    max_agent_runtime_s: int = 1800  # 30 min


# ---------------------------------------------------------------------------
# Spawn / Agent defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnDefaults:
    """Agent spawning, process management, worktree lifecycle."""

    disk_free_threshold_gb: float = 1.0  # refuse spawns below 1 GiB free
    spawn_failure_cooldown_s: float = 300.0  # 5 min
    lesson_cache_ttl_s: float = 300.0  # 5 min


@dataclass(frozen=True)
class AgentDefaults:
    """Heartbeat, idle detection, escalation tiers."""

    heartbeat_stale_s: float = 120.0  # 2 min
    idle_log_age_threshold_s: float = 180.0  # 3 min

    # Escalation tiers (seconds of heartbeat silence)
    escalation_warn_s: float = 60.0  # 1 min silence → warn
    escalation_sigusr1_s: float = 90.0  # 1.5 min → soft nudge via SIGUSR1
    escalation_sigterm_s: float = 120.0  # 2 min → graceful SIGTERM
    escalation_sigkill_s: float = 150.0  # 2.5 min → hard SIGKILL

    # Escalation count thresholds
    escalation_kill_count: int = 7
    escalation_high_count: int = 5
    escalation_med_count: int = 3

    zombie_pid_max_age_s: float = 7 * 24 * 3600  # 7 days

    # Max SDK turns forwarded to ``Runner.run_sync`` by the openai_agents
    # runner (adapters/openai_agents_runner.py). Bug 13 (D2 minimax
    # attempt-e938bd33, 2026-07-02): the SDK's own default of 10 was the
    # dominant failure mode for builtin-tool workflows - backend hit
    # MaxTurnsExceeded AFTER committing + POSTing /complete (wasted-work
    # kill) and qa hit it 3x mid-exploration. 30 is the saner default for
    # multi-tool workflows. Override via ``tuning.agent.max_turns``, the
    # ``BERNSTEIN_MAX_TURNS`` env var, or the runner manifest's
    # ``max_turns`` field. Set to ``None`` to omit the kwarg and fall back
    # to the SDK's own default (10).
    max_turns: int | None = 30


@dataclass(frozen=True)
class SLODefaults:
    """Error-budget floor for the observability SLO/incident subsystem."""

    # ErrorBudget.budget_total always tolerates at least this many failures,
    # even when total_tasks * (1 - slo_target) rounds below it (e.g. a small
    # task count). Raising this delays IncidentManager's auto-pause-on-
    # error-budget-depletion response, useful when early infra-death retries
    # (rate limits, transient auth) shouldn't count against a healthy run.
    # Tunable via ``tuning.slo.error_budget_min_failures``.
    error_budget_min_failures: int = 3


# ---------------------------------------------------------------------------
# Task defaults
# ---------------------------------------------------------------------------


def _freeze_dict_str_float(mapping: dict[str, float]) -> Mapping[str, float]:
    """Return a read-only view over a fresh copy of *mapping*.

    Using :class:`types.MappingProxyType` blocks in-place item mutation so that
    ``TASK.scope_timeout_s['small'] = 1`` raises :class:`TypeError`.
    """
    return MappingProxyType(mapping.copy())


def _freeze_dict_str_int(mapping: dict[str, int]) -> Mapping[str, int]:
    """Read-only view for ``Mapping[str, int]`` default factories."""
    return MappingProxyType(mapping.copy())


def _freeze_dict_str_str(mapping: dict[str, str]) -> Mapping[str, str]:
    """Read-only view for ``Mapping[str, str]`` default factories."""
    return MappingProxyType(mapping.copy())


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

    priority_decay_threshold_hours: float = 24.0  # age boost after 24h stale
    min_priority: int = 3  # floor priority (1=highest) after decay

    subtask_wait_timeout_s: float = 30 * 60  # 30 min
    max_combined_estimated_minutes: int = 60  # cap batched-task total minutes
    max_tasks_per_compacted_batch: int = 5  # cap tasks per batch for focus
    min_batch_size: int = 3  # don't batch below this - single-task faster

    max_io_retries: int = 3  # retry transient filesystem ops up to 3x


# ---------------------------------------------------------------------------
# Token / Context defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenDefaults:
    """Token monitoring, compaction, context management."""

    kill_threshold: int = 50_000  # kill agent if per-turn tokens exceed this
    min_samples_for_growth_check: int = 3  # need 3 samples for trend analysis
    quadratic_ratio: float = 2.0  # 2x growth flags quadratic context blowup
    sample_interval_s: float = 30.0  # sample token count every 30s

    compact_threshold_pct: float = 90.0  # trigger /compact at 90% context
    compact_max_failures: int = 3  # after 3 compact failures, give up
    compact_cooldown_s: float = 120.0  # wait 2 min between compact attempts
    nudge_threshold_pct: float = 80.0  # pre-compact warning at 80% context

    truncation_threshold_pct: float = 80.0  # truncate tool output above 80%
    rejection_threshold_pct: float = 95.0  # reject new work above 95%

    code_block_max_lines: int = 100  # truncate code blocks >100 lines
    file_listing_max_entries: int = 50  # truncate ls/find listings >50 items

    oversized_interval_tokens: int = 20_000  # flag single-turn intervals >20k
    min_loop_samples: int = 3  # need 3 samples to detect token loop


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
                "small": 1.0,  # baseline
                "medium": 1.5,  # 50% more turns for medium scope
                "large": 2.0,  # 2x turns for large scope
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
    opus_budget_multiplier: float = 2.0  # opus costs ~2x sonnet
    batch_max_turns: int = 200  # cap turns per batched run
    rate_limit_cooldown_s: float = 300.0  # 5 min
    rate_limit_cache_ttl_s: float = 180.0  # 3 min
    rate_limit_probe_timeout_s: float = 15.0  # bail probe after 15s
    fallback_cost_per_1k_tokens: float = 0.005  # rough avg when pricing unknown


# ---------------------------------------------------------------------------
# Quality gate defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDefaults:
    """Quality gate thresholds and timeouts."""

    intent_max_diff_chars: int = 8_000  # truncate diff for intent-check LLM
    intent_max_tokens: int = 256  # small LLM reply cap for intent check
    fork_context_max_chars: int = 4_000  # cap context handed to fork gate
    review_max_diff_chars: int = 10_000  # truncate diff for review LLM
    review_max_tokens: int = 1_024  # reply cap for review LLM


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

    poll_interval_s: float = 5.0  # poll approval file every 5s
    max_wait_s: float = 3600.0  # 1 hour


# ---------------------------------------------------------------------------
# Protocol defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtocolDefaults:
    """MCP, cluster, WebSocket protocol tuning."""

    mcp_probe_interval_s: float = 30.0  # health-check MCP server every 30s
    mcp_max_restarts: int = 5  # give up after 5 consecutive restart attempts
    mcp_max_backoff_s: float = 30.0  # cap MCP restart backoff at 30s
    mcp_backoff_multiplier: float = 2.0  # exponential backoff base

    cluster_autoscale_cooldown_s: float = 120.0  # 2 min between scale decisions
    cluster_min_nodes: int = 1  # always keep at least one node alive
    cluster_max_nodes: int = 20
    cluster_steal_threshold: int = 3  # steal work if queue >3 deeper than peer
    cluster_steal_cooldown_s: float = 10.0  # 10s between work-steal attempts


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
                "low": "haiku",  # cheapest model for trivial tasks
                "medium": "sonnet",  # balanced cost/quality default
                "high": "opus",  # highest quality for hard tasks
            }
        )
    )
    free_adapters: tuple[str, ...] = ("qwen", "gemini", "ollama")  # $0 runtime


# ---------------------------------------------------------------------------
# Phase pipeline defaults (opt-in discrete-phase-separation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhasePipelineDefaults:
    """Opt-in research/plan/implement phase separation.

    The pipeline is OFF by default for back-compat - single-phase plan files
    keep their existing behaviour.  Steps opt in by declaring
    ``phases: [research, plan, implement]`` and the global flag below must be
    True for the orchestrator to route through :class:`PhasedRunner`.
    """

    enabled: bool = False
    # No built-in defaults - Bernstein never silently falls back to a Claude
    # tier name. These are currently unread by any consumer (dead code); if a
    # future PhasedRunner reads them, it must treat None as "not configured"
    # and raise/skip rather than guessing a model.
    research_model: str | None = None
    plan_model: str | None = None
    implement_model: str | None = None
    verify_model: str | None = None
    artifact_root: str = ".sdd/runtime/phase_artifacts"
    gc_on_task_close: bool = True
    # Mechanical exit-criteria gate (R001..R005) at every phase boundary.
    # Defaults to True when phases are enabled; the gate runner is a no-op
    # for single-phase tasks regardless of this flag.
    gate_enabled: bool = True
    # Number of retries the failing phase is re-fired before the task is
    # marked ``failed`` with ``failure_kind="phase_gate"``.  v1 default is
    # 1 - one retry is the value that actually closes the loop without
    # busy-looping on a fundamentally broken artefact.
    gate_max_retries: int = 1
    # ``R005-byte-budget`` rejection counts as a hard fail rather than a
    # retry: bloated artefacts usually mean the agent misunderstood the
    # contract and a retry won't help.  Flip to ``False`` to allow retry.
    gate_byte_budget_hard_fail: bool = True


# ---------------------------------------------------------------------------
# Best-of-N delegation defaults (opt-in recursive-best-of-N pattern)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BestOfNDefaults:
    """Opt-in best-of-N candidate fan-out.

    OFF by default for back-compat - single-agent task assignment is
    unchanged.  Tasks opt in by setting ``Task.best_of_n=K``; callers
    must also flip ``BEST_OF_N.enabled`` (typically via the
    ``best_of_n`` section of ``bernstein.yaml``) for the orchestrator to
    actually fan out.
    """

    enabled: bool = False
    default_candidates: int = 1
    max_candidates: int = 5
    judge_enabled: bool = True
    judge_model: str = "haiku"
    score_weight_tests: float = 0.5
    score_weight_lint: float = 0.2
    score_weight_judge: float = 0.2
    score_weight_runtime: float = 0.1


# ---------------------------------------------------------------------------
# Trigger defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerDefaults:
    """Trigger rate limits and file watching."""

    max_tasks_per_minute: int = 20  # global trigger rate cap
    max_tasks_per_trigger_per_hour: int = 50  # per-source cap to avoid spam


# ---------------------------------------------------------------------------
# Janitor / retention defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JanitorDefaults:
    """Disk retention policy for long-running orchestrator artifacts.

    Controls both JSONL append-log rotation thresholds and directory-level
    pruning of per-run artifacts. See.
    """

    # Per-run directory retention
    run_retention_count: int = 20  # keep last 20 runs; older are pruned
    # Per-run WAL file retention under .sdd/runtime/wal/
    wal_retention_count: int = 50  # keep last 50 WAL files per run

    # Rotation thresholds for append-only JSONL files (bytes).
    bridge_lineage_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    task_notifications_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    idempotency_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    file_health_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    file_health_touches_rotate_bytes: int = 10 * 1024 * 1024  # 10 MiB
    replay_rotate_bytes: int = 50 * 1024 * 1024  # 50 MiB per run

    # Persistent fingerprint memoization store cap (MiB).  See
    # bernstein.core.persistence.fingerprint.MemoStore.
    memo_max_mb: int = 200


# ---------------------------------------------------------------------------
# MCP catalog defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogDefaults:
    """Opt-in flags for bundled MCP catalog manifests.

    Local manifests under ``core/protocols/mcp_catalog/manifests/`` are
    "available, disabled by default" until the operator opts in via the
    matching flag here (or its ``mcp.catalog.<entry>.enabled`` override
    in ``bernstein.yaml``). This keeps existing fleets free of surprise
    server registrations on upgrade.
    """

    cocoindex_code_enabled: bool = False  # mcp.catalog.cocoindex_code.enabled


# ---------------------------------------------------------------------------
# MCP tool-search lazy loading defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPToolSearchDefaults:
    """Lazy-loading thresholds for MCP tool descriptions in agent prompts.

    When the combined size of every MCP tool's name + summary + JSON Schema
    exceeds :attr:`threshold_tokens`, the prompt builder swaps the full
    catalog for a ``tool_search`` meta-tool plus a compact name+summary
    directory.  Full schemas are then fetched on demand by the agent.
    """

    enabled: bool = True
    threshold_tokens: int = 6000
    directory_budget_tokens: int = 1500


# ---------------------------------------------------------------------------
# Security defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecurityDefaults:
    """Structural security knobs (orchestration-time, not LLM-driven)."""

    # Lethal-trifecta enforcement: "enforce" denies any agent spawn whose
    # tool chain unions PRIVATE_DATA + UNTRUSTED_INPUT + EXTERNAL_COMM.
    # "warn" logs the violation; "off" disables the check entirely.
    lethal_trifecta_enforcement: Literal["enforce", "warn", "off"] = "enforce"


# ---------------------------------------------------------------------------
# Action-cache defaults (action-caching-replay ticket)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionCacheDefaults:
    """Action-level cache for deterministic LLM/tool replay.

    Layered on :class:`bernstein.core.persistence.fingerprint.MemoStore`
    - the action cache contributes the record schema and key derivation;
    eviction and on-disk format come from MemoStore.

    Modes:
      * ``record`` - always live, append every call to the cache.
      * ``replay`` - cache-only; misses raise ``CacheMiss``.  Used by the
        $0 CI smoke test.
      * ``hybrid`` - try cache, fall through to live on miss (default).
      * ``off``   - disable lookups and writes entirely.
    """

    enabled: bool = True
    mode: str = "hybrid"  # one of: record | replay | hybrid | off
    size_mb: int = 500


# ---------------------------------------------------------------------------
# Schema-validation retry defaults (schema-validation-retry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaRetryDefaults:
    """Bounds for the structured-output validation-retry helper.

    Used by :mod:`bernstein.core.tasks.schema_retry` to cap the number
    of times an agent is asked to fix its own malformed JSON / schema
    failure before the call site gives up.
    """

    max_attempts: int = 3  # industry-standard 2-3 attempts; see Self-Refine ICLR 2024


@dataclass(frozen=True)
class ReworkLedgerDefaults:
    """Thresholds for the rework-rate ledger and cascade auto-promotion.

    The ledger records one sample per (model, effort, phase) attempt and
    reports a rework-rate that the cascade router consults when picking
    a writer model. Auto-promotion fires when both gates pass:

    * ``samples >= min_samples`` - guard against premature decisions
      from a handful of noisy observations.
    * ``rate >= promotion_threshold`` - only promote when the cheap tier
      is observably costing more rework than it saves.

    ``window_hours`` bounds the freshness of considered samples so that
    a single bad afternoon doesn't pin the router on the expensive tier
    forever.
    """

    enabled: bool = True
    promotion_threshold: float = 0.30
    min_samples: int = 20
    window_hours: float = 24.0


@dataclass(frozen=True)
class LineageDefaults:
    """Lineage record schema-v2 configuration (regulator-class trail).

    The customer-signing layer is opt-in: when ``customer_signing_enabled``
    is False (the default), records continue to be written without a
    ``customer_signature`` field - the writer is fully back-compat with
    the v1 chain shipped in PR #996. When True, ``customer_signing_key_path``
    must point at a customer-controlled Ed25519 private key (PEM PKCS#8 or
    raw 32 bytes).

    ``regulatory_class_default`` is an operator-supplied free-text label
    (e.g. ``"production_detection_rule"``) that gets stamped on every
    record produced during the run; the recommended vocabulary is
    documented in ``docs/compliance/regulatory-lineage.md``.
    """

    customer_signing_enabled: bool = False
    customer_signing_key_path: str | None = None
    customer_signing_key_kind: Literal["ed25519", "rsa-4096"] = "ed25519"
    regulatory_class_default: str | None = None
    tamper_alert_enabled: bool = False
    tamper_alert_webhook_url: str | None = None
    tamper_alert_timeout_secs: float = 5.0
    tamper_alert_max_retries: int = 3


@dataclass(frozen=True)
class CompactionDefaults:
    """Thresholds and cost weights for the tiered compaction strategy.

    Used by :mod:`bernstein.core.memory.compaction`. Each tier has a
    relative cost weight (a multiplier on the per-token rate) and a set of
    trigger thresholds the policy consults to pick a tier.

    Cost weights are keyed by the tier's string name (``"micro"`` etc.) so
    this module stays free of an import on the compaction package; the
    package rebuilds the enum-keyed mapping from these values.
    """

    # Relative cost weight per tier (multiplier on the per-token rate).
    cost_weight_none: float = 0.0
    cost_weight_micro: float = 0.05
    cost_weight_auto: float = 0.5
    cost_weight_session_memory: float = 1.0
    cost_weight_time_based: float = 0.1
    # Micro tier: collapse tool-result bodies longer than this, keeping a
    # short head slice for context.
    micro_body_char_threshold: int = 240
    micro_keep_head_chars: int = 80
    # Auto tier: fire at or above this context-use fraction.
    auto_threshold_pct: float = 0.70
    # Time-based tier: fire when idle for at least this many seconds; drop
    # age-tagged blocks older than this many turns.
    idle_threshold_seconds: float = 300.0
    max_block_age_turns: int = 5
    # Shared token estimator: characters per token for English text.
    chars_per_token: int = 4
    # Sensitive-content gate over compaction input (bernstein.yaml tuning
    # section ``compaction``, keys ``sensitive_gate_*``). The gate refuses
    # to forward credential-shaped content to the LLM summary stage.
    sensitive_gate_enabled: bool = True
    # Extra operator-supplied deny patterns (regex strings). Hits are
    # redacted with a typed placeholder.
    sensitive_gate_extra_deny: tuple[str, ...] = ()
    # Allowlist entries: a rule id (``content.aws-access-key``) or a rule
    # id plus the first 8 hex chars of the span hash
    # (``content.aws-access-key:1a2b3c4d``). Suppressions are audit-logged.
    sensitive_gate_allow: tuple[str, ...] = ()


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
PHASE_PIPELINE = PhasePipelineDefaults()
BEST_OF_N = BestOfNDefaults()
TRIGGER = TriggerDefaults()
JANITOR = JanitorDefaults()
CATALOG = CatalogDefaults()
MCP_TOOL_SEARCH = MCPToolSearchDefaults()
SECURITY = SecurityDefaults()
ACTION_CACHE = ActionCacheDefaults()
SCHEMA_RETRY = SchemaRetryDefaults()
LINEAGE = LineageDefaults()
REWORK_LEDGER = ReworkLedgerDefaults()
COMPACTION = CompactionDefaults()
SLO = SLODefaults()

# Module-level constant for direct import - preferred when only the
# numeric cap is needed (no need to import the whole singleton).
SCHEMA_RETRY_MAX_ATTEMPTS: int = SCHEMA_RETRY.max_attempts
MCP_TOOL_SEARCH_ENABLED: bool = MCP_TOOL_SEARCH.enabled
MCP_TOOL_SEARCH_THRESHOLD_TOKENS: int = MCP_TOOL_SEARCH.threshold_tokens

# Abstract-diff PR review augmentation (abstracted-code-review).
ABSTRACT_DIFF_ENABLED: bool = True
ABSTRACT_DIFF_MAX_FILES: int = 50

# Per-model agent mode profiles (smart/deep/fast).  When ``False`` the
# spawner skips preamble injection and tool filtering - useful as a kill
# switch while the feature is rolled out.
MODE_PROFILES_ENABLED: bool = True

# Tiered compaction direct-import constants (preferred when only the
# scalar is needed). Cost weights stay enum-keyed in the compaction
# package, which rebuilds them from the ``COMPACTION`` singleton.
COMPACTION_MICRO_BODY_CHAR_THRESHOLD: int = COMPACTION.micro_body_char_threshold
COMPACTION_MICRO_KEEP_HEAD_CHARS: int = COMPACTION.micro_keep_head_chars
COMPACTION_AUTO_THRESHOLD_PCT: float = COMPACTION.auto_threshold_pct
COMPACTION_IDLE_THRESHOLD_SECONDS: float = COMPACTION.idle_threshold_seconds
COMPACTION_MAX_BLOCK_AGE_TURNS: int = COMPACTION.max_block_age_turns
COMPACTION_CHARS_PER_TOKEN: int = COMPACTION.chars_per_token


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
        "phase_pipeline": "PHASE_PIPELINE",
        "best_of_n": "BEST_OF_N",
        "trigger": "TRIGGER",
        "janitor": "JANITOR",
        "catalog": "CATALOG",
        "mcp_tool_search": "MCP_TOOL_SEARCH",
        "security": "SECURITY",
        "action_cache": "ACTION_CACHE",
        "schema_retry": "SCHEMA_RETRY",
        "lineage": "LINEAGE",
        "rework_ledger": "REWORK_LEDGER",
        "compaction": "COMPACTION",
        "slo": "SLO",
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
        "PHASE_PIPELINE": PhasePipelineDefaults,
        "BEST_OF_N": BestOfNDefaults,
        "TRIGGER": TriggerDefaults,
        "JANITOR": JanitorDefaults,
        "CATALOG": CatalogDefaults,
        "MCP_TOOL_SEARCH": MCPToolSearchDefaults,
        "SECURITY": SecurityDefaults,
        "ACTION_CACHE": ActionCacheDefaults,
        "SCHEMA_RETRY": SchemaRetryDefaults,
        "LINEAGE": LineageDefaults,
        "REWORK_LEDGER": ReworkLedgerDefaults,
        "COMPACTION": CompactionDefaults,
        "SLO": SLODefaults,
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
    module-level attribute is rebound atomically - no mutation of the existing
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
