"""Orchestrator loop: watch tasks, spawn agents, verify completion, repeat.

The orchestrator is DETERMINISTIC CODE, not an LLM. It matches tasks to agents
via the spawner and verifies completion via the janitor. See ADR-001.

This module is the public facade. Heavy lifting lives in:
- tick_pipeline.py   — task fetching, batching, server interaction, TypedDicts
- task_lifecycle.py  — claim/spawn, completion processing, retry/decompose
- agent_lifecycle.py — agent tracking, heartbeat, crash detection, reaping
"""

from __future__ import annotations

import concurrent.futures
import contextlib
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

from bernstein.core.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.agent_lifecycle import (
    check_kill_signals,
    check_loops_and_deadlocks,
    check_stale_agents,
    check_stalled_tasks,
    reap_dead_agents,
    recycle_idle_agents,
    refresh_agent_states,
    send_shutdown_signals,
)
from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.approval import ApprovalGate, ApprovalMode
from bernstein.core.bandit_router import BanditRouter
from bernstein.core.batch_api import ProviderBatchManager
from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.cluster import NodeHeartbeatClient
from bernstein.core.context import refresh_knowledge_base
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.cost_tracker import CostTracker
from bernstein.core.dep_validator import DependencyValidator
from bernstein.core.dependency_scan import (
    DependencyScanStatus,
    DependencyVulnerabilityFinding,
    DependencyVulnerabilityScanner,
)
from bernstein.core.evolution import EvolutionCoordinator, UpgradeStatus
from bernstein.core.fast_path import (
    FastPathStats,
    load_fast_path_config,
)
from bernstein.core.file_locks import FileLockManager
from bernstein.core.graph import TaskGraph
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
    TaskType,
    TestAgentConfig,
)
from bernstein.core.notifications import NotificationManager, NotificationPayload, NotificationTarget
from bernstein.core.quarantine import QuarantineStore
from bernstein.core.quota_poller import QuotaPoller
from bernstein.core.rate_limit_tracker import RateLimitTracker
from bernstein.core.recorder import RunRecorder
from bernstein.core.retrospective import generate_retrospective
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
from bernstein.core.signals import read_unresolved_pivots
from bernstein.core.slo import SLOTracker
from bernstein.core.task_lifecycle import (
    auto_decompose_task,
    claim_and_spawn_batches,
    collect_completion_data,
    maybe_retry_task,
    process_completed_tasks,
    retry_or_fail_task,
    should_auto_decompose,
)
from bernstein.core.tick_pipeline import (
    CompletionData,
    RuffViolation,
    TestResults,
    block_task,
    complete_task,
    fail_task,
    fetch_all_tasks,
    group_by_role,
    parse_backlog_file,
)
from bernstein.core.tick_pipeline import (
    compute_total_spent as compute_total_spent,
)
from bernstein.core.tick_pipeline import (
    total_spent_cache as total_spent_cache,
)
from bernstein.core.task_grouping import compact_small_tasks
from bernstein.core.token_monitor import check_token_growth
from bernstein.core.wal import WALWriter
from bernstein.core.watchdog import WatchdogManager, collect_watchdog_findings
from bernstein.core.workflow import WorkflowExecutor, load_workflow
from bernstein.evolution.governance import AdaptiveGovernor, GovernanceEntry, ProjectContext
from bernstein.evolution.risk import RiskScorer

# Preserve underscore-prefixed aliases so existing test imports keep working
_compute_total_spent = compute_total_spent
_total_spent_cache = total_spent_cache

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.container import ContainerConfig
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
    _MANAGER_REVIEW_COMPLETION_THRESHOLD: int = 3  # trigger review after this many completions
    _MANAGER_REVIEW_STALL_S: float = 300.0  # trigger review after 5 min of no progress

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
        self._workdir = workdir
        self._bulletin: BulletinBoard | None = bulletin
        self._notifier: NotificationManager | None = notifier
        self._cluster_config = cluster_config
        self._quality_gate_config: QualityGatesConfig | None = quality_gate_config
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
        self._task_to_session: dict[str, str] = {}  # task_id -> agent_id (reverse index)
        self._batch_sessions: dict[str, AgentSession] = {}
        self._processed_done_tasks: set[str] = set()  # avoid re-processing done tasks
        self._retried_task_ids: set[str] = set()  # tasks that already have a retry queued
        self._decomposed_task_ids: set[str] = set()  # large tasks queued for decomposition
        # Crash recovery: per-task crash count and preserved worktrees for resume
        self._crash_counts: dict[str, int] = {}  # task_id -> crash count
        self._preserved_worktrees: dict[str, Path] = {}  # task_id -> worktree to reuse
        self._running = False
        self._tick_count = 0
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
        self._idle_shutdown_ts: dict[str, float] = {}  # agent_id -> shutdown signal sent ts
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
                seed_path = workdir / "bernstein.yaml"
                if seed_path.exists():
                    load_model_policy_from_yaml(seed_path, self._router)
            # Warn on startup if policy leaves no viable providers
            policy_issues = self._router.validate_policy()
            for issue in policy_issues:
                logger.warning("Model policy: %s", issue)

        # Rate-limit-aware scheduling: detects 429s and throttles providers
        from bernstein.core.rate_limit_tracker import RateLimitTracker as _RLTracker

        self._rate_limit_tracker = _RLTracker()

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

        # Contextual bandit router: active when BERNSTEIN_ROUTING=bandit.
        # Persists policy to .sdd/routing/ so learning accumulates across runs.
        import os as _os

        _routing_mode = _os.environ.get("BERNSTEIN_ROUTING", "static").lower()
        self._bandit_router: BanditRouter | None = (
            BanditRouter(policy_dir=workdir / ".sdd" / "routing") if _routing_mode == "bandit" else None
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
        _seed_path = workdir / "bernstein.yaml"
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

        # Memory leak detection: sampled every few ticks
        self._memory_guard = MemoryGuard()

        # Agent signal manager: writes WAKEUP/SHUTDOWN files into
        # .sdd/runtime/signals/{session_id}/ for stale agent detection.
        self._signal_mgr = AgentSignalManager(self._workdir)

        # FIFO merge queue: serializes branch merges so only one runs at a time.
        # Conflict resolution tasks are created by process_completed_tasks when
        # a MergeResult reports conflicting_files.
        self._merge_queue = MergeQueue()

        # AgentOps: SLO tracking, error budget, runbooks, incident response.
        # Reset error budget each run — stale failure data from prior runs
        # should not throttle a fresh run's agent capacity.
        self._slo_tracker = SLOTracker()  # fresh tracker, no persistence from prior runs
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

    # -- Core tick -----------------------------------------------------------

    def tick(self) -> TickResult:
        """Execute one orchestrator cycle."""
        from bernstein.core.telemetry import start_span

        with start_span("orchestrator.tick", attributes={"tick": self._tick_count + 1}):
            return self._tick_internal()

    def _tick_internal(self) -> TickResult:
        """Actual tick implementation (previously tick())."""
        result = TickResult()
        self._tick_count += 1
        base = self._config.server_url
        _tick_http_reads = 0  # counts GET requests this tick (should stay at 1)

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

        # 0. Ingest any new backlog files before fetching tasks
        try:
            from bernstein.core.roadmap_runtime import emit_roadmap_wave

            emitted = emit_roadmap_wave(self._workdir)
            if emitted:
                logger.info("Emitted %d roadmap ticket(s) into backlog/open", len(emitted))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("roadmap wave emission failed: %s", exc)

        try:
            self.ingest_backlog()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
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
            if self._consecutive_server_failures >= 12:  # 12 ticks x ~30s = ~6 min
                logger.critical(
                    "Server unreachable for %d consecutive ticks — orchestrator stopping to prevent waste",
                    self._consecutive_server_failures,
                )
                self._running = False
            elif self._consecutive_server_failures >= 3:
                logger.warning(
                    "Server unreachable for %d ticks (%s). Supervisor should restart it.",
                    self._consecutive_server_failures,
                    exc,
                )
            else:
                logger.error("Failed to fetch tasks: %s", exc)
            result.errors.append(f"fetch_all: {exc}")
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
        done_ids = {t.id for t in done_tasks}
        open_tasks = [t for t in tasks_by_status["open"] if all(dep in done_ids for dep in t.depends_on)]
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
        all_tasks = [t for status_tasks in tasks_by_status.values() for t in status_tasks]
        self._latest_tasks_by_id = {task.id: task for task in all_tasks}
        task_graph = TaskGraph(all_tasks)
        analysis = task_graph.analyse()
        dep_validator = DependencyValidator()
        dep_validation = dep_validator.validate(all_tasks)
        for cycle in dep_validation.cycles:
            logger.error("Dependency cycle detected: %s", " -> ".join(cycle))
        for task_id, dep_id, dep_status in dep_validation.stuck_deps:
            logger.warning(
                "Task %s depends on %s which is %s — task remains blocked",
                task_id,
                dep_id,
                dep_status,
            )
        for warning in dep_validation.warnings:
            logger.warning("Dependency validation: %s", warning)
        critical_path_ids = set(dep_validator.critical_path(all_tasks))

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

        # Sync failure timestamps to spawner for cooldown enforcement
        self._spawner._agent_failure_timestamps = self._agent_failure_timestamps

        refresh_agent_states(self, tasks_by_status)
        alive_count = sum(1 for a in self._agents.values() if a.status != "dead")
        result.active_agents = alive_count

        # 3a. Build alive-per-role map for task distribution prioritization.
        # Starving roles (0 alive agents) get scheduled before well-served roles.
        _alive_per_role: dict[str, int] = {}
        for _agent in self._agents.values():
            if _agent.status != "dead":
                _alive_per_role[_agent.role] = _alive_per_role.get(_agent.role, 0) + 1

        # 2. Group into batches with starving-role prioritization wired in
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

        # 3c. Claim tasks and spawn agents for ready batches (skip if budget is exhausted)
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
        elif self._cost_tracker.budget_usd > 0 and self._cost_tracker.status().should_stop:
            _bs = self._cost_tracker.status()
            logger.warning(
                "Budget exhausted — $%.2f spent of $%.2f budget. "
                "Fix: increase budget with --budget N or wait for running tasks to complete",
                _bs.spent_usd,
                _bs.budget_usd,
            )
            self._notify(
                "budget.warning",
                "Budget cap reached",
                f"Spending cap of ${_bs.budget_usd:.2f} reached. "
                f"${_bs.spent_usd:.2f} spent ({_bs.percentage_used * 100:.0f}%). "
                "Agent spawning paused.",
                budget_usd=round(_bs.budget_usd, 2),
                spent_usd=round(_bs.spent_usd, 4),
                percent_used=round(_bs.percentage_used * 100, 1),
            )
        else:
            claim_and_spawn_batches(self, batches, alive_count, assigned_task_ids, done_ids, result)

        # Restore max_agents after adaptive-parallelism-adjusted spawning
        self._config.max_agents = _orig_max_agents

        if self._batch_api is not None:
            self._batch_api.poll(self)

        # 4. Check done tasks, run janitor, record evolution metrics
        process_completed_tasks(self, done_tasks, result)

        # 4x. Periodic git hygiene: every 5 completed tasks
        if len(done_tasks) > 0 and self._tick_count % 5 == 0:
            try:
                from bernstein.core.git_hygiene import run_hygiene

                run_hygiene(self._workdir)
            except Exception:
                pass

        # 4a-wf. Governed workflow: try to advance phase after processing completions
        if self._workflow_executor is not None and not self._workflow_executor.is_completed:
            all_tasks = [t for status_tasks in tasks_by_status.values() for t in status_tasks]
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
        if self._should_trigger_manager_review(self._failures_since_review):
            self._run_manager_queue_review()

        # 4b.6 AgentOps: update SLOs, check error budget, detect incidents
        if self._tick_count % 5 == 0:  # every 5 ticks
            collector = get_collector()
            self._slo_tracker.update_from_collector(collector)
            self._slo_tracker.save(self._workdir / ".sdd" / "metrics")

            # Track consecutive failures for incident detection
            if result.verified:
                self._consecutive_failures = 0
            if failed_tasks:
                self._consecutive_failures += len([t for t in failed_tasks if t.id not in self._retried_task_ids])

            # Check for incidents
            all_counted = self._slo_tracker.error_budget.total_tasks
            failed_counted = self._slo_tracker.error_budget.failed_tasks
            self._incident_manager.check_for_incidents(
                failed_task_count=failed_counted,
                total_task_count=all_counted,
                consecutive_failures=self._consecutive_failures,
                error_budget_depleted=self._slo_tracker.error_budget.is_depleted,
            )
            self._incident_manager.save(self._workdir / ".sdd" / "runtime")

        # 4c. Check heartbeat-based staleness; send WAKEUP/SHUTDOWN as needed
        check_stale_agents(self)

        # 4d. Check progress-snapshot-based stalls; send WAKEUP/SHUTDOWN/kill
        check_stalled_tasks(self)

        # 4d-ii. Token growth monitor: alert on quadratic growth, kill runaway agents
        check_token_growth(self)

        # 4d-ii.5 Loop and deadlock detection: kill looping agents, break lock cycles
        check_loops_and_deadlocks(self)

        # 4d-ii.6 Three-tier watchdog: mechanical checks -> AI triage -> human escalation
        self._watchdog.sync(collect_watchdog_findings(self))

        # 4d-iii. Cost anomaly detection: burn rate projection, stop on budget overrun
        for sig in self._anomaly_detector.check_tick(list(self._agents.values()), self._cost_tracker):
            self._handle_anomaly_signal(sig)

        # 4d-iv. Real-time cost recording: update budget status from live tokens
        self._record_live_costs()

        # 4e. Recycle idle agents (task already resolved but process still alive,
        #     or no heartbeat for idle threshold). SHUTDOWN → 30s grace → SIGKILL.
        recycle_idle_agents(self, tasks_by_status)

        # 5. Reap dead/stale agents and fail their tasks
        reap_dead_agents(self, result, tasks_by_status)

        # 6. Run evolution analysis cycle every N ticks
        if self._evolution is not None and self._tick_count % self._config.evolution_tick_interval == 0:
            self._run_evolution_cycle(result)

        # 6b. Refresh knowledge base every 5 evolution intervals
        if self._tick_count % (self._config.evolution_tick_interval * 5) == 0:
            try:
                refresh_knowledge_base(self._workdir)
            except OSError as exc:
                logger.warning("Knowledge base refresh failed: %s", exc)

        # 7. Check evolve mode: if all tasks done and no agents alive, trigger new cycle
        self._check_evolve(result, tasks_by_status)

        # 8. Replenish backlog in evolve mode when tasks run out
        self._replenish_backlog(result)

        # 8b. Generate run completion summary for non-evolve runs (reuse cached tasks)
        if (
            not self._config.evolve_mode
            and result.open_tasks == 0
            and result.active_agents == 0
            and not self._summary_written
        ):
            self._generate_run_summary(tasks_by_status["done"], tasks_by_status["failed"])

        # 9. Log summary
        self._log_summary(result)

        # 10. Record replay events for deterministic replay
        self._record_tick_events(result, tasks_by_status)

        return result

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
        consecutive_failures = 0
        max_consecutive_failures = 10
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
            elif tick_result is not None and (tick_result.spawned or tick_result.verified or tick_result.retried):
                self._idle_multiplier = 1
                time.sleep(self._config.poll_interval_s)
            else:
                self._idle_multiplier = min(self._idle_multiplier * 2, 1024)
                time.sleep(min(self._config.poll_interval_s * self._idle_multiplier, 30.0))

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
        self._post_bulletin("status", "run stopped")
        self._recorder.record(
            "run_completed",
            run_id=self._run_id,
            ticks=self._tick_count,
            fingerprint=self._recorder.fingerprint(),
        )
        logger.info(
            "Orchestrator stopped (replay: %s, fingerprint: %s)",
            self._recorder.path,
            self._recorder.fingerprint()[:16] + "...",
        )

    def _has_active_agents(self) -> bool:
        """Return True if any agents are still alive (not dead)."""
        alive = sum(1 for s in self._agents.values() if s.status != "dead")
        if alive > 0 and not self._running:
            logger.info("Orchestrator draining: %d agent(s) still active", alive)
        return alive > 0

    def stop(self) -> None:
        """Signal the run loop to exit after the current tick.

        Also writes SHUTDOWN signal files to all active agents so they can
        save WIP and exit cleanly before the orchestrator terminates.
        """
        self._shutting_down.set()
        self._running = False
        with contextlib.suppress(Exception):
            send_shutdown_signals(self, reason="orchestrator_stopped")

    def is_shutting_down(self) -> bool:
        """Return True when the orchestrator is draining for shutdown."""
        return self._shutting_down.is_set()

    def _drain_before_cleanup(self, timeout_s: float = 10.0) -> None:
        """Stop new work, wait briefly for active agents, then drain executor."""
        if self._executor_drained:
            return

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            active_sessions = [
                session
                for session in self._agents.values()
                if session.status != "dead" and self._spawner.check_alive(session)
            ]
            if not active_sessions:
                break
            time.sleep(0.2)

        try:
            self._executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            self._executor.shutdown(wait=True)
        self._executor_drained = True
        logger.info("Executor drained before cleanup")

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

        from bernstein.core.cost_anomaly import AnomalySignal

        assert isinstance(signal, AnomalySignal)
        self._anomaly_detector.record_signal(signal)
        if signal.action == "kill_agent" and signal.agent_id:
            logger.warning("Anomaly [%s]: %s — killing agent", signal.rule, signal.message)
            session = self._agents.get(signal.agent_id)
            if session:
                with contextlib.suppress(Exception):
                    self._spawner.kill(session)
        elif signal.action == "stop_spawning":
            logger.warning("Anomaly [%s]: %s — stopping new spawns", signal.rule, signal.message)
            self._stop_spawning = True
        else:
            logger.info("Anomaly [%s]: %s", signal.rule, signal.message)

    def _record_live_costs(self) -> None:
        """Update live cost tracker from active agent token usage."""
        any_change = False
        for session in self._agents.values():
            if session.status == "dead" or session.tokens_used <= 0:
                continue

            model_name = session.model_config.model if session.model_config else "sonnet"
            task_id = session.task_ids[0] if session.task_ids else f"live-{session.id}"
            delta_cost = self._cost_tracker.record_cumulative(
                agent_id=session.id,
                task_id=task_id,
                model=model_name,
                total_input_tokens=session.tokens_used,
                total_output_tokens=0,
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
        from bernstein.core.manager import ManagerAgent

        try:
            budget_pct = 1.0
            if self._cost_tracker.budget_usd > 0:
                status = self._cost_tracker.status()
                budget_pct = max(0.0, 1.0 - status.percentage_used)

            workdir = self._workdir
            manager = ManagerAgent(
                server_url=self._config.server_url,
                workdir=workdir,
                templates_dir=get_templates_dir(workdir),
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

            base = self._config.server_url
            for correction in result.corrections:
                try:
                    if correction.action == "reassign" and correction.task_id and correction.new_role:
                        self._client.patch(
                            f"{base}/tasks/{correction.task_id}",
                            json={"role": correction.new_role},
                        )
                        logger.info(
                            "Manager review: reassigned %s to role=%s (%s)",
                            correction.task_id,
                            correction.new_role,
                            correction.reason,
                        )
                    elif correction.action == "change_priority" and correction.task_id and correction.new_priority:
                        self._client.patch(
                            f"{base}/tasks/{correction.task_id}",
                            json={"priority": correction.new_priority},
                        )
                        logger.info(
                            "Manager review: changed priority of %s to %d (%s)",
                            correction.task_id,
                            correction.new_priority,
                            correction.reason,
                        )
                    elif correction.action == "cancel" and correction.task_id:
                        self._client.post(
                            f"{base}/tasks/{correction.task_id}/cancel",
                            json={"reason": correction.reason or "manager review"},
                        )
                        logger.info(
                            "Manager review: cancelled %s (%s)",
                            correction.task_id,
                            correction.reason,
                        )
                    elif correction.action == "add_task" and correction.new_task:
                        self._client.post(
                            f"{base}/tasks",
                            json=correction.new_task,
                        )
                        logger.info(
                            "Manager review: added task %r (%s)",
                            correction.new_task.get("title"),
                            correction.reason,
                        )
                except httpx.HTTPError as exc:
                    logger.warning("Manager review: correction %s failed: %s", correction.action, exc)

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

        # In-memory ownership check — filters out dead agents explicitly.
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
        return should_auto_decompose(task, self._decomposed_task_ids)

    def _auto_decompose_task(self, task: Task) -> None:
        """Delegate to task_lifecycle.auto_decompose_task."""
        auto_decompose_task(
            task,
            client=self._client,
            server_url=self._config.server_url,
            decomposed_task_ids=self._decomposed_task_ids,
        )

    # -- Session and cleanup ------------------------------------------------

    def _save_session_state(self) -> None:
        """Persist session state for fast resume on next start.

        Queries the task server for current task statuses and writes a
        session snapshot to ``.sdd/runtime/session.json``.  Errors are
        silently caught -- session saving is best-effort.
        """
        try:
            from bernstein.core.session import SessionState, save_session

            resp = self._client.get(f"{self._config.server_url}/tasks")
            resp.raise_for_status()
            from typing import cast as _cast_session

            tasks_data: Any = resp.json()
            task_list: list[dict[str, Any]] = []
            if isinstance(tasks_data, list):
                task_list = _cast_session("list[dict[str, Any]]", tasks_data)
            elif isinstance(tasks_data, dict):
                raw_dict = _cast_session("dict[str, Any]", tasks_data)
                task_list = _cast_session("list[dict[str, Any]]", raw_dict.get("tasks", []))

            done_ids: list[str] = [str(t["id"]) for t in task_list if t.get("status") == "done"]
            pending_ids: list[str] = [str(t["id"]) for t in task_list if t.get("status") in ("claimed", "in_progress")]

            state = SessionState(
                saved_at=time.time(),
                goal="",
                completed_task_ids=done_ids,
                pending_task_ids=pending_ids,
                cost_spent=self._cost_tracker.spent_usd,
            )
            save_session(self._workdir, state)
            logger.info("Session state saved (%d done, %d pending)", len(done_ids), len(pending_ids))
        except Exception:
            logger.debug("Failed to save session state (best-effort)", exc_info=True)

    def _cleanup(self) -> None:
        """Release resources held by the orchestrator."""
        # Save session state before releasing resources
        self._save_session_state()

        # SOC 2: generate Merkle seal on shutdown when audit mode is active
        if self._audit_mode and self._audit_log is not None:
            try:
                from bernstein.core.merkle import compute_seal, save_seal

                audit_dir = self._workdir / ".sdd" / "audit"
                merkle_dir = audit_dir / "merkle"
                _tree, seal = compute_seal(audit_dir)
                seal_path = save_seal(seal, merkle_dir)
                logger.info("Merkle audit seal written: %s (root=%s)", seal_path, seal["root_hash"])
            except Exception:
                logger.warning("Merkle seal generation on shutdown failed", exc_info=True)

        # Full git hygiene on shutdown
        try:
            from bernstein.core.git_hygiene import run_hygiene

            run_hygiene(self._workdir, full=True)
        except Exception:
            logger.debug("Git hygiene on shutdown failed (non-critical)", exc_info=True)

        # Stop cluster heartbeat client (unregisters from central server)
        if self._heartbeat_client is not None:
            self._heartbeat_client.stop()
            logger.info("Cluster heartbeat client stopped")

        # Cancel pending futures first
        for future in (self._pending_ruff_future, self._pending_test_future):
            if future is not None and not future.done():
                future.cancel()
        self._pending_ruff_future = None
        self._pending_test_future = None

        # Shut down the thread pool
        if not self._executor_drained:
            try:
                self._executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Python <3.9 doesn't have cancel_futures
                self._executor.shutdown(wait=False)
        logger.info("Executor shut down, background test/ruff processes released")

    def _restart(self) -> None:
        """Replace the current process with a fresh orchestrator."""
        import sys

        logger.info("Exec'ing fresh orchestrator process")
        os.execv(sys.executable, [sys.executable, *sys.argv])

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
        """If evolve mode is on and all tasks are done, trigger a new cycle.

        Args:
            result: Current tick result (mutated in place).
            tasks_by_status: Pre-fetched task snapshot keyed by status string.
        """
        evolve_path = self._workdir / ".sdd" / "runtime" / "evolve.json"
        if not evolve_path.exists():
            return

        try:
            evolve_cfg = json.loads(evolve_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        if not evolve_cfg.get("enabled"):
            return

        # Only trigger when idle: no open/claimed tasks, no alive agents
        open_tasks = tasks_by_status.get("open", [])
        claimed_tasks = tasks_by_status.get("claimed", [])
        alive = sum(1 for a in self._agents.values() if a.status != "dead")
        if open_tasks or claimed_tasks or alive > 0:
            return  # Still working

        # Check cycle limits
        cycle_count = evolve_cfg.get("_cycle_count", 0)
        max_cycles = evolve_cfg.get("max_cycles", 0)
        if max_cycles > 0 and cycle_count >= max_cycles:
            logger.info("Evolve: max cycles (%d) reached, stopping", max_cycles)
            return

        # Check budget cap
        budget_usd = evolve_cfg.get("budget_usd", 0)
        spent_usd = evolve_cfg.get("_spent_usd", 0.0)
        if budget_usd > 0 and spent_usd >= budget_usd:
            logger.info("Evolve: budget cap ($%.2f) reached, stopping", budget_usd)
            return

        # Diminishing returns backoff
        consecutive_empty = evolve_cfg.get("_consecutive_empty", 0)
        backoff_factor = min(2**consecutive_empty, 8) if consecutive_empty >= 3 else 1

        last_cycle_ts = evolve_cfg.get("_last_cycle_ts", 0)
        base_interval = evolve_cfg.get("interval_s", 300)
        effective_interval = base_interval * backoff_factor
        if time.time() - last_cycle_ts < effective_interval:
            return

        cycle_number = cycle_count + 1
        cycle_start = time.time()
        logger.info(
            "Evolve: triggering cycle %d (backoff=%dx, interval=%ds)",
            cycle_number,
            backoff_factor,
            effective_interval,
        )

        # Step 1: ANALYZE
        tasks_completed = len(tasks_by_status.get("done", []))
        tasks_failed = len(tasks_by_status.get("failed", []))

        # Step 2: VERIFY
        test_info = self._evolve_run_tests()

        # Step 3: COMMIT
        committed = self._evolve_auto_commit()

        # Step 3b: GOVERN
        # _governor is always non-None here because _check_evolve only runs
        # when evolve_mode is enabled, and we initialize the governor in that case.
        assert self._governor is not None, "AdaptiveGovernor must be initialized in evolve mode"
        weights_before = self._governor.get_current_weights()
        test_pass_rate = test_info.get("passed", 0) / max(test_info.get("passed", 0) + test_info.get("failed", 0), 1)
        gov_context = ProjectContext(
            cycle_number=cycle_number,
            test_pass_rate=test_pass_rate,
            lint_violations=evolve_cfg.get("_lint_violations", 0),
            security_issues_last_5_cycles=evolve_cfg.get("_security_issues", 0),
            codebase_size_files=evolve_cfg.get("_codebase_files", 0),
            consecutive_empty_cycles=consecutive_empty,
        )
        weights_after, weight_reason = self._governor.adjust_weights(weights_before, gov_context)
        self._governor.persist_weights(weights_after, reason=weight_reason)
        self._governor.log_decision(
            GovernanceEntry(
                cycle=cycle_number,
                timestamp=datetime.now(UTC).isoformat(),
                weights_before=weights_before.to_dict(),
                weights_after=weights_after.to_dict(),
                weight_change_reason=weight_reason,
                proposals_evaluated=tasks_completed + tasks_failed,
                proposals_applied=tasks_completed,
                risk_scores=self._last_cycle_risk_scores,
                outcome_metrics={
                    "test_pass_rate": test_pass_rate,
                    "committed": 1.0 if committed else 0.0,
                },
            )
        )
        logger.info(
            "Evolve: governance cycle %d -- weights adjusted (%s)",
            cycle_number,
            weight_reason,
        )

        # Step 4: PLAN
        focus_areas: list[str] = list(self._EVOLVE_FOCUS_AREAS)
        focus_idx: int = cycle_count % len(focus_areas)
        focus: str = str(focus_areas[focus_idx])
        self._evolve_spawn_manager(
            cycle_number=cycle_number,
            focus_area=focus,
            test_summary=test_info.get("summary", ""),
        )

        # Track diminishing returns
        produced_changes = committed or tasks_completed > 0
        if produced_changes:
            evolve_cfg["_consecutive_empty"] = 0
        else:
            evolve_cfg["_consecutive_empty"] = consecutive_empty + 1

        # Update state
        now = time.time()
        evolve_cfg["_cycle_count"] = cycle_number
        evolve_cfg["_last_cycle_ts"] = now
        with contextlib.suppress(OSError):
            evolve_path.write_text(json.dumps(evolve_cfg))

        # Log cycle metrics
        self._log_evolve_cycle(
            cycle_number,
            now,
            {
                "focus_area": focus,
                "tasks_completed": tasks_completed,
                "tasks_failed": tasks_failed,
                "tests_passed": test_info.get("passed", 0),
                "tests_failed": test_info.get("failed", 0),
                "commits_made": 1 if committed else 0,
                "backoff_factor": backoff_factor,
                "consecutive_empty": evolve_cfg.get("_consecutive_empty", 0),
                "duration_s": round(now - cycle_start, 2),
            },
        )

        self._post_bulletin(
            "status",
            f"evolve cycle {cycle_number} complete: focus={focus}, completed={tasks_completed}, committed={committed}",
        )

    _REPLENISH_COOLDOWN_S: float = 60.0
    _REPLENISH_MAX_TASKS: int = 5

    def _run_ruff_check(self) -> list[RuffViolation]:
        """Run ruff check and return parsed violations (runs in a background thread)."""
        import subprocess

        proc = subprocess.Popen(
            ["uv", "run", "ruff", "check", ".", "--output-format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self._workdir,
            start_new_session=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return []
        return json.loads(stdout) if stdout.strip() else []

    def _create_ruff_tasks(self, violations: list[RuffViolation]) -> None:
        """Create backlog tasks from ruff violations."""
        if not violations:
            logger.debug("Replenish: no ruff violations found, backlog is clean")
            return

        by_rule: dict[str, RuffViolation] = {}
        for v in violations:
            code = (v.get("code") or "unknown").strip()
            if code not in by_rule:
                by_rule[code] = v

        base = self._config.server_url
        created = 0
        for code, v in by_rule.items():
            if created >= self._REPLENISH_MAX_TASKS:
                break
            filename = v.get("filename", "")
            message = v.get("message", "")
            row = v.get("location", {}).get("row", "?")
            task_payload = {
                "title": f"Fix ruff violation {code}",
                "description": (
                    f"Fix all occurrences of ruff rule {code}.\n"
                    f"Example: {filename}:{row} -- {message}\n"
                    f"Run `uv run ruff check . --select {code}` to find all instances."
                ),
                "role": "backend",
                "priority": 3,
                "model": "sonnet",
                "effort": "low",
            }
            try:
                resp = self._client.post(f"{base}/tasks", json=task_payload)
                resp.raise_for_status()
                created += 1
                logger.info("Replenish: created task for ruff rule %s", code)
            except httpx.HTTPError as exc:
                logger.warning("Replenish: failed to create task for %s: %s", code, exc)

        if created:
            logger.info("Replenish: created %d lint-fix task(s)", created)

    def _replenish_backlog(self, result: TickResult) -> None:
        """Create fix tasks from ruff lint violations when evolve mode is idle."""
        if not self._config.evolve_mode:
            return
        if result.open_tasks > 0:
            return

        # Harvest a completed ruff future
        if self._pending_ruff_future is not None:
            if not self._pending_ruff_future.done():
                return  # still running; skip this tick
            try:
                violations: list[RuffViolation] = self._pending_ruff_future.result()
            except (concurrent.futures.CancelledError, RuntimeError) as exc:
                logger.warning("Replenish: ruff check failed: %s", exc)
                self._pending_ruff_future = None
                return
            self._pending_ruff_future = None
            self._create_ruff_tasks(violations)
            return

        # Check cooldown before submitting a new run
        now = time.time()
        if now - self._last_replenish_ts < self._REPLENISH_COOLDOWN_S:
            return

        self._last_replenish_ts = now
        self._pending_ruff_future = self._executor.submit(self._run_ruff_check)
        logger.debug("Replenish: ruff check submitted to background thread")

    def _run_pytest(self) -> TestResults:
        """Run pytest and return parsed results (runs in a background thread)."""
        import subprocess

        info: TestResults = {"passed": 0, "failed": 0, "summary": ""}
        proc = subprocess.Popen(
            ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=line"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=self._workdir,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            info["summary"] = "pytest timed out after 120s"
            logger.warning("Background pytest timed out, killed process group")
            return info

        output = stdout + stderr
        info["summary"] = output.strip().splitlines()[-1] if output.strip() else ""
        match = re.search(r"(\d+) passed", output)
        if match:
            info["passed"] = int(match.group(1))
        match = re.search(r"(\d+) failed", output)
        if match:
            info["failed"] = int(match.group(1))
        return info

    def _evolve_run_tests(self) -> TestResults:
        """Return test results from a background pytest run."""
        info: TestResults = {"passed": 0, "failed": 0, "summary": ""}

        if self._pending_test_future is not None:
            if not self._pending_test_future.done():
                return info
            try:
                info = self._pending_test_future.result()
            except (concurrent.futures.CancelledError, RuntimeError) as exc:
                logger.warning("Evolve: test run failed: %s", exc)
                info["summary"] = f"test run error: {exc}"
            self._pending_test_future = None
            return info

        self._pending_test_future = self._executor.submit(self._run_pytest)
        return info

    @staticmethod
    def _generate_evolve_commit_msg(staged_files: list[str]) -> str:
        """Build a short, descriptive commit message from the list of staged files."""
        if not staged_files:
            return "Evolve: housekeeping"

        LABEL_RULES: list[tuple[str, str]] = [
            ("src/bernstein/cli/dashboard", "improve dashboard"),
            ("src/bernstein/cli/main", "update CLI"),
            ("src/bernstein/cli/cost", "add cost tracking"),
            ("src/bernstein/cli/", "update CLI"),
            ("src/bernstein/core/orchestrator", "fix orchestrator"),
            ("src/bernstein/core/server", "fix server"),
            ("src/bernstein/core/models", "extend models"),
            ("src/bernstein/core/spawner", "fix spawner"),
            ("src/bernstein/core/", "update core"),
            ("src/bernstein/adapters/", "refactor adapters"),
            ("src/bernstein/evolution/", "tune evolution"),
            ("src/bernstein/agents/", "update agents"),
            ("tests/", "update tests"),
            ("docs/", "update docs"),
            ("README", "update README"),
            ("CONTRIBUTING", "update CONTRIBUTING"),
            (".sdd/backlog/", "add backlog tasks"),
        ]

        seen: set[str] = set()
        labels: list[str] = []
        for path in staged_files:
            for prefix, label in LABEL_RULES:
                if prefix in path and label not in seen:
                    seen.add(label)
                    labels.append(label)
                    break

        if not labels:
            first = staged_files[0].split("/")[-1]
            labels = [f"update {first}"]

        summary = "; ".join(labels[:3])
        return f"Evolve: {summary}"

    def _evolve_auto_commit(self) -> bool:
        """Auto-commit and push any uncommitted changes from the last cycle."""
        import subprocess

        from bernstein.core.git_ops import (
            checkout_discard,
            conventional_commit,
            safe_push,
            stage_all_except,
            status_porcelain,
        )

        try:
            changed = status_porcelain(self._workdir)
            if not changed:
                return False

            stage_all_except(self._workdir, exclude=[".sdd/runtime/", ".sdd/metrics/"])

            test_result = subprocess.run(
                ["uv", "run", "pytest", "tests/", "-x", "-q", "--tb=line"],
                capture_output=True,
                text=True,
                cwd=self._workdir,
                timeout=300,
            )
            if test_result.returncode != 0:
                logger.warning("Evolve: tests failed, rolling back changes")
                checkout_discard(self._workdir)
                return False

            result = conventional_commit(self._workdir, evolve=True)
            if not result.ok:
                logger.warning("Evolve: commit failed: %s", result.stderr)
                return False

            safe_push(self._workdir, "main")
            logger.info("Evolve: auto-committed and pushed changes")

            if "src/bernstein/" in changed:
                logger.info("Evolve: own source code changed, signaling restart")
                restart_flag = self._workdir / ".sdd" / "runtime" / "restart_requested"
                restart_flag.parent.mkdir(parents=True, exist_ok=True)
                restart_flag.write_text(str(time.time()))

            return True

        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Evolve: auto-commit failed: %s", exc)
            return False

    def _evolve_spawn_manager(
        self,
        cycle_number: int = 0,
        focus_area: str = "new_features",
        test_summary: str = "",
    ) -> None:
        """Spawn a manager agent to analyze the codebase and create new tasks."""
        base = self._config.server_url

        research_context = ""
        try:
            from bernstein.core.researcher import format_research_context, run_research_sync

            report = run_research_sync(self._workdir)
            research_context = format_research_context(report)
            if research_context:
                logger.info("Evolve: research produced %d bytes of context", len(research_context))
        except Exception as exc:
            logger.debug("Evolve: research unavailable: %s", exc)

        focus_instructions = {
            "new_features": "Focus on missing features that block real usage.",
            "user_interface": (
                "Focus on the CLI dashboard and user-facing experience. "
                "Improve the Textual dashboard (src/bernstein/cli/dashboard.py): "
                "better live metrics display, clearer task status, more useful panels. "
                "Also improve CLI output quality and error messages."
            ),
            "test_coverage": "Focus on test gaps and missing edge-case coverage.",
            "code_quality": "Focus on code smells, type safety, and refactoring.",
            "performance": "Focus on performance bottlenecks and efficiency.",
            "documentation": "Focus on missing docs that block contributors.",
        }
        focus_text = focus_instructions.get(focus_area, "Focus on high-impact improvements.")

        description = (
            f"You are a PRODUCT DIRECTOR in EVOLVE mode (cycle {cycle_number}). "
            "Think strategically: what would make this project genuinely useful "
            "to developers? What do competitors lack? What's the shortest path "
            "to a feature that gets people excited?\n\n"
            "Create tasks for specialist agents to implement. "
            "You plan, they code.\n\n"
            f"## This cycle's focus: {focus_area.replace('_', ' ')}\n"
            f"{focus_text}\n\n"
            + (f"## Current test state\n```\n{test_summary}\n```\n\n" if test_summary else "")
            + "## Rules (from self-evolving systems research)\n"
            "- NEVER create tasks that are cosmetic, trivial, or busy-work\n"
            "- Each task must have a measurable outcome (test passes, "
            "benchmark improves, bug is fixed)\n"
            "- Prefer config/prompt changes over code changes (cheaper, safer)\n"
            "- If tests already pass at 100%, focus on functionality, not more tests\n"
            "- If architecture is clean, focus on features users actually need\n"
            "- Create 3-5 tasks MAX. Quality over quantity.\n\n"
            "## Prioritization\n"
            "1. Bugs and broken functionality (P1)\n"
            "2. Missing features that block real usage (P1)\n"
            "3. Performance and reliability (P2)\n"
            "4. Code quality and test gaps (P2)\n"
            "5. Documentation (P3 -- only if truly missing)\n\n"
            "## Process\n"
            "1. Run `uv run pytest tests/ -q` to see current test state\n"
            "2. Read key files to understand architecture\n"
            "3. Identify 3-5 high-impact improvements\n"
            "4. Create tasks via HTTP. YOU decide model and effort per task:\n"
            f"   curl -X POST {base}/tasks -H 'Content-Type: application/json' \\\n"
            '   -d \'{"title": "...", "description": "...", '
            '"role": "backend", "priority": 2, '
            '"model": "sonnet", "effort": "high"}\'\n\n'
            "## Model/effort selection (you decide per task)\n"
            '- model: "opus" (deep reasoning, slow) or "sonnet" (fast, default)\n'
            '- effort: "max" (100 turns), "high" (50), "medium" (30), "low" (15)\n'
            "- Use sonnet/high for most implementation tasks (fast)\n"
            "- Use opus/max ONLY for complex architecture or security reviews\n"
            "- Use sonnet/low for simple fixes, typos, config changes\n\n"
            "## Task size -- KEEP THEM SMALL\n"
            "Each task MUST be completable in ONE file change, under 10 minutes.\n"
            "BAD: 'Implement entire web research module'\n"
            "GOOD: 'Add Tavily search function to researcher.py'\n"
            "GOOD: 'Add --evolve flag handling to cli/main.py'\n"
            "Break big features into 3-5 atomic file-level tasks.\n\n"
            "## README\n"
            "Every 3rd cycle, create a task to update README.md with:\n"
            "- Current feature state, correct CLI usage, accurate test count.\n\n"
            "5. Then exit.\n\n"
            "IMPORTANT: Do NOT implement changes yourself. Only create tasks."
        )

        if research_context:
            description += research_context

        task_body = {
            "title": f"Evolve cycle {cycle_number}: {focus_area.replace('_', ' ')}",
            "description": description,
            "role": "manager",
            "priority": 1,
            "scope": "medium",
            "complexity": "medium",
        }

        try:
            resp = self._client.post(f"{base}/tasks", json=task_body)
            resp.raise_for_status()
            task_id = resp.json().get("id", "?")
            logger.info("Evolve: created manager task %s (focus=%s)", task_id, focus_area)
        except httpx.HTTPError as exc:
            logger.error("Evolve: failed to create manager task: %s", exc)

    def _log_evolve_cycle(
        self,
        cycle_number: int,
        timestamp: float,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """Append an entry to the evolve_cycles.jsonl log."""
        metrics_dir = self._workdir / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True, exist_ok=True)
        log_path = metrics_dir / "evolve_cycles.jsonl"
        entry: dict[str, Any] = {
            "cycle": cycle_number,
            "timestamp": timestamp,
            "iso_time": datetime.fromtimestamp(timestamp, tz=UTC).isoformat(),
            "tick": self._tick_count,
        }
        if metrics:
            entry.update(metrics)
        try:
            with log_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as exc:
            logger.warning("Evolve: failed to write cycle log: %s", exc)

    # -- Evolution integration -----------------------------------------------

    def make_evolution_loop(self, **kwargs: Any) -> EvolutionLoop:
        """Create an EvolutionLoop wired to this orchestrator's AdaptiveGovernor.

        Passes the orchestrator's governor so the evolution loop shares the
        same weight history and governance log as the orchestrator's evolve
        cycles.  Any extra keyword arguments are forwarded to ``EvolutionLoop``.

        Returns:
            A fully-wired ``EvolutionLoop`` instance.
        """
        from bernstein.evolution.loop import EvolutionLoop

        return EvolutionLoop(
            state_dir=self._workdir / ".sdd",
            repo_root=self._workdir,
            governor=self._governor,
            **kwargs,
        )

    def _run_evolution_cycle(self, result: TickResult) -> None:
        """Run an evolution analysis cycle and create upgrade tasks from proposals."""
        assert self._evolution is not None
        try:
            proposals = self._evolution.run_analysis_cycle()

            # Score each proposal with Strategic Risk Score before routing
            cycle_risk_scores: list[float] = []
            for proposal in proposals:
                target_files = proposal.risk_assessment.affected_components
                # Estimate diff size from description length (heuristic)
                diff_estimate = max(len(proposal.proposed_change) // 10, 10)
                risk_score = self._risk_scorer.score_proposal(
                    target_files=target_files,
                    diff_size=diff_estimate,
                    test_coverage_delta=0.0,  # unknown pre-execution
                )
                cycle_risk_scores.append(risk_score.composite_risk)
                if self._risk_scorer.is_high_risk(risk_score):
                    logger.info(
                        "Proposal %s (%s) flagged high-risk (%.2f) — routing to sandbox",
                        proposal.id,
                        proposal.title,
                        risk_score.composite_risk,
                    )
                else:
                    logger.info(
                        "Proposal %s (%s) low-risk (%.2f) — fast-tracking",
                        proposal.id,
                        proposal.title,
                        risk_score.composite_risk,
                    )
            self._last_cycle_risk_scores = cycle_risk_scores

            # Persist pending proposals
            self._persist_pending_proposals()

            # Execute approved proposals; rollback on failure
            executed = self._evolution.execute_pending_upgrades()
            for proposal in executed:
                logger.info(
                    "Applied upgrade %s: %s (status=%s)",
                    proposal.id,
                    proposal.title,
                    proposal.status.value,
                )

            if not proposals:
                return

            _task_eligible_statuses = {UpgradeStatus.PENDING, UpgradeStatus.APPROVED}
            base = self._config.server_url
            for proposal in proposals:
                if proposal.status not in _task_eligible_statuses:
                    continue
                try:
                    # Score for task priority routing
                    target_files = proposal.risk_assessment.affected_components
                    diff_estimate = max(len(proposal.proposed_change) // 10, 10)
                    risk_score = self._risk_scorer.score_proposal(
                        target_files=target_files,
                        diff_size=diff_estimate,
                        test_coverage_delta=0.0,
                    )
                    is_high = self._risk_scorer.is_high_risk(risk_score)
                    task_body = {
                        "title": f"Upgrade: {proposal.title}",
                        "description": proposal.description,
                        "role": "backend",
                        "priority": 1 if is_high else 2,
                        "scope": "large" if is_high else "medium",
                        "complexity": "high" if is_high else "medium",
                        "estimated_minutes": 60 if is_high else 30,
                        "task_type": TaskType.UPGRADE_PROPOSAL.value,
                    }
                    resp = self._client.post(f"{base}/tasks", json=task_body)
                    resp.raise_for_status()
                    logger.info(
                        "Created upgrade task for proposal %s: %s (risk=%.2f)",
                        proposal.id,
                        proposal.title,
                        risk_score.composite_risk,
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Failed to create upgrade task for proposal %s: %s",
                        proposal.id,
                        exc,
                    )
                    result.errors.append(f"evolution_task: {exc}")
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Evolution analysis cycle failed: %s", exc)
            result.errors.append(f"evolution: {exc}")

    def _persist_pending_proposals(self) -> None:
        """Write pending upgrade proposals to .sdd/upgrades/pending.json."""
        if self._evolution is None:
            return
        upgrades_dir = self._workdir / ".sdd" / "upgrades"
        upgrades_dir.mkdir(parents=True, exist_ok=True)
        pending_path = upgrades_dir / "pending.json"
        pending = self._evolution.get_pending_upgrades()
        data = [
            {
                "id": p.id,
                "title": p.title,
                "category": p.category.value,
                "description": p.description,
                "status": p.status.value,
                "confidence": p.confidence,
                "created_at": p.created_at,
            }
            for p in pending
        ]
        pending_path.write_text(json.dumps(data, indent=2))

    # -- Backlog -------------------------------------------------------------

    def _sync_backlog_file(self, task: Task) -> None:
        """Move the matching .md file from backlog/open/ to backlog/closed/."""
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        if not open_dir.exists():
            return

        closed_dir = self._workdir / ".sdd" / "backlog" / "closed"
        closed_dir.mkdir(parents=True, exist_ok=True)

        title_words = self._backlog_words_from_title(task.title)

        best_match: str | None = None
        best_score = 0
        for md_file in open_dir.glob("*.md"):
            slug = re.sub(r"^\d+-", "", md_file.name[:-3])
            file_words = set(slug.split("-"))
            significant_file_words = {w for w in file_words if len(w) >= 4}
            overlap = title_words & significant_file_words
            if overlap and len(overlap) > best_score:
                best_score = len(overlap)
                best_match = md_file.name

        if best_match is None:
            return

        src = open_dir / best_match
        dst = closed_dir / best_match
        if not src.exists():
            return

        content = src.read_text(encoding="utf-8")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        summary = task.result_summary or ""
        content += f"\n\n---\n**completed**: {ts}\n**task_id**: {task.id}\n**result**: {summary}\n"
        dst.write_text(content, encoding="utf-8")
        src.unlink()
        logger.info("Synced backlog: %s -> closed/", best_match)

    def ingest_backlog(self) -> int:
        """Scan .sdd/backlog/open/ and POST any new files to the task server.

        Returns:
            Number of files ingested this call.
        """
        open_dir = self._workdir / ".sdd" / "backlog" / "open"
        if not open_dir.exists():
            return 0

        claimed_dir = self._workdir / ".sdd" / "backlog" / "claimed"

        count = 0
        for md_file in sorted(open_dir.glob("*.md")):
            if (claimed_dir / md_file.name).exists():
                continue

            content = md_file.read_text(encoding="utf-8")
            payload = parse_backlog_file(md_file.name, content)

            try:
                resp = self._client.post(f"{self._config.server_url}/tasks", json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("ingest_backlog: POST /tasks failed for %s: %s", md_file.name, exc)
                continue

            claimed_dir.mkdir(parents=True, exist_ok=True)
            md_file.rename(claimed_dir / md_file.name)
            count += 1
            logger.info("Ingested backlog file: %s", md_file.name)

        return count

    @staticmethod
    def _backlog_words_from_title(title: str) -> set[str]:
        """Extract significant lowercase words (>=4 chars) from a task title."""
        expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", title)
        tokens = re.split(r"[^a-zA-Z0-9]+", expanded.lower())
        return {w for w in tokens if len(w) >= 4}

    # -- Run summary --------------------------------------------------------

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

        task_lines: list[str] = []
        for task in sorted(done_tasks, key=lambda t: t.title):
            task_lines.append(f"- [x] {task.title}")
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

        lines = [
            "# Run Summary",
            "",
            f"**Total completed:** {total_completed}",
            f"**Total failed:** {total_failed}",
            f"**Files modified:** {files_modified}",
            f"**Estimated cost:** ${total_cost:.4f}",
            f"**Wall-clock duration:** {duration_str}",
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
            "run.completed",
            "Bernstein run complete",
            f"{total_completed} tasks done, {total_failed} failed in {duration_str}.",
            tasks_completed=total_completed,
            tasks_failed=total_failed,
            files_modified=files_modified,
            cost_usd=round(total_cost, 4),
            duration=duration_str,
        )

        generate_retrospective(
            done_tasks=done_tasks,
            failed_tasks=failed_tasks,
            collector=collector,
            runtime_dir=runtime_dir,
            run_start_ts=self._run_start_ts,
        )

        self._emit_summary_card(
            done_tasks=done_tasks,
            failed_tasks=failed_tasks,
            collector=collector,
            wall_clock_s=wall_clock_s,
            total_cost=total_cost,
        )

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

    def _record_tick_events(self, result: TickResult, tasks_by_status: dict[str, list[Task]]) -> None:
        """Record replay events from a completed tick for deterministic replay."""
        # Record spawned agents
        for session_id in result.spawned:
            session = self._agents.get(session_id)
            if session is not None:
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

        # Record verified (completed) tasks
        for task_id in result.verified:
            session = self._find_session_for_task(task_id)
            cost = 0.0
            if session is not None:
                cost = self._cost_tracker.status().spent_usd
            self._recorder.record(
                "task_completed",
                task_id=task_id,
                agent_id=session.id if session else None,
                cost_usd=round(cost, 4),
            )

        # Record verification failures
        for task_id, failed_signals in result.verification_failures:
            self._recorder.record(
                "task_verification_failed",
                task_id=task_id,
                failed_signals=failed_signals,
            )

        # Record reaped agents
        for agent_id in result.reaped:
            self._recorder.record("agent_reaped", agent_id=agent_id)

        # Record retried tasks
        for task_id in result.retried:
            self._recorder.record("task_retried", task_id=task_id)

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
        try:
            with state_path.open("w") as f:
                json.dump({"ts": time.time(), "agents": agents_snapshot}, f)
        except Exception:
            pass


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
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(sys.stderr),
                logging.FileHandler(log_dir / "orchestrator-debug.log"),
            ],
        )

    try:
        # Try to load adapter from seed if available
        adapter_name = args.adapter
        seed_path = workdir / "bernstein.yaml"
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
            except (json.JSONDecodeError, ValueError):
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
            try:
                orchestrator.run()
            finally:
                if mcp_manager is not None:
                    mcp_manager.stop_all()
    except Exception:
        logger.exception("Orchestrator crashed")
        sys.exit(1)
# ---------------------------------------------------------------------------
# Meta-messages for orchestrator nudges (T567)
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field


@dataclass
class OrchestratorNudge:
    """Meta-message for nudging orchestrator behavior."""

    nudge_type: str
    message: str
    priority: int = 1  # 1=low, 2=medium, 3=high
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    acknowledged: bool = False


class OrchestratorNudgeManager:
    """Manages meta-messages for orchestrator nudges."""

    def __init__(self):
        self.nudges: list[OrchestratorNudge] = []
        self._lock = threading.Lock()

    def add_nudge(
        self, nudge_type: str, message: str, priority: int = 1, metadata: dict[str, Any] | None = None
    ) -> OrchestratorNudge:
        """Add a nudge to the orchestrator."""
        nudge = OrchestratorNudge(nudge_type=nudge_type, message=message, priority=priority, metadata=metadata or {})

        with self._lock:
            self.nudges.append(nudge)

        logger.info(f"Orchestrator nudge added: {nudge_type} - {message}")
        return nudge

    def get_pending_nudges(self, priority_threshold: int = 0) -> list[OrchestratorNudge]:
        """Get pending nudges above priority threshold."""
        with self._lock:
            return [nudge for nudge in self.nudges if not nudge.acknowledged and nudge.priority >= priority_threshold]

    def acknowledge_nudge(self, nudge: OrchestratorNudge) -> None:
        """Mark a nudge as acknowledged."""
        with self._lock:
            nudge.acknowledged = True

    def clear_acknowledged(self) -> None:
        """Clear acknowledged nudges."""
        with self._lock:
            self.nudges = [nudge for nudge in self.nudges if not nudge.acknowledged]


# Global nudge manager
_nudge_manager = OrchestratorNudgeManager()


def nudge_orchestrator(
    nudge_type: str, message: str, priority: int = 1, metadata: dict[str, Any] | None = None
) -> OrchestratorNudge:
    """Send a meta-message nudge to the orchestrator (T567)."""
    return _nudge_manager.add_nudge(nudge_type, message, priority, metadata)


def get_orchestrator_nudges(priority_threshold: int = 0) -> list[OrchestratorNudge]:
    """Get pending orchestrator nudges."""
    return _nudge_manager.get_pending_nudges(priority_threshold)
