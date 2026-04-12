"""Orchestrator loop: watch tasks, spawn agents, verify completion, repeat.

The orchestrator is DETERMINISTIC CODE, not an LLM. It matches tasks to agents
via the spawner and verifies completion via the janitor. See ADR-001.

This module is the public facade. Heavy lifting lives in:
- tick_pipeline.py         — task fetching, batching, server interaction, TypedDicts
- task_lifecycle.py        — claim/spawn, completion processing, retry/decompose
- agent_lifecycle.py       — agent tracking, heartbeat, crash detection, reaping
- orchestrator_tick.py     — core tick loop and per-tick helpers
- orchestrator_run.py      — main run loop, startup coordination
- orchestrator_evolve.py   — evolution mode, ruff checks, test runs
- orchestrator_backlog.py  — file-based backlog ingestion
- orchestrator_summary.py  — end-of-run reports and summary cards
- orchestrator_cleanup.py  — stop, drain, save state, restart
- orchestrator_recovery.py — WAL crash recovery
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

import httpx

from bernstein.core.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.agent_lifecycle import (
    check_kill_signals,
    check_stale_agents,
    reap_dead_agents,
    refresh_agent_states,
    send_shutdown_signals,
)
from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.approval import ApprovalGate, ApprovalMode
from bernstein.core.bandit_router import BanditRouter
from bernstein.core.batch_api import ProviderBatchManager
from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.cluster import NodeHeartbeatClient
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.dependency_scan import (
    DependencyVulnerabilityFinding,
    DependencyVulnerabilityScanner,
)
from bernstein.core.evolution import EvolutionCoordinator
from bernstein.core.fast_path import (
    FastPathStats,
    load_fast_path_config,
)
from bernstein.core.file_locks import FileLockManager
from bernstein.core.incident import IncidentManager
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

# --- Sub-module re-exports (ORCH-009) ---
# These imports make all extracted functions available from this module
# so ``from bernstein.core.orchestrator import X`` continues to work.
from bernstein.core.orchestrator_backlog import backlog_words_from_title as backlog_words_from_title
from bernstein.core.orchestrator_backlog import ingest_backlog as ingest_backlog
from bernstein.core.orchestrator_backlog import sync_backlog_file as sync_backlog_file
from bernstein.core.orchestrator_cleanup import cleanup as cleanup
from bernstein.core.orchestrator_cleanup import drain_before_cleanup as drain_before_cleanup
from bernstein.core.orchestrator_cleanup import is_shutting_down as is_shutting_down
from bernstein.core.orchestrator_cleanup import restart as restart
from bernstein.core.orchestrator_cleanup import save_session_state as save_session_state
from bernstein.core.orchestrator_cleanup import stop as stop
from bernstein.core.orchestrator_config import check_source_changed as check_source_changed
from bernstein.core.orchestrator_config import maybe_reload_config as maybe_reload_config
from bernstein.core.orchestrator_evolve import _EVOLVE_FOCUS_AREAS as _EVOLVE_FOCUS_AREAS
from bernstein.core.orchestrator_evolve import check_evolve as check_evolve
from bernstein.core.orchestrator_evolve import create_ruff_tasks as create_ruff_tasks
from bernstein.core.orchestrator_evolve import evolve_auto_commit as evolve_auto_commit
from bernstein.core.orchestrator_evolve import evolve_run_tests as evolve_run_tests
from bernstein.core.orchestrator_evolve import evolve_spawn_manager as evolve_spawn_manager
from bernstein.core.orchestrator_evolve import generate_evolve_commit_msg as generate_evolve_commit_msg
from bernstein.core.orchestrator_evolve import log_evolve_cycle as log_evolve_cycle
from bernstein.core.orchestrator_evolve import make_evolution_loop as make_evolution_loop
from bernstein.core.orchestrator_evolve import persist_pending_proposals as persist_pending_proposals
from bernstein.core.orchestrator_evolve import replenish_backlog as replenish_backlog
from bernstein.core.orchestrator_evolve import run_evolution_cycle as run_evolution_cycle
from bernstein.core.orchestrator_evolve import run_pytest as run_pytest
from bernstein.core.orchestrator_evolve import run_ruff_check as run_ruff_check
from bernstein.core.orchestrator_recovery import recover_from_wal as recover_from_wal
from bernstein.core.orchestrator_run import run as run
from bernstein.core.orchestrator_summary import emit_summary_card as emit_summary_card
from bernstein.core.orchestrator_summary import generate_run_summary as generate_run_summary
from bernstein.core.orchestrator_tick import (
    _check_file_overlap as _check_file_overlap,
)
from bernstein.core.orchestrator_tick import (
    _check_server_health as _check_server_health,
)
from bernstein.core.orchestrator_tick import (
    _check_task_deadlines as _check_task_deadlines,
)
from bernstein.core.orchestrator_tick import (
    _check_workflow_approval as _check_workflow_approval,
)
from bernstein.core.orchestrator_tick import (
    _create_dependency_fix_task as _create_dependency_fix_task,
)
from bernstein.core.orchestrator_tick import (
    _find_session_for_task as _find_session_for_task,
)
from bernstein.core.orchestrator_tick import (
    _handle_anomaly_signal as _handle_anomaly_signal,
)
from bernstein.core.orchestrator_tick import (
    _kill_agent_for_cost_cap as _kill_agent_for_cost_cap,
)
from bernstein.core.orchestrator_tick import (
    _load_existing_dependency_scan_task_titles as _load_existing_dependency_scan_task_titles,
)
from bernstein.core.orchestrator_tick import (
    _log_summary as _log_summary,
)
from bernstein.core.orchestrator_tick import (
    _maybe_retry_task as _maybe_retry_task_fn,
)
from bernstein.core.orchestrator_tick import (
    _reconcile_claimed_tasks as _reconcile_claimed_tasks_fn,
)
from bernstein.core.orchestrator_tick import (
    _record_live_costs as _record_live_costs,
)
from bernstein.core.orchestrator_tick import (
    _record_provider_health as _record_provider_health,
)
from bernstein.core.orchestrator_tick import (
    _record_tick_events as _record_tick_events,
)
from bernstein.core.orchestrator_tick import (
    _release_file_ownership as _release_file_ownership,
)
from bernstein.core.orchestrator_tick import (
    _release_stale_claims as _release_stale_claims,
)
from bernstein.core.orchestrator_tick import (
    _release_task_to_session as _release_task_to_session,
)
from bernstein.core.orchestrator_tick import (
    _run_manager_queue_review as _run_manager_queue_review,
)
from bernstein.core.orchestrator_tick import (
    _run_scheduled_dependency_scan as _run_scheduled_dependency_scan,
)
from bernstein.core.orchestrator_tick import (
    _should_auto_decompose as _should_auto_decompose_fn,
)
from bernstein.core.orchestrator_tick import (
    _should_trigger_manager_review as _should_trigger_manager_review,
)
from bernstein.core.orchestrator_tick import (
    _tick_internal as _tick_internal,
)
from bernstein.core.orchestrator_tick import tick as tick
from bernstein.core.quality_gate_coalescer import QualityGateCoalescer
from bernstein.core.quarantine import QuarantineStore
from bernstein.core.quota_poller import QuotaPoller
from bernstein.core.rate_limit_tracker import RateLimitTracker
from bernstein.core.recorder import RunRecorder
from bernstein.core.router import TierAwareRouter, load_model_policy_from_yaml, load_providers_from_yaml
from bernstein.core.runbooks import RunbookEngine
from bernstein.core.runtime_state import (
    SessionReplayMetadata,
    current_git_branch,
    current_git_sha,
    hash_file,
    write_session_replay_metadata,
)
from bernstein.core.semantic_cache import ResponseCacheManager
from bernstein.core.slo import SLOTracker
from bernstein.core.task_lifecycle import (
    claim_and_spawn_batches,
    collect_completion_data,
    process_completed_tasks,
    retry_or_fail_task,
)
from bernstein.core.tick_pipeline import (
    CompletionData,
    RuffViolation,
    TestResults,
    block_task,
    complete_task,
    fail_task,
    fetch_all_tasks,
    parse_backlog_file,
)
from bernstein.core.tick_pipeline import (
    compute_total_spent as compute_total_spent,
)
from bernstein.core.tick_pipeline import (
    group_by_role as group_by_role,
)
from bernstein.core.tick_pipeline import (
    total_spent_cache as total_spent_cache,
)
from bernstein.core.wal import WALWriter
from bernstein.core.watchdog import WatchdogManager
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
    from bernstein.core.quality_gates import QualityGatesConfig
    from bernstein.core.spawner import AgentSpawner
    from bernstein.evolution.loop import EvolutionLoop

logger = logging.getLogger(__name__)


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
#   from bernstein.core.orchestrator import _fail_task, _complete_task, ...
# continues to work.
# ---------------------------------------------------------------------------
_task_from_dict: Callable[[dict[str, Any]], Task] = lambda raw: Task.from_dict(raw)  # noqa: E731
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

    _SPAWN_BACKOFF_BASE_S: float = 30.0  # base backoff; actual = base * 2^failures
    _SPAWN_BACKOFF_MAX_S: float = 300.0  # ceiling for exponential backoff
    _MAX_SPAWN_FAILURES: int = 3  # consecutive failures before marking tasks failed
    _MAX_DEAD_AGENTS_KEPT: int = 20  # purge oldest dead agents beyond this
    _MAX_PROCESSED_DONE: int = 500  # cap _processed_done_tasks set size
    _MANAGER_REVIEW_COMPLETION_THRESHOLD: int = 7  # trigger review after this many completions
    _MANAGER_REVIEW_STALL_S: float = 900.0  # trigger review after 15 min of no progress
    _STALE_CLAIM_TIMEOUT_S: float = 900.0  # default fallback; prefer config.stale_claim_timeout_s

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
        self._dependency_scanner = DependencyVulnerabilityScanner(workdir)
        # Track spawn failures per batch for backoff: task_ids -> (fail_count, last_fail_ts)
        self._spawn_failures: dict[frozenset[str], tuple[int, float]] = {}
        self._spawn_failure_history: dict[frozenset[str], list[Any]] = {}
        self._latest_tasks_by_id: dict[str, Task] = {}
        # Track last backlog replenishment timestamp
        self._last_replenish_ts: float = 0.0
        # Run completion summary state
        self._summary_written: bool = False
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
        # Load model policy — checked on every init so late-bound routers pick it up
        if self._router is not None:
            model_policy_yaml = workdir / ".sdd" / "config" / "model_policy.yaml"
            if model_policy_yaml.exists():
                load_model_policy_from_yaml(model_policy_yaml, self._router)
            else:
                # Fall back to bernstein.yaml model_policy section
                seed_path = workdir / _BERNSTEIN_YAML
                if seed_path.exists():
                    load_model_policy_from_yaml(seed_path, self._router)
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
        # Always initialize the governor — it's lightweight and evolve mode
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
        run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self._run_id = run_id
        self._cost_tracker = CostTracker(
            run_id=run_id,
            budget_usd=config.budget_usd,
        )
        self._cost_cap_killed_agents: set[str] = set()

        # Cost anomaly detector: layered on top of cost_tracker, fires
        # AnomalySignals the orchestrator acts on (log/stop/kill).
        from bernstein.core.cost_anomaly import CostAnomalyDetector

        self._anomaly_detector = CostAnomalyDetector(config.cost_anomaly, workdir)

        # Deterministic replay recorder: appends events to
        # .sdd/runs/{run_id}/replay.jsonl for post-hoc debugging.
        self._recorder = RunRecorder(run_id=run_id, sdd_dir=workdir / ".sdd")
        _seed_path = workdir / _BERNSTEIN_YAML
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
        self._config_path: Path = workdir / _BERNSTEIN_YAML
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

        # Convergence guard: blocks spawn waves when merge queue, active
        # agent count, error rate, or spawn rate exceed safe thresholds.
        from bernstein.core.convergence_guard import ConvergenceGuard

        self._convergence_guard = ConvergenceGuard(config.convergence)

        # AgentOps: SLO tracking, error budget, runbooks, incident response.
        # Reset error budget AND metric collector each run — stale failure
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
                logger.warning("Unknown workflow %r — running in adaptive mode", config.workflow)

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
            logger.info("SOC 2 audit mode active — logging to %s", audit_dir)
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
        """Delegate to orchestrator_config.check_source_changed."""
        return check_source_changed(self)

    def _maybe_reload_config(self) -> bool:
        """Delegate to orchestrator_config.maybe_reload_config."""
        return maybe_reload_config(self)

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
        return dict(self._agents)

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

        self._bulletin.post(
            BulletinMessage(
                agent_id="orchestrator",
                type=_cast("MessageType", msg_type),
                content=content,
            )
        )

    def _notify(self, event: str, title: str, body: str, **metadata: Any) -> None:
        """Fire a notification event if a NotificationManager is configured.

        Args:
            event: Notification event name (e.g. ``"run.completed"``).
            title: Short human-readable title.
            body: Longer description / summary.
            **metadata: Arbitrary key-value pairs attached to the payload.
        """
        if self._notifier is None:
            return
        payload = NotificationPayload(event=event, title=title, body=body, metadata=dict(metadata))
        self._notifier.notify(event, payload)

    # -- Core tick (delegates to orchestrator_tick) --------------------------

    def tick(self) -> TickResult:
        """Execute one orchestrator cycle."""
        return tick(self)

    def _tick_internal(self) -> TickResult:
        """Actual tick implementation."""
        return _tick_internal(self)

    def _check_task_deadlines(self, running_tasks: list[Task]) -> None:
        """Delegate to orchestrator_tick._check_task_deadlines."""
        _check_task_deadlines(self, running_tasks)

    def _check_workflow_approval(self) -> None:
        """Delegate to orchestrator_tick._check_workflow_approval."""
        _check_workflow_approval(self)

    def _handle_anomaly_signal(self, signal: object) -> None:
        """Delegate to orchestrator_tick._handle_anomaly_signal."""
        _handle_anomaly_signal(self, signal)

    def _record_live_costs(self) -> None:
        """Delegate to orchestrator_tick._record_live_costs."""
        _record_live_costs(self)

    def _check_server_health(self) -> bool:
        """Delegate to orchestrator_tick._check_server_health."""
        return _check_server_health(self)

    def _record_provider_health(
        self,
        session: AgentSession,
        success: bool,
        latency_ms: float = 0.0,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> None:
        """Delegate to orchestrator_tick._record_provider_health."""
        _record_provider_health(self, session, success, latency_ms, cost_usd, tokens)

    def _reconcile_claimed_tasks(self) -> int:
        """Delegate to orchestrator_tick._reconcile_claimed_tasks."""
        return _reconcile_claimed_tasks_fn(self)

    def _release_stale_claims(self, claimed_tasks: list[Task]) -> int:
        """Delegate to orchestrator_tick._release_stale_claims."""
        return _release_stale_claims(self, claimed_tasks)

    def _check_file_overlap(self, batch: list[Task]) -> bool:
        """Delegate to orchestrator_tick._check_file_overlap."""
        return _check_file_overlap(self, batch)

    def _should_auto_decompose(self, task: Task) -> bool:
        """Delegate to orchestrator_tick._should_auto_decompose."""
        return _should_auto_decompose_fn(self, task)

    def _auto_decompose_task(self, task: Task) -> None:
        """Delegate to orchestrator_tick._auto_decompose_task."""
        from bernstein.core.orchestrator_tick import _auto_decompose_task

        _auto_decompose_task(self, task)

    def _kill_agent_for_cost_cap(self, session: AgentSession) -> None:
        """Delegate to orchestrator_tick._kill_agent_for_cost_cap."""
        _kill_agent_for_cost_cap(self, session)

    def _find_session_for_task(self, task_id: str) -> AgentSession | None:
        """Delegate to orchestrator_tick._find_session_for_task."""
        return _find_session_for_task(self, task_id)

    def _release_file_ownership(self, agent_id: str) -> None:
        """Delegate to orchestrator_tick._release_file_ownership."""
        _release_file_ownership(self, agent_id)

    def _release_task_to_session(self, task_ids: list[str]) -> None:
        """Delegate to orchestrator_tick._release_task_to_session."""
        _release_task_to_session(self, task_ids)

    def _log_summary(self, result: TickResult) -> None:
        """Delegate to orchestrator_tick._log_summary."""
        _log_summary(self, result)

    def _record_tick_events(self, result: TickResult, tasks_by_status: dict[str, list[Task]]) -> None:
        """Delegate to orchestrator_tick._record_tick_events."""
        _record_tick_events(self, result, tasks_by_status)

    # -- Run loop (delegates to orchestrator_run) ---------------------------

    def run(self) -> None:
        """Run the orchestrator loop until stopped."""
        from bernstein.core.orchestrator_run import run as _run

        _run(self)

    def _has_active_agents(self) -> bool:
        """Delegate to orchestrator_run._has_active_agents."""
        from bernstein.core.orchestrator_run import _has_active_agents

        return _has_active_agents(self)

    def _run_scheduled_dependency_scan(self) -> None:
        """Delegate to orchestrator_tick._run_scheduled_dependency_scan."""
        _run_scheduled_dependency_scan(self)

    def _load_existing_dependency_scan_task_titles(self) -> set[str]:
        """Delegate to orchestrator_tick._load_existing_dependency_scan_task_titles."""
        return _load_existing_dependency_scan_task_titles(self)

    def _create_dependency_fix_task(
        self,
        finding: DependencyVulnerabilityFinding,
        existing_titles: set[str],
    ) -> str | None:
        """Delegate to orchestrator_tick._create_dependency_fix_task."""
        return _create_dependency_fix_task(self, finding, existing_titles)

    def _should_trigger_manager_review(self, failed_count: int) -> bool:
        """Delegate to orchestrator_tick._should_trigger_manager_review."""
        return _should_trigger_manager_review(self, failed_count)

    def _run_manager_queue_review(self) -> None:
        """Delegate to orchestrator_tick._run_manager_queue_review."""
        _run_manager_queue_review(self)

    def _collect_completion_data(self, session: AgentSession) -> CompletionData:
        """Delegate to task_lifecycle.collect_completion_data."""
        return collect_completion_data(self._workdir, session)

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
        """Delegate to orchestrator_tick._maybe_retry_task."""
        return _maybe_retry_task_fn(self, task)

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
        )

    # -- Cleanup (delegates to orchestrator_cleanup) -------------------------

    def stop(self) -> None:
        """Delegate to orchestrator_cleanup.stop."""
        from bernstein.core.orchestrator_cleanup import stop as _stop

        _stop(self)

    def is_shutting_down(self) -> bool:
        """Delegate to orchestrator_cleanup.is_shutting_down."""
        from bernstein.core.orchestrator_cleanup import is_shutting_down as _is_shutting_down

        return _is_shutting_down(self)

    def _drain_before_cleanup(self, timeout_s: float | None = None) -> None:
        """Delegate to orchestrator_cleanup.drain_before_cleanup."""
        from bernstein.core.orchestrator_cleanup import drain_before_cleanup as _drain

        _drain(self, timeout_s)

    def _save_session_state(self) -> None:
        """Delegate to orchestrator_cleanup.save_session_state."""
        from bernstein.core.orchestrator_cleanup import save_session_state as _save

        _save(self)

    def _cleanup(self) -> None:
        """Delegate to orchestrator_cleanup.cleanup."""
        from bernstein.core.orchestrator_cleanup import cleanup as _cleanup

        _cleanup(self)

    def _restart(self) -> None:
        """Delegate to orchestrator_cleanup.restart."""
        from bernstein.core.orchestrator_cleanup import restart as _restart

        _restart(self)

    # -- Recovery (delegates to orchestrator_recovery) -----------------------

    def _recover_from_wal(self) -> list[tuple[str, Any]]:
        """Delegate to orchestrator_recovery.recover_from_wal."""
        return recover_from_wal(self)

    # -- Evolve mode (delegates to orchestrator_evolve) ----------------------

    _EVOLVE_FOCUS_AREAS: ClassVar[list[str]] = _EVOLVE_FOCUS_AREAS

    _REPLENISH_COOLDOWN_S: float = 60.0
    _REPLENISH_MAX_TASKS: int = 5

    def _check_evolve(self, result: TickResult, tasks_by_status: dict[str, list[Task]]) -> None:
        """Delegate to orchestrator_evolve.check_evolve."""
        check_evolve(self, result, tasks_by_status)

    def _run_ruff_check(self) -> list[RuffViolation]:
        """Delegate to orchestrator_evolve.run_ruff_check."""
        return run_ruff_check(self)

    def _create_ruff_tasks(self, violations: list[RuffViolation]) -> None:
        """Delegate to orchestrator_evolve.create_ruff_tasks."""
        create_ruff_tasks(self, violations)

    def _replenish_backlog(self, result: TickResult) -> None:
        """Delegate to orchestrator_evolve.replenish_backlog."""
        replenish_backlog(self, result)

    def _run_pytest(self) -> TestResults:
        """Delegate to orchestrator_evolve.run_pytest."""
        return run_pytest(self)

    def _evolve_run_tests(self) -> TestResults:
        """Delegate to orchestrator_evolve.evolve_run_tests."""
        return evolve_run_tests(self)

    @staticmethod
    def _generate_evolve_commit_msg(staged_files: list[str]) -> str:
        """Delegate to orchestrator_evolve.generate_evolve_commit_msg."""
        return generate_evolve_commit_msg(staged_files)

    def _evolve_auto_commit(self) -> bool:
        """Delegate to orchestrator_evolve.evolve_auto_commit."""
        return evolve_auto_commit(self)

    def _evolve_spawn_manager(
        self,
        cycle_number: int = 0,
        focus_area: str = "new_features",
        test_summary: str = "",
    ) -> None:
        """Delegate to orchestrator_evolve.evolve_spawn_manager."""
        evolve_spawn_manager(self, cycle_number, focus_area, test_summary)

    def _log_evolve_cycle(
        self,
        cycle_number: int,
        timestamp: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Delegate to orchestrator_evolve.log_evolve_cycle."""
        log_evolve_cycle(self, cycle_number, timestamp, metrics)

    def make_evolution_loop(self, **kwargs: Any) -> EvolutionLoop:
        """Delegate to orchestrator_evolve.make_evolution_loop."""
        return make_evolution_loop(self, **kwargs)

    def _run_evolution_cycle(self, result: TickResult) -> None:
        """Delegate to orchestrator_evolve.run_evolution_cycle."""
        run_evolution_cycle(self, result)

    def _persist_pending_proposals(self) -> None:
        """Delegate to orchestrator_evolve.persist_pending_proposals."""
        persist_pending_proposals(self)

    # -- Backlog (delegates to orchestrator_backlog) -------------------------

    def _sync_backlog_file(self, task: Task) -> None:
        """Delegate to orchestrator_backlog.sync_backlog_file."""
        sync_backlog_file(self, task)

    def ingest_backlog(self) -> int:
        """Delegate to orchestrator_backlog.ingest_backlog."""
        return ingest_backlog(self)

    def _claim_backlog_file(self, backlog_file: Path, open_dir: Path, claimed_dir: Path) -> None:
        """Delegate to orchestrator_backlog._claim_backlog_file."""
        from bernstein.core.orchestrator_backlog import _claim_backlog_file

        _claim_backlog_file(self, backlog_file, open_dir, claimed_dir)

    def _ingest_backlog_one_by_one(
        self,
        batch_files: list[tuple[Path, ParsedBacklogTask]],
        open_dir: Path,
        claimed_dir: Path,
    ) -> int:
        """Delegate to orchestrator_backlog._ingest_backlog_one_by_one."""
        from bernstein.core.orchestrator_backlog import _ingest_backlog_one_by_one

        return _ingest_backlog_one_by_one(self, batch_files, open_dir, claimed_dir)

    @staticmethod
    def _backlog_words_from_title(title: str) -> set[str]:
        """Delegate to orchestrator_backlog.backlog_words_from_title."""
        return backlog_words_from_title(title)

    # -- Run summary (delegates to orchestrator_summary) --------------------

    def _generate_run_summary(
        self,
        done_tasks: list[Task],
        failed_tasks: list[Task],
    ) -> None:
        """Delegate to orchestrator_summary.generate_run_summary."""
        generate_run_summary(self, done_tasks, failed_tasks)

    def _emit_summary_card(
        self,
        done_tasks: list[Task],
        failed_tasks: list[Task],
        collector: Any,
        wall_clock_s: float,
        total_cost: float,
    ) -> None:
        """Delegate to orchestrator_summary.emit_summary_card."""
        emit_summary_card(self, done_tasks, failed_tasks, collector, wall_clock_s, total_cost)


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


def _build_notification_manager(seed: Any | None) -> NotificationManager | None:
    """Build a NotificationManager from seed notify/webhooks settings."""
    if seed is None:
        return None

    targets: list[NotificationTarget] = []

    notify_cfg = getattr(seed, "notify", None)
    if notify_cfg is not None and getattr(notify_cfg, "webhook_url", None):
        events: list[str] = []
        if bool(getattr(notify_cfg, "on_complete", True)):
            events.append("run.completed")
        if bool(getattr(notify_cfg, "on_failure", True)):
            events.append("task.failed")
        if events:
            targets.append(
                NotificationTarget(
                    type="webhook",
                    url=str(notify_cfg.webhook_url),
                    events=events,
                )
            )

    if notify_cfg is not None and bool(getattr(notify_cfg, "desktop", False)):
        targets.append(
            NotificationTarget(
                type="desktop",
                url="",
                events=["task.completed", "task.failed"],
            )
        )

    for webhook_cfg in getattr(seed, "webhooks", ()):
        url = str(getattr(webhook_cfg, "url", "")).strip()
        events = [str(event_name) for event_name in getattr(webhook_cfg, "events", ())]
        if not url or not events:
            continue
        targets.append(NotificationTarget(type="webhook", url=url, events=events))

    smtp_cfg = getattr(seed, "smtp", None)
    if smtp_cfg:
        targets.append(
            NotificationTarget(
                type="email",
                url="",
                events=["task.completed", "task.failed", "approval.needed", "run.completed"],
            )
        )

    if not targets:
        return None
    return NotificationManager(targets, smtp_config=smtp_cfg)


if __name__ == "__main__":
    import argparse
    import sys
    from pathlib import Path

    from bernstein.adapters.registry import get_adapter
    from bernstein.core.seed import SeedConfig, parse_seed
    from bernstein.core.spawner import AgentSpawner

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8052)
    parser.add_argument("--adapter", type=str, default="claude")
    parser.add_argument("--cells", type=int, default=1, help="Number of parallel cells (1=single-cell)")
    args = parser.parse_args()

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
        from bernstein.core.deterministic import DeterministicStore, set_active_store

        _det_store = DeterministicStore(
            _sdd_dir / "runs" / _replay_run_id,
            replay=True,
        )
        set_active_store(_det_store)
        logger.info(
            "Deterministic replay mode: loaded %d cached LLM responses from run %s",
            _det_store.cached_count,
            _replay_run_id,
        )
    elif _deterministic_seed_env:
        # Recording mode: capture LLM responses so this run can be replayed later.
        # The run_id is not known yet here; we set it once Orchestrator.__init__ runs.
        # We create a placeholder store using a temp dir keyed by the seed value;
        # the real store is set after the orchestrator assigns its run_id below.
        pass  # store will be set after orchestrator is instantiated

    try:
        # Try to load adapter from seed if available
        adapter_name = args.adapter
        seed_path = workdir / _BERNSTEIN_YAML
        seed: SeedConfig | None = None
        if seed_path.exists():
            try:
                seed = parse_seed(seed_path)
                adapter_name = getattr(seed, "cli", adapter_name)
            except Exception as exc:
                logger.warning("Failed to parse seed for adapter config: %s", exc)

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

        # Configure model fallback tracker from bernstein.yaml (AGENT-004).
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
                mcp_manager = MCPManager(mcp_server_configs)
                mcp_manager.start_all()
                logger.info(
                    "MCPManager started %d server(s): %s",
                    len(mcp_server_configs),
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

        # Parse agent resource limits from config (AGENT-013)
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

        spawner = AgentSpawner(
            adapter=adapter_inst,
            templates_dir=get_templates_dir(workdir),
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
            runtime_bridge=runtime_bridge,
            resource_limits=agent_rlimits,
            warm_pool=warm_pool,
        )
        budget_usd = 0.0
        dry_run = False
        approval_mode = "auto"
        merge_strategy = "pr"
        auto_merge = True
        workflow_mode: str | None = None
        run_config_path = workdir / ".sdd" / "runtime" / "run_config.json"
        if run_config_path.exists():
            try:
                run_cfg = json.loads(run_config_path.read_text())
                budget_usd = float(run_cfg.get("budget_usd", 0.0))
                dry_run = bool(run_cfg.get("dry_run", False))
                approval_mode = str(run_cfg.get("approval", "auto"))
                merge_strategy = str(run_cfg.get("merge_strategy", "pr"))
                auto_merge = bool(run_cfg.get("auto_merge", True))
                workflow_mode = run_cfg.get("workflow") or None
            except ValueError:
                pass
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
            )

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
        )

        if args.cells > 1:
            from bernstein.core.models import Cell
            from bernstein.core.multi_cell import MultiCellOrchestrator

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
                from bernstein.core.deterministic import DeterministicStore, set_active_store

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
                if mcp_manager is not None:
                    mcp_manager.stop_all()
    except Exception:
        logger.exception("Orchestrator crashed")
        sys.exit(1)
# ---------------------------------------------------------------------------
# Meta-messages for orchestrator nudges (T567)
# Re-exported from nudge_manager.py for backward compatibility (ORCH-009).
# ---------------------------------------------------------------------------
from bernstein.core.nudge_manager import OrchestratorNudge as OrchestratorNudge  # noqa: E402
from bernstein.core.nudge_manager import OrchestratorNudgeManager as OrchestratorNudgeManager  # noqa: E402
from bernstein.core.nudge_manager import get_orchestrator_nudges as get_orchestrator_nudges  # noqa: E402
from bernstein.core.nudge_manager import nudge_manager as nudge_manager  # noqa: E402
from bernstein.core.nudge_manager import nudge_orchestrator as nudge_orchestrator  # noqa: E402

_nudge_manager = nudge_manager  # backward-compat alias
