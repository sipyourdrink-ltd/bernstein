"""Orchestrator loop: watch tasks, spawn agents, verify completion, repeat.

The orchestrator is DETERMINISTIC CODE, not an LLM. It matches tasks to agents
via the spawner and verifies completion via the janitor. See ADR-001.

Design note: the tick loop is single-threaded (``while self._running`` in
``run``), so no concurrent-tick guard is required. If threaded ticks are ever
introduced, reintroduce a non-blocking guard (see git history for the removed
``tick_guard`` / ``concurrency_guard`` modules).

This module is the public facade. Heavy lifting lives in:
- tick_pipeline.py   - task fetching, batching, server interaction, TypedDicts
- task_lifecycle.py  - claim/spawn, completion processing, retry/decompose
- agent_lifecycle.py - agent tracking, heartbeat, crash detection, reaping
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import itertools
import json
import logging
import os
import re
import signal
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from bernstein.core.agent_lifecycle import (
    reap_dead_agents,
    refresh_agent_states,
)
from bernstein.core.agent_recycling import (
    check_kill_signals,
    check_loops_and_deadlocks,
    check_stale_agents,
    check_stalled_tasks,
    recycle_idle_agents,
    send_shutdown_signals,
)
from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.approval import ApprovalGate, ApprovalMode
from bernstein.core.bandit_router import BanditRouter
from bernstein.core.batch_api import ProviderBatchManager
from bernstein.core.bulletin import BulletinBoard, BulletinMessage, SignalActionFailure
from bernstein.core.cluster import NodeHeartbeatClient
from bernstein.core.context import refresh_knowledge_base
from bernstein.core.context_degradation_detector import (
    ContextDegradationConfig,
    ContextDegradationDetector,
)
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.cost.budget_actions import BudgetAction, BudgetPolicy, apply_policy
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.defaults import ORCHESTRATOR
from bernstein.core.dep_validator import DependencyValidator
from bernstein.core.dependency_scan import (
    DependencyScanStatus,
    DependencyVulnerabilityFinding,
    DependencyVulnerabilityScanner,
)
from bernstein.core.fast_path import (
    FastPathStats,
    load_fast_path_config,
)
from bernstein.core.file_locks import FileLockManager
from bernstein.core.hook_events import HookEvent
from bernstein.core.incident import IncidentManager
from bernstein.core.knowledge.task_graph import TaskGraph
from bernstein.core.manifest import build_manifest, save_manifest
from bernstein.core.memory_guard import MemoryGuard
from bernstein.core.merge_queue import MergeQueue
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    AgentSession,
    BatchConfig,
    ClusterConfig,
    ClusterTopology,
    ContainerIsolationConfig,
    NodeCapacity,
    OrchestratorConfig,
    ProgressSnapshot,
    Task,
    TestAgentConfig,
)
from bernstein.core.notifications import NotificationManager, NotificationPayload, NotificationTarget
from bernstein.core.orchestration.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.orchestration.evolution import EvolutionCoordinator
from bernstein.core.orchestration.tick_pipeline import (
    CompletionData,
    RuffViolation,
    TestResults,
    block_task,
    complete_task,
    fail_task,
    fetch_active_holds,
    fetch_all_tasks,
    group_by_role,
    parse_backlog_file,
)
from bernstein.core.orchestration.tick_pipeline import (
    compute_total_spent as compute_total_spent,
)
from bernstein.core.orchestration.tick_pipeline import (
    total_spent_cache as total_spent_cache,
)
from bernstein.core.quality_gate_coalescer import QualityGateCoalescer
from bernstein.core.quarantine import QuarantineStore
from bernstein.core.quota_poller import QuotaPoller
from bernstein.core.rate_limit_tracker import RateLimitTracker
from bernstein.core.replay.journal import EventJournal, seal_journal_into_spine
from bernstein.core.retrospective import generate_retrospective
from bernstein.core.router import TierAwareRouter, load_model_policy_from_yaml, load_providers_from_yaml
from bernstein.core.runbooks import RunbookEngine
from bernstein.core.runtime_state import (
    SessionReplayMetadata,
    current_git_branch,
    current_git_sha,
    hash_file,
    rotate_log_file,
    write_session_replay_metadata,
)
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.seed import resolve_seed_path
from bernstein.core.semantic_cache import ResponseCacheManager
from bernstein.core.signals import read_unresolved_pivots
from bernstein.core.slo import SLOTracker, apply_error_budget_adjustments
from bernstein.core.task_grouping import compact_small_tasks
from bernstein.core.task_lifecycle import (
    auto_decompose_task,
    claim_and_spawn_batches,
    collect_completion_data,
    evict_degraded_sessions,
    maybe_retry_task,
    prepare_speculative_warm_pool,
    process_completed_tasks,
    retry_or_fail_task,
    should_auto_decompose,
)
from bernstein.core.token_monitor import check_token_growth
from bernstein.core.wal import WALEntry, WALReader, WALRecovery, WALWriter
from bernstein.core.wal_replay import WALReplayEngine
from bernstein.core.watchdog import WatchdogManager, collect_watchdog_findings
from bernstein.core.workflow import WorkflowExecutor, load_workflow
from bernstein.evolution.governance import AdaptiveGovernor
from bernstein.evolution.risk import RiskScorer

_BERNSTEIN_YAML = "bernstein.yaml"

# Preserve underscore-prefixed aliases so existing test imports keep working
_compute_total_spent = compute_total_spent
_total_spent_cache = total_spent_cache

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.backlog_parser import ParsedBacklogTask
    from bernstein.core.container import ContainerConfig
    from bernstein.core.permission_mode import PermissionMode
    from bernstein.core.protocols.cluster.mesh_coordinator import MeshCoordinator
    from bernstein.core.quality_gates import QualityGatesConfig
    from bernstein.core.spawner import AgentSpawner
    from bernstein.evolution.loop import EvolutionLoop

logger = logging.getLogger(__name__)


def start_cluster_coordinator(
    cluster_cfg: ClusterConfig | None,
    *,
    sdd_dir: Path,
) -> MeshCoordinator | None:
    """Start the coordination path this cluster topology selects (#2558).

    ``ClusterTopology.MESH`` was declared in the enum and accepted by config
    but nothing ever branched on it, so a ``topology: mesh`` deployment silently
    ran STAR. This is that branch.

    * **STAR** (and ``hierarchical``, and cluster mode off) -> ``None``. The
      central ``NodeRegistry`` and ``POST /cluster/steal`` remain the arbiter
      and nothing about that path changes.
    * **MESH** -> a :class:`MeshCoordinator` over the signed, Merkle-chained
      claim journal. There is no central arbiter to consult: nodes self-claim
      against the journal and converge by the deterministic ``entry_hash``
      rule.

    Provisioning the journal and the node signing identity happens here, at
    startup, so a misconfigured MESH deployment fails at boot with one loud log
    line rather than mid-run on the first claim.

    Args:
        cluster_cfg: The resolved cluster config, or ``None`` when cluster mode
            is off.
        sdd_dir: The project ``.sdd`` directory holding the journal and the
            node signing identity.

    Returns:
        The MESH coordinator, or ``None`` for every non-MESH topology.
    """
    if cluster_cfg is None or cluster_cfg.topology is not ClusterTopology.MESH:
        return None
    from bernstein.core.protocols.cluster.mesh_coordinator import build_mesh_coordinator

    coordinator = build_mesh_coordinator(cluster_cfg, sdd_dir=sdd_dir)
    if coordinator is None:
        logger.error(
            "cluster.topology is 'mesh' but the claim journal could not be provisioned; "
            "leaderless coordination is disabled",
        )
        return None
    logger.info(
        "MESH topology: leaderless claim journal at %s (node %s, %d gossip peer(s)); "
        "the central node registry and /cluster/steal are not used",
        coordinator.journal.path,
        coordinator.node_id,
        len(coordinator.peers),
    )
    return coordinator


def _build_container_config(iso: ContainerIsolationConfig) -> ContainerConfig | None:
    """Build a ContainerConfig from OrchestratorConfig container_isolation settings.

    Args:
        iso: Container isolation settings from OrchestratorConfig.

    Returns:
        ContainerConfig ready for AgentSpawner, or None if isolation is disabled.
    """
    if not iso.enabled:
        return None

    from bernstein.core.container import (
        ContainerConfig,
        ContainerRuntime,
        NetworkMode,
        ResourceLimits,
        SecurityProfile,
        TwoPhaseSandboxConfig,
    )

    two_phase: TwoPhaseSandboxConfig | None = None
    if iso.two_phase_sandbox:
        two_phase = TwoPhaseSandboxConfig(
            setup_commands=iso.sandbox_setup_commands,
        )

    try:
        runtime = ContainerRuntime(iso.runtime)
    except ValueError:
        logger.warning("Unknown container runtime %r, falling back to docker", iso.runtime)
        runtime = ContainerRuntime.DOCKER

    try:
        network = NetworkMode(iso.network_mode)
    except ValueError:
        logger.warning("Unknown network mode %r, falling back to host", iso.network_mode)
        network = NetworkMode.HOST

    return ContainerConfig(
        runtime=runtime,
        image=iso.image,
        resource_limits=ResourceLimits(
            cpu_cores=iso.cpu_cores,
            memory_mb=iso.memory_mb,
            pids_limit=iso.pids_limit,
            read_only_rootfs=iso.read_only_rootfs,
        ),
        security=SecurityProfile(
            drop_capabilities=tuple(iso.drop_capabilities),
        ),
        network_mode=network,
        two_phase_sandbox=two_phase,
    )


# ---------------------------------------------------------------------------
# Backward-compatible aliases so external code that does
#   from bernstein.core.orchestration.orchestrator import _fail_task, _complete_task, ...
# continues to work.
# ---------------------------------------------------------------------------
_EVENT_RUN_COMPLETED = "run.completed"
_EVENT_TASK_COMPLETED = HookEvent.TASK_COMPLETED.value
_EVENT_TASK_FAILED = HookEvent.TASK_FAILED.value
# ---------------------------------------------------------------------------
_task_from_dict: Callable[[dict[str, Any]], Task] = Task.from_dict
_fetch_all_tasks = fetch_all_tasks
_fail_task = fail_task
_block_task = block_task
_complete_task = complete_task
_parse_backlog_file = parse_backlog_file


class ShutdownInProgress(RuntimeError):
    """Raised when a spawn is attempted after shutdown has started."""


class Orchestrator:
    """The main loop: watch tasks, spawn agents, verify completion, repeat.

    The orchestrator is a deterministic scheduler. It never calls an LLM
    directly. It polls the task server, groups work into batches, spawns
    short-lived agents via the spawner, and verifies done tasks via the
    janitor.

    Args:
        config: Orchestrator tuning knobs.
        spawner: Agent spawner (owns the CLI adapter).
        workdir: Project working directory for janitor verification.
        client: httpx client for server communication (injectable for testing).
    """

    _SPAWN_BACKOFF_BASE_S: float = ORCHESTRATOR.spawn_backoff_base_s
    _SPAWN_BACKOFF_MAX_S: float = ORCHESTRATOR.spawn_backoff_max_s
    _MAX_SPAWN_FAILURES: int = ORCHESTRATOR.max_spawn_failures
    _MAX_DEAD_AGENTS_KEPT: int = ORCHESTRATOR.max_dead_agents_kept
    _MAX_PROCESSED_DONE: int = ORCHESTRATOR.max_processed_done
    _MANAGER_REVIEW_COMPLETION_THRESHOLD: int = ORCHESTRATOR.manager_review_completion_threshold
    _MANAGER_REVIEW_STALL_S: float = ORCHESTRATOR.manager_review_stall_s
    _STALE_CLAIM_TIMEOUT_S: float = ORCHESTRATOR.stale_claim_timeout_s

    def __init__(
        self,
        config: OrchestratorConfig,
        spawner: AgentSpawner,
        workdir: Path,
        client: httpx.Client | None = None,
        evolution: EvolutionCoordinator | None = None,
        router: TierAwareRouter | None = None,
        bulletin: BulletinBoard | None = None,
        cluster_config: ClusterConfig | None = None,
        notifier: NotificationManager | None = None,
        quality_gate_config: QualityGatesConfig | None = None,
        formal_verification_config: Any | None = None,
    ) -> None:
        self._config = config
        self._spawner = spawner
        self._warm_pool = getattr(spawner, "_warm_pool", None)
        self._workdir = workdir

        # Resolve the effective permission mode once at startup so every
        # tick and spawn uses the same mode consistently.
        from bernstein.core.permission_mode import resolve_mode

        self._permission_mode = resolve_mode(config.permission_mode)
        self._bulletin: BulletinBoard | None = bulletin
        self._notifier: NotificationManager | None = notifier
        self._cluster_config = cluster_config
        self._quality_gate_config: QualityGatesConfig | None = quality_gate_config
        self._gate_coalescer: QualityGateCoalescer = QualityGateCoalescer()
        # Formal verification gate is invoked by task_lifecycle._run_verification_gates
        # only when OrchestratorConfig.formal_verification_enabled is True. Default
        # remains False so deployments without Z3/Lean4 installed are unaffected.
        self._formal_verification_config: Any | None = formal_verification_config
        _headers: dict[str, str] = {}
        if config.auth_token:
            _headers["Authorization"] = f"Bearer {config.auth_token}"
        self._client = client or httpx.Client(
            timeout=10.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            headers=_headers,
        )
        self._agents: dict[str, AgentSession] = {}
        self._lock_manager = FileLockManager(workdir)
        self._file_ownership: dict[str, str] = {}  # filepath -> agent_id (legacy alias; use _lock_manager)
        from bernstein.core.loop_detector import LoopDetector

        self._loop_detector = LoopDetector()
        self._loop_mtime_cache: dict[str, float] = {}  # file_path -> last observed mtime
        import uuid as _uuid

        self.session_id: str = _uuid.uuid4().hex[:16]  # Unique ID for this orchestrator session
        self._task_to_session: dict[str, str] = {}  # task_id -> agent_id (reverse index)
        self._batch_sessions: dict[str, AgentSession] = {}
        self._processed_done_tasks: collections.OrderedDict[str, None] = collections.OrderedDict()  # FIFO eviction
        self._retried_task_ids: set[str] = set()  # tasks that already have a retry queued
        self._decomposed_task_ids: set[str] = set()  # large tasks queued for decomposition
        # Crash recovery: per-task crash count and preserved worktrees for resume
        self._crash_counts: dict[str, int] = {}  # task_id -> crash count
        self._preserved_worktrees: dict[str, Path] = {}  # task_id -> worktree to reuse
        # Agent affinity: task_id -> preferred_agent_id for downstream tasks
        # Populated when a task completes; used by group_by_role to batch related work.
        self._agent_affinity: dict[str, str] = {}
        self._running = False
        self._tick_count = 0
        self._consecutive_server_failures: int = 0
        self._cached_critical_path_ids: set[str] = set()
        self._dependency_scanner = DependencyVulnerabilityScanner(
            workdir,
            enabled=config.dependency_scan_enabled,
        )
        # Track spawn failures per batch for backoff: task_ids -> (fail_count, last_fail_ts)
        self._spawn_failures: dict[frozenset[str], tuple[int, float]] = {}
        self._spawn_failure_history: dict[frozenset[str], list[Any]] = {}
        # Bug 1 (2026-07-02, claim-conflict-churn): per-task-id claim-conflict
        # episode counter + backoff deadline. A "conflict episode" is one
        # tick's bounded burst of re-fetch-and-retry attempts against a 409
        # (see task_lifecycle._claim_task_with_conflict_retry). Without this,
        # a task stuck in perpetual 409 (stale file lock, unmet dependency,
        # etc.) got re-attempted every tick forever -- 144 consecutive
        # identical `claim?expected_version=1` -> 409 requests were observed
        # from a single session in work/bernstein/proofs/d2/claim-loop-evidence.
        # task_id -> (episode_count, backoff_until_epoch_s)
        self._claim_conflict_state: dict[str, tuple[int, float]] = {}
        self._latest_tasks_by_id: dict[str, Task] = {}
        # Track last backlog replenishment timestamp
        self._last_replenish_ts: float = 0.0
        # Run completion summary state
        self._summary_written: bool = False
        # Guards the FINAL (shutdown-final) retrospective regeneration so it
        # only ever runs once per process, regardless of which terminal path
        # triggers it (tick-loop drain in run(), or the tick-level
        # quiescence self-stop in _tick_internal's "8b" block). See
        # _regenerate_final_retrospective.
        self._final_retrospective_regenerated: bool = False
        # Defect 29 (2026-07-02, work/bernstein/proofs/d2/minimax/attempt-83808a8a):
        # self-stop ordering race. The run-summary MUST NOT fire until the
        # post-/complete chain (task_done -> reap_completed_agent ->
        # merge_worktree_branch -> _apply_janitor_verdict_action) has drained
        # for every completed task. We track each terminal task from the
        # moment we first see it in done/failed state and only decrement the
        # counter once the task has been through ``process_completed_tasks``
        # AND its session is no longer alive. The 8b summary block gates
        # ``_generate_run_summary`` on this counter reaching zero.
        self._pending_post_complete_count: int = 0
        self._post_complete_seen_task_ids: set[str] = set()
        self._run_start_ts: float = time.time()
        self._agent_failure_timestamps: dict[str, float] = {}  # adapter_name -> last failure ts
        self._shutting_down = threading.Event()
        self._executor_drained = False
        # Background thread pool for non-blocking ruff/pytest runs
        self._executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._pending_ruff_future: concurrent.futures.Future[list[RuffViolation]] | None = None
        self._pending_test_future: concurrent.futures.Future[TestResults] | None = None
        self._spawner.set_shutdown_event(self._shutting_down)

        # Provider-aware routing and health tracking
        self._router = router
        if self._router is not None and not self._router.state.providers:
            providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
            if providers_yaml.exists():
                load_providers_from_yaml(providers_yaml, self._router)
        # Load model policy - checked on every init so late-bound routers pick it up
        if self._router is not None:
            model_policy_yaml = workdir / ".sdd" / "config" / "model_policy.yaml"
            if model_policy_yaml.exists():
                load_model_policy_from_yaml(model_policy_yaml, self._router)
            else:
                # Fall back to bernstein.yaml model_policy section. Resolve via
                # resolve_seed_path() (explicit --seed-path / BERNSTEIN_SEED_PATH
                # env / workdir default) instead of hardcoding workdir/bernstein.yaml
                # -- otherwise role_model_policy is silently lost whenever the
                # real seed lives elsewhere, and agents spawn on the default
                # model instead of the configured one.
                _seed_path_for_policy = resolve_seed_path(workdir)
                logger.info(
                    "Orchestrator.__init__: resolved seed_path=%s for model_policy fallback (exists=%s)",
                    _seed_path_for_policy,
                    _seed_path_for_policy.exists(),
                )
                if _seed_path_for_policy.exists():
                    load_model_policy_from_yaml(_seed_path_for_policy, self._router)
            # Warn on startup if policy leaves no viable providers
            policy_issues = self._router.validate_policy()
            for issue in policy_issues:
                logger.warning("Model policy: %s", issue)

        # Telemetry
        from bernstein.core.telemetry import init_telemetry

        init_telemetry(config.telemetry.otlp_endpoint if hasattr(config, "telemetry") else None)

        # Self-evolution feedback loop
        if config.evolution_enabled:
            self._evolution = evolution or EvolutionCoordinator(
                state_dir=workdir / ".sdd",
            )
        else:
            self._evolution: EvolutionCoordinator | None = None

        # Adaptive governance: adjusts metric weights each evolution cycle.
        # Always initialize the governor - it's lightweight and evolve mode
        # can be activated at runtime via evolve.json even if not in config.
        self._governor = AdaptiveGovernor(state_dir=workdir / ".sdd")

        # Strategic Risk Scorer: scores proposals before routing
        self._risk_scorer = RiskScorer()
        self._last_cycle_risk_scores: list[float] = []

        # Pre-initialize the global metrics collector with the correct path so
        # subsequent calls to get_collector() (without args) write to the right
        # directory regardless of cwd at call time.
        get_collector(workdir / ".sdd" / "metrics")

        # Initialize the duration predictor and auto-retrain in the background
        # if the training dataset has grown since the last training run.
        try:
            from bernstein.core.duration_predictor import get_predictor as _get_predictor

            _dp = _get_predictor(workdir / ".sdd" / "models")
            self._executor.submit(_dp.train)
        except Exception as _dp_exc:
            logger.debug("Duration predictor startup skipped: %s", _dp_exc)

        # Fast-path: deterministic execution for trivial tasks (L0).
        # Load patterns from routing.yaml so the YAML config is authoritative.
        routing_yaml = workdir / ".sdd" / "config" / "routing.yaml"
        if routing_yaml.exists():
            load_fast_path_config(routing_yaml)
        self._fast_path_stats = FastPathStats()

        # Cross-run task quarantine: skip repeatedly-failing tasks
        self._quarantine = QuarantineStore(workdir / ".sdd" / "runtime" / "quarantine.json")
        try:
            RecommendationEngine(workdir).ensure_seed_file()
        except Exception as exc:
            logger.debug("Recommendation seed bootstrap skipped: %s", exc)

        # Rate-limit tracker: detects 429s in agent logs and throttles providers

        self._rate_limit_tracker = RateLimitTracker()

        # Semantic response cache: reuse completed agent results for
        # functionally identical tasks (cosine >= 0.95 skips spawn).
        self._response_cache = ResponseCacheManager(workdir)
        self._batch_api = ProviderBatchManager(workdir, config.batch) if config.batch.enabled else None
        self._quota_poller = QuotaPoller(router=self._router, workdir=workdir) if self._router is not None else None
        if self._quota_poller is not None:
            self._quota_poller.poll_once()

        # Contextual bandit router: active when BERNSTEIN_ROUTING=bandit
        # or bandit-shadow.
        # Persists policy to .sdd/routing/ so learning accumulates across runs.
        import os as _os

        _routing_mode = _os.environ.get("BERNSTEIN_ROUTING", "static").lower()
        self._bandit_routing_mode = _routing_mode
        self._bandit_router: BanditRouter | None = (
            BanditRouter(policy_dir=workdir / ".sdd" / "routing")
            if _routing_mode in {"bandit", "bandit-shadow"}
            else None
        )

        # Adaptive polling backoff: multiplied by 2 each idle tick, reset on work.
        self._idle_multiplier: int = 1

        # Per-run cost budget tracker.  When budget_usd > 0 the tracker
        # emits warnings at 80%/95% and blocks spawns at 100%.
        # Issue #1320: ``BERNSTEIN_HARD_BUDGET_USD`` is the kill-switch
        # cap (set by ``bernstein run --hard-budget``). Attach a rolling
        # JSONL ledger so per-call attribution lands in
        # ``.sdd/cost/ledger.jsonl``.
        run_id = os.environ.get("BERNSTEIN_RUN_ID") or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self._run_id = run_id
        # Wave 3 (per-agent instrumentation): export the run id onto the
        # process environment so it threads through
        # ``bernstein.adapters.env_isolation.build_filtered_env`` into every
        # spawned agent subprocess, letting each agent's
        # ``RunInstrumenter`` (bernstein.core.instrumentation) write its
        # llm-calls/tool-calls/conversation JSONL under this same
        # ``.sdd/runs/<run_id>/...`` tree wave 2's timing data lives in.
        # Best-effort: os.environ writes essentially never fail, but this
        # must never be allowed to abort orchestrator startup either way.
        try:
            os.environ["BERNSTEIN_RUN_ID"] = run_id
            logger.info("Exported BERNSTEIN_RUN_ID=%s for spawned-agent instrumentation", run_id)
        except Exception as exc:  # intentional-broad-except: defensive, must never block startup
            logger.warning("Failed to export BERNSTEIN_RUN_ID=%s to process env: %s", run_id, exc)
        hard_budget_usd = 0.0
        _raw_hard = os.environ.get("BERNSTEIN_HARD_BUDGET_USD", "").strip()
        if _raw_hard:
            try:
                hard_budget_usd = max(0.0, float(_raw_hard))
            except ValueError:
                logger.warning(
                    "Invalid BERNSTEIN_HARD_BUDGET_USD=%r; ignoring hard cap.",
                    _raw_hard,
                )

        from bernstein.core.cost.spend_ledger import SpendLedger

        spend_ledger = SpendLedger(
            path=workdir / ".sdd" / "cost" / "ledger.jsonl",
            run_id=run_id,
            budget_usd=config.budget_usd,
            hard_budget_usd=hard_budget_usd,
        )
        self._spend_ledger = spend_ledger

        self._cost_tracker = CostTracker(
            run_id=run_id,
            budget_usd=config.budget_usd,
            hard_budget_usd=hard_budget_usd,
            spend_ledger=spend_ledger,
        )
        self._cost_cap_killed_agents: set[str] = set()

        # Cost autopilot: when cost_autopilot=True in bernstein.yaml, evaluates
        # spend each tick and downgrades task models once budget exceeds 80%.
        self._cost_autopilot: Any | None = None
        if config.cost_autopilot and config.budget_usd > 0:
            from bernstein.core.cost.cost_autopilot import CostAutopilot, CostAutopilotConfig

            self._cost_autopilot = CostAutopilot(
                CostAutopilotConfig(enabled=True, budget_usd=config.budget_usd),
                self._cost_tracker,
            )

        # Budget enforcement policy: evaluated each tick against the cost
        # tracker to decide whether to pause, downgrade, or abort spawning.
        # Kept as an attribute so tests (and seed config) can override.
        self._budget_policy: BudgetPolicy = BudgetPolicy.default()
        # Track last-seen policy action so we only notify on transitions.
        self._last_budget_action: BudgetAction = BudgetAction.CONTINUE
        # kill-switch state. When ``should_stop`` transitions
        # True we record the timestamp here and SHUTDOWN all live agents;
        # subsequent ticks that run ``kill_grace_period_s`` seconds later
        # SIGKILL any session still alive so budget overrun stays bounded.
        self._budget_stop_fired_at: float | None = None
        self._budget_stop_killed_agents: set[str] = set()

        # Cost anomaly detector: layered on top of cost_tracker, fires
        # AnomalySignals the orchestrator acts on (log/stop/kill).
        from bernstein.core.cost_anomaly import CostAnomalyDetector

        self._anomaly_detector = CostAnomalyDetector(config.cost_anomaly, workdir)

        # Context degradation detector: flags agents whose cross-model review
        # verdicts decline (N consecutive rejects).  Degraded sessions are
        # checkpointed + SHUTDOWN during each tick so a fresh replacement
        # starts with a recovery-context preamble.  Disabled by default.
        _ctx_raw = getattr(config, "context_degradation", None)
        _ctx_cfg: ContextDegradationConfig = (
            _ctx_raw if isinstance(_ctx_raw, ContextDegradationConfig) else ContextDegradationConfig(enabled=False)
        )
        self._context_degradation = ContextDegradationDetector(_ctx_cfg, workdir)
        # Recovery context keyed by task_id - consumed by the replacement
        # agent's prompt.  Populated when a degraded session is evicted.
        self._context_recovery: dict[str, str] = {}

        # Canonical Merkle-chained event journal: the single always-on
        # recorder. Appends every run event to
        # .sdd/runs/{run_id}/journal.jsonl where the head hash is the run
        # identity. Records by default (issue #2293); size is bounded by
        # BERNSTEIN_REPLAY_RETENTION over past runs rather than an on/off
        # gate.
        self._recorder = EventJournal(run_id=run_id, sdd_dir=workdir / ".sdd")

        # Live OTLP export of the journal projection : each
        # appended journal entry streams its journal-anchored span to the
        # operator's collector. Default off -- attach_live_export returns
        # None (constructing nothing, touching no network) unless
        # BERNSTEIN_OTEL_ENDPOINT is set. Finalized in the stop path so
        # the run's otel.projection audit event anchors the live trace.
        from bernstein.core.observability.otel_bridge import attach_live_export

        self._otel_stream = attach_live_export(self._recorder, workdir=workdir)

        # Providers whose mutation-observability capability has been
        # recorded into the journal this run (issue #2507). One
        # ``provider_state_capability`` entry per provider per run keeps an
        # absence of mutation entries distinguishable from an inability to
        # observe them.
        self._mutation_capability_recorded: set[str] = set()

        # Lazily-built audit chain for mirroring provider-state mutations into
        # the HMAC chain (issue #2646). Cached so the run's key is loaded once.
        self._provider_audit_chain: Any | None = None

        # Replay gateway: captures LLM + tool dispatch responses into
        # .sdd/runs/{run_id}/events.jsonl so a run can be re-executed
        # against recorded fixtures. This is the fixture-replay engine, a
        # distinct concern from the canonical event journal above; it is
        # OFF by default (opt in with BERNSTEIN_RECORD=1) and writes
        # nothing on a normal run.
        from bernstein.core.replay import ReplayGateway as _ReplayGateway

        self._replay_gateway = _ReplayGateway(
            run_id=run_id,
            sdd_dir=workdir / ".sdd",
        )
        _seed_path = resolve_seed_path(workdir)
        self._replay_metadata = SessionReplayMetadata(
            run_id=run_id,
            started_at=time.time(),
            git_sha=current_git_sha(workdir),
            git_branch=current_git_branch(workdir),
            config_hash=hash_file(_seed_path if _seed_path.exists() else None),
            seed_path=str(_seed_path) if _seed_path.exists() else None,
        )
        write_session_replay_metadata(workdir / ".sdd", self._replay_metadata)

        # Write-Ahead Log: hash-chained JSONL for crash-safe durability
        # and execution fingerprinting. WAL entries are written before
        # actions execute so decisions survive crashes.
        self._wal_writer = WALWriter(run_id=run_id, sdd_dir=workdir / ".sdd")

        # Approval gate: controls whether verified work is merged directly,
        # held for interactive review, or pushed as a GitHub PR.
        # merge_strategy="pr" activates PR mode by default; "direct" forces auto.
        # An explicit approval override ("review" or "pr") takes precedence.
        if config.approval == "workflow":
            self._approval_gate = ApprovalGate(
                mode=ApprovalMode.AUTO,  # base mode, overridden per-task in task_completion.py
                workdir=workdir,
                auto_merge=config.auto_merge,
                pr_labels=config.pr_labels,
            )
        else:
            if config.approval != "auto":
                _effective_approval = config.approval
            elif config.merge_strategy == "direct":
                _effective_approval = "auto"
            else:
                # merge_strategy="pr" (default) -> PR mode
                _effective_approval = "pr"
            _approval_mode = ApprovalMode(_effective_approval)
            self._approval_gate: ApprovalGate | None = (
                ApprovalGate(
                    mode=_approval_mode,
                    workdir=workdir,
                    auto_merge=config.auto_merge,
                    pr_labels=config.pr_labels,
                )
                if _approval_mode != ApprovalMode.AUTO
                else None
            )

        # Manager queue review: trigger after N completions/failures or stall.
        self._completions_since_review: int = 0
        self._failures_since_review: int = 0
        self._last_review_ts: float = 0.0

        # Hot-reload: track source file mtimes so the orchestrator can
        # detect when agents modify its own code and restart in-place.
        self._source_mtime: float = time.time()

        # Config hot-reload: track bernstein.yaml mtime so mutable config
        # fields (max_agents, budget_usd) are picked up without restart.
        self._config_path: Path = resolve_seed_path(workdir)
        self._config_mtime: float = self._config_path.stat().st_mtime if self._config_path.exists() else 0.0

        # Memory leak detection: sampled every few ticks
        self._memory_guard = MemoryGuard()

        # Agent signal manager: writes WAKEUP/SHUTDOWN files into
        # .sdd/runtime/signals/{session_id}/ for stale agent detection.
        self._signal_mgr = AgentSignalManager(self._workdir)

        # FIFO merge queue: serializes branch merges so only one runs at a time.
        # Conflict resolution tasks are created by process_completed_tasks when
        # a MergeResult reports conflicting_files.
        self._merge_queue = MergeQueue()
        # Audit-091 fix: wire the queue into the spawner so every agent merge
        # enqueues through it.  The spawner is constructed before the
        # orchestrator, so the hook is applied here via a setter.
        if hasattr(self._spawner, "set_merge_queue"):
            self._spawner.set_merge_queue(self._merge_queue)

        # Convergence guard: blocks spawn waves when merge queue, active
        # agent count, error rate, or spawn rate exceed safe thresholds.
        from bernstein.core.orchestration.convergence_guard import ConvergenceGuard

        self._convergence_guard = ConvergenceGuard(config.convergence)

        # AgentOps: SLO tracking, error budget, runbooks, incident response.
        # Reset error budget AND metric collector each run - stale failure
        # data from prior runs must not throttle a fresh run's agent capacity.
        self._slo_tracker = SLOTracker()
        # Clear prior-run task metrics so error budget starts at 0/0
        get_collector().reset_task_metrics()
        self._runbook_engine = RunbookEngine()
        self._incident_manager = IncidentManager()
        self._consecutive_failures: int = 0

        # Adaptive parallelism: dynamically adjusts effective max_agents based
        # on error rate and CPU load.  See adaptive_parallelism.py.
        self._adaptive_parallelism = AdaptiveParallelism(configured_max=config.max_agents)

        # Governed workflow mode: when config.workflow is set (e.g. "governed"),
        # the executor drives the run through deterministic phases, filtering
        # tasks and blocking advancement until guards pass.
        self._workflow_executor: WorkflowExecutor | None = None
        if config.workflow:
            defn = load_workflow(config.workflow)
            if defn is not None:
                self._workflow_executor = WorkflowExecutor(
                    definition=defn,
                    run_id=run_id,
                    sdd_dir=workdir / ".sdd",
                )
                logger.info(
                    "Governed workflow active: %s (hash=%s, phases=%s)",
                    defn.name,
                    self._workflow_executor.definition_hash[:16] + "...",
                    defn.phase_names(),
                )
            else:
                logger.warning("Unknown workflow %r - running in adaptive mode", config.workflow)

        # Run manifest: hashable configuration record for compliance (non-critical).
        self._manifest = None
        try:
            _wf_name = self._workflow_executor.definition.name if self._workflow_executor else ""
            _wf_hash = self._workflow_executor.definition_hash if self._workflow_executor else ""
            _cli_name = spawner._adapter.name() if hasattr(spawner, "_adapter") else "auto"  # type: ignore[union-attr]
            self._manifest = build_manifest(
                run_id=run_id,
                config=config,
                cli=_cli_name,
                model=None,
                workflow_name=_wf_name,
                workflow_definition_hash=_wf_hash,
            )
            save_manifest(self._manifest, workdir / ".sdd")
        except Exception:
            logger.debug("Manifest creation skipped (non-critical)", exc_info=True)

        # Compliance mode: activate subsystems based on compliance config.
        self._compliance = config.compliance
        if self._compliance is not None:
            from bernstein.core.compliance import persist_compliance_config

            compliance: ComplianceConfig = self._compliance
            persist_compliance_config(compliance, workdir / ".sdd")

            # Log prerequisite warnings
            prereq_warnings = compliance.check_prerequisites()
            for w in prereq_warnings:
                logger.warning("Compliance: %s", w)

            preset_label = compliance.preset.value if compliance.preset else "custom"
            features = []
            if compliance.audit_logging:
                features.append("audit")
            if compliance.audit_hmac_chain:
                features.append("hmac-chain")
            if compliance.wal_enabled:
                features.append("wal")
            if compliance.wal_signed:
                features.append("signed-wal")
            if compliance.governed_workflow:
                features.append("governed")
            if compliance.approval_gates:
                features.append("approval-gates")
            if compliance.mandatory_human_review:
                features.append("mandatory-review")
            if compliance.execution_fingerprint:
                features.append("fingerprint")
            if compliance.ai_content_labels:
                features.append("ai-labels")
            if compliance.data_residency:
                features.append(f"data-residency:{compliance.data_residency_region}")
            if compliance.sbom_enabled:
                features.append("sbom")
            if compliance.evidence_bundle:
                features.append("evidence-bundle")

            logger.info(
                "Compliance mode active: preset=%s features=[%s]",
                preset_label,
                ", ".join(features),
            )

        # SOC 2 audit mode: enable via --audit flag or compliance preset
        self._audit_mode = os.environ.get("BERNSTEIN_AUDIT") == "1" or (
            self._compliance is not None and self._compliance.audit_logging
        )
        if self._audit_mode:
            from bernstein.core.audit import AuditLog
            from bernstein.core.lifecycle import set_audit_log

            audit_dir = workdir / ".sdd" / "audit"
            self._audit_log = AuditLog(audit_dir)
            set_audit_log(self._audit_log)
            logger.info("SOC 2 audit mode active - logging to %s", audit_dir)
        else:
            self._audit_log = None

        # Progress-snapshot stall detection state (see check_stalled_tasks).
        # Tracks how many consecutive identical snapshots each task has had.
        self._stall_counts: dict[str, int] = {}  # task_id -> consecutive identical count
        self._last_snapshot: dict[str, ProgressSnapshot | None] = {}  # task_id -> last snapshot
        self._last_snapshot_ts: dict[str, float] = {}  # task_id -> last snapshot timestamp

        # Idle-agent recycling: tracks when a SHUTDOWN was sent to an idle agent
        # so the grace period can be enforced (30 s before SIGKILL).
        self._idle_shutdown_ts: dict[str, float] = {}  # session_id -> shutdown_sent_ts
        self._watchdog_log_state: dict[str, tuple[int, int]] = {}
        self._watchdog = WatchdogManager(
            workdir,
            self._client,
            self._config.server_url,
            notify=self._notify,
            post_bulletin=self._post_bulletin,
        )

        # Cluster heartbeat client: when cluster mode is enabled and this node
        # is a worker (server_url points to a remote central server), send
        # periodic heartbeats with current capacity.
        self._heartbeat_client: NodeHeartbeatClient | None = None
        if cluster_config and cluster_config.enabled and cluster_config.server_url:
            self._heartbeat_client = NodeHeartbeatClient(
                server_url=cluster_config.server_url,
                interval_s=cluster_config.node_heartbeat_interval_s,
                auth_token=cluster_config.auth_token or config.auth_token,
                capacity_fn=self._current_capacity,
            )

        # CI autofix poller: lazily constructed on first use so
        # the orchestrator doesn't import httpx-async machinery unless the
        # flag is enabled.  _last_ci_poll_ts enforces the poll_interval_s
        # cadence regardless of tick frequency.
        self._ci_monitor: Any | None = None
        self._ci_autofix_pipeline: Any | None = None
        self._last_ci_poll_ts: float = 0.0

        # LLM watcher (P1 - opt-in advisory observer above the deterministic
        # orchestrator).  Off by default; the watcher imports the LLM stack
        # only when ``BERNSTEIN_LLM_WATCHER_ENABLED`` is truthy.  It receives
        # a frozen ``WatcherEvent`` snapshot only - no orchestrator handle,
        # no task store, no spawner - so it is structurally read-only.  See
        # ``bernstein.core.observability.llm_watcher`` for the full contract.
        from bernstein.core.observability.llm_watcher import build_watcher_from_env

        self._llm_watcher = build_watcher_from_env()
        # Track signals for ``_log_summary`` / future CLI surfaces; capped
        # to keep memory bounded under long-lived runs.
        self._llm_watcher_signals: collections.deque[Any] = collections.deque(maxlen=64)

    # -- Hot-reload source detection -----------------------------------------

    # Key source files whose modification triggers an orchestrator restart.
    _HOT_RELOAD_SOURCES: ClassVar[list[str]] = [
        "src/bernstein/core/orchestrator.py",
        "src/bernstein/core/spawner.py",
        "src/bernstein/core/router.py",
        "src/bernstein/core/server.py",
        "src/bernstein/core/models.py",
    ]

    def _check_source_changed(self) -> bool:
        """Check if orchestrator source files changed since last tick.

        Compares mtime of key source files against the timestamp recorded
        at startup (or the last restart).  When any file is newer, a
        restart is warranted so the orchestrator picks up the new code.

        Returns:
            True if at least one source file was modified after startup.
        """
        from pathlib import Path as _Path

        for rel in self._HOT_RELOAD_SOURCES:
            src = _Path(rel)
            try:
                if src.exists() and src.stat().st_mtime > self._source_mtime:
                    logger.info("Source changed: %s", rel)
                    return True
            except OSError:
                continue
        return False

    def _maybe_reload_config(self) -> bool:
        """Hot-reload mutable fields from bernstein.yaml when the file changes.

        Only safe-to-reload fields (max_agents, budget_usd) are updated.
        Structural changes (cli adapter, team composition) still require a
        full restart.

        Returns:
            True if config was reloaded, False otherwise.
        """
        try:
            if not self._config_path.exists():
                return False
            current_mtime = self._config_path.stat().st_mtime
            if current_mtime <= self._config_mtime:
                return False
        except OSError:
            return False

        from bernstein.core.seed import parse_seed

        try:
            seed = parse_seed(self._config_path)
        except Exception as exc:
            logger.warning("Config hot-reload: failed to parse %s: %s", self._config_path, exc)
            self._config_mtime = current_mtime  # avoid retry every tick
            return False

        changed: list[str] = []
        if seed.max_agents != self._config.max_agents:
            changed.append(f"max_agents {self._config.max_agents} -> {seed.max_agents}")
            self._config.max_agents = seed.max_agents
        if seed.budget_usd is not None and seed.budget_usd != self._config.budget_usd:
            changed.append(f"budget_usd {self._config.budget_usd} -> {seed.budget_usd}")
            self._config.budget_usd = seed.budget_usd
            self._cost_tracker.budget_usd = seed.budget_usd

        self._config_mtime = current_mtime
        if changed:
            logger.info("Config hot-reload: %s", ", ".join(changed))
        return bool(changed)

    def _current_capacity(self) -> NodeCapacity:
        """Build a NodeCapacity snapshot reflecting current agent usage."""
        alive = sum(1 for a in self._agents.values() if a.status != "dead")
        return NodeCapacity(
            max_agents=self._config.max_agents,
            available_slots=max(0, self._config.max_agents - alive),
            active_agents=alive,
        )

    @property
    def active_agents(self) -> dict[str, AgentSession]:
        """Currently tracked agent sessions, keyed by session id."""
        return self._agents.copy()

    @property
    def permission_mode(self) -> PermissionMode:
        """The resolved permission mode for this orchestrator session."""
        return self._permission_mode

    @property
    def bulletin(self) -> BulletinBoard | None:
        """The bulletin board, if one was provided."""
        return self._bulletin

    def _post_bulletin(self, msg_type: str, content: str) -> None:
        """Post a message to the bulletin board if one is configured.

        Args:
            msg_type: Message category (status, alert, finding, etc.).
            content: Free-text message body.
        """
        if self._bulletin is None:
            return
        from typing import cast as _cast

        from bernstein.core.bulletin import MessageType

        try:
            self._bulletin.post(
                BulletinMessage(
                    agent_id="orchestrator",
                    type=_cast("MessageType", msg_type),
                    content=content,
                )
            )
        except SignalActionFailure:
            # The message is on the board and queued for retry; an advisory
            # bulletin post must not abort the orchestrator loop (#2648).
            logger.exception("bulletin signal action pending retry for %s message", msg_type)

    def _check_task_deadlines(self, running_tasks: list[Task]) -> None:
        """Check deadlines on running tasks and escalate or notify.

        For tasks past their deadline with some time remaining (warning window),
        fire a ``task.deadline_warning``.  For tasks that are fully exceeded,
        fire ``task.deadline_exceeded``, append a meta message for the next agent,
        and fail the task so the retry logic kicks in with deadline-aware escalation.

        Args:
            running_tasks: Tasks currently in claimed or in_progress state.
        """
        now = time.time()
        warning_window = ORCHESTRATOR.deadline_warning_window_s

        for task in running_tasks:
            if task.deadline is None:
                continue

            elapsed = now - task.deadline

            # Fully exceeded: fail immediately with escalation
            if elapsed > 0:
                logger.warning(
                    "Task %s ('%s') deadline exceeded (%.0fs overdue)",
                    task.id,
                    task.title,
                    elapsed,
                )
                # Fail the task so the retry path will do deadline-aware escalation
                try:
                    self._client.post(
                        f"{self._config.server_url}/tasks/{task.id}/fail",
                        json={"reason": f"Deadline exceeded ({elapsed:.0f}s overdue)"},
                    )
                except Exception as exc:
                    logger.warning("Failed to fail deadline for task %s: %s", task.id, exc)
                self._notify(
                    "task.deadline_exceeded",
                    title=f"Task deadline exceeded: {task.title}",
                    body=(f"Task {task.id} (role={task.role}) exceeded its deadline by {elapsed:.0f}s."),
                    task_id=task.id,
                    role=task.role,
                )

            # Warning window: task is about to expire soon
            elif 0 < task.deadline - now <= warning_window:
                remaining = task.deadline - now
                logger.warning(
                    "Task %s ('%s') deadline approaching in %.0fs",
                    task.id,
                    task.title,
                    remaining,
                )
                self._notify(
                    "task.deadline_warning",
                    title=f"Task deadline approaching: {task.title}",
                    body=(f"Task {task.id} (role={task.role}) will exceed its deadline in {remaining:.0f}s."),
                    task_id=task.id,
                    role=task.role,
                )

    def _notify(self, event: str, title: str, body: str, **metadata: Any) -> None:
        """Fire a notification event if a NotificationManager is configured.

        Args:
            event: Notification event name (e.g. ``_EVENT_RUN_COMPLETED``).
            title: Short human-readable title.
            body: Longer description / summary.
            **metadata: Arbitrary key-value pairs attached to the payload.
        """
        if self._notifier is None:
            return
        payload = NotificationPayload(event=event, title=title, body=body, metadata=metadata.copy())
        self._notifier.notify(event, payload)

    def _evaluate_budget_policy(self, tasks: list[Task]) -> Any | None:
        """Evaluate the budget policy for this tick and apply model downgrades.

        When ``budget_usd`` is 0 (unlimited) the policy is not evaluated and
        ``None`` is returned. Otherwise the policy is evaluated against the
        current spend ratio; on ``DOWNGRADE_MODEL`` the task model fields are
        rewritten in place so downstream spawn code picks up the cheaper
        tier. Transitions between actions emit a single notification so the
        operator is warned on escalation without log spam.

        Args:
            tasks: Ready tasks eligible for spawn this tick.

        Returns:
            The :class:`BudgetActionResult` produced by
            :func:`apply_policy`, or ``None`` when no budget is configured.
        """
        if self._cost_tracker.budget_usd <= 0:
            return None
        status = self._cost_tracker.status()
        result = apply_policy(self._budget_policy, status.percentage_used, tasks=tasks)
        if result.action != self._last_budget_action:
            logger.info(
                "Budget policy transition: %s -> %s at %.1f%% spend (threshold %.0f%%) - %s",
                self._last_budget_action.value,
                result.action.value,
                result.percentage_used * 100,
                result.threshold_pct * 100,
                result.message,
            )
            # Fire a structured notification on every transition so operators
            # have a single event per escalation (pause/downgrade/abort).
            if result.action != BudgetAction.CONTINUE:
                self._notify(
                    f"budget.policy.{result.action.value}",
                    f"Budget policy: {result.action.value}",
                    result.message
                    or (f"{result.percentage_used * 100:.1f}% of budget used; action={result.action.value}."),
                    action=result.action.value,
                    threshold_pct=round(result.threshold_pct, 4),
                    percentage_used=round(result.percentage_used, 4),
                )
            self._last_budget_action = result.action
        return result

    def _evaluate_cost_policy_dispatch(
        self,
        batches: list[list[Task]],
        cost_estimates: dict[str, float],
    ) -> Any | None:
        """Halt this tick's spawns before a USD cap is breached (#2354).

        Consults the deterministic USD dispatch policy
        (:func:`~bernstein.core.cost.scheduling.dispatch_gate.evaluate_run_dispatch`)
        with the batches about to spawn, costed from ``cost_estimates`` and the
        run's persisted spend ledger. Behaviour:

        * **Fail-open (no-op)** when no ``cost_policy`` -- or no positive cap --
          is configured: returns ``None`` without touching the ledger, so an
          absent or disabled price policy never blocks a dispatch.
        * **Fail-closed** when the projected next dispatch would breach a cap:
          the halting :class:`DispatchDecision` is sealed into the lineage spine
          and mirrored into the HMAC audit chain as a verifiable receipt, and
          returned so the caller blocks the spawn.

        Args:
            batches: Role-grouped batches about to be claimed/spawned this tick.
            cost_estimates: ``task_id -> estimated_cost_usd`` from this tick.

        Returns:
            The halting ``DispatchDecision`` (spawns must be blocked), or
            ``None`` to proceed.
        """
        from bernstein.core.cost.scheduling.dispatch_gate import (
            build_dispatch_candidates,
            evaluate_run_dispatch,
            resolve_cost_caps,
            resolve_knob_matrix,
            resolve_price_table,
        )

        caps = resolve_cost_caps(getattr(self._config, "cost_policy", None))
        if caps is None or not batches:
            return None  # fail-open: nothing to enforce

        from bernstein.core.cost.spend_ledger import SpendLedger

        now_ts = time.time()
        day_key = datetime.now(UTC).strftime("%Y-%m-%d")
        cost_policy = getattr(self._config, "cost_policy", None)
        price_table = resolve_price_table(cost_policy)
        knob_matrix = resolve_knob_matrix(cost_policy)
        candidates = build_dispatch_candidates(
            batches,
            cost_estimates=cost_estimates,
            run_id=self._run_id,
            day_key=day_key,
            knob_matrix=knob_matrix,
        )
        entries = SpendLedger.load_entries(self._spend_ledger.path)
        outcome = evaluate_run_dispatch(
            candidates=candidates,
            entries=entries,
            caps=caps,
            price_table_hash=price_table.content_hash(),
            now_ts=now_ts,
        )
        if outcome.halt is None:
            return None  # admitted: within every configured cap

        self._seal_cost_dispatch_halt(outcome.halt, timestamp=int(now_ts))
        return outcome.halt

    def _seal_cost_dispatch_halt(self, decision: Any, *, timestamp: int) -> None:
        """Seal a cost-policy halt into the lineage spine + audit chain (#2354).

        The halt receipt is the proof, not a decoration: sealing failures are
        logged and swallowed so a missing key or IO error never crashes the run
        (the spawn is still blocked). Only the exception type is logged on this
        path because it touches the audit HMAC key.
        """
        try:
            from bernstein.core.cost.scheduling.receipt import build_dispatch_receipt
            from bernstein.core.security.audit import load_or_create_audit_key
            from bernstein.core.security.audit_chain import AuditChainStore

            hmac_key = load_or_create_audit_key()
            chain = AuditChainStore(self._workdir / ".sdd" / "audit", key=hmac_key)
            build_dispatch_receipt(
                decision=decision,
                workdir=self._workdir,
                lineage_root=self._workdir / ".sdd" / "lineage",
                hmac_key=hmac_key,
                timestamp=timestamp,
                chain=chain,
            )
        except Exception as exc:
            # Sealing is a provenance aid; a failure must never crash the run
            # (the spawn is still held). Log only the exception type: this path
            # touches the audit HMAC key.
            logger.warning("cost: failed to seal dispatch halt receipt: %s", type(exc).__name__)

    # -- Core tick -----------------------------------------------------------

    def tick(self) -> TickResult:
        """Execute one orchestrator cycle."""
        from bernstein.core.telemetry import start_span

        tick_start = time.monotonic()
        with start_span("orchestrator.tick", attributes={"tick": self._tick_count + 1}):
            result = self._tick_internal()
        tick_duration = time.monotonic() - tick_start
        if tick_duration > 30.0:
            logger.warning("Tick took %.1fs (threshold 30s)", tick_duration)
        return result

    def _tick_internal(self) -> TickResult:
        """Actual tick implementation (previously tick())."""
        result = TickResult()
        self._tick_count += 1
        base = self._config.server_url
        _tick_http_reads = 0  # counts GET requests this tick (should stay at 1)

        # Phase scheduling: fast ops every tick, normal every 6, slow every 30.
        # This prevents heavy operations (SLO, evolution, watchdog) from
        # blocking the fast control loop (spawn, reap, heartbeat).
        _run_normal = self._tick_count % ORCHESTRATOR.normal_tick_phase == 0
        _run_slow = self._tick_count % ORCHESTRATOR.slow_tick_phase == 0
        logger.debug(
            "tick #%d phases: fast%s%s",
            self._tick_count,
            "+normal" if _run_normal else "",
            "+slow" if _run_slow else "",
        )

        # Record tick start for deterministic replay
        self._recorder.record("tick_start", tick=self._tick_count)
        if self._quota_poller is not None:
            self._quota_poller.maybe_poll()

        # WAL: record tick boundary for crash recovery and audit trail
        try:
            self._wal_writer.write_entry(
                decision_type="tick_start",
                inputs={"tick": self._tick_count},
                output={},
                actor="orchestrator",
            )
        except OSError:
            logger.debug("WAL write failed for tick_start %d", self._tick_count)

        # 0-pre. Proactive server health check (every normal tick).
        # Detects server crashes early so the watchdog can restart it
        # before we waste time attempting task fetches / spawns.
        if _run_normal and not self._check_server_health():
            result.errors.append("server_health_check_failed")
            return result

        # 0. Ingest any new backlog files before fetching tasks.
        #    Rate-limited to 10 files/tick with title dedup to prevent
        #    server overload and duplicate task creation.
        #    Gated behind _run_normal - no need to scan 300 files every tick.
        if _run_normal:
            try:
                from bernstein.core.roadmap_runtime import emit_roadmap_wave

                emitted = emit_roadmap_wave(self._workdir)
                if emitted:
                    logger.info("Emitted %d roadmap ticket(s) into backlog/open", len(emitted))
            except (OSError, ValueError) as exc:
                logger.warning("roadmap wave emission failed: %s", exc)

            try:
                self.ingest_backlog()
            except (OSError, ValueError) as exc:
                logger.warning("ingest_backlog failed: %s", exc)

            if self._running:
                self._run_scheduled_dependency_scan()

        # 1. Fetch all tasks in a single bulk request, bucketed client-side.
        try:
            tasks_by_status = fetch_all_tasks(self._client, base)
            _tick_http_reads += 1  # single GET /tasks (no status filter)
            self._consecutive_server_failures = 0  # Reset on success
        except httpx.HTTPError as exc:
            self._consecutive_server_failures = getattr(self, "_consecutive_server_failures", 0) + 1
            if self._consecutive_server_failures >= ORCHESTRATOR.server_failure_threshold:
                logger.critical(
                    "Server unreachable for %d consecutive ticks - orchestrator stopping to prevent waste",
                    self._consecutive_server_failures,
                )
                self._running = False
            elif self._consecutive_server_failures >= ORCHESTRATOR.server_failure_warn:
                logger.warning(
                    "Server unreachable for %d ticks (%s). Supervisor should restart it.",
                    self._consecutive_server_failures,
                    exc,
                )
            else:
                logger.error("Failed to fetch tasks: %s", exc)
            result.errors.append(f"fetch_all: {exc}")
            # Even when the server is unreachable, refresh agent states and
            # reap zombies so dead processes don't accumulate across ticks.
            refresh_agent_states(self, {})
            reap_dead_agents(self, result, {})
            return result

        logger.debug(
            "tick #%d: %d HTTP read(s) this tick (open=%d claimed=%d done=%d failed=%d)",
            self._tick_count,
            _tick_http_reads,
            len(tasks_by_status.get("open", [])),
            len(tasks_by_status.get("claimed", [])),
            len(tasks_by_status.get("done", [])),
            len(tasks_by_status.get("failed", [])),
        )

        # The server returns tasks matching the requested status; apply the
        # dependency filter here for "open" tasks.
        done_tasks = tasks_by_status["done"]
        # A dependency is satisfied by any terminal-success status, not just
        # "done": once a done task's agent is reaped and its branch merged,
        # the task moves to "closed" (the store soft-archives via status).
        # Checking "done" alone re-blocked dependents forever after that
        # transition, so lower-priority independent tasks were claimed ahead
        # of a high-priority task whose dependency had already completed.
        done_ids = {t.id for t in done_tasks} | {t.id for t in tasks_by_status.get("closed", [])}
        now = time.time()
        open_tasks = [
            t
            for t in tasks_by_status["open"]
            if all(dep in done_ids for dep in t.depends_on)
            # Skip tasks with future created_at (retry backoff)
            and t.created_at <= now
        ]
        result.open_tasks = len(open_tasks)

        # 1b. Hold back tasks blocked by unresolved high-severity pivots
        ready_tasks = open_tasks
        try:
            unresolved = read_unresolved_pivots(self._workdir)
            if unresolved:
                blocked_ids: set[str] = set()
                for pivot in unresolved:
                    blocked_ids.update(pivot.affected_tickets)
                if blocked_ids:
                    before = len(ready_tasks)
                    ready_tasks = [t for t in ready_tasks if t.id not in blocked_ids]
                    held = before - len(ready_tasks)
                    if held:
                        logger.warning(
                            "Holding %d task(s) pending VP pivot review: %s",
                            held,
                            blocked_ids,
                        )
        except OSError as exc:
            logger.warning("Failed to read pivot signals: %s", exc)

        # 1b-i. Check task deadlines - warn or fail running tasks past deadline
        try:
            self._check_task_deadlines(
                tasks_by_status.get("claimed", []) + tasks_by_status.get("in_progress", []),
            )
        except Exception as exc:
            logger.warning("Deadline check failed: %s", exc)

        # 1b-i.5. Release claimed tasks stuck without a live agent (every normal tick)
        if _run_normal:
            try:
                self._release_stale_claims(tasks_by_status.get("claimed", []))
            except Exception as exc:
                logger.warning("Stale claim release failed: %s", exc)

        # 1b-i.6. Priority aging: boost long-waiting open/blocked tasks
        # so that lower-priority work does not starve behind a steady stream of
        # P1 tickets. Gated behind the ``priority_aging_enabled`` config flag
        # (default OFF) and run every ``priority_aging_interval_ticks`` ticks.
        if (
            self._config.priority_aging_enabled
            and self._config.priority_aging_interval_ticks > 0
            and self._tick_count % self._config.priority_aging_interval_ticks == 0
        ):
            try:
                from bernstein.core.tasks.priority_aging import AgingConfig, apply_aging

                aging_targets = ready_tasks + list(tasks_by_status.get("blocked", []))
                results = apply_aging(aging_targets, AgingConfig())
                if results:
                    logger.info(
                        "priority_aging: boosted %d task(s) on tick #%d",
                        len(results),
                        self._tick_count,
                    )
            except Exception as exc:
                logger.warning("priority_aging pass failed: %s", exc)

        # 1b-ii. Governed workflow: filter tasks to current phase only
        if self._workflow_executor is not None and not self._workflow_executor.is_completed:
            before_wf = len(ready_tasks)
            ready_tasks = self._workflow_executor.filter_tasks_for_current_phase(ready_tasks)
            held_wf = before_wf - len(ready_tasks)
            if held_wf:
                logger.info(
                    "Workflow phase %r: holding %d task(s) outside current phase",
                    self._workflow_executor.current_phase_name,
                    held_wf,
                )
            # Check for file-based approval grant
            self._check_workflow_approval()

        # 1c. Build task graph and compute optimal parallelism
        #     Graph analysis + dependency validation are expensive - gate behind
        #     _run_normal. The all_tasks list and task ID cache are always needed.
        all_tasks = list(itertools.chain.from_iterable(tasks_by_status.values()))
        self._latest_tasks_by_id = {task.id: task for task in all_tasks}

        task_graph: TaskGraph | None = None
        if _run_normal:
            task_graph = TaskGraph(all_tasks)
            analysis = task_graph.analyse()
            dep_validator = DependencyValidator()
            dep_validation = dep_validator.validate(all_tasks)
            for cycle in dep_validation.cycles:
                logger.error("Dependency cycle detected: %s", " -> ".join(cycle))
            for task_id, dep_id, dep_status in dep_validation.stuck_deps:
                logger.warning(
                    "Task %s depends on %s which is %s - task remains blocked",
                    task_id,
                    dep_id,
                    dep_status,
                )
            for warning in dep_validation.warnings:
                logger.warning("Dependency validation: %s", warning)
            critical_path_ids = set(dep_validator.critical_path(all_tasks))
            # Cache for use in fast ticks
            self._cached_critical_path_ids = critical_path_ids

            if analysis.parallel_width < self._config.max_agents and analysis.parallel_width > 0:
                logger.debug(
                    "Graph parallel width (%d) < max_agents (%d) -- dependency filter already limits concurrency",
                    analysis.parallel_width,
                    self._config.max_agents,
                )

            if analysis.bottlenecks:
                logger.info(
                    "Graph bottleneck(s): %s -- %d downstream tasks blocked",
                    analysis.bottlenecks,
                    sum(len(task_graph.dependents(b)) for b in analysis.bottlenecks),
                )

            # Persist graph snapshot for dashboard / debugging
            try:
                task_graph.save(self._workdir / ".sdd" / "runtime")
            except OSError as exc:
                logger.debug("Failed to save task graph: %s", exc)
        else:
            # Fast tick: skip the expensive graph analysis, validation and
            # snapshot persistence, but still recompute the critical path -
            # it is a cheap O(V+E) traversal and claim ordering depends on
            # it. The very first tick is always a fast tick (_tick_count is
            # incremented to 1 before the phase check, and 1 % phase != 0),
            # so reusing only the cache from the last normal tick left the
            # first spawn batch without the critical-path priority boost
            # and served stale boosts to tasks created between normal ticks.
            critical_path_ids = set(DependencyValidator().critical_path(all_tasks))
            self._cached_critical_path_ids = critical_path_ids

        # 3. Count alive agents, spawn if capacity (capped by graph parallel width)
        # 2b. Rate-limit recovery: restore providers whose throttle window expired.
        _recovered = self._rate_limit_tracker.recover_expired_throttles(self._router)
        if _recovered:
            logger.info("Rate-limit: recovered providers %s", _recovered)
        # Sync active-agent counts into the router for load-spreading scores.
        if self._router is not None:
            self._router.update_active_agent_counts(self._rate_limit_tracker.get_all_active_counts())

        # 2c. Poll Provider Batch API
        if self._batch_api is not None:
            self._batch_api.poll(self)

        # 2d. Detect loops and deadlocks
        check_loops_and_deadlocks(self)

        # 2e. Recycle idle agents
        recycle_idle_agents(self, tasks_by_status)

        # 2e-i. Evict agents flagged by the context-degradation detector.
        #       Checkpoints progress, stashes a recovery-context preamble
        #       keyed by task_id, and writes SHUTDOWN so the next tick's
        #       refresh_agent_states reaps the dead process and a fresh
        #       spawn replaces it.
        try:
            evict_degraded_sessions(self)
        except Exception as exc:
            logger.warning("context_degradation: evict pass failed: %s", exc)

        # Sync failure timestamps to spawner for cooldown enforcement
        self._spawner._agent_failure_timestamps = self._agent_failure_timestamps

        refresh_agent_states(self, tasks_by_status)
        alive_count = sum(1 for a in self._agents.values() if a.status != "dead")
        result.active_agents = alive_count

        if task_graph is not None:
            prepare_speculative_warm_pool(self, task_graph, all_tasks)

        # 3a. Build alive-per-role map for task distribution prioritization.
        # Starving roles (0 alive agents) get scheduled before well-served roles.
        _alive_per_role: dict[str, int] = {}
        for _agent in self._agents.values():
            if _agent.status != "dead":
                _alive_per_role[_agent.role] = _alive_per_role.get(_agent.role, 0) + 1

        # 2. Group into batches with starving-role prioritization wired in
        budget_status = self._cost_tracker.status()
        cost_estimates: dict[str, float] = {}
        if ready_tasks:
            from bernstein.core.cost_estimation import estimate_spawn_cost

            metrics_dir = self._workdir / ".sdd" / "metrics"
            for task in ready_tasks:
                try:
                    estimate = estimate_spawn_cost(task, metrics_dir=metrics_dir)
                    cost_estimates[task.id] = estimate.estimated_cost_usd
                except Exception as exc:
                    logger.debug("Cost estimate unavailable for task %s: %s", task.id, exc)
        priority_overrides = {
            task.id: max(1, task.priority - 1) for task in ready_tasks if task.id in critical_path_ids
        }
        # Build task creation timestamp map for fair scheduling
        task_created_at = {task.id: task.created_at for task in ready_tasks}
        batches = group_by_role(
            ready_tasks,
            self._config.max_tasks_per_agent,
            alive_per_role=_alive_per_role,
            priority_overrides=priority_overrides,
            task_created_at=task_created_at,
            agent_affinity=self._agent_affinity or None,
            cost_estimates=cost_estimates or None,
            budget_remaining_usd=budget_status.remaining_usd,
        )
        batches = compact_small_tasks(batches, self._config.max_tasks_per_agent)

        # Track which task IDs are already assigned to active agents
        assigned_task_ids: set[str] = set()
        for agent in self._agents.values():
            if agent.status != "dead":
                assigned_task_ids.update(agent.task_ids)

        # 3b. Adaptive parallelism: adjust effective max_agents based on
        # recent error rate and system CPU load.
        _orig_max_agents = self._config.max_agents
        _effective_max = self._adaptive_parallelism.effective_max_agents()
        self._config.max_agents = _effective_max

        # Record parallelism_level metric for time-series dashboards
        from bernstein.core.metric_collector import MetricType

        _ap_status = self._adaptive_parallelism.status()
        get_collector()._write_metric_point(
            MetricType.PARALLELISM_LEVEL,
            float(_effective_max),
            {
                "configured_max": str(_ap_status.configured_max),
                "error_rate": f"{_ap_status.error_rate:.3f}",
                "cpu_percent": f"{_ap_status.cpu_percent:.1f}",
                "reason": _ap_status.last_adjustment_reason,
            },
        )

        # 3c. Claim tasks and spawn agents for ready batches. Consult the
        # BudgetPolicy first: evaluate() maps the current spend ratio to a
        # PAUSE / DOWNGRADE_MODEL / ABORT action. apply_policy() mutates
        # batched tasks' model fields in place for DOWNGRADE_MODEL so the
        # spawner picks up the cheaper tier.
        budget_decision = self._evaluate_budget_policy(
            list(itertools.chain.from_iterable(batches)),
        )
        if self._cost_autopilot is not None:
            _ap_override = self._cost_autopilot.evaluate()
            if _ap_override is not None:
                logger.info("CostAutopilot: %s", _ap_override.reason)
                for _task in itertools.chain.from_iterable(batches):
                    if not _task.model or _task.model == _ap_override.from_model:
                        _task.model = _ap_override.to_model
        # Cost-aware dispatch gate (#2354): consult the deterministic USD cost
        # policy before claiming/spawning. When a configured cap would be
        # breached by the next dispatch, the tick halts with a sealed,
        # offline-verifiable receipt; no policy (or no cap) is a clean no-op.
        cost_dispatch_halt = self._evaluate_cost_policy_dispatch(batches, cost_estimates)
        if self._config.dry_run:
            for batch in batches:
                for task in batch:
                    logger.info(
                        "[DRY RUN] Would spawn %s agent for: %s (model=%s, effort=%s)",
                        task.role,
                        task.title,
                        task.model,
                        task.effort,
                    )
                    result.dry_run_planned.append((task.role, task.title, task.model, task.effort))
        elif cost_dispatch_halt is not None:
            # Fail-closed: the next dispatch would breach a configured USD cap.
            # The halting decision is already sealed into the lineage spine +
            # audit chain; hold every spawn this tick so the run stops before
            # the cap is breached (issue #2354, AC1).
            logger.warning(
                "Cost policy HALT: next dispatch would breach the %s USD cap by $%.4f (run=%s); "
                "holding spawns. Dispatch receipt %s",
                cost_dispatch_halt.breached_dimension,
                cost_dispatch_halt.projected_overrun_usd,
                self._run_id,
                cost_dispatch_halt.decision_hash,
            )
            self._notify(
                "cost.dispatch.halt",
                "Cost policy halt",
                f"Next dispatch would breach the {cost_dispatch_halt.breached_dimension} USD cap by "
                f"${cost_dispatch_halt.projected_overrun_usd:.4f}; spawns held. "
                f"Dispatch receipt {cost_dispatch_halt.decision_hash}.",
                breached_dimension=cost_dispatch_halt.breached_dimension,
                projected_overrun_usd=round(cost_dispatch_halt.projected_overrun_usd, 6),
                decision_hash=cost_dispatch_halt.decision_hash,
            )
            result.cost_dispatch_halt = cost_dispatch_halt.decision_hash
        elif budget_decision is not None and budget_decision.action == BudgetAction.ABORT:
            _bs = self._cost_tracker.status()
            logger.warning(
                "Budget exhausted - $%.2f spent of $%.2f budget. "
                "Fix: increase budget with --budget N or wait for running tasks to complete",
                _bs.spent_usd,
                _bs.budget_usd,
            )
            self._notify(
                "budget.exhausted",
                "Budget cap reached",
                f"Spending cap of ${_bs.budget_usd:.2f} reached. "
                f"${_bs.spent_usd:.2f} spent ({_bs.percentage_used * 100:.0f}%). "
                "Agent spawning paused.",
                budget_usd=round(_bs.budget_usd, 2),
                spent_usd=round(_bs.spent_usd, 4),
                percent_used=round(_bs.percentage_used * 100, 1),
            )
            # ABORT used to only skip spawn, so in-flight agents
            # kept draining budget until they completed on their own.  Now
            # we SHUTDOWN every live session and (after ``kill_grace_period_s``)
            # SIGKILL any still alive so overruns stay bounded.
            self._enforce_budget_killswitch()
        elif budget_decision is not None and budget_decision.action == BudgetAction.PAUSE:
            _bs = self._cost_tracker.status()
            logger.warning(
                "Budget policy PAUSE triggered at %.1f%% - holding spawns until approval",
                _bs.percentage_used * 100,
            )
            # Fire a one-shot notification when the action first transitions.
            # (apply_policy writes this into self._last_budget_action.)
        else:
            claim_and_spawn_batches(self, batches, alive_count, assigned_task_ids, done_ids, result)

        # Restore max_agents after adaptive-parallelism-adjusted spawning
        self._config.max_agents = _orig_max_agents

        if self._batch_api is not None:
            self._batch_api.poll(self)

        # 4. Check done tasks, run janitor, record evolution metrics
        process_completed_tasks(self, done_tasks, result)

        # 4x. Periodic git hygiene
        # Gated behind _run_slow - git operations are IO-heavy.
        if _run_slow and len(done_tasks) > 0:
            with contextlib.suppress(Exception):
                from bernstein.core.git_hygiene import run_hygiene

                active_ids = {s.id for s in self._agents.values() if s.status != "dead"}
                run_hygiene(self._workdir, active_session_ids=active_ids)

        # 4x-ii. Periodic worktree garbage collection
        # Gated behind _run_slow - worktree GC is IO-heavy.
        if _run_slow:
            try:
                active_ids = {s.id for s in self._agents.values() if s.status != "dead"}
                cleaned = self._spawner.prune_orphan_worktrees(active_ids)
                if cleaned:
                    logger.info("Periodic worktree GC: cleaned %d orphan worktree(s)", cleaned)
            except Exception as exc:
                logger.debug("Periodic worktree GC failed: %s", exc)

        # 4a-wf. Governed workflow: try to advance phase after processing completions
        if self._workflow_executor is not None and not self._workflow_executor.is_completed:
            all_tasks = list(itertools.chain.from_iterable(tasks_by_status.values()))
            phase_event = self._workflow_executor.try_advance(all_tasks)
            if phase_event is not None:
                self._recorder.record(
                    "workflow_phase_advanced",
                    workflow_hash=phase_event.workflow_hash,
                    from_phase=phase_event.from_phase,
                    to_phase=phase_event.to_phase,
                    reason=phase_event.reason,
                    tasks_completed=list(phase_event.tasks_completed),
                )
                self._post_bulletin(
                    "status",
                    f"Workflow phase: {phase_event.from_phase} -> {phase_event.to_phase}",
                )

        # 4b. Use cached failed tasks and maybe retry with escalation
        failed_tasks = tasks_by_status["failed"]
        for task in failed_tasks:
            if self._maybe_retry_task(task):
                result.retried.append(task.id)

        # 4b.5 Feed outcomes to adaptive parallelism controller
        for _task_id in result.verified:
            self._adaptive_parallelism.record_outcome(success=True)
        for _ft in failed_tasks:
            if _ft.id not in self._retried_task_ids:
                self._adaptive_parallelism.record_outcome(success=False)

        # 4b.6 Track completions/failures for manager review trigger
        self._completions_since_review += len(result.verified)
        self._failures_since_review += len([t for t in failed_tasks if t.id not in self._retried_task_ids])

        # Check for explicit review trigger (e.g. from `bernstein review` CLI)
        _review_flag = self._workdir / ".sdd" / "runtime" / "review_requested"
        if _review_flag.exists():
            _review_flag.unlink(missing_ok=True)
            self._completions_since_review = max(
                self._completions_since_review,
                self._MANAGER_REVIEW_COMPLETION_THRESHOLD,
            )

        # Run manager queue review when triggered (periodic correction pass)
        # Gated behind _run_slow - manager review involves an LLM call.
        if _run_slow and self._should_trigger_manager_review(self._failures_since_review):
            self._run_manager_queue_review()

        # 4b.6 AgentOps: update SLOs, check error budget, detect incidents
        # Gated behind _run_slow - SLO/incident tracking is expensive and
        # doesn't need sub-minute granularity.
        if _run_slow:
            collector = get_collector()
            self._slo_tracker.update_from_collector(collector)
            self._slo_tracker.save(self._workdir / ".sdd" / "metrics")

            # Apply error-budget-driven throttling adjustments
            adjusted_max, _ = apply_error_budget_adjustments(self._config.max_agents, self._slo_tracker)
            self._adaptive_parallelism.set_slo_constraint(
                adjusted_max if adjusted_max != self._config.max_agents else None
            )

            # Track consecutive failures for incident detection
            if result.verified:
                self._consecutive_failures = 0
            if failed_tasks:
                self._consecutive_failures += len([t for t in failed_tasks if t.id not in self._retried_task_ids])

            # Check for incidents
            all_counted = self._slo_tracker.error_budget.total_tasks
            failed_counted = self._slo_tracker.error_budget.failed_tasks
            incident = self._incident_manager.check_for_incidents(
                failed_task_count=failed_counted,
                total_task_count=all_counted,
                consecutive_failures=self._consecutive_failures,
                error_budget_depleted=self._slo_tracker.error_budget.is_depleted,
            )
            self._incident_manager.save(self._workdir / ".sdd" / "runtime")

            # Notify PagerDuty on SEV1/SEV2 incidents
            if incident is not None and incident.severity in ("sev1", "sev2"):
                self._notify(
                    "incident.critical",
                    f"Incident [{incident.severity.value.upper()}]: {incident.title}",
                    incident.description,
                    incident_id=incident.id,
                    severity=incident.severity.value,
                    failed_tasks=str(failed_counted),
                    total_tasks=str(all_counted),
                    consecutive_failures=str(self._consecutive_failures),
                )

        # 4c. Check heartbeat-based staleness; send WAKEUP/SHUTDOWN as needed
        check_stale_agents(self)

        # 4d. Check progress-snapshot-based stalls; send WAKEUP/SHUTDOWN/kill
        check_stalled_tasks(self)

        # 4d-ii. Token growth monitor: alert on quadratic growth, kill runaway agents
        check_token_growth(self)

        # 4d-ii.5 Loop and deadlock detection: kill looping agents, break lock cycles
        check_loops_and_deadlocks(self)

        # 4d-ii.6 Three-tier watchdog: mechanical checks -> AI triage -> human escalation
        # Gated behind _run_slow - watchdog sync is heavyweight.
        if _run_slow:
            self._watchdog.sync(collect_watchdog_findings(self))

        # 4d-ii.7 Stalled-manager detector (#1267). Run BEFORE the generic
        # supervisor times out and emits a misleading "Server unresponsive"
        # kill so operators see the real cause + remediation pointer.
        from bernstein.core.orchestration.stalled_manager import handle_stalled_manager

        if handle_stalled_manager(self) is not None:
            # Diagnostic already logged + persisted; _running is now False so
            # _run_loop will exit cleanly on its next iteration.
            return result

        # 4d-ii.8 Self-healing liveness watchdog (#1224): periodic check for
        # stuck adapter sessions on pre-approved safety prompts. Off by
        # default; opt-in via BERNSTEIN_WATCHDOG_ENABLED=1. Complementary
        # to the stalled-manager detector above - different failure mode,
        # different recovery action.
        if _run_slow:
            self._run_liveness_watchdog()

        # 4d-iii. Cost anomaly detection: burn rate projection, stop on budget overrun
        # Gated behind _run_slow - anomaly detection doesn't need every-tick granularity.
        if _run_slow:
            for sig in self._anomaly_detector.check_tick(list(self._agents.values()), self._cost_tracker):
                self._handle_anomaly_signal(sig)

        # 4d-iv. Real-time cost recording: update budget status from live tokens
        self._record_live_costs()

        # 4d-v. CI autofix poll: opt-in via config.ci_autofix.enabled.
        # _maybe_poll_ci_autofix internally rate-limits to poll_interval_s
        # (default 60s) and short-circuits when the flag is off, so calling
        # every tick is cheap.
        try:
            created = self._maybe_poll_ci_autofix()
            if created:
                logger.info(
                    "CI autofix poll created %d fix task(s): %s",
                    len(created),
                    ", ".join(created),
                )
        except Exception as exc:
            logger.warning("CI autofix poll raised: %s", exc)

        # 4e. Recycle idle agents (task already resolved but process still alive,
        #     or no heartbeat for idle threshold). SHUTDOWN → 30s grace → SIGKILL.
        recycle_idle_agents(self, tasks_by_status)

        # 4e-ii. Log-growth idle heuristic: catch agents wedged in a
        #     dead MCP/tool call that still emit heartbeats from a side thread but
        #     produce no log output or git activity. Complements recycle_idle_agents
        #     (heartbeat-based) - gated behind _run_normal since the log tail scan
        #     is IO-heavy and does not need every-tick granularity.
        if _run_normal:
            try:
                from bernstein.core.agents.idle_detection import integrate_idle_detection

                integrate_idle_detection(self)
            except Exception as exc:  # tick-level safety net
                logger.warning("Log-growth idle detection failed: %s", exc)

        # 5. Reap dead/stale agents and fail their tasks
        reap_dead_agents(self, result, tasks_by_status)

        # 5b. Retry any pushes that failed in previous ticks (normal cadence)
        if _run_normal:
            try:
                retried = self._spawner.retry_pending_pushes()
                if retried:
                    logger.info("Retried %d pending push(es) successfully", retried)
            except Exception as exc:
                logger.warning("Pending push retry failed: %s", exc)

        # 6. Run evolution analysis cycle every N ticks
        # Gated behind _run_slow - evolution analysis is heavyweight.
        if _run_slow and self._evolution is not None and self._tick_count % self._config.evolution_tick_interval == 0:
            self._run_evolution_cycle(result)

        # 6b. Refresh knowledge base every 5 evolution intervals
        # Gated behind _run_slow - knowledge base refresh is IO-heavy.
        if _run_slow and self._tick_count % (self._config.evolution_tick_interval * 5) == 0:
            try:
                refresh_knowledge_base(self._workdir)
            except OSError as exc:
                logger.warning("Knowledge base refresh failed: %s", exc)

        # 7. Check evolve mode: if all tasks done and no agents alive, trigger new cycle
        self._check_evolve(result, tasks_by_status)

        # 8. Replenish backlog in evolve mode when tasks run out
        self._replenish_backlog(result)

        # 8b. Generate run completion summary for non-evolve runs.
        #
        # Bug (2026-07-02 D2 openrouter final leg,
        # work/bernstein/proofs/d2/openrouter/attempt-final-f15a2a9e):
        # `tasks_by_status` here is the snapshot fetched at the TOP of this
        # tick (step 1, above), before `reap_dead_agents` (step 5, above)
        # can synchronously auto-complete/fail a task within this SAME
        # tick (e.g. an orphan auto-complete after its agent died mid-run).
        # Passing that stale snapshot to `_generate_run_summary` made a
        # task that had JUST been auto-completed look like it never
        # happened: `compute_run_health([])` is vacuously HEALTHY with
        # every TERMINATOR_* count at 0, even though spawner.log had
        # already logged the `orphan_auto_complete` event for that exact
        # task earlier in this same tick. Fix: refetch immediately before
        # generating the summary so it reflects whatever reap_dead_agents
        # just did.
        # Regression (2026-07-02 D2 minimax leg,
        # work/bernstein/proofs/d2/minimax/attempt-e938bd33): this block was
        # previously ALSO gated on `not self._summary_written`. On that run,
        # tick #7 detected quiescence, wrote the summary + interim
        # retrospective (setting `_summary_written = True`), and the settle
        # window then correctly said "run continues" (a retry task had
        # appeared: open=1). But once `_summary_written` was True, every
        # LATER quiescent tick skipped this entire block - so the
        # tick-quiescence self-stop and its shutdown-final retrospective
        # regeneration could never fire again. The run's final quiescence
        # (last retry permanently failed at 01:57:15) passed silently and
        # the process was torn down with the stale tick-7 INTERIM
        # retrospective on disk (3 tasks/$0.0174 vs the real 5/$0.0375).
        # Fix: the `_summary_written` one-shot guard now wraps ONLY the
        # summary generation below; quiescence detection, the settle-window
        # check, the self-stop, and shutdown-final regeneration re-run on
        # every quiescent tick until confirmed.
        if not self._config.evolve_mode and result.open_tasks == result.active_agents == 0:
            refreshed_tasks_by_status = tasks_by_status
            try:
                refreshed_tasks_by_status = fetch_all_tasks(self._client, base)
                logger.info(
                    "8b quiescence detected (tick #%d): refetched tasks before run-summary "
                    "generation to capture any same-tick reap_dead_agents completions "
                    "(done %d->%d, failed %d->%d)",
                    self._tick_count,
                    len(tasks_by_status["done"]),
                    len(refreshed_tasks_by_status["done"]),
                    len(tasks_by_status["failed"]),
                    len(refreshed_tasks_by_status["failed"]),
                )
            except httpx.HTTPError:
                logger.exception(
                    "8b quiescence refetch failed (tick #%d) - falling back to the "
                    "pre-reap snapshot for run-summary generation; the interim "
                    "retrospective may undercount tasks reap_dead_agents completed "
                    "this tick",
                    self._tick_count,
                )

            # Defect 29 (2026-07-02, work/bernstein/proofs/d2/minimax/attempt-83808a8a):
            # process_completed_tasks (the post-/complete chain) ran at step
            # 1c above, BEFORE the refetch. Any task that became terminal
            # during this same tick - e.g. via reap_dead_agents' orphan
            # auto-complete at step 5, or via a /complete POST that landed
            # between step 1 and step 8b - was NOT in process_completed_tasks'
            # input list. Drive the chain for those newly-terminal tasks here
            # so the 8b summary gate (``_pending_post_complete_count == 0``)
            # can fire on this tick instead of waiting one full poll cycle.
            _newly_terminal_for_chain: list[Task] = []
            for bucket in ("done", "failed"):
                for task in refreshed_tasks_by_status.get(bucket, []):
                    if task.id not in self._processed_done_tasks:
                        _newly_terminal_for_chain.append(task)
            if _newly_terminal_for_chain:
                logger.info(
                    "8b pre-summary chain processing (tick #%d): %d newly-terminal "
                    "task(s) added to process_completed_tasks: %s",
                    self._tick_count,
                    len(_newly_terminal_for_chain),
                    ", ".join(t.id for t in _newly_terminal_for_chain),
                )
                try:
                    _chain_result = TickResult()
                    # Pass both done and failed - process_completed_tasks
                    # filters to ``_processed_done_tasks`` itself so a
                    # failed task gets registered the same way as a done
                    # task. A pure done-only filter here would strand
                    # failed tasks in the drain tracker.
                    process_completed_tasks(
                        self,
                        _newly_terminal_for_chain.copy(),
                        _chain_result,
                    )
                except Exception as exc:
                    logger.exception(
                        "8b pre-summary process_completed_tasks raised (tick #%d): %s",
                        self._tick_count,
                        exc,
                    )

            # Defect 29: refresh the drain tracker against the latest snapshot,
            # then log the run-end decision with all its inputs so a 2-minute
            # diagnosis from logs alone is possible (house rule 2).
            _pending_post_complete = self._refresh_drain_tracker(refreshed_tasks_by_status)
            if _pending_post_complete > 0:
                _still_pending = sorted(
                    tid
                    for tid in self._post_complete_seen_task_ids
                    if tid not in set(self._processed_done_tasks.keys())
                )
                _action = "defer_summary"
                logger.info(
                    "run_end_check: tick=#%d open=%d active=%d pending_post_complete=%d "
                    "summary_written=%s -> action=%s (waiting for post-/complete chain "
                    "on tasks: %s)",
                    self._tick_count,
                    result.open_tasks,
                    result.active_agents,
                    _pending_post_complete,
                    self._summary_written,
                    _action,
                    _still_pending or "<none>",
                )
            elif not self._summary_written:
                _action = "fire_summary"
                logger.info(
                    "run_end_check: tick=#%d open=%d active=%d pending_post_complete=%d "
                    "summary_written=%s -> action=%s (all terminal tasks drained)",
                    self._tick_count,
                    result.open_tasks,
                    result.active_agents,
                    _pending_post_complete,
                    self._summary_written,
                    _action,
                )
                self._generate_run_summary(refreshed_tasks_by_status["done"], refreshed_tasks_by_status["failed"])
            else:
                _action = "summary_already_written"
                logger.info(
                    "run_end_check: tick=#%d open=%d active=%d pending_post_complete=%d "
                    "summary_written=%s -> action=%s (proceeding to settle-window "
                    "self-stop check; shutdown-final regeneration still pending)",
                    self._tick_count,
                    result.open_tasks,
                    result.active_agents,
                    _pending_post_complete,
                    self._summary_written,
                    _action,
                )

            # Only self-stop once real work has actually happened (at least
            # one task has reached a terminal state). A brand-new/empty
            # backlog reading open=0/agents=0 on tick #1 is not "the run
            # finished" - it's "nothing has been scheduled yet" (e.g. a
            # server that hasn't ingested its seed task yet, or a test
            # harness driving tick() directly against an empty transport).
            # Self-stopping in that case would end the orchestrator before
            # it ever does anything.
            _had_any_terminal_task = bool(refreshed_tasks_by_status["done"] or refreshed_tasks_by_status["failed"])
            if not _had_any_terminal_task:
                logger.debug(
                    "8b quiescence (tick #%d) with zero terminal tasks - not eligible "
                    "for self-stop yet (nothing has actually run)",
                    self._tick_count,
                )

            # Confirm this quiescence is real rather than a momentary gap
            # before more child tasks appear (see the A5 stale-retrospective
            # bug this module already guards against: a run reported
            # 100%/HEALTHY at T+58s and 2 more tasks subsequently failed).
            # A short settle window catches that race without waiting a
            # full poll interval.
            #
            # This self-stop is also what makes shutdown-final retrospective
            # regeneration actually fire for runs that end via a `--quiet` /
            # wait-for-completion CLI path (cli/run_bootstrap.py's
            # `_wait_for_run_completion`, which polls this process's HTTP
            # server from a SEPARATE CLI process and never sends this
            # process a stop signal). This orchestrator subprocess is
            # launched detached (`start_new_session=True` in
            # server_launch._start_spawner), so without self-stopping here
            # it just idles until the container's PID-namespace teardown
            # SIGKILLs it - unmaskable, no Python cleanup code runs, and
            # `run()`'s post-tick-loop shutdown-final regeneration never
            # gets a chance to execute.
            if _had_any_terminal_task:
                # Overridable via env var so tests (and any operator who
                # wants a tighter/looser margin) don't have to eat a real
                # 2s sleep per quiescent tick.
                _settle_s = float(os.environ.get("BERNSTEIN_QUIESCENCE_SETTLE_S", "2.0"))
                if _settle_s > 0:
                    time.sleep(_settle_s)
                try:
                    settled = fetch_all_tasks(self._client, base)
                except httpx.HTTPError:
                    logger.exception(
                        "Quiescence settle re-check failed (tick #%d) after %.1fs - not "
                        "self-stopping; relying on the normal tick loop or an external "
                        "stop signal instead",
                        self._tick_count,
                        _settle_s,
                    )
                    settled = None
                if settled is not None:
                    settled_open = len(settled["open"])
                    settled_agents = sum(1 for a in self._agents.values() if a.status != "dead")
                    if settled_open == settled_agents == 0:
                        # Hold/release API (supplements the settle-timer
                        # self-stop above): external callers can call
                        # POST /orchestrator/holds to keep this orchestrator
                        # alive even when it looks fully quiescent - e.g. a
                        # dashboard mid-review, or an external scheduler about
                        # to enqueue follow-up tasks that hasn't posted them
                        # yet. Checked here, right before the actual
                        # self-stop, so a hold acquired at any point up to
                        # this instant still prevents the stop for this tick.
                        try:
                            _active_holds = fetch_active_holds(self._client, base)
                        except Exception as exc:  # intentional-broad-except: must never crash the tick loop
                            logger.warning(
                                "fetch_active_holds raised during quiescence self-stop check (tick #%d): %s "
                                "- treating as no active holds",
                                self._tick_count,
                                exc,
                            )
                            _active_holds = []
                        if _active_holds:
                            _hold_reasons = [sanitize_log(str(h.get("reason", "<no reason>"))) for h in _active_holds]
                            logger.info(
                                "Quiescence detected but %d active hold(s) present (tick #%d) - skipping self-stop: %s",
                                len(_active_holds),
                                self._tick_count,
                                _hold_reasons,
                            )
                        else:
                            logger.info(
                                "Quiescence confirmed after %.1fs settle window (tick #%d, "
                                "open=%d agents=%d, no active holds) - self-stopping",
                                _settle_s,
                                self._tick_count,
                                settled_open,
                                settled_agents,
                            )
                            self._regenerate_final_retrospective(trigger_path="tick-quiescence-self-stop")
                            self._running = False
                    else:
                        logger.info(
                            "Quiescence NOT confirmed after %.1fs settle window (tick #%d): "
                            "open=%d agents=%d - run continues",
                            _settle_s,
                            self._tick_count,
                            settled_open,
                            settled_agents,
                        )

        # 9. Log summary
        self._log_summary(result)

        # 11. Record replay events for deterministic replay
        self._record_tick_events(result, tasks_by_status)

        # 12. LLM watcher (opt-in, off by default).  Single hook point that
        #     hands an immutable ``WatcherEvent`` snapshot to the watcher.
        #     The watcher receives no orchestrator handle and cannot mutate
        #     state.  Failures are swallowed - orchestrator stability wins.
        self._dispatch_watcher_events(result)

        return result

    def _dispatch_watcher_events(self, result: TickResult) -> None:
        """Hand watcher events to the opt-in LLM observer.

        Called once at the end of ``_tick_internal``.  Builds a frozen
        snapshot per spawned/verified task and passes it to the watcher.
        The watcher receives no orchestrator handle, no task store, and
        no spawner - only the snapshot.  This is the single, well-defined
        hook required by the watcher's read-only contract.

        Args:
            result: The :class:`TickResult` produced by the current tick.

        Side effects:
            * Appends any returned :class:`Suggestion` records to
              ``self._llm_watcher_signals`` for downstream surfaces.
            * Never mutates ``result``, ``tasks_by_status``, or any
              orchestrator state besides the bounded signal deque.
            * Catches every exception so a misbehaving watcher cannot
              crash the orchestrator.
        """
        watcher = getattr(self, "_llm_watcher", None)
        if watcher is None or not watcher.config.enabled:
            return

        try:
            from bernstein.core.observability.llm_watcher import WatcherEvent
        except Exception:
            return

        events: list[WatcherEvent] = []
        now = time.time()
        for task_id in result.spawned:
            events.append(
                WatcherEvent(
                    kind="task_spawned",
                    run_id=self._run_id,
                    timestamp=now,
                    payload={"task_id": task_id, "tick": self._tick_count},
                )
            )
        for task_id in result.verified:
            events.append(
                WatcherEvent(
                    kind="task_completed",
                    run_id=self._run_id,
                    timestamp=now,
                    payload={"task_id": task_id, "tick": self._tick_count},
                )
            )

        if not events:
            return

        # Schedule observe() coroutines on a short-lived event loop so the
        # synchronous tick is never blocked by network I/O for longer than
        # the watcher's per-call timeout.
        try:
            import asyncio

            async def _drain() -> list[Any]:
                drained: list[Any] = []
                for event in events:
                    try:
                        sigs = await watcher.observe(event)
                    except Exception:
                        # Defence-in-depth: observe() already swallows
                        # exceptions, but the watcher contract is too
                        # important to leave a single uncaught error path.
                        logger.warning(
                            "LLM watcher observe() raised for kind=%s",
                            event.kind,
                            exc_info=True,
                        )
                        sigs = []
                    drained.extend(sigs)
                return drained

            try:
                asyncio.get_running_loop()
                inside_loop = True
            except RuntimeError:
                inside_loop = False

            # If the tick ever runs inside an async context the watcher
            # is skipped - the dispatcher refuses to nest event loops.
            signals = asyncio.run(_drain()) if not inside_loop else []

            for sig in signals:
                self._llm_watcher_signals.append(sig)
        except Exception:
            logger.warning("LLM watcher dispatch failed", exc_info=True)

    def _check_workflow_approval(self) -> None:
        """Check for file-based workflow approval grant.

        Looks for ``.sdd/runtime/workflow/approve_{phase_name}`` files.
        When found, grants approval and removes the file.
        """
        if self._workflow_executor is None or not self._workflow_executor.approval_pending:
            return
        phase_name = self._workflow_executor.current_phase_name
        approval_file = self._workdir / ".sdd" / "runtime" / "workflow" / f"approve_{phase_name}"
        if approval_file.exists():
            reason = approval_file.read_text().strip() or "file-based approval"
            approval_file.unlink(missing_ok=True)
            # Also clean up the pending request file
            pending = self._workdir / ".sdd" / "runtime" / "workflow" / f"approval_pending_{phase_name}.json"
            pending.unlink(missing_ok=True)
            self._workflow_executor.grant_approval(reason=reason)
            self._recorder.record(
                "workflow_approval_granted",
                phase=phase_name,
                reason=reason,
            )
            logger.info("Workflow approval granted for phase %r via file", phase_name)

    def _regenerate_final_retrospective(self, trigger_path: str) -> None:
        """Regenerate the FINAL retrospective by re-reading final event state.

        Idempotent and safe to call from more than one terminal path
        (tick-loop drain, tick-level quiescence self-stop, future
        interrupt/kill paths) - only the first caller actually writes;
        later callers just log which path was skipped and why. This is the
        single source of truth for "what triggered shutdown-final
        regeneration" so a `grep` of the logs always finds an explicit
        answer (logging-is-the-debugging-interface rule).

        Args:
            trigger_path: Human-readable name of the terminal path that
                invoked this (e.g. "tick-loop-drain",
                "tick-quiescence-self-stop"). Logged at INFO so a run's
                actual shutdown path is always visible in the logs.
        """
        if self._config.evolve_mode:
            logger.warning(
                "shutdown-final regeneration skipped via %s: evolve_mode is on",
                trigger_path,
            )
            return
        if self._final_retrospective_regenerated:
            logger.info(
                "shutdown-final regeneration already performed by an earlier terminal "
                "path - skipping duplicate run triggered via %s",
                trigger_path,
            )
            return
        logger.info("shutdown-final regeneration triggered via %s", trigger_path)
        try:
            # Re-fetch FINAL event state here rather than reusing any
            # previously-cached task snapshot. This is the fix for the
            # sibling bug found alongside the "never fires" bug (same
            # ground-truth run): the 8b tick-level mid-run summary was
            # feeding generate_retrospective() a snapshot fetched BEFORE
            # reap_dead_agents() (tick step 5) synchronously auto-completed
            # an orphaned task within that same tick, so the retrospective
            # vacuously reported 0 done / 0 failed / HEALTHY while
            # spawner.log had already logged the orphan_auto_complete event.
            # Fetching fresh here, at the moment of actual shutdown, is the
            # only way to guarantee every such in-tick completion is
            # reflected.
            final_tasks = fetch_all_tasks(self._client, self._config.server_url)
            status_histogram = {status: len(tasks) for status, tasks in final_tasks.items()}
            runtime_dir = self._workdir / ".sdd" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            generate_retrospective(
                done_tasks=final_tasks.get("done", []),
                failed_tasks=final_tasks.get("failed", []),
                collector=get_collector(self._workdir / ".sdd" / "metrics"),
                runtime_dir=runtime_dir,
                run_start_ts=self._run_start_ts,
                trigger_reason="shutdown-final",
                full_status_counts=status_histogram,
            )
            self._final_retrospective_regenerated = True
        except Exception:
            logger.exception(
                "Final retrospective regeneration failed at shutdown via %s (non-fatal)",
                trigger_path,
            )

    def run(self) -> None:
        """Run the orchestrator loop until stopped.

        Blocks the calling thread. Call ``stop()`` from another thread or
        a signal handler to break the loop. Individual tick failures are
        caught and logged so a single bad tick cannot kill the loop.
        """
        self._running = True
        logger.info(
            "Orchestrator started (poll=%ds, max_agents=%d, server=%s)",
            self._config.poll_interval_s,
            self._config.max_agents,
            self._config.server_url,
        )
        # Start cluster heartbeat client (registers this node with central server)
        if self._heartbeat_client is not None:
            self._heartbeat_client.start()
            logger.info("Cluster heartbeat client started")
        self._post_bulletin("status", "run started")
        self._notify("run.started", "Bernstein run started", "Agents are being spawned.")
        # Reconcile tasks left in "claimed" from a previous run whose agents no
        # longer exist.  Must happen after the server is confirmed reachable but
        # before the first tick.
        self._reconcile_claimed_tasks()
        _run_started_extra: dict[str, object] = {}
        if self._workflow_executor is not None:
            _run_started_extra["workflow_name"] = self._workflow_executor.definition.name
            _run_started_extra["workflow_hash"] = self._workflow_executor.definition_hash
        self._recorder.record(
            "run_started",
            run_id=self._run_id,
            max_agents=self._config.max_agents,
            budget_usd=self._config.budget_usd,
            git_sha=self._replay_metadata.git_sha,
            git_branch=self._replay_metadata.git_branch,
            config_hash=self._replay_metadata.config_hash,
            **_run_started_extra,
        )
        # WAL recovery: detect uncommitted entries from crashed previous runs.
        # Must run after WAL writer is initialized (in __init__) so that
        # acknowledgement entries are written to the current run's WAL.
        try:
            self._recover_from_wal()
        except Exception:
            logger.exception("WAL recovery failed (non-fatal) - continuing startup")
        # Audit log integrity check: verify the last N HMAC-chained entries.
        try:
            from bernstein.core.audit_integrity import verify_on_startup

            _integrity = verify_on_startup(self._workdir / ".sdd")
            if not _integrity.valid:
                logger.warning(
                    "Audit integrity check found %d error(s) - review with 'bernstein audit verify'",
                    len(_integrity.errors),
                )
            elif _integrity.entries_checked > 0:
                logger.info(
                    "Audit integrity OK (%d entries verified in %.1fms)",
                    _integrity.entries_checked,
                    _integrity.duration_ms,
                )
        except Exception:
            logger.exception("Audit integrity check failed (non-fatal) - continuing startup")
        # Zombie cleanup: terminate orphaned agent processes from prior crashed runs.
        try:
            from bernstein.core.zombie_cleanup import scan_and_cleanup_zombies

            _zr = scan_and_cleanup_zombies(self._workdir)
            if _zr.orphans_found:
                logger.info(
                    "Zombie cleanup: found=%d killed=%d stale=%d errors=%d",
                    _zr.orphans_found,
                    _zr.orphans_killed,
                    _zr.stale_removed,
                    len(_zr.errors),
                )
        except Exception:
            logger.exception("Zombie cleanup failed (non-fatal) - continuing startup")
        consecutive_failures = 0
        max_consecutive_failures = ORCHESTRATOR.max_consecutive_failures
        while self._running or self._has_active_agents():
            tick_result: TickResult | None = None
            try:
                tick_result = self.tick()
                consecutive_failures = 0
            except Exception:
                consecutive_failures += 1
                logger.exception(
                    "Tick %d failed (%d consecutive failures)",
                    self._tick_count,
                    consecutive_failures,
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        "Stopping after %d consecutive tick failures",
                        consecutive_failures,
                    )
                    break
            if self._config.dry_run:
                break
            # Adaptive backoff: double sleep when idle, reset when work is found.
            # On server failure: sleep longer to give supervisor time to restart.
            server_failures = getattr(self, "_consecutive_server_failures", 0)
            if server_failures > 0:
                # Backoff: 5s, 10s, 15s, 20s, 30s (capped)
                time.sleep(min(5.0 * server_failures, 30.0))
            elif tick_result is not None and (
                tick_result.spawned or tick_result.verified or tick_result.retried or tick_result.open_tasks > 0
            ):
                self._idle_multiplier = 1
                time.sleep(self._config.poll_interval_s)
            else:
                self._idle_multiplier = min(self._idle_multiplier * 2, 8)
                time.sleep(min(self._config.poll_interval_s * self._idle_multiplier, 30.0))

            # Hot-reload bernstein.yaml config (mutable fields only)
            self._maybe_reload_config()

            # Check if a restart was requested (own source code changed)
            restart_flag = self._workdir / ".sdd" / "runtime" / "restart_requested"
            needs_restart = False
            if restart_flag.exists():
                restart_flag.unlink(missing_ok=True)
                needs_restart = True
            elif self._config.evolve_mode and self._check_source_changed():
                needs_restart = True

            if needs_restart:
                logger.info("Restarting orchestrator (own code updated)")
                self._save_session_state()
                self._restart()
                return  # _restart calls os.execv, but just in case

        self._drain_before_cleanup()
        self._cleanup()

        # A5 fix: unconditionally regenerate the FINAL retrospective here, at
        # true orchestrator shutdown (the tick loop has actually exited),
        # overwriting any earlier "mid-run" snapshot written by
        # _generate_run_summary()'s tick-level "queue looks empty" heuristic.
        # This is the only point where every task has had the chance to
        # reach a terminal state, so it's the only point a HEALTHY verdict
        # can be trusted (see canary A5 stale-retrospective bug: a run
        # reported 100%/HEALTHY at T+58s while 2 of 3 tasks subsequently
        # failed, and the report was never refreshed).
        #
        # Regression (2026-07-02 D2 openrouter final leg,
        # work/bernstein/proofs/d2/openrouter/attempt-final-f15a2a9e): this
        # block only runs if the tick loop actually exits via `self._running`
        # going False, i.e. someone called `.stop()` (normally the SIGINT/
        # SIGTERM handler installed around `orchestrator.run()` in this
        # module's __main__ block). But `bernstein run --quiet` /
        # `_wait_for_run_completion()` (cli/run_bootstrap.py) never signals
        # this process - it just polls the task server's HTTP /status
        # endpoint from an entirely separate CLI process and returns once
        # quiescent. This orchestrator subprocess is launched detached
        # (`start_new_session=True` in server_launch._start_spawner), so
        # when the CLI process then exits and the container's PID namespace
        # tears down, the kernel SIGKILLs every process in the namespace
        # unconditionally - no Python code here ever runs. See
        # `_regenerate_final_retrospective` / the tick()-level
        # "tick-quiescence-self-stop" path below for the self-triggered
        # fix that covers that terminal path too; this call is now a no-op
        # (guarded by `_final_retrospective_regenerated`) whenever that path
        # already handled it, and remains the sole trigger for interrupt/
        # kill-signal shutdowns where `.stop()` IS called.
        self._regenerate_final_retrospective(trigger_path="tick-loop-drain")

        self._post_bulletin("status", "run stopped")
        self._recorder.record(
            "run_completed",
            run_id=self._run_id,
            ticks=self._tick_count,
            fingerprint=self._recorder.fingerprint(),
        )
        self._seal_journal_into_lineage_spine()
        if self._otel_stream is not None:
            self._otel_stream.finalize()
        logger.info(
            "Orchestrator stopped (replay: %s, fingerprint: %s)",
            self._recorder.path,
            self._recorder.fingerprint()[:16] + "...",
        )

    def _seal_journal_into_lineage_spine(self) -> None:
        """Wire the journal head into the f01 lineage spine (issue #2293).

        Records the finalized journal head into the run's lineage spine so
        the run's replay identity and its artifact provenance share one
        root. Failures are logged, never raised: sealing is a provenance
        aid and must not fail a run that already completed.
        """
        try:
            from bernstein.core.security.audit import load_or_create_audit_key

            hmac_key = load_or_create_audit_key()
            seal_journal_into_spine(
                self._recorder,
                lineage_root=self._workdir / ".sdd" / "lineage",
                hmac_key=hmac_key,
                actor="orchestrator",
            )
        except Exception as exc:
            logger.warning("Failed to seal journal head into lineage spine: %s", sanitize_log(str(exc)))
        else:
            self._seal_intent_capsules(hmac_key)

    def _seal_intent_capsules(self, hmac_key: bytes) -> None:
        """Commit the finished journal's end for every capsule bound to this run (#2649).

        Offline verification cannot otherwise tell a truncated journal from a
        short one: the journal chain recomputes from genesis, so any prefix
        verifies on its own. Sealing here -- once the run is over and the
        journal is final -- is what makes a later ``bernstein intent verify``
        able to attest completeness rather than only "no drift in what remains".

        Idempotent, and failures are logged rather than raised: sealing is an
        attestation aid and must not fail a run that already completed.
        """
        try:
            from bernstein.core.security.audit_chain import AuditChainStore
            from bernstein.core.security.intent_capsule import seal_capsules_bound_to_run

            sdd_dir = self._workdir / ".sdd"
            sealed = seal_capsules_bound_to_run(
                chain=AuditChainStore(sdd_dir / "audit", key=hmac_key),
                sdd_dir=sdd_dir,
                run_id=self._run_id,
            )
            if sealed:
                logger.info("Sealed %d intent capsule(s) for run %s", len(sealed), self._run_id)
        except Exception as exc:
            logger.warning("Failed to seal intent capsules: %s", sanitize_log(str(exc)))

    def _has_active_agents(self) -> bool:
        """Return True if any agents are still alive (not dead)."""
        alive = sum(1 for s in self._agents.values() if s.status != "dead")
        if alive > 0 and not self._running:
            logger.info("Orchestrator draining: %d agent(s) still active", alive)
        return alive > 0

    def _recover_from_wal(self) -> list[tuple[str, Any]]:
        """Check WAL files from previous runs for uncommitted entries.

        Scans all WAL files in ``.sdd/runtime/wal/`` (excluding the current
        run) for entries written with ``committed=False`` - these represent
        task claims where the agent was never successfully spawned (crash
        between claim and spawn).

        For each uncommitted entry an acknowledgement record is appended to
        the current run's WAL so the recovery itself is auditable.  In
        addition, orphaned ``task_claimed`` entries (no matching
        ``task_spawn_confirmed``) are actively retried: a ``task_retry`` WAL
        entry is recorded and ``POST /tasks/{id}/force-claim`` is called with
        reason ``crash_recovery`` so the task returns to the *open* queue
        instead of being silently abandoned. Any prior
        worktrees with uncommitted work are moved to
        ``.sdd/worktrees/preserved/`` and surfaced on the bulletin board so
        fresh agents can resume that work.

        Before the legacy acknowledgement path runs, the
        :class:`WALReplayEngine` performs an idempotent replay:
        uncommitted ``task_claimed`` entries whose task the server still
        reports as *claimed* are transitioned to FAILED with reason
        ``claimed but never spawned`` so the standard retry machinery picks
        them up.  Idempotency markers persisted by the engine prevent
        double-execution across subsequent boots.

        Returns:
            List of (run_id, WALEntry) tuples for all uncommitted entries found.
        """
        sdd_dir = self._workdir / ".sdd"
        # run the WALReplayEngine first so uncommitted task_claimed
        # entries that are still claimed on the server are transitioned to
        # FAILED (reason "claimed but never spawned") with idempotency
        # protection.  Any failure is logged and swallowed so startup is never
        # blocked by a corrupted WAL -- ops can replay manually.
        try:
            self._replay_wal_with_engine(sdd_dir)
        except Exception:
            logger.exception("WAL replay engine failed (non-fatal) - continuing with legacy recovery")
        uncommitted = WALRecovery.scan_all_uncommitted(
            sdd_dir,
            exclude_run_id=self._run_id,
        )
        if not uncommitted:
            # No uncommitted WAL entries, but abandoned worktrees from a
            # prior crash may still carry unsaved work -- preserve them.
            self._preserve_prior_worktrees_with_wip()
            return []

        logger.warning(
            "WAL recovery: found %d uncommitted entries from previous run(s)",
            len(uncommitted),
        )

        # Identify orphaned claims: uncommitted task_claimed entries with
        # no matching task_spawn_confirmed in the same run.  These are the
        # work-loss cases the prior implementation only logged and acked
        # . Use a (run_id, seq) key for O(1) membership checks.
        orphaned = WALRecovery.find_orphaned_claims(
            sdd_dir,
            exclude_run_id=self._run_id,
        )
        orphaned_keys = {(run_id, entry.seq) for run_id, entry in orphaned}

        # Identify orphaned claim_confirmed entries: the spawn created a
        # worktree but the process was SIGKILL'd before task_spawn_confirmed
        # was written.  These represent potentially valuable WIP on disk
        # that must not be silently reaped.
        crashed_spawns = self._find_orphaned_claim_confirmed(sdd_dir)
        crashed_spawn_keys = {(run_id, entry.seq) for run_id, entry in crashed_spawns}
        # task_ids that already have a claim_confirmed crash entry -- skip
        # the task_claimed-level force-claim for them, since the crashed-spawn
        # handler both preserves the worktree and /fail's the task which
        # re-opens it on the server.  Doing both would produce a confusing
        # double-handling trail (task ends up both failed and force-claimed).
        crashed_spawn_task_ids = {str(e.inputs.get("task_id", "")) for _, e in crashed_spawns}

        for run_id, entry in uncommitted:
            is_orphan = (run_id, entry.seq) in orphaned_keys
            is_crashed_spawn = (run_id, entry.seq) in crashed_spawn_keys
            logger.info(
                "WAL uncommitted [run=%s seq=%d]: %s %s%s%s",
                run_id,
                entry.seq,
                entry.decision_type,
                entry.inputs,
                " (orphan: no spawn_confirmed)" if is_orphan else "",
                " (crashed_spawn: worktree materialised)" if is_crashed_spawn else "",
            )
            # Record acknowledgement in current run's WAL for auditability
            try:
                self._wal_writer.write_entry(
                    decision_type="wal_recovery_ack",
                    inputs={
                        "original_run_id": run_id,
                        "original_seq": entry.seq,
                        "original_decision_type": entry.decision_type,
                        "original_inputs": entry.inputs,
                    },
                    output={
                        "action": "acknowledged",
                        "orphan": is_orphan,
                        "crashed_spawn": is_crashed_spawn,
                    },
                    actor="orchestrator",
                    committed=True,
                )
            except OSError:
                logger.debug("WAL write failed for recovery ack (run=%s seq=%d)", run_id, entry.seq)

        # Actively retry orphaned claims so each task returns to the open
        # queue instead of being silently abandoned ( fix part a).
        # Skip tasks that also have a claim_confirmed crash entry -- those
        # are handled by the crashed-spawn path below so the worktree is
        # preserved to the graveyard before the task is /fail'd.
        retried = 0
        for run_id, entry in orphaned:
            if str(entry.inputs.get("task_id", "")) in crashed_spawn_task_ids:
                continue
            if self._retry_orphaned_claim(run_id, entry):
                retried += 1

        # For claim_confirmed orphans: preserve the materialised worktree to
        # ``.sdd/graveyard/<task_id>/<ts>/`` (if dirty or has commits) then
        # POST /tasks/{id}/fail so the task returns to the open queue with a
        # clear reason operators can review.
        crashed_recovered = 0
        for run_id, entry in crashed_spawns:
            if self._recover_crashed_spawn(run_id, entry):
                crashed_recovered += 1

        # Preserve any prior worktrees that still have uncommitted changes
        # so a fresh agent can resume them ( fix part b).
        preserved_paths = self._preserve_prior_worktrees_with_wip()

        # Close every prior-run WAL we observed so future boots do not
        # re-scan the same uncommitted entries forever.
        # We close the union of run_ids that held uncommitted entries and
        # run_ids that held orphaned claims -- in practice these are the
        # only WALs whose entries were returned by the scan helpers.
        closed_run_ids = sorted({r for r, _ in uncommitted} | {r for r, _ in orphaned} | {r for r, _ in crashed_spawns})
        for closed_run_id in closed_run_ids:
            run_uncommitted = sum(1 for r, _ in uncommitted if r == closed_run_id)
            run_orphaned = sum(1 for r, _ in orphaned if r == closed_run_id)
            try:
                WALRecovery.close_wal(
                    closed_run_id,
                    sdd_dir,
                    reason="recovered_by_orchestrator",
                    uncommitted_count=run_uncommitted,
                    orphaned_count=run_orphaned,
                )
            except OSError as exc:
                # Never block orchestrator startup on marker write failure;
                # next boot will simply re-run recovery for this WAL.
                logger.warning(
                    "WAL recovery: failed to write .closed marker for run=%s: %s",
                    closed_run_id,
                    exc,
                )

        self._recorder.record(
            "wal_recovery",
            uncommitted_count=len(uncommitted),
            orphaned_count=len(orphaned),
            retried_count=retried,
            crashed_spawn_count=len(crashed_spawns),
            crashed_recovered=crashed_recovered,
            preserved_worktrees=[str(p) for p in preserved_paths],
            run_ids=sorted({r for r, _ in uncommitted}),
            closed_run_ids=closed_run_ids,
        )
        if retried:
            logger.warning(
                "WAL recovery: retried %d orphaned claim(s) via /tasks/{id}/force-claim",
                retried,
            )
        if crashed_recovered:
            logger.warning(
                "WAL recovery: recovered %d crashed spawn(s); worktrees moved to .sdd/graveyard/",
                crashed_recovered,
            )
        return uncommitted

    def _replay_wal_with_engine(self, sdd_dir: Path) -> None:
        """Run :class:`WALReplayEngine` to transition orphaned claims to FAILED.

        wires :meth:`WALReplayEngine.scan_and_replay` into startup
        so uncommitted ``task_claimed`` entries from a crashed prior run are
        not only logged.  For each orphan whose task is still reported as
        *claimed* on the server, the engine's replay handler:

        1. POSTs ``/tasks/{task_id}/fail`` with reason
           ``claimed but never spawned`` (the standard fail path runs the
           existing retry machinery via ``retry_or_fail_task``).
        2. Appends a ``task_retry`` entry to the current WAL for auditability.

        The engine's :class:`IdempotencyStore` records the entry hash so
        subsequent orchestrator boots skip already-handled entries -- this
        prevents double-fails if a recovery cycle completes but the
        ``.closed`` marker write fails.

        Args:
            sdd_dir: The ``.sdd`` directory root.
        """
        # Fetch the set of task IDs currently marked as claimed on the
        # server.  If the server is unreachable we treat the set as empty
        # which means the replay handler will decline to fail anything and
        # fall back to the legacy force-claim path below.
        claimed_on_server: set[str] = set()
        try:
            resp = self._client.get(f"{self._config.server_url}/tasks?status=claimed")
            resp.raise_for_status()
            payload = resp.json()
            tasks_iter = payload if isinstance(payload, list) else payload.get("tasks", [])
            for task in tasks_iter:
                tid = str(task.get("id", ""))
                if tid:
                    claimed_on_server.add(tid)
        except Exception as exc:
            logger.debug(
                "WAL replay: unable to fetch claimed tasks from server (%s); falling back to legacy force-claim path",
                exc,
            )

        engine = WALReplayEngine(
            sdd_dir=sdd_dir,
            current_run_id=self._run_id,
            wal_writer=self._wal_writer,
        )

        def _handler(entry: WALEntry) -> bool:
            """Replay a single WAL entry.  Return True to mark executed."""
            # Only task_claimed entries are actionable during recovery.
            # Other replay-worthy decision types (task_created,
            # task_completed, agent_spawned, ...) are marked executed so the
            # idempotency store short-circuits them on subsequent boots.
            if entry.decision_type != "task_claimed":
                return True
            task_id = str(entry.inputs.get("task_id", ""))
            if not task_id:
                # Malformed entry; mark as handled so we never revisit it.
                return True
            # Only fail tasks the server still considers claimed.  Anything
            # else is either already resolved or will be handled by the
            # legacy recovery path below (which force-claims back to open).
            if task_id not in claimed_on_server:
                return True
            try:
                resp = self._client.post(
                    f"{self._config.server_url}/tasks/{task_id}/fail",
                    json={"reason": "claimed but never spawned"},
                )
                resp.raise_for_status()
            except Exception as exc:
                logger.warning(
                    "WAL replay: /tasks/%s/fail failed (%s) - orphan will be handled by legacy force-claim recovery",
                    task_id,
                    exc,
                )
                return False
            # Record the retry intent in the current WAL so the fail->retry
            # transition is auditable end-to-end.
            try:
                self._wal_writer.write_entry(
                    decision_type="task_retry",
                    inputs={
                        "task_id": task_id,
                        "reason": "claimed_but_never_spawned",
                        "source": "wal_replay_engine",
                    },
                    output={"action": "failed_for_retry"},
                    actor="orchestrator",
                    committed=True,
                )
            except OSError:
                logger.debug("WAL replay: task_retry write failed (task=%s)", task_id)
            logger.info(
                "WAL replay: failed orphan task %s (claimed but never spawned)",
                task_id,
            )
            return True

        summary = engine.scan_and_replay(replay_handler=_handler)
        if summary.total_uncommitted:
            logger.info(
                "WAL replay engine: total=%d replayed=%d idempotent=%d stale=%d failed=%d",
                summary.total_uncommitted,
                summary.replayed,
                summary.skipped_idempotent,
                summary.skipped_stale,
                summary.failed,
            )

    def _retry_orphaned_claim(self, run_id: str, entry: Any) -> bool:
        """Re-queue a single orphaned claim from a crashed prior run.

        Writes a committed ``task_retry`` WAL entry and POSTs
        ``/tasks/{task_id}/force-claim`` with reason ``crash_recovery`` so
        the task transitions back to *open* on the task server and can be
        claimed again by a fresh agent.  Any network / WAL failure is
        logged and swallowed -- the surrounding recovery loop must continue.

        Args:
            run_id: Run ID of the WAL file the orphan was found in.
            entry: The ``task_claimed`` WAL entry (committed=False) with no
                matching ``task_spawn_confirmed`` in the same run.

        Returns:
            True when the force-claim POST succeeded.
        """
        task_id = str(entry.inputs.get("task_id", ""))
        if not task_id:
            return False

        # WAL: record the retry intent (auditable, committed).
        try:
            self._wal_writer.write_entry(
                decision_type="task_retry",
                inputs={
                    "task_id": task_id,
                    "reason": "crash_recovery",
                    "original_run_id": run_id,
                    "original_seq": entry.seq,
                },
                output={"action": "force_claim_requested"},
                actor="orchestrator",
                committed=True,
            )
        except OSError:
            logger.debug("WAL write failed for task_retry (run=%s task=%s)", run_id, task_id)

        try:
            resp = self._client.post(
                f"{self._config.server_url}/tasks/{task_id}/force-claim",
                params={"reason": "crash_recovery"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "WAL recovery: force-claim failed for orphaned task %s (run=%s): %s",
                task_id,
                run_id,
                exc,
            )
            return False

        logger.info(
            "WAL recovery: force-claimed orphaned task %s (run=%s, original_seq=%d)",
            task_id,
            run_id,
            entry.seq,
        )
        return True

    def _find_orphaned_claim_confirmed(self, sdd_dir: Path) -> list[tuple[str, Any]]:
        """Return uncommitted ``claim_confirmed`` entries without a spawn confirm.

        A ``claim_confirmed`` entry is written AFTER the worktree is
        materialised but BEFORE ``task_spawn_confirmed``.  On a SIGKILL in
        that window the worktree exists on disk but no committed entry
        exists.  The returned tuples drive graveyard preservation + /fail
        so no work is silently reaped.

        WALs with a ``.closed`` sidecar marker are skipped so
        crashed spawns handled by a prior recovery are not retried forever.

        Args:
            sdd_dir: The ``.sdd`` directory root.

        Returns:
            List of ``(run_id, WALEntry)`` tuples with
            ``decision_type == "claim_confirmed"`` and ``committed=False``
            whose matching ``task_spawn_confirmed`` is missing from the
            same run's WAL.
        """
        wal_dir = sdd_dir / "runtime" / "wal"
        if not wal_dir.is_dir():
            return []

        crashed: list[tuple[str, Any]] = []
        for wal_file in sorted(wal_dir.glob("*.wal.jsonl")):
            run_id = wal_file.name.removesuffix(".wal.jsonl")
            if run_id == self._run_id:
                continue
            if WALRecovery.is_wal_closed(run_id, sdd_dir):
                continue
            reader = WALReader(run_id=run_id, sdd_dir=sdd_dir)
            try:
                entries = list(reader.iter_entries())
            except FileNotFoundError:
                continue
            confirmed_task_ids: set[str] = {
                str(e.inputs.get("task_id", ""))
                for e in entries
                if e.decision_type == "task_spawn_confirmed" and e.committed
            }
            for entry in entries:
                if entry.decision_type != "claim_confirmed" or entry.committed:
                    continue
                task_id = str(entry.inputs.get("task_id", ""))
                if not task_id or task_id in confirmed_task_ids:
                    continue
                crashed.append((run_id, entry))
        return crashed

    def _recover_crashed_spawn(self, run_id: str, entry: Any) -> bool:
        """Preserve a crashed-spawn worktree and /fail the task.

        Moves the materialised worktree (recorded in ``entry.inputs``) to
        ``.sdd/graveyard/<task_id>/<ts>/`` when it contains unsaved work
        (dirty status OR commits ahead of ``HEAD``) and POSTs
        ``/tasks/{task_id}/fail`` with reason
        ``spawned worktree missing after crash`` so the server transitions
        the task back to the *open* queue.  All failures are logged and
        swallowed -- the surrounding recovery loop must continue.

        A ``task_retry`` entry (``reason=crashed_spawn_recovery``) is
        appended to the current run's WAL for auditability.

        Args:
            run_id: Run ID of the WAL file the crashed spawn was found in.
            entry: The ``claim_confirmed`` WAL entry (committed=False) with
                no matching ``task_spawn_confirmed`` in the same run.

        Returns:
            True when the /fail POST succeeded.
        """
        import shutil
        import subprocess
        from pathlib import Path as _Path

        task_id = str(entry.inputs.get("task_id", ""))
        worktree_path_str = str(entry.inputs.get("worktree_path", ""))
        if not task_id:
            return False

        # Preserve worktree to graveyard if it still exists and has WIP.
        graveyard_dest: _Path | None = None
        if worktree_path_str:
            src = _Path(worktree_path_str)
            if src.is_dir():
                has_wip = False
                try:
                    status = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=src,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    has_wip = status.returncode == 0 and bool(status.stdout.strip())
                    if not has_wip:
                        # Also preserve if the branch has commits not in
                        # the parent (covers ``git commit`` without push).
                        rev = subprocess.run(
                            ["git", "log", "-1", "--format=%H"],
                            cwd=src,
                            capture_output=True,
                            text=True,
                            timeout=5,
                            check=False,
                        )
                        has_wip = rev.returncode == 0 and bool(rev.stdout.strip())
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.debug("git status failed for crashed worktree %s: %s", src, exc)
                    # Be conservative: still preserve if git call fails.
                    has_wip = True

                if has_wip:
                    graveyard_root = self._workdir / ".sdd" / "graveyard" / task_id
                    graveyard_dest = graveyard_root / str(int(time.time()))
                    try:
                        graveyard_dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(graveyard_dest))
                        logger.warning(
                            "WAL recovery: preserved crashed worktree %s -> %s (task=%s)",
                            src,
                            graveyard_dest,
                            task_id,
                        )
                    except OSError as exc:
                        logger.warning(
                            "WAL recovery: could not move crashed worktree %s to graveyard: %s",
                            src,
                            exc,
                        )
                        graveyard_dest = None

        # WAL: record the retry intent (auditable, committed).
        try:
            self._wal_writer.write_entry(
                decision_type="task_retry",
                inputs={
                    "task_id": task_id,
                    "reason": "crashed_spawn_recovery",
                    "original_run_id": run_id,
                    "original_seq": entry.seq,
                    "graveyard_path": str(graveyard_dest) if graveyard_dest else "",
                },
                output={"action": "fail_requested"},
                actor="orchestrator",
                committed=True,
            )
        except OSError:
            logger.debug("WAL write failed for crashed-spawn task_retry (run=%s task=%s)", run_id, task_id)

        # POST /tasks/{id}/fail so the task returns to the open queue with a
        # clear reason operators can review.
        try:
            resp = self._client.post(
                f"{self._config.server_url}/tasks/{task_id}/fail",
                json={"reason": "spawned worktree missing after crash"},
            )
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "WAL recovery: /fail failed for crashed-spawn task %s (run=%s): %s",
                task_id,
                run_id,
                exc,
            )
            return False

        logger.info(
            "WAL recovery: failed crashed-spawn task %s (run=%s, original_seq=%d)",
            task_id,
            run_id,
            entry.seq,
        )
        return True

    def _preserve_prior_worktrees_with_wip(self) -> list[Path]:
        """Move worktrees from prior runs with uncommitted work to preserved/.

        Scans ``.sdd/worktrees/`` for directories whose name is not in the
        current run's active sessions.  For any such directory with a
        non-empty ``git status --porcelain`` the directory is renamed into
        ``.sdd/worktrees/preserved/<session_id>-<timestamp>`` and a bulletin
        message is posted so a fresh agent can pick it up.  Worktrees with
        a clean status are left untouched -- the normal zombie cleanup will
        remove them.

        Errors are logged at debug level and swallowed; this runs on the
        startup hot-path and must never block orchestrator boot.

        Returns:
            List of preserved worktree paths (after the move).
        """
        import subprocess

        worktree_base = self._workdir / ".sdd" / "worktrees"
        if not worktree_base.is_dir():
            return []

        preserved_root = worktree_base / "preserved"
        active_session_ids = set(self._agents.keys())
        preserved: list[Path] = []

        for entry in worktree_base.iterdir():
            if not entry.is_dir():
                continue
            # Skip bookkeeping dirs (.locks) and the preserved root itself
            if entry.name.startswith(".") or entry.name == "preserved":
                continue
            if entry.name in active_session_ids:
                continue
            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=entry,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.debug("git status failed for %s: %s", entry, exc)
                continue
            if result.returncode != 0 or not result.stdout.strip():
                # Clean or non-git; let zombie cleanup handle it.
                continue

            try:
                preserved_root.mkdir(parents=True, exist_ok=True)
                dest = preserved_root / f"{entry.name}-{int(time.time())}"
                entry.rename(dest)
            except OSError as exc:
                logger.debug("Failed to preserve worktree %s: %s", entry, exc)
                continue

            preserved.append(dest)
            logger.warning(
                "WAL recovery: preserved worktree with uncommitted work at %s",
                dest,
            )
            self._post_bulletin(
                "alert",
                f"crash_recovery: preserved worktree with uncommitted changes at {dest} (resume or reconcile manually)",
            )

        return preserved

    def stop(self) -> None:
        """Delegate to orchestrator_cleanup.stop."""
        from bernstein.core.orchestration import orchestrator_cleanup

        orchestrator_cleanup.stop(self)

    def is_shutting_down(self) -> bool:
        """Delegate to orchestrator_cleanup.is_shutting_down."""
        from bernstein.core.orchestration import orchestrator_cleanup

        return orchestrator_cleanup.is_shutting_down(self)

    def _drain_before_cleanup(self, timeout_s: float | None = None) -> None:
        """Delegate to orchestrator_cleanup.drain_before_cleanup."""
        from bernstein.core.orchestration import orchestrator_cleanup

        orchestrator_cleanup.drain_before_cleanup(self, timeout_s=timeout_s)

    # -- Delegating methods (keep as methods for backward compat) -----------

    def _refresh_agent_states(self, tasks_snapshot: dict[str, list[Task]]) -> None:
        """Delegate to agent_lifecycle.refresh_agent_states."""
        refresh_agent_states(self, tasks_snapshot)

    def _claim_and_spawn_batches(
        self,
        batches: list[list[Task]],
        alive_count: int,
        assigned_task_ids: set[str],
        done_ids: set[str],
        result: TickResult,
    ) -> None:
        """Delegate to task_lifecycle.claim_and_spawn_batches."""
        claim_and_spawn_batches(self, batches, alive_count, assigned_task_ids, done_ids, result)

    def _process_completed_tasks(self, done_tasks: list[Task], result: TickResult) -> None:
        """Delegate to task_lifecycle.process_completed_tasks."""
        process_completed_tasks(self, done_tasks, result)

    def _maybe_retry_task(self, task: Task) -> bool:
        """Delegate to task_lifecycle.maybe_retry_task."""
        session = self._find_session_for_task(task.id)
        return maybe_retry_task(
            task,
            retried_task_ids=self._retried_task_ids,
            max_task_retries=self._config.max_task_retries,
            client=self._client,
            server_url=self._config.server_url,
            quarantine=self._quarantine,
            workdir=self._workdir,
            session_id=session.id if session is not None else None,
        )

    def _handle_anomaly_signal(self, signal: object) -> None:
        """Dispatch an anomaly signal: log, stop spawning, or kill agent."""
        import contextlib

        from bernstein.core import heartbeat as heartbeat_protocol
        from bernstein.core.cost_anomaly import AnomalySignal

        assert isinstance(signal, AnomalySignal)
        self._anomaly_detector.record_signal(signal)
        if signal.action == "kill_agent" and signal.agent_id:
            logger.warning("Anomaly [%s]: %s - killing agent", signal.rule, signal.message)
            session = self._agents.get(signal.agent_id)
            if session:
                with contextlib.suppress(Exception):
                    self._spawner.kill(session)
                # Every forced-kill path must reap the session's backgrounded
                # heartbeat shell loop (see heartbeat.py's
                # _reap_session_heartbeat_loop / Defect-10) so the loop does
                # not outlive the agent it was monitoring.
                with contextlib.suppress(Exception):
                    heartbeat_protocol._reap_session_heartbeat_loop(self, session, reason="anomaly_kill")
        elif signal.action == "stop_spawning":
            logger.warning("Anomaly [%s]: %s - stopping new spawns", signal.rule, signal.message)
            self._stop_spawning = True
        else:
            logger.info("Anomaly [%s]: %s", signal.rule, signal.message)

    def _run_liveness_watchdog(self) -> None:
        """Run one liveness-watchdog pass over current adapter sessions.

        Off by default; the watchdog short-circuits when
        ``BERNSTEIN_WATCHDOG_ENABLED`` is not set. Failures are logged
        and swallowed so a watchdog bug never breaks the tick loop.
        """
        from bernstein.core.orchestration.watchdog import (
            SessionSnapshot,
        )
        from bernstein.core.orchestration.watchdog import (
            is_enabled as _watchdog_enabled,
        )
        from bernstein.core.orchestration.watchdog import (
            tick as _watchdog_tick,
        )

        if not _watchdog_enabled():
            return

        snapshots: list[SessionSnapshot] = []
        for session in self._agents.values():
            if getattr(session, "status", "") == "dead":
                continue
            session_id = str(getattr(session, "id", ""))
            if not session_id:
                continue
            recent_output = str(getattr(session, "last_output_tail", "") or "")
            is_paused = bool(getattr(session, "awaiting_prompt", False))
            approved = frozenset(getattr(session, "approved_prompt_classes", frozenset()))
            snapshots.append(
                SessionSnapshot(
                    session_id=session_id,
                    recent_output=recent_output,
                    is_paused=is_paused,
                    approved_prompt_classes=approved,
                )
            )
        if not snapshots:
            return

        spawner = self._spawner

        def _respond(session_id: str, keystroke: str) -> bool:
            send = getattr(spawner, "send_keystroke", None)
            if send is None:
                return False
            try:
                return bool(send(session_id, keystroke))
            except Exception as exc:  # spawner failures must not break tick
                logger.warning("watchdog respond failed for %s: %s", session_id, exc)
                return False

        audit_path = self._workdir / ".sdd" / "audit" / "watchdog_recoveries.jsonl"
        try:
            result = _watchdog_tick(snapshots, _respond, audit_path)
        except Exception as exc:  # tick-level safety net
            logger.warning("liveness watchdog tick raised: %s", exc)
            return

        if result.recoveries:
            logger.info(
                "Liveness watchdog applied %d recovery action(s): %s",
                len(result.recoveries),
                ", ".join(r.session_id for r in result.recoveries),
            )
        if result.skipped_model_questions and self._notify is not None:
            for session_id in result.skipped_model_questions:
                with contextlib.suppress(Exception):
                    self._notify(
                        "approval.needed",
                        "Watchdog escalation",
                        f"Model question awaiting operator on session {session_id}",
                        severity="medium",
                        source="prompt_waiting",
                        session_id=session_id,
                    )

    def _record_live_costs(self) -> None:
        """Update live cost tracker from active agent token usage.

        Bug item-7 (2026-07-02, D2 claude FAIL-NOTE): ``session.tokens_used``
        is never populated on the openai_agents provider path, so this tick
        skipped every live agent and the run ledger's ``spent_usd`` stayed
        0.0 for the entire run - budget guards were unenforceable while real
        cost accumulated only in the ``.tokens`` sidecars. Fix: read each
        live session's cumulative sidecar totals (the same source of truth
        the orphan-recovery path prices dead agents from) and feed them to
        the delta-safe ``record_cumulative``. ``session.tokens_used`` remains
        the fallback for providers that don't write a sidecar.
        """
        from bernstein.core.cost.cost import read_tokens_sidecar_totals  # local: avoid import cycle

        any_change = False
        for session in self._agents.values():
            if session.status == "dead":
                continue

            # Same sidecar location contract as the openai_agents runner and
            # the orphan-recovery path: .sdd/runtime/<session_id>.tokens at
            # the orchestrator root (never inside a per-task worktree).
            sidecar_path = self._workdir / ".sdd" / "runtime" / f"{session.id}.tokens"
            sidecar_in, sidecar_out = read_tokens_sidecar_totals(sidecar_path)
            if sidecar_in > 0 or sidecar_out > 0:
                total_in, total_out = sidecar_in, sidecar_out
                token_source = str(sidecar_path)
            elif session.tokens_used > 0:
                total_in, total_out = session.tokens_used, 0
                token_source = "session.tokens_used"
            else:
                continue
            # "tokens" here are LLM usage counts, not credentials.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.debug(
                "live_cost_tick: session=%s tokens_in=%d tokens_out=%d source=%s",
                session.id,
                total_in,
                total_out,
                token_source,
            )

            model_name = session.model_config.model if session.model_config else "sonnet"
            task_id = session.task_ids[0] if session.task_ids else f"live-{session.id}"
            # Issue #1320: tag the call with the session's role so the
            # ledger can attribute spend by role without an extra lookup.
            role_label = getattr(session, "role", "") or ""
            delta_cost = self._cost_tracker.record_cumulative(
                agent_id=session.id,
                task_id=task_id,
                model=model_name,
                total_input_tokens=total_in,
                total_output_tokens=total_out,
                role=role_label,
            )
            if delta_cost > 0:
                any_change = True

            if (
                self._config.max_cost_per_agent > 0
                and session.id not in self._cost_cap_killed_agents
                and self._cost_tracker.spent_for_agent(session.id) >= self._config.max_cost_per_agent
            ):
                self._kill_agent_for_cost_cap(session)
                any_change = True

        if not any_change:
            return

        try:
            self._cost_tracker.save(self._workdir / ".sdd")
        except OSError as exc:
            logger.warning("Failed to persist live cost tracker: %s", exc)
        status = self._cost_tracker.status()
        self._post_bulletin(
            "status",
            f"live_cost_update: {status.spent_usd:.4f} USD spent ({status.percentage_used * 100:.1f}%)",
        )

    def _run_scheduled_dependency_scan(self) -> None:
        """Run the weekly dependency scan and enqueue remediation tasks."""
        try:
            existing_titles = self._load_existing_dependency_scan_task_titles()
            result = self._dependency_scanner.run_if_due(
                create_fix_task=lambda finding: self._create_dependency_fix_task(finding, existing_titles),
                audit_log=self._audit_log,
            )
        except Exception as exc:
            logger.warning("Dependency scan failed: %s", exc)
            return

        if result is None:
            return

        log_level = logging.WARNING if result.status == DependencyScanStatus.VULNERABLE else logging.INFO
        logger.log(
            log_level,
            "Dependency scan completed: %s (%d findings)",
            result.status.value,
            len(result.findings),
        )
        self._post_bulletin("status", f"dependency_scan: {result.summary}")

    def _load_existing_dependency_scan_task_titles(self) -> set[str]:
        """Load open remediation task titles so weekly scans do not duplicate them."""
        try:
            response = self._client.get(f"{self._config.server_url}/tasks")
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return set()

        if not isinstance(payload, list):
            return set()
        return {
            str(item.get("title", ""))
            for item in payload
            if isinstance(item, dict)
            and str(item.get("status", "")) in {"open", "claimed", "in_progress", "pending_approval"}
        }

    def _create_dependency_fix_task(
        self,
        finding: DependencyVulnerabilityFinding,
        existing_titles: set[str],
    ) -> str | None:
        """Create one remediation task per vulnerable package."""
        title = f"Upgrade vulnerable dependency: {finding.package}"
        if title in existing_titles:
            return None

        description = (
            f"{finding.source} reported {finding.package} {finding.installed_version} as vulnerable.\n\n"
            f"Advisory: {finding.advisory_id}\n"
            f"Summary: {finding.summary or 'No summary provided.'}"
        )
        if finding.fix_versions:
            description += f"\nRecommended fix versions: {', '.join(finding.fix_versions)}"

        try:
            response = self._client.post(
                f"{self._config.server_url}/tasks",
                json={
                    "title": title,
                    "description": description,
                    "role": "security",
                    "priority": 2,
                    "task_type": "fix",
                },
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to create dependency fix task for %s: %s", finding.package, exc)
            return None

        existing_titles.add(title)
        return title

    def _kill_agent_for_cost_cap(self, session: AgentSession) -> None:
        """Terminate an agent that exceeded the hard per-session cost cap."""
        cap = self._config.max_cost_per_agent
        spent = self._cost_tracker.spent_for_agent(session.id)
        self._cost_cap_killed_agents.add(session.id)
        logger.warning(
            "Killing agent %s: max_cost_per_agent exceeded ($%.4f >= $%.4f)",
            session.id,
            spent,
            cap,
        )
        self._post_bulletin(
            "alert",
            f"agent {session.id[:12]} exceeded max_cost_per_agent (${spent:.2f} >= ${cap:.2f})",
        )
        self._notify(
            "budget.warning",
            "Agent cost cap exceeded",
            f"Agent {session.id} exceeded max_cost_per_agent",
            agent_id=session.id,
            spent_usd=round(spent, 6),
            cap_usd=round(cap, 6),
        )

        with contextlib.suppress(Exception):
            self._spawner.kill(session)
        # Every forced-kill path must reap the session's backgrounded
        # heartbeat shell loop (see heartbeat.py's
        # _reap_session_heartbeat_loop / Defect-10) so the loop does not
        # outlive the agent it was monitoring.
        with contextlib.suppress(Exception):
            from bernstein.core import heartbeat as heartbeat_protocol

            heartbeat_protocol._reap_session_heartbeat_loop(self, session, reason="cost_cap_kill")

        from bernstein.core.lifecycle import transition_agent

        transition_agent(session, "dead", actor="orchestrator", reason="max_cost_per_agent exceeded")
        self._release_file_ownership(session.id)
        self._release_task_to_session(session.task_ids)
        self._record_provider_health(session, success=False)

        for task_id in list(session.task_ids):
            with contextlib.suppress(Exception):
                retry_or_fail_task(
                    task_id,
                    f"Agent {session.id} exceeded max_cost_per_agent (${cap:.2f})",
                    client=self._client,
                    server_url=self._config.server_url,
                    max_task_retries=self._config.max_task_retries,
                    retried_task_ids=self._retried_task_ids,
                    workdir=self._workdir,
                    role_model_policy=getattr(self._spawner, "role_model_policy", None),
                    default_adapter_name=getattr(self._spawner, "default_adapter_name", None),
                )

    def _enforce_budget_killswitch(self) -> None:
        """Terminate in-flight agents once the budget kill-switch has fired.

        prior to this hook the ABORT branch only blocked new
        spawns, so any agents still running when ``should_stop`` flipped
        kept consuming budget (overruns of 150%+ observed).  The new
        contract is:

        * On the first tick where ``should_stop`` is True we SHUTDOWN
          every live session so they can commit WIP, stamp
          ``_budget_stop_fired_at``, and emit a single ``budget.exhaust``
          bulletin + notification carrying the final spend.
        * On any later tick where at least ``kill_grace_period_s`` have
          elapsed we SIGKILL remaining live sessions via the spawner.
          Each agent is only killed once (``_budget_stop_killed_agents``).
        * When spend drops back under the threshold (e.g. after a hot
          reload of ``budget_usd``) the state is reset so the switch can
          re-arm.
        """
        status = self._cost_tracker.status()
        if not status.should_stop:
            # Budget was increased or the tracker reset - re-arm so a
            # future exhaustion triggers a fresh SHUTDOWN wave.
            if self._budget_stop_fired_at is not None:
                logger.info("Budget kill-switch re-armed (spend back under threshold)")
                self._budget_stop_fired_at = None
                self._budget_stop_killed_agents.clear()
            return

        live_sessions = [s for s in self._agents.values() if s.status != "dead"]

        # First transition: SHUTDOWN everyone and emit the exhaust event.
        if self._budget_stop_fired_at is None:
            self._budget_stop_fired_at = time.time()
            reason = (
                f"budget exhausted: ${status.spent_usd:.4f} of ${status.budget_usd:.2f} "
                f"({status.percentage_used * 100:.1f}%)"
            )
            logger.warning(
                "Budget kill-switch fired - sending SHUTDOWN to %d live agent(s); SIGKILL after %ds grace period",
                len(live_sessions),
                self._cost_tracker.kill_grace_period_s,
            )
            if live_sessions:
                with contextlib.suppress(Exception):
                    self._send_shutdown_signals(reason)
            self._post_bulletin(
                "alert",
                f"budget.exhaust: ${status.spent_usd:.4f}/${status.budget_usd:.2f} "
                f"({status.percentage_used * 100:.1f}%); "
                f"SHUTDOWN sent to {len(live_sessions)} agent(s)",
            )
            self._notify(
                "budget.exhaust",
                "Budget exhausted - terminating agents",
                (
                    f"Spent ${status.spent_usd:.4f} of ${status.budget_usd:.2f} "
                    f"({status.percentage_used * 100:.1f}%). SHUTDOWN sent to "
                    f"{len(live_sessions)} in-flight agent(s); SIGKILL after "
                    f"{self._cost_tracker.kill_grace_period_s}s grace."
                ),
                budget_usd=round(status.budget_usd, 4),
                spent_usd=round(status.spent_usd, 6),
                percent_used=round(status.percentage_used * 100, 2),
                live_agents=len(live_sessions),
                grace_period_s=int(self._cost_tracker.kill_grace_period_s),
            )
            return

        # Subsequent ticks: once the grace window has elapsed, SIGKILL
        # anything still alive so unbounded overrun is prevented.
        elapsed = time.time() - self._budget_stop_fired_at
        if elapsed < self._cost_tracker.kill_grace_period_s:
            return

        pending_kill = [s for s in live_sessions if s.id not in self._budget_stop_killed_agents]
        if not pending_kill:
            return

        logger.warning(
            "Budget kill-switch grace period expired (%.1fs elapsed); SIGKILLing %d agent(s) still alive",
            elapsed,
            len(pending_kill),
        )
        from bernstein.core import heartbeat as heartbeat_protocol

        for session in pending_kill:
            self._budget_stop_killed_agents.add(session.id)
            with contextlib.suppress(Exception):
                self._spawner.kill(session)
            # Every forced-kill path must reap the session's backgrounded
            # heartbeat shell loop (see heartbeat.py's
            # _reap_session_heartbeat_loop / Defect-10) so the loop does
            # not outlive the agent it was monitoring.
            with contextlib.suppress(Exception):
                heartbeat_protocol._reap_session_heartbeat_loop(self, session, reason="budget_killswitch")
        self._post_bulletin(
            "alert",
            f"budget.exhaust: SIGKILLed {len(pending_kill)} agent(s) after "
            f"{int(self._cost_tracker.kill_grace_period_s)}s grace",
        )

    def _reap_dead_agents(self, result: TickResult, tasks_snapshot: dict[str, list[Task]]) -> None:
        """Delegate to agent_lifecycle.reap_dead_agents."""
        reap_dead_agents(self, result, tasks_snapshot)

    def _check_stale_agents(self) -> None:
        """Delegate to agent_lifecycle.check_stale_agents."""
        check_stale_agents(self)

    def _check_kill_signals(self, result: TickResult) -> None:
        """Delegate to agent_lifecycle.check_kill_signals."""
        check_kill_signals(self, result)

    def _send_shutdown_signals(self, reason: str) -> None:
        """Delegate to agent_lifecycle.send_shutdown_signals."""
        send_shutdown_signals(self, reason)

    def _find_session_for_task(self, task_id: str) -> AgentSession | None:
        """Return the agent session that owns *task_id*, or None.

        Args:
            task_id: ID of the task to look up.

        Returns:
            Matching AgentSession, or None if not found.
        """
        agent_id = self._task_to_session.get(task_id)
        if agent_id is None:
            return None
        return self._agents.get(agent_id) or self._batch_sessions.get(agent_id)

    def _record_provider_health(
        self,
        session: AgentSession,
        success: bool,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> None:
        """Update provider health and cost in the router based on task outcome.

        No-op when no router is configured or the session has no provider.

        Args:
            session: Agent session whose provider to update.
            success: Whether the task completed successfully.
            latency_ms: Approximate task latency in milliseconds.
            cost_usd: Cost of the task in USD.
            tokens: Number of tokens used.
        """
        if self._router is not None and session.provider is not None:
            self._router.update_provider_health(session.provider, success, latency_ms)
            if cost_usd > 0 or tokens > 0:
                self._router.record_provider_cost(session.provider, tokens, cost_usd)

    def _release_file_ownership(self, agent_id: str) -> None:
        """Release all files owned by the given agent."""
        self._lock_manager.release(agent_id)
        # Always clean the legacy dict so code that reads _file_ownership directly stays consistent
        to_remove = [fp for fp, owner in self._file_ownership.items() if owner == agent_id]
        for fp in to_remove:
            del self._file_ownership[fp]

    def _release_task_to_session(self, task_ids: list[str]) -> None:
        """Remove reverse-index entries for the given task IDs."""
        for tid in task_ids:
            self._task_to_session.pop(tid, None)

    def _maybe_poll_ci_autofix(self) -> list[str]:
        """Poll GitHub Actions for failing runs if the feature flag is enabled.

        Calls :meth:`CIMonitor.poll` at most once per ``poll_interval_s`` seconds,
        tracked via ``_last_ci_poll_ts``.  Lazily constructs the monitor and
        pipeline on first use so the hot path is free when the flag is off.

        Returns:
            List of fix-task IDs created during this poll (may be empty).
            Always empty when the flag is disabled, when the repo is not
            configured, or when a GitHub token cannot be resolved.
        """
        ci_cfg = getattr(self._config, "ci_autofix", None)
        if ci_cfg is None or not ci_cfg.enabled:
            return []
        if not ci_cfg.repo:
            return []

        now = time.time()
        if now - self._last_ci_poll_ts < ci_cfg.poll_interval_s:
            return []
        self._last_ci_poll_ts = now

        token = ci_cfg.token or os.environ.get("GITHUB_TOKEN", "")
        if not token:
            logger.debug("CI autofix poll: GITHUB_TOKEN not set - skipping")
            return []

        if self._ci_monitor is None or self._ci_autofix_pipeline is None:
            from bernstein.core.quality.ci_fix import CIAutofixPipeline
            from bernstein.core.quality.ci_monitor import CIMonitor

            self._ci_monitor = CIMonitor()
            self._ci_autofix_pipeline = CIAutofixPipeline(
                server_url=self._config.server_url,
                repo_root=self._workdir,
            )

        try:
            return self._ci_monitor.poll(
                ci_cfg.repo,
                token,
                self._ci_autofix_pipeline,
                per_page=ci_cfg.per_page,
            )
        except Exception as exc:
            logger.warning("CI autofix poll failed: %s", exc)
            return []

    def _check_server_health(self) -> bool:
        """Ping the task server health endpoint with a short timeout.

        Updates ``_consecutive_server_failures`` and logs CRITICAL after 3
        consecutive failures so the external watchdog (or operator) knows
        the server needs attention.

        Returns:
            True if the server responded successfully.
        """
        try:
            resp = self._client.get(
                f"{self._config.server_url}/status",
                timeout=5.0,
            )
            resp.raise_for_status()
            self._consecutive_server_failures = 0
            return True
        except (httpx.HTTPError, httpx.TimeoutException):
            self._consecutive_server_failures += 1
            if self._consecutive_server_failures >= ORCHESTRATOR.server_failure_warn:
                logger.critical(
                    "Task server health check failed %d consecutive times - "
                    "server may have crashed (watchdog should restart it)",
                    self._consecutive_server_failures,
                )
            return False

    def _reconcile_claimed_tasks(self) -> int:
        """Unclaim orphaned tasks from previous orchestrator runs.

        On startup the ``_task_to_session`` map is empty, so any task that
        the server still considers "claimed" is orphaned.  For each such
        task we POST ``/tasks/{id}/force-claim`` which transitions it back
        to *open* so it can be picked up again.

        Returns:
            Number of tasks that were unclaimed.
        """
        try:
            resp = self._client.get(f"{self._config.server_url}/tasks?status=claimed")
            resp.raise_for_status()
            claimed = resp.json()
        except Exception:
            return 0

        unclaimed = 0
        for task in claimed if isinstance(claimed, list) else claimed.get("tasks", []):
            task_id = task.get("id", "")
            if task_id not in self._task_to_session:
                with contextlib.suppress(Exception):
                    self._client.post(
                        f"{self._config.server_url}/tasks/{task_id}/force-claim",
                    )
                    unclaimed += 1
                    logger.info(
                        "Unclaimed orphan task %s (%s)",
                        task_id,
                        task.get("title", ""),
                    )

        if unclaimed:
            logger.warning(
                "Reconciled %d orphaned claimed tasks from previous run",
                unclaimed,
            )
        return unclaimed

    def _release_stale_claims(self, claimed_tasks: list[Task]) -> int:
        """Fail claimed tasks that have been stuck longer than the timeout.

        When an agent dies silently (no crash signal, no heartbeat timeout),
        its claimed tasks stay in "claimed" forever.  This method detects
        tasks with no matching live agent that have exceeded the stale claim
        timeout and marks them failed so they can be retried.

        Args:
            claimed_tasks: Tasks with status "claimed" from the current tick.

        Returns:
            Number of tasks released.
        """
        now = time.time()
        timeout = self._config.stale_claim_timeout_s
        released = 0
        for task in claimed_tasks:
            # Skip tasks that have a known live agent in this session
            if task.id in self._task_to_session:
                agent_id = self._task_to_session[task.id]
                agent = self._agents.get(agent_id)
                if agent is not None and agent.status != "dead":
                    continue

            # Use claimed_at (when available) to measure actual time in claimed
            # state.  Fall back to created_at for legacy tasks that pre-date the
            # claimed_at field - this is conservative (over-counts) but safe.
            claim_epoch = task.claimed_at if task.claimed_at is not None else task.created_at
            age_s = now - claim_epoch
            if age_s < timeout:
                continue

            try:
                fail_task(
                    self._client,
                    self._config.server_url,
                    task.id,
                    reason=f"Stale claim: task stuck in claimed state for {age_s / 60:.0f}m with no live agent",
                )
                released += 1
                logger.warning(
                    "Released stale claimed task %s (%s) - stuck for %.0fm",
                    task.id,
                    task.title,
                    age_s / 60,
                )
            except Exception:
                logger.debug("Failed to release stale task %s", task.id, exc_info=True)

        if released:
            logger.warning("Released %d stale claimed task(s)", released)
        return released

    def _collect_completion_data(self, session: AgentSession) -> CompletionData:
        """Delegate to task_lifecycle.collect_completion_data."""
        return collect_completion_data(self._workdir, session)

    def _should_trigger_manager_review(self, failed_count: int) -> bool:
        """Return True when a manager queue review is warranted.

        Triggers on:
        - 3+ completions since last review
        - Any task failure
        - 5 minutes of no review (stall guard)

        Args:
            failed_count: Number of tasks failed since last review.

        Returns:
            True if the manager should review the queue.
        """
        now = time.time()
        if self._completions_since_review >= self._MANAGER_REVIEW_COMPLETION_THRESHOLD:
            return True
        if failed_count > 0:
            return True
        return self._last_review_ts > 0 and (now - self._last_review_ts) >= self._MANAGER_REVIEW_STALL_S

    def _run_manager_queue_review(self) -> None:
        """Invoke manager queue review and apply corrections.

        Fetches the task queue, calls the ManagerAgent to review it, and
        applies corrections (reassign, cancel, change_priority, add_task)
        via the task server.  All changes go through the server so the
        deterministic orchestrator remains in full control.
        """
        from bernstein import get_templates_dir
        from bernstein.core.orchestration.manager import ManagerAgent

        try:
            budget_pct = 1.0
            if self._cost_tracker.budget_usd > 0:
                status = self._cost_tracker.status()
                budget_pct = max(0.0, 1.0 - status.percentage_used)

            workdir = self._workdir
            _mgr_provider, _mgr_model = _resolve_manager_llm(workdir)
            manager = ManagerAgent(
                server_url=self._config.server_url,
                workdir=workdir,
                templates_dir=get_templates_dir(workdir),
                model=_mgr_model,
                provider=_mgr_provider,
            )

            result = manager.review_queue_sync(
                completed_count=self._completions_since_review,
                failed_count=self._failures_since_review,
                budget_remaining_pct=budget_pct,
            )

            self._last_review_ts = time.time()
            self._completions_since_review = 0
            self._failures_since_review = 0

            if result.skipped:
                return

            task_states = _fetch_task_states(self._client, self._config.server_url)
            _apply_manager_corrections(
                self._client,
                self._config.server_url,
                self._workdir,
                result.corrections,
                task_states,
            )

            if result.corrections:
                self._post_bulletin(
                    "status",
                    f"Manager review applied {len(result.corrections)} correction(s): {result.reasoning}",
                )

        except Exception as exc:
            logger.warning("Manager queue review failed: %s", exc)

    def _retry_or_fail_task(
        self,
        task_id: str,
        reason: str,
        tasks_snapshot: dict[str, list[Task]] | None = None,
    ) -> None:
        """Delegate to task_lifecycle.retry_or_fail_task."""
        retry_or_fail_task(
            task_id,
            reason,
            client=self._client,
            server_url=self._config.server_url,
            max_task_retries=self._config.max_task_retries,
            retried_task_ids=self._retried_task_ids,
            tasks_snapshot=tasks_snapshot,
            workdir=self._workdir,
            role_model_policy=getattr(self._spawner, "role_model_policy", None),
            default_adapter_name=getattr(self._spawner, "default_adapter_name", None),
        )

    def _check_file_overlap(self, batch: list[Task]) -> bool:
        """Return True if any file in *batch* is currently owned by an active agent.

        Checks both the in-memory ``_file_ownership`` dict (cross-referenced
        against live agent status) and the persistent ``_lock_manager`` (for
        crash-recovery locks held across process restarts).  Dead agents do not
        block new batches even if they appear in the ownership index.
        """
        all_files = [f for task in batch for f in task.owned_files]
        if not all_files:
            return False

        # In-memory ownership check - filters out dead agents explicitly.
        for fpath in all_files:
            owner_id = self._file_ownership.get(fpath)
            if owner_id:
                session = self._agents.get(owner_id)
                if session and session.status == "working":
                    logger.debug(
                        "File %s owned by active agent %s, deferring batch",
                        fpath,
                        owner_id,
                    )
                    return True

        # Persistent lock check (survives crashes via FileLockManager TTL).
        conflicts = self._lock_manager.check_conflicts(all_files)
        if conflicts:
            for fpath, lock in conflicts:
                logger.debug(
                    "File %s locked by agent %s (task %s), deferring batch",
                    fpath,
                    lock.agent_id,
                    lock.task_id,
                )
            return True
        return False

    def _should_auto_decompose(self, task: Task) -> bool:
        """Delegate to task_lifecycle.should_auto_decompose."""
        if not self._config.auto_decompose:
            return False
        return should_auto_decompose(
            task,
            self._decomposed_task_ids,
            force_parallel=self._config.force_parallel or self._config.auto_decompose,
        )

    def _auto_decompose_task(self, task: Task) -> None:
        """Delegate to task_lifecycle.auto_decompose_task."""
        auto_decompose_task(
            task,
            client=self._client,
            server_url=self._config.server_url,
            decomposed_task_ids=self._decomposed_task_ids,
            workdir=self._workdir,
        )

    # -- Session and cleanup ------------------------------------------------

    def _save_session_state(self) -> None:
        """Delegate to orchestrator_cleanup.save_session_state."""
        from bernstein.core.orchestration import orchestrator_cleanup

        orchestrator_cleanup.save_session_state(self)

    def _cleanup(self) -> None:
        """Delegate to orchestrator_cleanup.cleanup."""
        from bernstein.core.orchestration import orchestrator_cleanup

        orchestrator_cleanup.cleanup(self)

    def _restart(self) -> None:
        """Delegate to orchestrator_cleanup.restart."""
        from bernstein.core.orchestration import orchestrator_cleanup

        orchestrator_cleanup.restart(self)

    # -- Evolve mode ---------------------------------------------------------

    # Priority rotation for evolve mode -- each cycle emphasizes a different area
    _EVOLVE_FOCUS_AREAS: ClassVar[list[str]] = [
        "new_features",
        "user_interface",
        "test_coverage",
        "code_quality",
        "performance",
        "documentation",
    ]

    def _check_evolve(self, result: TickResult, tasks_by_status: dict[str, list[Task]]) -> None:
        """Delegate to orchestrator_evolve.check_evolve."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.check_evolve(self, result, tasks_by_status)

    _REPLENISH_COOLDOWN_S: float = 60.0
    _REPLENISH_MAX_TASKS: int = 5

    def _run_ruff_check(self) -> list[RuffViolation]:
        """Delegate to orchestrator_evolve.run_ruff_check."""
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.run_ruff_check(self)

    def _create_ruff_tasks(self, violations: list[RuffViolation]) -> None:
        """Delegate to orchestrator_evolve.create_ruff_tasks."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.create_ruff_tasks(self, violations)

    def _replenish_backlog(self, result: TickResult) -> None:
        """Delegate to orchestrator_evolve.replenish_backlog."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.replenish_backlog(self, result)

    def _run_pytest(self) -> TestResults:
        """Delegate to orchestrator_evolve.run_pytest."""
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.run_pytest(self)

    def _evolve_run_tests(self) -> TestResults:
        """Delegate to orchestrator_evolve.evolve_run_tests."""
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.evolve_run_tests(self)

    @staticmethod
    def _generate_evolve_commit_msg(staged_files: list[str]) -> str:
        """Delegate to orchestrator_evolve.generate_evolve_commit_msg."""
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.generate_evolve_commit_msg(staged_files)

    def _evolve_auto_commit(self) -> bool:
        """Delegate to orchestrator_evolve.evolve_auto_commit."""
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.evolve_auto_commit(self)

    def _evolve_spawn_manager(
        self,
        cycle_number: int = 0,
        focus_area: str = "new_features",
        test_summary: str = "",
    ) -> None:
        """Delegate to orchestrator_evolve.evolve_spawn_manager."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.evolve_spawn_manager(
            self,
            cycle_number=cycle_number,
            focus_area=focus_area,
            test_summary=test_summary,
        )

    def _log_evolve_cycle(
        self,
        cycle_number: int,
        timestamp: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Delegate to orchestrator_evolve.log_evolve_cycle."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.log_evolve_cycle(self, cycle_number, timestamp, metrics)

    # -- Evolution integration -----------------------------------------------

    def make_evolution_loop(self, **kwargs: Any) -> EvolutionLoop:
        """Delegate to orchestrator_evolve.make_evolution_loop.

        Returns:
            A fully-wired ``EvolutionLoop`` instance.
        """
        from bernstein.core.orchestration import orchestrator_evolve

        return orchestrator_evolve.make_evolution_loop(self, **kwargs)

    def _run_evolution_cycle(self, result: TickResult) -> None:
        """Delegate to orchestrator_evolve.run_evolution_cycle."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.run_evolution_cycle(self, result)

    def _persist_pending_proposals(self) -> None:
        """Delegate to orchestrator_evolve.persist_pending_proposals."""
        from bernstein.core.orchestration import orchestrator_evolve

        orchestrator_evolve.persist_pending_proposals(self)

    # -- Backlog -------------------------------------------------------------

    def _collect_backlog_files(self) -> list[Path]:
        """Collect and filter backlog files from open/ and issues/ directories."""
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        issues_dir = self._workdir / ".sdd" / "backlog" / "issues"

        backlog_files: list[Path] = []
        for src_dir in (open_dir, issues_dir):
            if src_dir.exists():
                backlog_files.extend(src_dir.glob("*.md"))
                backlog_files.extend(src_dir.glob("*.yaml"))
                backlog_files.extend(src_dir.glob("*.yml"))
        backlog_files.sort()

        task_filter = os.environ.get("BERNSTEIN_TASK_FILTER")
        if task_filter:
            task_filter_lower = task_filter.lower()
            backlog_files = [f for f in backlog_files if task_filter_lower in f.name.lower()]

        return backlog_files

    def _ensure_ingested_titles(self) -> set[str]:
        """Lazily initialize and return the set of already-ingested task titles."""
        if not hasattr(self, "_ingested_titles"):
            self._ingested_titles: set[str] = set()
            with contextlib.suppress(Exception):
                resp = self._client.get(f"{self._config.server_url}/tasks")
                resp.raise_for_status()
                for task in resp.json():
                    title = task.get("title", "")
                    if title:
                        self._ingested_titles.add(title.lower().strip())
        return self._ingested_titles

    def ingest_backlog(self) -> int:
        """Scan .sdd/backlog/open/ and .sdd/backlog/issues/ for new task files.

        Both directories are scanned so that GitHub-synced P0/P1 tickets
        (in ``issues/``) are ingested alongside internal backlog (``open/``).
        Candidates are sorted by priority so P0 tasks are ingested first.

        - ``open/`` files are **moved** to ``claimed/`` after ingestion.
        - ``issues/`` files stay in place; a marker is created in ``claimed/``
          to prevent re-ingestion.

        Returns:
            Number of files ingested this call.
        """
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        claimed_dir = self._workdir / ".sdd" / "backlog" / "claimed"

        backlog_files = self._collect_backlog_files()
        if not backlog_files:
            return 0

        # Rate-limit ingestion: max 50 files per tick to prevent server overload.
        _MAX_INGEST_PER_TICK = 50

        existing_titles = self._ensure_ingested_titles()

        claimed_dir.mkdir(parents=True, exist_ok=True)

        from bernstein.core.backlog_parser import parse_backlog_text

        # Phase 1: Parse all candidates, filter dupes, sort by priority
        candidates: list[tuple[Path, ParsedBacklogTask]] = []
        for backlog_file in backlog_files:
            if (claimed_dir / backlog_file.name).exists():
                continue

            content = backlog_file.read_text(encoding="utf-8")
            parsed_task = parse_backlog_text(backlog_file.name, content)
            if parsed_task is None:
                logger.warning("ingest_backlog: could not parse %s - skipping", backlog_file.name)
                self._claim_backlog_file(backlog_file, open_dir, claimed_dir)
                continue

            title_key = parsed_task.title.lower().strip()
            if title_key in existing_titles:
                self._claim_backlog_file(backlog_file, open_dir, claimed_dir)
                continue

            candidates.append((backlog_file, parsed_task))

        # Sort by priority (lower = more critical) so P0 tasks are ingested first
        candidates.sort(key=lambda t: t[1].priority)
        batch_files = candidates[:_MAX_INGEST_PER_TICK]

        if not batch_files:
            return 0

        # Phase 2: POST batch - single HTTP call for all collected tasks
        payloads = [parsed.to_task_payload() for _, parsed in batch_files]
        try:
            resp = self._client.post(
                f"{self._config.server_url}/tasks/batch",
                json={"tasks": payloads},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 422):
                # 404: server doesn't support batch yet (older build).
                # 422: one task in the batch fails pydantic validation - a
                # single oversized title would otherwise poison every batch
                # for the whole run.  Fall back to one-by-one so valid tasks
                # still land and only the bad task keeps retrying.
                return self._ingest_backlog_one_by_one(batch_files, open_dir, claimed_dir)
            logger.warning("ingest_backlog: batch POST failed: %s", exc)
            return 0  # Move NONE on failure
        except httpx.HTTPError as exc:
            logger.warning("ingest_backlog: batch POST failed: %s", exc)
            return 0

        # Phase 3: Mark files as claimed - only on success
        count = 0
        for backlog_file, parsed in batch_files:
            title_key = parsed.title.lower().strip()
            existing_titles.add(title_key)
            self._claim_backlog_file(backlog_file, open_dir, claimed_dir)
            count += 1
            logger.info("Ingested backlog file: %s (from %s/)", backlog_file.name, backlog_file.parent.name)

        return count

    def _claim_backlog_file(self, backlog_file: Path, open_dir: Path, claimed_dir: Path) -> None:
        """Mark a backlog file as claimed.

        Files from ``open/`` are moved into ``claimed/``.
        Files from ``issues/`` stay in place - only a marker is created in
        ``claimed/`` so they are not re-ingested.
        """
        with contextlib.suppress(OSError):
            if backlog_file.parent == open_dir:
                backlog_file.rename(claimed_dir / backlog_file.name)
            else:
                (claimed_dir / backlog_file.name).touch()

    def _ingest_backlog_one_by_one(
        self,
        batch_files: list[tuple[Path, ParsedBacklogTask]],
        open_dir: Path,
        claimed_dir: Path,
    ) -> int:
        """Fallback: ingest files one-by-one when server lacks batch endpoint."""
        count = 0
        for backlog_file, parsed in batch_files:
            payload = parsed.to_task_payload()
            try:
                resp = self._client.post(
                    f"{self._config.server_url}/tasks",
                    json=payload,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "ingest_backlog: POST failed for %s: %s",
                    backlog_file.name,
                    exc,
                )
                continue  # Skip this file, try next

            self._ingested_titles.add(parsed.title.lower().strip())
            self._claim_backlog_file(backlog_file, open_dir, claimed_dir)
            count += 1
            logger.info("Ingested backlog file (one-by-one): %s", backlog_file.name)
        return count

    # -- Run summary --------------------------------------------------------

    def _refresh_drain_tracker(self, refreshed_tasks_by_status: dict[str, list[Task]]) -> int:
        """Update the post-/complete drain counter against freshly-fetched tasks.

        Defect 29 (2026-07-02, work/bernstein/proofs/d2/minimax/attempt-83808a8a):
        the run-summary used to fire the moment ``open_tasks == active_agents == 0``
        held, with no check that the post-/complete chain (task_done ->
        ``_reap_and_cleanup_session`` -> ``_apply_janitor_verdict_action``)
        had actually run for the just-completed tasks. On MiniMax-M2.7-highspeed
        ~40 s child runtimes that race always lost - janitor never fired,
        ``generate_retrospective`` was skipped because the chain never registered
        the completion in ``_processed_done_tasks``, and no merge-to-main
        happened before the orchestrator self-stopped.

        Definition of "drained" for a single task:

        * Its id is in ``self._processed_done_tasks`` (the orchestrator's
          ``process_completed_tasks`` step has at least started the chain), AND
        * Its session is either absent (``_find_session_for_task`` returns
          ``None`` -- the agent never had a worktree, or it was cleaned up)
          or its session.status == ``"dead"`` (the reap step inside
          ``_reap_and_cleanup_session`` transitioned it).

        The function returns the current pending count after the refresh.

        Args:
            refreshed_tasks_by_status: Latest task buckets from the refetch
                inside the step 8b block.

        Returns:
            The number of tasks whose post-/complete chain has NOT yet drained.
        """
        terminal_ids: set[str] = set()
        for bucket in ("done", "failed"):
            terminal_ids.update(t.id for t in refreshed_tasks_by_status.get(bucket, []))

        # Increment for any newly-observed terminal task.
        for tid in terminal_ids:
            if tid not in self._post_complete_seen_task_ids:
                self._pending_post_complete_count += 1
                self._post_complete_seen_task_ids.add(tid)

        # Decrement for tasks whose chain has fully drained. We iterate over
        # a snapshot because we mutate the set below.
        for tid in list(self._post_complete_seen_task_ids):
            if tid not in self._processed_done_tasks:
                # process_completed_tasks hasn't started the chain for this
                # task yet (e.g. auto-complete-by-reap happened in this tick
                # AFTER process_completed_tasks ran). Leave the counter alone.
                continue
            session = self._find_session_for_task(tid)
            if session is None or session.status == "dead":
                self._pending_post_complete_count -= 1
                self._post_complete_seen_task_ids.discard(tid)

        return self._pending_post_complete_count

    def _generate_run_summary(
        self,
        done_tasks: list[Task],
        failed_tasks: list[Task],
    ) -> None:
        """Write a run completion summary to .sdd/runtime/summary.md."""
        runtime_dir = self._workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        summary_path = runtime_dir / "summary.md"

        total_completed = len(done_tasks)
        total_failed = len(failed_tasks)
        wall_clock_s = time.time() - self._run_start_ts

        collector = get_collector(self._workdir / ".sdd" / "metrics")
        total_cost = collector.get_total_cost()
        files_modified: int = sum(getattr(m, "files_modified", 0) for m in collector.task_metrics.values())

        # Defect 29 firing log: every input that the run-end decision was
        # made against, so a 2-minute diagnosis from logs alone is possible.
        logger.info(
            "run_summary_firing: tick=#%d tasks_completed=%d tasks_failed=%d "
            "files_modified=%d cost_usd=%.4f wall_clock_s=%.1f "
            "pending_post_complete=%d processed_done_tasks=%d -> action=fire_summary",
            self._tick_count,
            total_completed,
            total_failed,
            files_modified,
            total_cost,
            wall_clock_s,
            self._pending_post_complete_count,
            len(self._processed_done_tasks),
        )

        task_lines: list[str] = [f"- [x] {task.title}" for task in sorted(done_tasks, key=lambda t: t.title)]
        for task in sorted(failed_tasks, key=lambda t: t.title):
            task_lines.append(f"- [ ] {task.title} *(failed)*")

        hours, rem = divmod(int(wall_clock_s), 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            duration_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes:
            duration_str = f"{minutes}m {seconds}s"
        else:
            duration_str = f"{seconds}s"

        # Cross-task KB counters are best-effort: the import is lazy so a
        # broken module never blocks summary generation.
        kb_publish = 0
        kb_subscribe = 0
        try:
            from bernstein.core.memory.cross_task_kb import get_global_counter

            kb_publish, kb_subscribe = get_global_counter().snapshot()
        except Exception as exc:  # pragma: no cover - summary must never break
            logger.debug("cross_task_kb counter snapshot failed: %s", exc)

        lines = [
            "# Run Summary",
            "",
            f"**Total completed:** {total_completed}",
            f"**Total failed:** {total_failed}",
            f"**Files modified:** {files_modified}",
            f"**Estimated cost:** ${total_cost:.4f}",
            f"**Wall-clock duration:** {duration_str}",
            f"**kb.publish:** {kb_publish}",
            f"**kb.subscribe:** {kb_subscribe}",
            "",
            "## Tasks",
            "",
        ]
        lines.extend(task_lines)
        lines.append("")

        summary_path.write_text("\n".join(lines))
        self._summary_written = True
        logger.info("Run complete. Summary at .sdd/runtime/summary.md")

        self._post_bulletin(
            "status",
            f"run complete: {total_completed} tasks done, {total_failed} failed, "
            f"${total_cost:.4f} spent, {duration_str} elapsed",
        )
        self._notify(
            _EVENT_RUN_COMPLETED,
            "Bernstein run complete",
            f"{total_completed} tasks done, {total_failed} failed in {duration_str}.",
            tasks_completed=total_completed,
            tasks_failed=total_failed,
            files_modified=files_modified,
            cost_usd=round(total_cost, 4),
            duration=duration_str,
        )

        # NOTE: _generate_run_summary() is invoked from the tick-level "queue
        # looks empty" heuristic (see 8b in tick()), not true orchestrator
        # shutdown - child tasks can still fail afterwards (janitor rejection,
        # etc.). Mark this retrospective INTERIM; run() re-generates the
        # FINAL retrospective, unconditionally overwriting this one, once the
        # tick loop actually exits (see A5 stale-retrospective bug).
        generate_retrospective(
            done_tasks=done_tasks,
            failed_tasks=failed_tasks,
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=self._run_start_ts,
            trigger_reason="mid-run",
        )

        # Auto-PR: create a GitHub PR if BERNSTEIN_AUTO_PR is set
        if os.environ.get("BERNSTEIN_AUTO_PR") == "1":
            self._create_auto_pr(done_tasks, total_cost, duration_str)

        self._emit_summary_card(
            done_tasks=done_tasks,
            failed_tasks=failed_tasks,
            collector=collector,
            wall_clock_s=wall_clock_s,
            total_cost=total_cost,
        )

    def _get_pr_diff_stats(self, branch: str) -> dict[str, int]:
        """Get diff statistics for PR body."""
        import subprocess

        stats = {"files": 0, "insertions": 0, "deletions": 0}
        with contextlib.suppress(Exception):
            result = subprocess.run(
                ["git", "diff", "--shortstat", f"origin/main...{branch}"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                text = result.stdout.strip()
                if m := re.search(r"(\d+) files? changed", text[:500]):
                    stats["files"] = int(m.group(1))
                if m := re.search(r"(\d+) insertions?", text[:500]):
                    stats["insertions"] = int(m.group(1))
                if m := re.search(r"(\d+) deletions?", text[:500]):
                    stats["deletions"] = int(m.group(1))
        return stats

    def _create_auto_pr(
        self,
        done_tasks: list[Task],
        _total_cost: float,
        _duration_str: str,
    ) -> None:
        """Create a GitHub PR with the completed work.

        Called when BERNSTEIN_AUTO_PR=1 is set and all tasks complete.
        """
        from bernstein.core.git.git_pr import create_github_pr

        current_branch = self._get_current_branch()
        if current_branch is None:
            return

        if current_branch in ("main", "master"):
            logger.info("Auto-PR: skipping - already on %s branch", current_branch)
            return

        if not self._has_commits_ahead(current_branch):
            return

        if not self._push_branch(current_branch):
            return

        existing_url = self._check_existing_pr(current_branch)
        if existing_url:
            self._notify(
                "pr.exists",
                "Pull request already exists",
                f"PR for branch {current_branch}: {existing_url}",
                pr_url=existing_url,
            )
            return

        pr_title = done_tasks[0].title if len(done_tasks) == 1 else f"Bernstein: {len(done_tasks)} tasks completed"
        body = self._build_pr_body(done_tasks, current_branch)

        pr_result = create_github_pr(
            cwd=self._workdir,
            title=pr_title,
            body=body,
            head=current_branch,
            base="main",
        )

        if pr_result.success:
            logger.info("Auto-PR created: %s", pr_result.pr_url)
            self._notify(
                "pr.created",
                "Pull request created",
                f"PR for {len(done_tasks)} task(s): {pr_result.pr_url}",
                pr_url=pr_result.pr_url,
            )
        else:
            logger.warning("Auto-PR failed: %s", pr_result.error)

    def _get_current_branch(self) -> str | None:
        """Get the current git branch name, or None on failure."""
        import subprocess

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            return result.stdout.strip()
        except Exception as exc:
            logger.warning("Auto-PR: failed to get current branch: %s", exc)
            return None

    def _has_commits_ahead(self, branch: str) -> bool:
        """Check if the branch has commits ahead of origin/main."""
        import subprocess

        with contextlib.suppress(Exception):
            result = subprocess.run(
                ["git", "log", "origin/main..HEAD", "--oneline"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            if not result.stdout.strip():
                logger.info("Auto-PR: skipping - no commits ahead of main")
                return False
        return True

    def _push_branch(self, branch: str) -> bool:
        """Push the branch to origin. Returns True on success."""
        import subprocess

        try:
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            return True
        except Exception as exc:
            logger.warning("Auto-PR: failed to push branch: %s", exc)
            return False

    def _check_existing_pr(self, branch: str) -> str | None:
        """Check if a PR already exists for this branch. Returns URL or None."""
        import subprocess

        with contextlib.suppress(Exception):
            pr_check = subprocess.run(
                ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
                cwd=self._workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if pr_check.returncode == 0 and pr_check.stdout.strip():
                url = pr_check.stdout.strip()
                logger.info("Auto-PR: PR already exists for branch %s: %s", branch, url)
                return url
        return None

    def _build_pr_body(self, done_tasks: list[Task], branch: str) -> str:
        """Build the PR body text."""
        diff_stats = self._get_pr_diff_stats(branch)
        body_lines = ["## Summary", ""]

        if len(done_tasks) == 1:
            body_lines.append(done_tasks[0].description or done_tasks[0].title)
        else:
            body_lines.extend((f"Completed {len(done_tasks)} tasks:", ""))
            for task in done_tasks:
                body_lines.append(f"- {task.title}")
        body_lines.append("")

        if diff_stats["files"] > 0:
            body_lines.extend(
                [
                    "## Changes",
                    "",
                    f"**{diff_stats['files']}** files changed, "
                    f"**+{diff_stats['insertions']}** insertions, "
                    f"**-{diff_stats['deletions']}** deletions",
                    "",
                ]
            )

        evidence = self._evidence_projection_for_tasks(done_tasks)
        if evidence:
            body_lines.extend(["## Evidence", "", evidence, ""])

        body_lines.extend(["---", "*Generated by Bernstein*"])
        return "\n".join(body_lines)

    def _evidence_projection_for_tasks(self, done_tasks: list[Task]) -> str:
        """Project the first completed task's sealed evidence bundle (AC3).

        Reads the sealed verification-evidence bundle off disk for each done task
        in turn and returns the tracker-comment projection (gate verdict, anchor
        prefix, offline verify command) for the first one present -- the same
        pointer the canonical ``bernstein pr`` path emits.

        Fail-open (issue #2362): resolving or projecting a bundle must never
        block or fail auto-PR creation, so any error is caught, sanitised-logged,
        and swallowed, and a task with no sealed bundle yields an empty string --
        the body is then exactly as before.
        """
        try:
            from bernstein.core.evidence.bundle import read_evidence_bundle
            from bernstein.github_app.evidence_projection import build_evidence_projection

            for task in done_tasks:
                bundle = read_evidence_bundle(self._workdir, task.id)
                if bundle is not None:
                    return build_evidence_projection(bundle)
        except Exception as exc:  # fail-open: evidence must never block auto-PR
            logger.warning(
                "Auto-PR: evidence projection failed; PR body unchanged: %s",
                sanitize_log(str(exc)),
            )
        return ""

    def _collect_isolation_downgrades(self) -> list[dict[str, str]]:
        """Return requested-vs-actual isolation downgrades from the spawner.

        Issue #3014: when a container isolation request cannot be honoured, the
        spawn falls back to a weaker boundary and records the downgrade.
        Draining it into the run summary makes the downgrade visible in the run
        outcome.
        Guarded so an older or stubbed spawner without the attribute never
        breaks summary emission.
        """
        downgrades = getattr(self._spawner, "isolation_downgrades", None)
        if not isinstance(downgrades, list):
            return []
        return [d.as_dict() for d in downgrades]

    def _emit_summary_card(
        self,
        done_tasks: list[Task],
        failed_tasks: list[Task],
        collector: Any,
        wall_clock_s: float,
        total_cost: float,
    ) -> None:
        """Print the end-of-run summary card and write summary.json.

        Suppressed when the ``BERNSTEIN_QUIET`` environment variable is set.

        Args:
            done_tasks: Completed tasks.
            failed_tasks: Failed tasks.
            collector: Live MetricsCollector for quality metrics.
            wall_clock_s: Wall-clock duration in seconds.
            total_cost: Total cost in USD.
        """
        from bernstein.cli.summary_card import RunSummaryData, print_summary_card, write_summary_json

        total = len(done_tasks) + len(failed_tasks)

        # Quality score: fraction of completed tasks where janitor verification passed.
        task_metrics = collector._task_metrics  # type: ignore[reportPrivateUsage]
        verified = [m for m in task_metrics.values() if m.end_time is not None]
        quality_score: float | None = None
        if verified:
            quality_score = sum(1 for m in verified if m.janitor_passed) / len(verified)

        summary_data = RunSummaryData(
            run_id=self._run_id,
            tasks_completed=len(done_tasks),
            tasks_total=total,
            tasks_failed=len(failed_tasks),
            wall_clock_seconds=wall_clock_s,
            total_cost_usd=total_cost,
            quality_score=quality_score,
            isolation_downgrades=self._collect_isolation_downgrades(),
        )

        sdd_dir = self._workdir / ".sdd"
        try:
            write_summary_json(summary_data, self._run_id, sdd_dir)
        except OSError as exc:
            logger.warning("Failed to write summary.json: %s", exc)

        quiet = os.environ.get("BERNSTEIN_QUIET", "").strip() == "1"
        if not quiet:
            try:
                print_summary_card(summary_data)
            except Exception as exc:
                logger.debug("Summary card render failed (non-critical): %s", exc)

    def _record_spawned_events(self, result: TickResult) -> None:
        """Record spawn and claim events for new agents."""
        for session_id in result.spawned:
            session = self._agents.get(session_id)
            if session is None:
                continue
            self._record_mutation_capability_once(session)
            self._recorder.record(
                "agent_spawned",
                agent_id=session.id,
                role=session.role,
                model=session.model_config.model if session.model_config else None,
                provider=session.provider,
                task_ids=session.task_ids,
                agent_source=session.agent_source,
            )
            for tid in session.task_ids:
                self._recorder.record(
                    "task_claimed",
                    task_id=tid,
                    agent_id=session.id,
                    model=session.model_config.model if session.model_config else None,
                )

    def _record_tick_events(self, result: TickResult, _tasks_by_status: dict[str, list[Task]]) -> None:
        """Record replay events from a completed tick for deterministic replay."""
        self._record_spawned_events(result)

        for task_id in result.verified:
            session = self._find_session_for_task(task_id)
            cost = self._cost_tracker.status().spent_usd if session is not None else 0.0
            self._recorder.record(
                "task_completed",
                task_id=task_id,
                agent_id=session.id if session else None,
                cost_usd=round(cost, 4),
            )

        for task_id, failed_signals in result.verification_failures:
            self._recorder.record("task_verification_failed", task_id=task_id, failed_signals=failed_signals)

        for agent_id in result.reaped:
            self._record_provider_mutations_for(agent_id)
            self._recorder.record("agent_reaped", agent_id=agent_id)

        for task_id in result.retried:
            self._recorder.record("task_retried", task_id=task_id)

    def _record_mutation_capability_once(self, session: AgentSession) -> None:
        """Record a provider's mutation-observability capability once per run.

        Issue #2507: the adapter contract declares whether provider-side
        context mutations are observable at all. Recording the declaration
        into the journal keeps an absence of mutation entries
        distinguishable from an inability to see them.

        Issue #2646: the capability is resolved and recorded *before* the
        dedup key is set, and the key is the resolved adapter name (not the
        raw provider label). A failed record is retried on the next spawn
        instead of being marked done, and provider-less sessions that resolve
        to different adapters no longer collapse to a single key. Failures are
        logged and swallowed: capability recording must never break a spawn
        tick.
        """
        try:
            from bernstein.core.replay.provider_state import (
                capability_for_provider,
                record_mutation_capability,
            )

            model = session.model_config.model if session.model_config else ""
            adapter_name, capability = capability_for_provider(session.provider, model)
            if adapter_name in self._mutation_capability_recorded:
                return
            record_mutation_capability(self._recorder, adapter=adapter_name, capability=capability)
            self._mutation_capability_recorded.add(adapter_name)
        except Exception as exc:
            logger.warning("provider-state: capability recording failed: %s", type(exc).__name__)

    def _record_provider_mutations_for(self, agent_id: str) -> None:
        """Chain a reaped agent's observed provider-side mutations (issue #2507).

        Reads the mutation signals the adapter observed for the session and
        chains each one into the run journal as a content-addressed
        ``provider_state_mutation`` entry, before the ``agent_reaped`` event,
        so nothing downstream builds on unrecorded state. In deterministic
        modes the entries are flagged and replay verification fails closed.
        Failures are logged and swallowed: recording must never break a tick.
        """
        session = self._agents.get(agent_id)
        if session is None:
            return
        try:
            from bernstein.adapters.registry import adapter_name_for_provider, get_adapter
            from bernstein.core.replay.provider_state import record_agent_mutations

            model = session.model_config.model if session.model_config else ""
            adapter_name = adapter_name_for_provider(session.provider, model)
            if not adapter_name:
                return
            adapter = get_adapter(adapter_name)
            signals = adapter.observed_provider_mutations(self._workdir, session.id)
            if not signals:
                return
            record_agent_mutations(
                self._recorder,
                signals,
                agent_id=agent_id,
                audit_chain=self._get_provider_audit_chain(),
            )
        except Exception as exc:
            logger.warning(
                "provider-state: mutation recording failed for agent %s: %s",
                agent_id,
                type(exc).__name__,
            )
            # Fail closed under deterministic/replay mode: a dropped capture
            # (I/O or parse error) must not read as an absence of mutations
            # (#2646). The remediation is fully guarded in the helper.
            self._record_capture_failure(agent_id, reason=type(exc).__name__)

    def _record_capture_failure(self, agent_id: str, *, reason: str) -> None:
        """Chain a fail-closed marker when provider-mutation capture failed.

        Issue #2646: in deterministic/replay mode a dropped capture must make
        replay verification fail closed rather than pass as an absence of
        mutations. Outside deterministic mode there is no replay to verify, so
        the marker is skipped. The marker is a load-bearing journal entry; if
        even the marker cannot be recorded the failure is logged (never
        raised) so a reap tick is never broken.
        """
        try:
            from bernstein.core.replay.provider_state import (
                deterministic_mode_active,
                record_capture_failure,
            )

            if not deterministic_mode_active():
                return
            record_capture_failure(self._recorder, agent_id=agent_id, reason=reason)
        except Exception as exc:
            logger.warning(
                "provider-state: capture-failure marker not recorded for agent %s: %s",
                agent_id,
                type(exc).__name__,
            )

    def _get_provider_audit_chain(self) -> Any | None:
        """Return the run's audit chain for mirroring provider-state mutations.

        Issue #2646: provider mutations chained into the replay journal are
        additionally mirrored into the HMAC audit chain so a verifier can
        confirm each entry is chain-attested. Built once and cached; returns
        ``None`` when the chain cannot be constructed (missing key, IO error).
        Mirroring is best-effort -- the journal entry is the load-bearing
        record and a chain failure must never block it. Only the exception
        type is logged: this path touches the audit HMAC key.
        """
        if self._provider_audit_chain is not None:
            return self._provider_audit_chain
        try:
            from bernstein.core.security.audit import load_or_create_audit_key
            from bernstein.core.security.audit_chain import AuditChainStore

            hmac_key = load_or_create_audit_key()
            self._provider_audit_chain = AuditChainStore(self._workdir / ".sdd" / "audit", key=hmac_key)
            return self._provider_audit_chain
        except Exception as exc:
            logger.warning("provider-state: audit chain unavailable: %s", type(exc).__name__)
            return None

    def _log_summary(self, result: TickResult) -> None:
        """Write a one-line summary and agent state snapshot each tick."""
        log_dir = self._workdir / ".sdd" / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "orchestrator.log"

        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        alive = sum(1 for a in self._agents.values() if a.status != "dead")
        fp = self._fast_path_stats
        fp_tag = f" fast_path={fp.tasks_bypassed} saved=${fp.estimated_cost_saved_usd:.2f}" if fp.tasks_bypassed else ""
        line = (
            f"[{ts}] open={result.open_tasks} agents={alive} "
            f"spawned={len(result.spawned)} reaped={len(result.reaped)} "
            f"verified={len(result.verified)} errors={len(result.errors)}{fp_tag}\n"
        )
        rotate_log_file(log_path)
        with log_path.open("a") as f:
            f.write(line)

        # Dump agent state for the live dashboard
        agents_snapshot = [
            {
                "id": s.id,
                "role": s.role,
                "status": s.status,
                "exit_code": s.exit_code,
                "model": s.model_config.model if s.model_config else None,
                "task_ids": s.task_ids,
                "pid": s.pid,
                "spawn_ts": s.spawn_ts,
                "runtime_s": round(time.time() - s.spawn_ts) if s.spawn_ts > 0 else 0,
                "agent_source": s.agent_source,
                "provider": s.provider,
                "cell_id": s.cell_id,
                "parent_id": s.parent_id,
                "log_path": str(getattr(s, "log_path", "")),
                "worktree_path": str(getattr(s, "worktree_path", "")),
                "tokens_used": s.tokens_used,
                "token_budget": s.token_budget,
                "context_window_tokens": s.context_window_tokens,
                "context_utilization_pct": s.context_utilization_pct,
                "context_utilization_alert": s.context_utilization_alert,
                "runtime_backend": s.runtime_backend,
                "bridge_session_key": s.bridge_session_key,
                "bridge_run_id": s.bridge_run_id,
                "transition_reason": s.transition_reason.value if s.transition_reason is not None else "",
                "abort_reason": s.abort_reason.value if s.abort_reason is not None else "",
                "abort_detail": s.abort_detail,
                "finish_reason": s.finish_reason,
            }
            for s in self._agents.values()
        ]
        state_path = log_dir / "agents.json"
        with contextlib.suppress(Exception), state_path.open("w") as f:
            json.dump({"ts": time.time(), "agents": agents_snapshot}, f)


class TickResult:
    """Summary of one orchestrator tick.

    Pure data container -- no logic, no side effects.
    """

    def __init__(self) -> None:
        self.open_tasks: int = 0
        self.active_agents: int = 0
        self.spawned: list[str] = []
        self.reaped: list[str] = []
        self.verified: list[str] = []
        self.verification_failures: list[tuple[str, list[str]]] = []
        self.retried: list[str] = []
        self.errors: list[str] = []
        # Populated when dry_run=True: (role, title, model, effort) tuples
        self.dry_run_planned: list[tuple[str, str, str | None, str | None]] = []
        # Set to the halting decision hash when the cost-aware dispatch policy
        # (issue #2354) refused this tick's spawns before a USD cap was
        # breached; None when no cost policy fired.
        self.cost_dispatch_halt: str | None = None


def _resolve_manager_llm(workdir: Path) -> tuple[str, str]:
    """Resolve internal LLM provider/model from seed config.

    Returns:
        Tuple of (provider, model).
    """
    from bernstein.core.seed import parse_seed

    provider = "openrouter_free"
    model = "nvidia/nemotron-3-super-120b-a12b"
    seed_path = resolve_seed_path(workdir)
    if seed_path.exists():
        with contextlib.suppress(Exception):
            seed = parse_seed(seed_path)
            provider = seed.internal_llm_provider
            model = seed.internal_llm_model
    return provider, model


def _fetch_task_states(client: httpx.Client, server_url: str) -> dict[str, str]:
    """Fetch task ID -> status map from server for validation."""
    task_states: dict[str, str] = {}
    try:
        resp = client.get(f"{server_url}/tasks")
        resp.raise_for_status()
        for t in resp.json():
            task_states[t["id"]] = t.get("status", "unknown")
    except httpx.HTTPError as exc:
        logger.warning(
            "Manager review: failed to fetch task states for validation: %s",
            exc,
        )
    return task_states


def _apply_manager_corrections(
    client: httpx.Client,
    server_url: str,
    workdir: Path,
    corrections: list[Any],
    task_states: dict[str, str],
) -> None:
    """Apply manager review corrections to the task server."""
    valid_roles: set[str] | None = None
    _cancellable_states = {"open", "claimed", "in_progress"}

    for correction in corrections:
        try:
            # Validate task_id exists in server state (skip add_task which has no task_id)
            if (
                correction.action != "add_task"
                and correction.task_id
                and task_states
                and correction.task_id not in task_states
            ):
                logger.warning(
                    "Manager review: skipping %s for non-existent task %s",
                    correction.action,
                    correction.task_id,
                )
                continue

            if correction.action == "reassign":
                valid_roles = _apply_reassign(client, server_url, workdir, correction, valid_roles)
            elif correction.action == "change_priority":
                _apply_change_priority(client, server_url, correction)
            elif correction.action == "cancel":
                _apply_cancel(client, server_url, correction, task_states, _cancellable_states)
            elif correction.action == "add_task":
                _apply_add_task(client, server_url, correction)
        except httpx.HTTPError as exc:
            logger.warning("Manager review: correction %s failed: %s", correction.action, exc)


def _apply_reassign(
    client: httpx.Client,
    server_url: str,
    workdir: Path,
    correction: Any,
    valid_roles: set[str] | None,
) -> set[str] | None:
    """Apply a reassign correction. Returns valid_roles (lazily populated)."""
    if not correction.task_id or not correction.new_role:
        return valid_roles
    if valid_roles is None:
        from bernstein import get_templates_dir
        from bernstein.core.context import available_roles

        valid_roles = set(available_roles(get_templates_dir(workdir) / "roles"))
    if correction.new_role not in valid_roles:
        logger.warning(
            "Manager review: skipping reassign to invalid role %r (valid: %s)",
            correction.new_role,
            ", ".join(sorted(valid_roles)),
        )
        return valid_roles
    client.patch(
        f"{server_url}/tasks/{correction.task_id}",
        json={"role": correction.new_role},
    )
    logger.info(
        "Manager review: reassigned %s to role=%s (%s)",
        correction.task_id,
        correction.new_role,
        correction.reason,
    )
    return valid_roles


def _apply_change_priority(client: httpx.Client, server_url: str, correction: Any) -> None:
    """Apply a change_priority correction."""
    if not correction.task_id or not correction.new_priority:
        return
    client.patch(
        f"{server_url}/tasks/{correction.task_id}",
        json={"priority": correction.new_priority},
    )
    logger.info(
        "Manager review: changed priority of %s to %d (%s)",
        correction.task_id,
        correction.new_priority,
        correction.reason,
    )


def _apply_cancel(
    client: httpx.Client,
    server_url: str,
    correction: Any,
    task_states: dict[str, str],
    cancellable_states: set[str],
) -> None:
    """Apply a cancel correction."""
    if not correction.task_id:
        return
    status = task_states.get(correction.task_id)
    if status and status not in cancellable_states:
        logger.warning(
            "Manager review: skipping cancel for task %s in non-cancellable state %r",
            correction.task_id,
            status,
        )
        return
    client.post(
        f"{server_url}/tasks/{correction.task_id}/cancel",
        json={"reason": correction.reason or "manager review"},
    )
    logger.info(
        "Manager review: cancelled %s (%s)",
        correction.task_id,
        correction.reason,
    )


def _apply_add_task(client: httpx.Client, server_url: str, correction: Any) -> None:
    """Apply an add_task correction."""
    if not correction.new_task:
        return
    client.post(
        f"{server_url}/tasks",
        json=correction.new_task,
    )
    logger.info(
        "Manager review: added task %r (%s)",
        correction.new_task.get("title"),
        correction.reason,
    )


def _build_notification_manager(seed: Any | None) -> NotificationManager | None:
    """Build a NotificationManager from seed notify/webhooks settings."""
    if seed is None:
        return None

    targets: list[NotificationTarget] = []
    _collect_notify_targets(seed, targets)
    _collect_webhook_targets(seed, targets)
    _collect_smtp_targets(seed, targets)

    if not targets:
        return None
    smtp_cfg = getattr(seed, "smtp", None)
    return NotificationManager(targets, smtp_config=smtp_cfg)


def _collect_notify_targets(seed: Any, targets: list[NotificationTarget]) -> None:
    """Collect notification targets from seed.notify config."""
    notify_cfg = getattr(seed, "notify", None)
    if notify_cfg is None:
        return
    if getattr(notify_cfg, "webhook_url", None):
        events: list[str] = []
        if bool(getattr(notify_cfg, "on_complete", True)):
            events.append(_EVENT_RUN_COMPLETED)
        if bool(getattr(notify_cfg, "on_failure", True)):
            events.append(_EVENT_TASK_FAILED)
        if events:
            targets.append(
                NotificationTarget(
                    type="webhook",
                    url=str(notify_cfg.webhook_url),
                    events=events,
                )
            )
    if bool(getattr(notify_cfg, "desktop", False)):
        targets.append(
            NotificationTarget(
                type="desktop",
                url="",
                events=[_EVENT_TASK_COMPLETED, _EVENT_TASK_FAILED],
            )
        )


def _collect_webhook_targets(seed: Any, targets: list[NotificationTarget]) -> None:
    """Collect webhook targets from seed.webhooks list."""
    for webhook_cfg in getattr(seed, "webhooks", ()):
        url = str(getattr(webhook_cfg, "url", "")).strip()
        events = [str(event_name) for event_name in getattr(webhook_cfg, "events", ())]
        if not url or not events:
            continue
        targets.append(NotificationTarget(type="webhook", url=url, events=events))


def _collect_smtp_targets(seed: Any, targets: list[NotificationTarget]) -> None:
    """Collect email notification target from seed.smtp config."""
    smtp_cfg = getattr(seed, "smtp", None)
    if not smtp_cfg:
        return
    targets.append(
        NotificationTarget(
            type="email",
            url="",
            events=[_EVENT_TASK_COMPLETED, _EVENT_TASK_FAILED, "approval.needed", _EVENT_RUN_COMPLETED],
        )
    )


def _resolve_spawner_adapter_name(
    explicit_adapter: str | None,
    seed_cli: str | None,
) -> str | None:
    """Resolve the orchestrator's adapter with explicit-override precedence.

    An adapter supplied out-of-band -- the ``--adapter`` flag or the
    ``BERNSTEIN_ADAPTER`` env var, both surfaced here as ``explicit_adapter``
    -- always wins over the seed's ``cli`` field. The seed value is only a
    fallback for when no adapter was passed explicitly.

    This mirrors the run-model precedence (``args.model or seed.model``) and
    is load-bearing for ``bernstein run --idle``: that mode exports
    ``BERNSTEIN_ADAPTER=mock`` precisely so the orchestrator never spawns real
    Claude agents (and burns tokens / 401s) when the resolved
    ``bernstein.yaml`` leaves ``cli`` at its ``auto`` default. Letting the
    seed's ``cli`` clobber an explicit adapter silently defeats that contract.

    Returns ``None`` only when neither source configured an adapter, which the
    caller treats as a fatal misconfiguration (Bernstein never defaults to
    Claude).
    """
    explicit = (explicit_adapter or "").strip()
    if explicit:
        return explicit
    seed_value = (seed_cli or "").strip()
    return seed_value or None


if __name__ == "__main__":
    import argparse
    import sys
    import traceback
    from pathlib import Path

    from bernstein.adapters.registry import get_adapter
    from bernstein.core.seed import SeedConfig, parse_seed
    from bernstein.core.spawner import AgentSpawner

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8052)
    # The adapter default deliberately falls through to the BERNSTEIN_ADAPTER
    # env var first, with NO further fallback.  Bernstein must never silently
    # spawn Claude (and burn real tokens) because an adapter was never
    # configured - the operator must pass ``--adapter``, set
    # ``BERNSTEIN_ADAPTER``, or configure ``cli`` in the seed. Absence of all
    # three is a fatal misconfiguration, checked below once the seed-resolved
    # value (if any) has also been consulted.
    _adapter_env_default = os.environ.get("BERNSTEIN_ADAPTER", "").strip() or None
    parser.add_argument("--adapter", type=str, default=_adapter_env_default)
    parser.add_argument("--cells", type=int, default=1, help="Number of parallel cells (1=single-cell)")
    # Run-level model override (from ``bernstein run --model``), threaded through
    # by the CLI's spawner launcher. Falls back to BERNSTEIN_MODEL env var so
    # any nested resolve_adapter()-style callers can also honour it. This is
    # the value AgentSpawner uses to coerce Claude tier names (opus/sonnet/
    # haiku) emitted by the heuristic model selector into a model the active
    # non-Claude adapter understands - see _coerce_model_for_non_claude_adapter.
    _model_env_default = os.environ.get("BERNSTEIN_MODEL", "").strip() or None
    parser.add_argument("--model", type=str, default=_model_env_default)
    # Resolved bernstein.yaml path, threaded through from bootstrap_from_seed()
    # via _start_spawner()'s --seed-path / BERNSTEIN_SEED_PATH env var. Fixes
    # the bug where this subprocess independently re-derived
    # ``workdir / "bernstein.yaml"`` and silently lost role_model_policy (and
    # every other seed setting) whenever the real seed lived elsewhere.
    parser.add_argument(
        "--seed-path",
        type=str,
        default=os.environ.get("BERNSTEIN_SEED_PATH", "").strip() or None,
    )
    args = parser.parse_args()

    print(f"[SPAWNER-DEBUG] orchestrator __main__: parsed CLI args={vars(args)!r}", file=sys.stderr, flush=True)
    print(
        f"[SPAWNER-DEBUG] orchestrator __main__: BERNSTEIN_SEED_PATH env var="
        f"{os.environ.get('BERNSTEIN_SEED_PATH', '<unset>')!r}",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"[SPAWNER-DEBUG] orchestrator __main__: BERNSTEIN_ADAPTER env var="
        f"{os.environ.get('BERNSTEIN_ADAPTER', '<unset>')!r}, BERNSTEIN_MODEL env var="
        f"{os.environ.get('BERNSTEIN_MODEL', '<unset>')!r}",
        file=sys.stderr,
        flush=True,
    )

    workdir = Path.cwd()

    # Configure logging so errors are visible in spawner.log (stdout/stderr)
    log_dir = workdir / ".sdd" / "runtime"
    log_dir.mkdir(parents=True, exist_ok=True)

    from bernstein.core.json_logging import setup_json_logging

    setup_json_logging()

    if not any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers):
        from logging.handlers import RotatingFileHandler

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stderr),
                RotatingFileHandler(
                    log_dir / "orchestrator-debug.log",
                    maxBytes=10 * 1024 * 1024,
                    backupCount=1,
                ),
            ],
        )

    # Apply deterministic random seed if requested via env var.
    _deterministic_seed_env = os.environ.get("BERNSTEIN_DETERMINISTIC_SEED", "").strip()
    if _deterministic_seed_env:
        import random

        try:
            _seed_int = int(_deterministic_seed_env)
            random.seed(_seed_int)
            logger.info("Deterministic mode: random.seed(%d)", _seed_int)
        except ValueError:
            logger.warning("Invalid BERNSTEIN_DETERMINISTIC_SEED=%r, ignoring", _deterministic_seed_env)

    # Set up deterministic LLM response store (recording or replay mode).
    _replay_run_id = os.environ.get("BERNSTEIN_REPLAY_RUN_ID", "").strip()
    _sdd_dir = workdir / ".sdd"
    if _replay_run_id:
        from bernstein.core.orchestration.deterministic import (
            ReplayRecordingMissingError,
            allow_live_miss,
            open_replay_store,
            set_active_store,
        )

        # Hermetic by default (issue #1832): a cache miss aborts the run via
        # ReplayMissError rather than silently calling the live model. The
        # opt-in BERNSTEIN_REPLAY_ALLOW_LIVE_MISS flag restores live
        # fall-through (logged per miss) for record-extend workflows.
        _strict_replay = not allow_live_miss()
        # Refuse a strict replay against a run with no recording before any
        # agent is spawned or any network call is made (issue #2790). The
        # CLI-adapter path never records, so cached_count == 0 there; without
        # this guard replay silently degrades to a full live run.
        try:
            _det_store = open_replay_store(
                _sdd_dir / "runs" / _replay_run_id,
                strict=_strict_replay,
            )
        except ReplayRecordingMissingError as exc:
            logger.error("FATAL: %s", exc)
            print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        set_active_store(_det_store)
        logger.info(
            "Deterministic replay mode (%s): loaded %d cached LLM responses from run %s",
            "strict/hermetic" if _strict_replay else "non-strict/live-miss-allowed",
            _det_store.cached_count,
            _replay_run_id,
        )
    elif _deterministic_seed_env:
        # Recording mode: capture LLM responses so this run can be replayed later.
        # The run_id is not known yet here; we set it once Orchestrator.__init__ runs.
        # We create a placeholder store using a temp dir keyed by the seed value;
        # the real store is set after the orchestrator assigns its run_id below.
        pass  # store will be set after orchestrator is instantiated

    print("[SPAWNER-DEBUG] orchestrator __main__: entering top-level try block", file=sys.stderr, flush=True)
    try:
        # Try to load adapter from seed if available
        adapter_name = args.adapter
        seed_path = Path(args.seed_path) if args.seed_path else resolve_seed_path(workdir)
        logger.info(
            "orchestrator __main__: resolved seed_path=%s (from --seed-path=%s, exists=%s)",
            seed_path,
            args.seed_path,
            seed_path.exists(),
        )
        seed: SeedConfig | None = None
        if seed_path.exists():
            print(
                f"[SPAWNER-DEBUG] orchestrator __main__: seed file exists at {seed_path}, attempting parse_seed()",
                file=sys.stderr,
                flush=True,
            )
            try:
                seed = parse_seed(seed_path)
                # An explicit --adapter / BERNSTEIN_ADAPTER wins over the
                # seed's ``cli`` (which defaults to ``auto``); the seed value
                # is only a fallback. Without this, ``bernstein run --idle``
                # (which exports BERNSTEIN_ADAPTER=mock) is silently overridden
                # by the seed default and spawns real Claude agents.
                adapter_name = _resolve_spawner_adapter_name(args.adapter, getattr(seed, "cli", None))
                _seed_role_model_policy = getattr(seed, "role_model_policy", None)
                if not _seed_role_model_policy:
                    print(
                        "[SPAWNER-DEBUG] orchestrator __main__: role_model_policy is None/empty "
                        f"after seed parse (seed_path={seed_path})",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    print(
                        "[SPAWNER-DEBUG] orchestrator __main__: parsed role_model_policy="
                        f"{json.dumps(_seed_role_model_policy, default=str)}",
                        file=sys.stderr,
                        flush=True,
                    )
                print(
                    "[SPAWNER-DEBUG] orchestrator __main__: seed parse SUCCESS - cli="
                    f"{getattr(seed, 'cli', None)!r}, top-level model="
                    f"{getattr(seed, 'model', None)!r}",
                    file=sys.stderr,
                    flush=True,
                )
                logger.info(
                    "orchestrator __main__: parsed seed from %s (cli=%s, model=%s, role_model_policy=%s)",
                    seed_path,
                    getattr(seed, "cli", None),
                    getattr(seed, "model", None),
                    getattr(seed, "role_model_policy", None),
                )
            except Exception as exc:
                print(
                    f"[SPAWNER-DEBUG] orchestrator __main__: seed parse FAILED for {seed_path}: "
                    f"{exc!r}\n{traceback.format_exc()}",
                    file=sys.stderr,
                    flush=True,
                )
                logger.warning("Failed to parse seed for adapter config: %s", exc)
        else:
            print(
                f"[SPAWNER-DEBUG] orchestrator __main__: seed file does NOT exist at {seed_path} "
                "- role_model_policy and model will be unset from seed",
                file=sys.stderr,
                flush=True,
            )

        if not adapter_name:
            logger.error(
                "FATAL: no adapter configured. Bernstein does not default to Claude - "
                "pass --adapter, set BERNSTEIN_ADAPTER, or configure 'cli' in bernstein.yaml."
            )
            sys.exit(1)

        # Run-level model: ``--model`` flag (threaded from ``bernstein run
        # --model``) wins, falling back to the seed's resolved model (also
        # set from the same CLI flag when the seed is parsed in-process).
        # This is the value that reaches AgentSpawner as ``default_model``
        # so child-task spawns can coerce Claude tier names for non-Claude
        # adapters instead of passing them through literally.
        run_model: str | None = args.model or (getattr(seed, "model", None) if seed else None)
        print(
            f"[SPAWNER-DEBUG] orchestrator __main__: resolved run_model={run_model!r} "
            f"(args.model={args.model!r}, seed.model="
            f"{getattr(seed, 'model', None) if seed else '<no seed>'!r})",
            file=sys.stderr,
            flush=True,
        )

        if adapter_name == "auto":
            # Auto mode: default to Claude Code (primary), others used via routing
            adapter_inst = get_adapter("claude")
            if not adapter_inst:
                # Fallback: try any available adapter
                for fallback_name in ("codex", "gemini", "qwen"):
                    adapter_inst = get_adapter(fallback_name)
                    if adapter_inst:
                        logger.info("Auto mode: using %s as primary adapter", fallback_name)
                        break
        else:
            adapter_inst = get_adapter(adapter_name)
        if not adapter_inst:
            logger.error("No adapter found (tried: %s)", adapter_name)
            sys.exit(1)

        # Create TierAwareRouter from providers.yaml if available
        router: TierAwareRouter | None = None
        providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
        if providers_yaml.exists():
            router = TierAwareRouter()
            load_providers_from_yaml(providers_yaml, router)
            logger.info("Loaded TierAwareRouter from %s", providers_yaml)
            # Load model policy for this router instance
            model_policy_yaml = workdir / ".sdd" / "config" / "model_policy.yaml"
            if model_policy_yaml.exists():
                load_model_policy_from_yaml(model_policy_yaml, router)
            elif seed_path.exists():
                load_model_policy_from_yaml(seed_path, router)

        # Configure model fallback tracker from bernstein.yaml.
        # Reads model_fallback: section and wires the configurable chain into
        # the process-global singleton before any agents are spawned.
        if seed and getattr(seed, "model_fallback", None):
            from bernstein.core.model_fallback import initialize_fallback_tracker

            mf = seed.model_fallback
            initialize_fallback_tracker(
                fallback_chain=mf.fallback_chain or None,
                strike_limit=mf.strike_limit,
                include_timeouts=mf.include_timeouts,
                trigger_codes=frozenset(mf.trigger_codes),
            )

        # Resolve + install the per-agent credential policy.
        # Default-on: walks DEFAULT_POLICY_PATHS for a config file and
        # falls back to the bundled examples/credential-policies/default.yaml
        # so a fresh checkout still gets fail-closed env-var scoping.
        # Operators can opt out via BERNSTEIN_DISABLE_CREDENTIAL_SCOPING=1.
        try:
            from bernstein.core.credential_scoping import resolve_default_policy

            resolve_default_policy(workdir=workdir)
        except Exception as exc:
            # Never let policy load kill the orchestrator - log and fall
            # through to the legacy unscoped path. Only the exception
            # message is logged here, not credential values.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.warning("Credential policy resolution failed: %s", exc)

        # Load MCP config from user global + project seed
        mcp_config = None
        if adapter_name == "claude":
            from bernstein.adapters.claude import load_mcp_config

            project_mcp = None
            if seed_path.exists():
                try:
                    seed_cfg = parse_seed(seed_path)
                    project_mcp = seed_cfg.mcp_servers
                except Exception as exc:
                    logger.warning("Failed to parse seed for MCP config: %s", exc)
            mcp_config = load_mcp_config(project_servers=project_mcp)
            if mcp_config:
                logger.info("Loaded MCP config with %d server(s)", len(mcp_config.get("mcpServers", {})))

        # Initialize MCPManager from seed mcp_servers config
        from bernstein.core.mcp_manager import MCPManager, parse_server_configs

        mcp_manager: MCPManager | None = None
        if seed and seed.mcp_servers:
            mcp_server_configs = parse_server_configs(seed.mcp_servers)
            if mcp_server_configs:
                # Pass through the signing mode from bernstein.yaml
                # (default 'warn' so the new gate is non-blocking).
                # BERNSTEIN_MCP_SIGNING_MODE env-var still wins inside
                # MCPManager - see _resolve_signing_mode.
                signing_mode = getattr(seed, "mcp_signing_mode", "warn")
                mcp_manager = MCPManager(
                    mcp_server_configs,
                    signing_mode=signing_mode,
                )
                mcp_manager.start_all()
                logger.info(
                    "MCPManager started %d server(s) with signing_mode=%s: %s",
                    len(mcp_server_configs),
                    mcp_manager.signing_mode,
                    ", ".join(mcp_manager.server_names),
                )

        # Load agency catalog from seed config
        from bernstein.core.agency_loader import load_agency_catalog

        agency_catalog = None
        if seed and seed.agent_catalog:
            catalog_path = Path(seed.agent_catalog)
            if not catalog_path.is_absolute():
                catalog_path = workdir / catalog_path
            agency_catalog = load_agency_catalog(catalog_path)
            if agency_catalog:
                logger.info("Loaded %d agency agents from %s", len(agency_catalog), catalog_path)

        # Build catalog registry and populate loaded_agents from Agency cache
        import asyncio as _asyncio

        from bernstein.agents.agency_provider import AgencyProvider as _AgencyProvider
        from bernstein.agents.catalog import CatalogRegistry as _CatalogRegistry

        catalog_registry: _CatalogRegistry | None = seed.catalogs if seed else None
        if catalog_registry is None:
            catalog_registry = _CatalogRegistry.default()

        agency_cache_path = _AgencyProvider.default_cache_path()
        if agency_cache_path.exists():
            try:
                _provider = _AgencyProvider(local_path=agency_cache_path)
                _agency_agents = _asyncio.run(_provider.fetch_agents())
                for _a in _agency_agents:
                    catalog_registry.register_agent(_a)
                logger.info(
                    "Loaded %d Agency specialist(s) into catalog from %s",
                    len(_agency_agents),
                    agency_cache_path,
                )
            except Exception as _exc:
                logger.warning("Failed to load Agency agents into catalog: %s", _exc)

        from bernstein import get_templates_dir
        from bernstein.core.mcp_registry import MCPRegistry

        mcp_registry_path = workdir / ".sdd" / "config" / "mcp_servers.yaml"
        mcp_registry: MCPRegistry | None = None
        if mcp_registry_path.exists():
            mcp_registry = MCPRegistry(config_path=mcp_registry_path)
            logger.info("Loaded MCP auto-discovery registry from %s", mcp_registry_path)

        # Legacy container isolation env vars are still supported. The newer
        # ``--sandbox`` CLI flag sets BERNSTEIN_SANDBOX_RUNTIME and routes
        # through the adapter-aware sandbox config below.
        _container_enabled = os.environ.get("BERNSTEIN_CONTAINER", "0").strip() in ("1", "true", "yes")
        _container_image = os.environ.get("BERNSTEIN_CONTAINER_IMAGE", "bernstein-agent:latest")
        _two_phase = os.environ.get("BERNSTEIN_TWO_PHASE_SANDBOX", "0").strip() in ("1", "true", "yes")
        _sandbox_runtime = os.environ.get("BERNSTEIN_SANDBOX_RUNTIME", "").strip().lower()
        sandbox_config = (
            seed.sandbox if seed is not None and seed.sandbox is not None and seed.sandbox.enabled else None
        )
        if _sandbox_runtime:
            from bernstein.core.sandbox import DockerSandbox

            base_sandbox = sandbox_config or DockerSandbox(enabled=True)
            sandbox_config = DockerSandbox(
                enabled=True,
                runtime="podman" if _sandbox_runtime == "podman" else "docker",
                default_image=_container_image or base_sandbox.default_image,
                adapter_images=base_sandbox.adapter_images,
                cpu_cores=base_sandbox.cpu_cores,
                memory_mb=base_sandbox.memory_mb,
                disk_mb=base_sandbox.disk_mb,
                pids_limit=base_sandbox.pids_limit,
                network_mode=base_sandbox.network_mode,
                drop_capabilities=base_sandbox.drop_capabilities,
                read_only_rootfs=base_sandbox.read_only_rootfs,
                extra_mounts=base_sandbox.extra_mounts,
            )

        _container_iso = ContainerIsolationConfig(
            enabled=_container_enabled,
            image=_container_image,
            two_phase_sandbox=_two_phase,
        )
        container_config = None if sandbox_config is not None else _build_container_config(_container_iso)
        if container_config is not None and _container_iso.auto_build_image:
            from bernstein.core.container import ensure_agent_image

            ensure_agent_image(_container_iso.runtime, _container_iso.image)

        # ``--sandbox docker`` (BERNSTEIN_SANDBOX_RUNTIME=docker) previously
        # provisioned one run-level SandboxSession shared by every agent,
        # so a single exec timeout killed the container for all of them
        # and concurrent agents shared one /workspace clone. Attach the
        # DockerSandboxBackend plus a manifest factory instead: the
        # spawner provisions ONE session per spawn and destroys it when
        # the exec future resolves (issue #2162).
        #
        # BERNSTEIN_SANDBOX_RUNTIME is only ever set by an explicit
        # ``--sandbox`` flag, so reaching this block always means the
        # operator explicitly requested container isolation. A missing
        # Docker SDK or dead daemon is therefore a loud failure
        # (SandboxSelectionError) rather than a silent degrade to legacy
        # container / host worktree execution, which would drop the
        # isolation boundary without a console signal (issue #2809).
        _docker_sandbox_backend = None
        _docker_manifest_factory = None
        if _sandbox_runtime == "docker":
            import subprocess as _subprocess

            from bernstein.core.sandbox.explicit_attach import attach_docker_backend
            from bernstein.core.sandbox.manifest import GitRepoEntry, WorkspaceManifest

            _branch_result = _subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            _current_branch = _branch_result.stdout.strip() or "HEAD"

            def _make_docker_manifest(_src: str = str(workdir), _branch: str = _current_branch) -> WorkspaceManifest:
                return WorkspaceManifest(
                    root="/workspace",
                    repo=GitRepoEntry(src_path=_src, branch=_branch),
                )

            # Fail fast at wiring time so a dead daemon surfaces before
            # any spawn. ``explicit=True`` turns an unavailable backend
            # into a raised SandboxSelectionError instead of a silent
            # host fallback.
            _docker_sandbox_backend = attach_docker_backend(explicit=True)
            _docker_manifest_factory = _make_docker_manifest
            logger.info(
                "Docker sandbox backend attached; one session per agent spawn (branch=%s)",
                _current_branch,
            )

        def _teardown_docker_sandbox() -> None:
            """Destroy any Docker sandbox sessions the backend still tracks.

            Per-agent sessions are normally destroyed when their exec
            future resolves; this backend-level sweep catches anything
            left behind so no ``sleep infinity`` container outlives the
            orchestrator.
            """
            if _docker_sandbox_backend is None:
                return
            try:
                _asyncio.run(_docker_sandbox_backend.destroy_all())
                logger.info("Docker sandbox backend cleanup complete")
            except Exception:
                logger.warning("Failed to clean up Docker sandbox sessions", exc_info=True)

        runtime_bridge = None
        openclaw_cfg = seed.bridges.openclaw if seed is not None and seed.bridges is not None else None
        if openclaw_cfg is not None and openclaw_cfg.enabled:
            from bernstein.bridges.base import BridgeConfig
            from bernstein.bridges.openclaw import OpenClawBridge

            runtime_bridge = OpenClawBridge(
                BridgeConfig(
                    bridge_type="openclaw",
                    endpoint=openclaw_cfg.url,
                    api_key=openclaw_cfg.api_key,
                    timeout_seconds=int(openclaw_cfg.request_timeout_s),
                    max_log_bytes=openclaw_cfg.max_log_bytes,
                    extra={
                        "agent_id": openclaw_cfg.agent_id,
                        "workspace_mode": openclaw_cfg.workspace_mode,
                        "fallback_to_local": openclaw_cfg.fallback_to_local,
                        "connect_timeout_s": openclaw_cfg.connect_timeout_s,
                        "request_timeout_s": openclaw_cfg.request_timeout_s,
                        "session_prefix": openclaw_cfg.session_prefix,
                        "model_override": openclaw_cfg.model_override,
                    },
                ),
                workdir=workdir,
            )

        # Parse agent resource limits from config
        from bernstein.core.resource_limits import DEFAULT_AGENT_LIMITS
        from bernstein.core.resource_limits import ResourceLimits as _ResourceLimits

        agent_rlimits: _ResourceLimits | None = None
        if seed and getattr(seed, "agent_resource_limits", None) is not None:
            if isinstance(seed.agent_resource_limits, dict):
                agent_rlimits = _ResourceLimits.from_dict(seed.agent_resource_limits)
            elif isinstance(seed.agent_resource_limits, _ResourceLimits):
                agent_rlimits = seed.agent_resource_limits
        if agent_rlimits is None:
            agent_rlimits = DEFAULT_AGENT_LIMITS

        from bernstein.core.warm_pool import WarmPool, WarmPoolConfig

        warm_pool = WarmPool(
            config=WarmPoolConfig(
                max_slots=max(1, min(2, seed.max_agents if seed else 2)),
            ),
        )

        _spawner_role_model_policy = seed.role_model_policy if seed else None
        _spawner_policy_repr = (
            json.dumps(_spawner_role_model_policy, default=str) if _spawner_role_model_policy else "<None/empty>"
        )
        print(
            "[SPAWNER-DEBUG] orchestrator __main__: constructing AgentSpawner with "
            f"role_model_policy={_spawner_policy_repr}, "
            f"default_model={run_model!r}, adapter={adapter_inst!r}",
            file=sys.stderr,
            flush=True,
        )
        spawner = AgentSpawner(
            adapter=adapter_inst,
            templates_dir=get_templates_dir(workdir) / "roles",
            workdir=workdir,
            router=router,
            mcp_config=mcp_config,
            mcp_registry=mcp_registry,
            mcp_manager=mcp_manager,
            agency_catalog=agency_catalog,
            catalog=catalog_registry,
            use_worktrees=True,  # Always use worktrees for isolation + auto-commit
            worktree_setup_config=seed.worktree_setup if seed else None,
            enable_caching=True,
            container_config=container_config,
            sandbox=sandbox_config,
            role_model_policy=seed.role_model_policy if seed else None,
            provider_availability=seed.provider_availability if seed else None,
            runtime_bridge=runtime_bridge,
            resource_limits=agent_rlimits,
            warm_pool=warm_pool,
            sandbox_backend=_docker_sandbox_backend,
            sandbox_manifest_factory=_docker_manifest_factory,
            sandbox_options={"image": _container_image} if _docker_sandbox_backend is not None else None,
            sandbox_server_port=args.port,
            default_model=run_model,
            # Explicit adapter selection (--adapter / BERNSTEIN_ADAPTER / a
            # non-"auto" seed cli) must never be overridden by model-name
            # inference at spawn time (#2751). "auto" is the only unpinned
            # state - adapter_name is guaranteed non-empty by the fatal
            # check above.
            adapter_pinned=adapter_name != "auto",
        )
        run_config_budget_usd: float | None = None
        dry_run = False
        approval_mode = "auto"
        merge_strategy = "pr"
        auto_merge = True
        workflow_mode: str | None = None
        run_config_path = workdir / ".sdd" / "runtime" / "run_config.json"
        if run_config_path.exists():
            with contextlib.suppress(ValueError):
                run_cfg = json.loads(run_config_path.read_text())
                run_config_budget_usd = float(run_cfg.get("budget_usd", 0.0))
                dry_run = bool(run_cfg.get("dry_run", False))
                approval_mode = str(run_cfg.get("approval", "auto"))
                merge_strategy = str(run_cfg.get("merge_strategy", "pr"))
                auto_merge = bool(run_cfg.get("auto_merge", True))
                workflow_mode = run_cfg.get("workflow") or None

        # Layered budget resolution.
        #   Precedence: BERNSTEIN_MAX_COST_USD > run_config.json > seed.budget_usd > 0.
        # The env var is set by the ``bernstein run --max-cost-usd N`` CLI flag.
        # Off-by-default: if neither flag, run-config, nor seed sets a positive
        # cap the run remains unbounded (``budget_usd = 0.0``).
        from bernstein.core.cost_tracker import resolve_run_budget_usd

        budget_usd = resolve_run_budget_usd(
            run_config_value=run_config_budget_usd,
            seed_value=seed.budget_usd if seed is not None else None,
        )
        # Env var override for workflow mode
        workflow_mode = os.environ.get("BERNSTEIN_WORKFLOW", workflow_mode or "") or None

        # Resolve compliance config: env var > run_config > seed config
        compliance_config = None
        compliance_preset_env = os.environ.get("BERNSTEIN_COMPLIANCE")
        if compliance_preset_env:
            from bernstein.core.compliance import ComplianceConfig, CompliancePreset

            compliance_config = ComplianceConfig.from_preset(CompliancePreset(compliance_preset_env.lower()))
        elif seed and seed.compliance:
            compliance_config = seed.compliance
        else:
            from bernstein.core.compliance import load_compliance_config

            compliance_config = load_compliance_config(workdir / ".sdd")

        # Compliance can force governed workflow mode
        if compliance_config and compliance_config.governed_workflow and not workflow_mode:
            workflow_mode = "governed"

        # Resolve cluster-aware settings from env vars + seed config
        server_url = os.environ.get("BERNSTEIN_SERVER_URL", f"http://127.0.0.1:{args.port}")
        auth_token = os.environ.get("BERNSTEIN_AUTH_TOKEN")

        # Build cluster config: env vars take precedence over seed file
        cluster_cfg: ClusterConfig | None = seed.cluster if seed else None
        cluster_enabled = os.environ.get("BERNSTEIN_CLUSTER_ENABLED", "").lower() in ("1", "true", "yes")
        if cluster_enabled:
            cluster_cfg = ClusterConfig(
                enabled=True,
                topology=(cluster_cfg.topology if cluster_cfg else ClusterTopology.STAR),
                auth_token=auth_token or (cluster_cfg.auth_token if cluster_cfg else None),
                node_heartbeat_interval_s=(cluster_cfg.node_heartbeat_interval_s if cluster_cfg else 15),
                node_timeout_s=(cluster_cfg.node_timeout_s if cluster_cfg else 60),
                server_url=os.environ.get("BERNSTEIN_SERVER_URL") or (cluster_cfg.server_url if cluster_cfg else None),
                bind_host=os.environ.get("BERNSTEIN_BIND_HOST", "127.0.0.1"),
                gossip_peers=(cluster_cfg.gossip_peers if cluster_cfg else ()),
                claim_lease_ttl_s=(cluster_cfg.claim_lease_ttl_s if cluster_cfg else 300),
                claim_journal_path=(cluster_cfg.claim_journal_path if cluster_cfg else None),
                # Carried through the env override too: dropping the pins here
                # would leave a MESH node folding nothing from its peers (#2997).
                gossip_peer_keys=(cluster_cfg.gossip_peer_keys if cluster_cfg else ()),
            )

        # Topology branch (#2558): MESH starts a leaderless coordinator instead
        # of taking its assignments from the central node registry.
        start_cluster_coordinator(cluster_cfg, sdd_dir=workdir / ".sdd")

        # Resolve compliance can force approval gates
        if compliance_config and compliance_config.approval_gates and approval_mode == "auto":
            approval_mode = "review"

        _ab_test = os.environ.get("BERNSTEIN_AB_TEST", "0").strip() in ("1", "true", "yes")
        notifier = _build_notification_manager(seed)

        config = OrchestratorConfig(
            server_url=server_url,
            max_agents=seed.max_agents if seed else 6,
            budget_usd=budget_usd,
            dry_run=dry_run,
            auth_token=auth_token,
            approval=approval_mode,
            merge_strategy=merge_strategy,
            auto_merge=auto_merge,
            workflow=workflow_mode,
            compliance=compliance_config,
            ab_test=_ab_test,
            batch=seed.batch if seed else BatchConfig(),
            max_cost_per_agent=seed.max_cost_per_agent if seed else 0.0,
            test_agent=seed.test_agent if seed else TestAgentConfig(),
            cost_autopilot=seed.cost_autopilot if seed else False,
            judge_model=seed.judge_model if seed else None,
            judge_provider=seed.judge_provider if seed else None,
            cost_policy=getattr(seed, "cost_policy", None) if seed else None,
        )

        if args.cells > 1:
            from bernstein.core.models import Cell
            from bernstein.core.orchestration.multi_cell import MultiCellOrchestrator

            multi_orchestrator = MultiCellOrchestrator(
                config=config,
                spawner=spawner,
                workdir=workdir,
            )
            for i in range(args.cells):
                cell_id = f"cell-{i + 1}"
                role = "vp" if i == 0 else "manager"
                cell = Cell(
                    id=cell_id,
                    name=f"Cell {i + 1} ({role})",
                    max_workers=config.max_agents,
                )
                multi_orchestrator.register_cell(cell)
            logger.info(
                "Starting MultiCellOrchestrator with %d cells (VP on cell-1)",
                args.cells,
            )

            def _multi_signal_handler(signum: int, _frame: object) -> None:
                logger.info("Signal %d received, stopping multi-cell orchestrator", signum)
                multi_orchestrator.stop()

            signal.signal(signal.SIGINT, _multi_signal_handler)
            signal.signal(signal.SIGTERM, _multi_signal_handler)
            try:
                multi_orchestrator.run()
            finally:
                _teardown_docker_sandbox()
                if mcp_manager is not None:
                    mcp_manager.stop_all()
        else:
            orchestrator = Orchestrator(
                config=config,
                spawner=spawner,
                workdir=workdir,
                router=router,
                cluster_config=cluster_cfg,
                notifier=notifier,
                quality_gate_config=seed.quality_gates if seed else None,
                formal_verification_config=seed.formal_verification if seed else None,
            )

            def _signal_handler(signum: int, _frame: object) -> None:
                logger.info("Signal %d received, stopping orchestrator", signum)
                orchestrator.stop()

            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)

            # Activate recording store once we know the orchestrator's run_id.
            if _deterministic_seed_env and not _replay_run_id:
                from bernstein.core.orchestration.deterministic import DeterministicStore, set_active_store

                _rec_store = DeterministicStore(
                    _sdd_dir / "runs" / orchestrator._run_id,
                    replay=False,
                )
                set_active_store(_rec_store)
                logger.info(
                    "Deterministic recording mode: LLM calls will be saved to %s",
                    _rec_store.calls_path,
                )

            _profile_enabled = os.environ.get("BERNSTEIN_PROFILE", "").strip() in ("1", "true")
            try:
                if _profile_enabled:
                    from bernstein.core.profiler import ProfilerSession, resolve_profile_output_dir

                    _prof_dir = resolve_profile_output_dir(workdir)
                    with ProfilerSession(_prof_dir):
                        orchestrator.run()
                else:
                    orchestrator.run()
            finally:
                _teardown_docker_sandbox()
                if mcp_manager is not None:
                    mcp_manager.stop_all()
    except Exception:
        _crash_tb = traceback.format_exc()
        print(
            f"[SPAWNER-DEBUG] orchestrator __main__: FATAL uncaught exception:\n{_crash_tb}",
            file=sys.stderr,
            flush=True,
        )
        logger.exception("Orchestrator crashed")
        try:
            _crash_log_dir = workdir / ".sdd" / "runtime"
            _crash_log_dir.mkdir(parents=True, exist_ok=True)
            _crash_log_path = _crash_log_dir / "spawner_crash.log"
            with _crash_log_path.open("a", encoding="utf-8") as _crash_fh:
                _crash_fh.write(f"\n=== spawner crash at {datetime.now(UTC).isoformat()} ===\n")
                _crash_fh.write(_crash_tb)
                _crash_fh.write("\n")
            print(
                f"[SPAWNER-DEBUG] orchestrator __main__: crash traceback appended to {_crash_log_path}",
                file=sys.stderr,
                flush=True,
            )
        except Exception as _log_exc:  # pragma: no cover - crash logging must never itself crash the reporting path
            print(
                f"[SPAWNER-DEBUG] orchestrator __main__: FAILED to write spawner_crash.log: {_log_exc!r}",
                file=sys.stderr,
                flush=True,
            )
        sys.exit(1)
# ---------------------------------------------------------------------------
# Meta-messages for orchestrator nudges (T567)
# Re-exported from nudge_manager.py for backward compatibility (ORCH-009).
# ---------------------------------------------------------------------------
from bernstein.core.orchestration.nudge_manager import OrchestratorNudge as OrchestratorNudge  # noqa: E402
from bernstein.core.orchestration.nudge_manager import (  # noqa: E402
    OrchestratorNudgeManager as OrchestratorNudgeManager,
)
from bernstein.core.orchestration.nudge_manager import get_orchestrator_nudges as get_orchestrator_nudges  # noqa: E402
from bernstein.core.orchestration.nudge_manager import nudge_manager as nudge_manager  # noqa: E402
from bernstein.core.orchestration.nudge_manager import nudge_orchestrator as nudge_orchestrator  # noqa: E402

_TESTS_DIR = "tests/"

_nudge_manager = nudge_manager  # backward-compat alias
