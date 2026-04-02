"""Core data models for tasks, agents, and cells."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Any, Literal

logger = logging.getLogger(__name__)


class TaskStoreUnavailable(Exception):
    """Raised when the task store cannot operate after exhausting retries.

    The orchestrator should catch this and degrade gracefully (e.g. switch
    to read-only mode or pause task dispatch).
    """


class KillReason(StrEnum):
    """Reasons for killing an agent session."""

    MANUAL = "manual"
    SCOPE_VIOLATION = "scope_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    GUARDRAIL_VIOLATION = "guardrail_violation"


class TransitionReason(StrEnum):
    """Canonical reasons for lifecycle transitions in the runtime pipeline."""

    COMPLETED = "completed"
    ABORTED = "aborted"
    RETRY = "retry"
    PROMPT_TOO_LONG = "prompt_too_long"
    MAX_OUTPUT_TOKENS = "max_output_tokens"
    MAX_TURNS = "max_turns"
    PROVIDER_413 = "provider_413"
    PROVIDER_529 = "provider_529"
    COMPACTION_FAILED = "compaction_failed"
    STOP_HOOK_BLOCKED = "stop_hook_blocked"
    PERMISSION_DENIED = "permission_denied"
    SIBLING_ABORTED = "sibling_aborted"
    ORPHAN_RECOVERED = "orphan_recovered"


class AbortReason(StrEnum):
    """Canonical reasons for abnormal agent termination."""

    USER_INTERRUPT = "user_interrupt"
    SHUTDOWN_SIGNAL = "shutdown_signal"
    TIMEOUT = "timeout"
    OOM = "oom"
    PERMISSION_DENIED = "permission_denied"
    PROVIDER_ERROR = "provider_error"
    BASH_ERROR = "bash_error"
    SIBLING_ABORTED = "sibling_aborted"
    PARENT_ABORTED = "parent_aborted"
    COMPACT_FAILURE = "compact_failure"
    UNKNOWN = "unknown"


class ProviderType(Enum):
    """Supported API provider types."""

    CLAUDE = "claude"
    CURSOR = "cursor"
    GEMINI = "gemini"
    CODEX = "codex"
    QWEN = "qwen"
    KIRO = "kiro"
    KILO = "kilo"
    OPENCODE = "opencode"


class ApiTier(Enum):
    """API subscription tiers."""

    FREE = "free"
    PLUS = "plus"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    UNLIMITED = "unlimited"


@dataclass(frozen=True)
class RateLimit:
    """Rate limit configuration for an API tier."""

    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    tokens_per_day: int | None = None


@dataclass(frozen=True)
class CostStructure:
    """Cost structure for an API tier."""

    input_cost_per_1k_tokens: float = 0.0
    output_cost_per_1k_tokens: float = 0.0
    monthly_subscription: float = 0.0
    overage_cost_per_1k_tokens: float = 0.0


@dataclass(frozen=True)
class ApiTierInfo:
    """Information about an API tier and remaining quota."""

    provider: ProviderType
    tier: ApiTier
    rate_limit: RateLimit | None = None
    cost_structure: CostStructure | None = None
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    reset_timestamp: int | None = None  # Unix timestamp for rate limit reset
    is_active: bool = True


class Scope(Enum):
    SMALL = "small"  # <30 min, single file
    MEDIUM = "medium"  # 30-120 min, few files
    LARGE = "large"  # 2-8 hours, subsystem


class Complexity(Enum):
    LOW = "low"  # Docs, formatting, simple fixes
    MEDIUM = "medium"  # Feature implementation, tests
    HIGH = "high"  # Architecture, complex reasoning, security


class TaskStatus(Enum):
    PLANNED = "planned"  # Awaiting human approval before execution (plan mode)
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_FOR_SUBTASKS = "waiting_for_subtasks"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"  # Agent crashed mid-task; pending crash recovery
    PENDING_APPROVAL = "pending_approval"  # Completed; awaiting human approval before taking effect


class TaskType(Enum):
    """Type of task for categorization and prioritization."""

    STANDARD = "standard"  # Regular implementation task
    UPGRADE_PROPOSAL = "upgrade_proposal"  # Self-evolution upgrade suggestion
    FIX = "fix"  # Bug fix or janitor-created fix
    RESEARCH = "research"  # Research/exploration task


@dataclass(frozen=True)
class RiskAssessment:
    """Risk assessment for upgrade proposals.

    Attributes:
        level: Overall risk level (low, medium, high, critical).
        breaking_changes: Whether this introduces breaking changes.
        affected_components: List of components that may be affected.
        mitigation: Suggested mitigation strategies.
    """

    level: Literal["low", "medium", "high", "critical"] = "medium"
    breaking_changes: bool = False
    affected_components: list[str] = field(default_factory=list[str])
    mitigation: str = ""


@dataclass(frozen=True)
class RollbackPlan:
    """Rollback plan for upgrade proposals.

    Attributes:
        steps: Step-by-step rollback instructions.
        revert_commit: Git commit hash to revert to (if applicable).
        data_migration: Any data migration rollback steps.
        estimated_rollback_minutes: Time estimate for rollback.
    """

    steps: list[str] = field(default_factory=list[str])
    revert_commit: str | None = None
    data_migration: str = ""
    estimated_rollback_minutes: int = 30


@dataclass(frozen=True)
class UpgradeProposalDetails:
    """Details specific to upgrade proposal tasks.

    Attributes:
        current_state: Description of current implementation.
        proposed_change: Description of the proposed upgrade.
        benefits: Expected benefits of the upgrade.
        risk_assessment: Risk analysis of the upgrade.
        rollback_plan: How to revert if the upgrade fails.
        cost_estimate_usd: Estimated cost impact in USD.
        performance_impact: Expected performance impact description.
    """

    current_state: str = ""
    proposed_change: str = ""
    benefits: list[str] = field(default_factory=list[str])
    risk_assessment: RiskAssessment = field(default_factory=RiskAssessment)
    rollback_plan: RollbackPlan = field(default_factory=RollbackPlan)
    cost_estimate_usd: float = 0.0
    performance_impact: str = ""


@dataclass(frozen=True)
class CompletionSignal:
    """Janitor signal for automatic task verification."""

    type: Literal["path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"]
    value: str  # path, glob pattern, test command, search string, or review instruction


@dataclass
class Task:
    """A unit of work for an agent."""

    id: str
    title: str
    description: str
    role: str  # Which specialist role
    priority: int = 2  # 1=critical, 2=normal, 3=nice-to-have
    scope: Scope = Scope.MEDIUM
    complexity: Complexity = Complexity.MEDIUM
    estimated_minutes: int = 30
    status: TaskStatus = TaskStatus.OPEN
    task_type: TaskType = TaskType.STANDARD  # Type of task
    upgrade_details: UpgradeProposalDetails | None = None  # For upgrade proposals
    depends_on: list[str] = field(default_factory=list[str])
    parent_task_id: str | None = None
    completion_signals: list[CompletionSignal] = field(default_factory=list[CompletionSignal])
    owned_files: list[str] = field(default_factory=list[str])
    assigned_agent: str | None = None
    result_summary: str | None = None
    tenant_id: str = "default"
    cell_id: str | None = None  # Which cell this task belongs to
    repo: str | None = None  # Target repo in a multi-repo workspace
    depends_on_repo: str | None = None  # Cross-repo dependency source repo, used with depends_on task IDs
    # Manager-specified routing hints (override auto-routing when set)
    model: str | None = None  # "opus", "sonnet", "haiku"
    effort: str | None = None  # "max", "high", "medium", "low"
    mcp_servers: list[str] = field(default_factory=list[str])  # MCP server names for this task
    slack_context: dict[str, Any] | None = None  # Slack slash command or event metadata
    batch_eligible: bool | None = None  # Non-urgent: None=auto-detect, True=explicit batch, False=explicit realtime
    eu_ai_act_risk: Literal["minimal", "limited", "high", "unacceptable"] = "minimal"
    approval_required: bool = False  # Pause after completion until explicitly approved
    risk_level: Literal["low", "medium", "high", "critical"] = "low"  # Risk for approval workflow routing
    sensitivity: Literal["public", "internal", "confidential"] = "internal"  # Data classification level
    max_output_tokens: int | None = None  # Escalated limit for model output
    meta_messages: list[str] = field(default_factory=list[str])  # Operational nudges/hints (T423)
    created_at: float = field(default_factory=time.time)
    progress_log: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])  # [{timestamp, message, percent}]
    version: int = 1  # Optimistic locking: incremented on every status change

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Task:
        """Deserialise a server JSON response into a Task.

        Args:
            raw: Dict from the task server JSON response.

        Returns:
            Populated Task dataclass.
        """
        task_type = TaskType.STANDARD
        if "task_type" in raw:
            try:
                task_type = TaskType(raw["task_type"])
            except ValueError:
                logger.warning("Invalid task_type %r from server", raw["task_type"])

        signals: list[CompletionSignal] = []
        for sig in raw.get("completion_signals", []):
            try:
                signals.append(CompletionSignal(type=sig["type"], value=sig["value"]))
            except (KeyError, TypeError):
                logger.warning("Invalid completion_signal entry: %r", sig)

        upgrade_details: UpgradeProposalDetails | None = None
        raw_upgrade = raw.get("upgrade_details")
        if raw_upgrade:
            risk = RiskAssessment(**raw_upgrade.get("risk_assessment", {}))
            rollback = RollbackPlan(**raw_upgrade.get("rollback_plan", {}))
            upgrade_details = UpgradeProposalDetails(
                current_state=raw_upgrade.get("current_state", ""),
                proposed_change=raw_upgrade.get("proposed_change", ""),
                benefits=raw_upgrade.get("benefits", []),
                risk_assessment=risk,
                rollback_plan=rollback,
                cost_estimate_usd=raw_upgrade.get("cost_estimate_usd", 0.0),
                performance_impact=raw_upgrade.get("performance_impact", ""),
            )

        return cls(
            id=raw["id"],
            title=raw["title"],
            description=raw["description"],
            role=raw["role"],
            priority=raw.get("priority", 2),
            scope=Scope(raw.get("scope", "medium")),
            complexity=Complexity(raw.get("complexity", "medium")),
            estimated_minutes=raw.get("estimated_minutes", 30),
            status=TaskStatus(raw.get("status", "open")),
            task_type=task_type,
            upgrade_details=upgrade_details,
            depends_on=raw.get("depends_on", []),
            parent_task_id=raw.get("parent_task_id"),
            completion_signals=signals,
            owned_files=raw.get("owned_files", []),
            assigned_agent=raw.get("assigned_agent"),
            result_summary=raw.get("result_summary"),
            tenant_id=str(raw.get("tenant_id", "default") or "default"),
            cell_id=raw.get("cell_id"),
            repo=raw.get("repo"),
            depends_on_repo=raw.get("depends_on_repo"),
            model=raw.get("model"),
            effort=raw.get("effort"),
            mcp_servers=list(raw.get("mcp_servers", [])),
            batch_eligible=(lambda v: None if v is None else bool(v))(raw.get("batch_eligible")),
            eu_ai_act_risk=raw.get("eu_ai_act_risk", "minimal"),
            approval_required=bool(raw.get("approval_required", False)),
            risk_level=raw.get("risk_level", "low"),
            max_output_tokens=raw.get("max_output_tokens"),
            meta_messages=list(raw.get("meta_messages", [])),
            created_at=raw.get("created_at", time.time()),
            progress_log=list(raw.get("progress_log", [])),
            version=raw.get("version", 1),
        )


@dataclass(frozen=True)
class JudgeVerdict:
    """Result of an LLM judge evaluation of task completion."""

    verdict: Literal["accept", "retry"]
    confidence: float  # 0.0 to 1.0
    feedback: str
    flagged_for_review: bool = False  # True when confidence < 0.7


@dataclass
class GuardrailResult:
    """Result of a single guardrail check on an agent's diff.

    Attributes:
        check: Check name (e.g. "secret_detection", "scope_enforcement").
        passed: Whether the check passed.
        blocked: True if this is a hard block (merge must not proceed).
        detail: Human-readable description of findings.
        files: Files involved in any violation.
    """

    check: str
    passed: bool
    blocked: bool
    detail: str
    files: list[str] = field(default_factory=list[str])


@dataclass
class JanitorResult:
    """Result of a janitor run for a single task."""

    task_id: str
    passed: bool
    signal_results: list[tuple[str, bool, str]]  # (signal_desc, passed, detail)
    fix_tasks_created: list[str] = field(default_factory=list[str])  # IDs of created fix tasks
    judge_verdict: JudgeVerdict | None = None  # Set when llm_judge signal was evaluated
    pr_url: str | None = None  # PR URL if created after successful verification
    guardrail_results: list[GuardrailResult] = field(
        default_factory=list[GuardrailResult]
    )  # Pre-merge guardrail checks


@dataclass(frozen=True)
class ModelConfig:
    """Which model and effort to use for a task."""

    model: str  # e.g. "opus", "sonnet", "gpt-4.1"
    effort: str  # e.g. "max", "high", "normal"
    max_tokens: int = 200_000
    is_batch: bool = False  # Use provider batch API (~50% cost reduction) for non-urgent tasks
    aliases: list[str] = field(default_factory=list)  # Other names this model answers to


# ---------------------------------------------------------------------------
# Cost tracking models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCostSummary:
    """Per-agent cost accumulation across a run.

    Attributes:
        agent_id: The agent session identifier.
        total_cost_usd: Sum of all costs incurred by this agent.
        task_count: Number of tasks (invocations) recorded for this agent.
        model_breakdown: Mapping of model name → cost in USD for that model.
    """

    agent_id: str
    total_cost_usd: float
    task_count: int
    model_breakdown: dict[str, float]


@dataclass(frozen=True)
class ModelCostBreakdown:
    """Per-model cost breakdown across a run.

    Attributes:
        model: Model name (e.g. ``"sonnet"``, ``"opus"``).
        total_cost_usd: Sum of all costs incurred using this model.
        total_tokens: Total input + output tokens consumed by this model.
        invocation_count: Number of times this model was invoked.
    """

    model: str
    total_cost_usd: float
    total_tokens: int
    invocation_count: int


@dataclass(frozen=True)
class RunCostProjection:
    """Estimated final cost for an ongoing run.

    Computes a simple linear projection: ``projected_total = current +
    avg_cost_per_task * tasks_remaining``.  Confidence grows toward 1.0 as
    more tasks complete and the per-task average stabilises.

    Attributes:
        run_id: Orchestrator run identifier.
        tasks_done: Tasks completed so far.
        tasks_remaining: Tasks still in backlog / in-progress.
        current_cost_usd: Cost incurred so far.
        projected_total_usd: Estimated final cost if the run completes.
        avg_cost_per_task_usd: Average cost per completed task (0 if no data).
        budget_usd: Budget cap (0 = unlimited).
        within_budget: True when projected total does not exceed the cap.
        confidence: 0.0-1.0; reaches 1.0 after 5 completed tasks.
    """

    run_id: str
    tasks_done: int
    tasks_remaining: int
    current_cost_usd: float
    projected_total_usd: float
    avg_cost_per_task_usd: float
    budget_usd: float
    within_budget: bool
    confidence: float


@dataclass
class RunCostReport:
    """Aggregated cost report for a run, suitable for persistence.

    Attributes:
        run_id: Orchestrator run identifier.
        total_spent_usd: Total cost incurred in this run.
        budget_usd: Budget cap (0 = unlimited).
        per_agent: Per-agent cost summaries, sorted by spend descending.
        per_model: Per-model cost breakdowns, sorted by spend descending.
        projection: Run-end cost projection, or ``None`` if no task counts provided.
        timestamp: Unix timestamp when this report was generated.
    """

    run_id: str
    total_spent_usd: float
    budget_usd: float
    per_agent: list[AgentCostSummary]
    per_model: list[ModelCostBreakdown]
    projection: RunCostProjection | None
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        proj: dict[str, Any] | None = None
        if self.projection is not None:
            p = self.projection
            proj = {
                "run_id": p.run_id,
                "tasks_done": p.tasks_done,
                "tasks_remaining": p.tasks_remaining,
                "current_cost_usd": p.current_cost_usd,
                "projected_total_usd": p.projected_total_usd,
                "avg_cost_per_task_usd": p.avg_cost_per_task_usd,
                "budget_usd": p.budget_usd,
                "within_budget": p.within_budget,
                "confidence": p.confidence,
            }
        return {
            "run_id": self.run_id,
            "total_spent_usd": self.total_spent_usd,
            "budget_usd": self.budget_usd,
            "timestamp": self.timestamp,
            "per_agent": [
                {
                    "agent_id": a.agent_id,
                    "total_cost_usd": a.total_cost_usd,
                    "task_count": a.task_count,
                    "model_breakdown": a.model_breakdown,
                }
                for a in self.per_agent
            ],
            "per_model": [
                {
                    "model": m.model,
                    "total_cost_usd": m.total_cost_usd,
                    "total_tokens": m.total_tokens,
                    "invocation_count": m.invocation_count,
                }
                for m in self.per_model
            ],
            "projection": proj,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RunCostReport:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        per_agent = [
            AgentCostSummary(
                agent_id=a["agent_id"],
                total_cost_usd=float(a["total_cost_usd"]),
                task_count=int(a["task_count"]),
                model_breakdown={k: float(v) for k, v in a.get("model_breakdown", {}).items()},
            )
            for a in d.get("per_agent", [])
        ]
        per_model = [
            ModelCostBreakdown(
                model=m["model"],
                total_cost_usd=float(m["total_cost_usd"]),
                total_tokens=int(m["total_tokens"]),
                invocation_count=int(m["invocation_count"]),
            )
            for m in d.get("per_model", [])
        ]
        proj: RunCostProjection | None = None
        if d.get("projection"):
            p = d["projection"]
            proj = RunCostProjection(
                run_id=str(p["run_id"]),
                tasks_done=int(p["tasks_done"]),
                tasks_remaining=int(p["tasks_remaining"]),
                current_cost_usd=float(p["current_cost_usd"]),
                projected_total_usd=float(p["projected_total_usd"]),
                avg_cost_per_task_usd=float(p["avg_cost_per_task_usd"]),
                budget_usd=float(p["budget_usd"]),
                within_budget=bool(p["within_budget"]),
                confidence=float(p["confidence"]),
            )
        return cls(
            run_id=str(d["run_id"]),
            total_spent_usd=float(d.get("total_spent_usd", 0.0)),
            budget_usd=float(d.get("budget_usd", 0.0)),
            per_agent=per_agent,
            per_model=per_model,
            projection=proj,
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass
class ProgressSnapshot:
    """A point-in-time progress snapshot reported by an agent.

    Agents POST this to the task server every 60 seconds.  The orchestrator
    compares consecutive snapshots to detect stalled agents.

    Attributes:
        timestamp: Unix timestamp when the snapshot was written.
        files_changed: Number of files modified since the agent started.
        tests_passing: Number of tests currently passing (-1 = unknown).
        errors: Number of active errors / compilation failures.
        last_file: Last file the agent was editing (empty string if unknown).
    """

    timestamp: float
    files_changed: int = 0
    tests_passing: int = -1
    errors: int = 0
    last_file: str = ""

    def is_same_progress(self, other: ProgressSnapshot) -> bool:
        """Return True if *other* shows the same progress as self.

        Compares the meaningful counters; ignores timestamp and last_file
        (a new file open without other changes does not count as progress).
        """
        return (
            self.files_changed == other.files_changed
            and self.tests_passing == other.tests_passing
            and self.errors == other.errors
        )


@dataclass
class AgentHeartbeat:
    """Heartbeat written by an agent to signal it is still making progress.

    Agents write this to `.sdd/runtime/heartbeats/{session_id}.json` every
    30 seconds so the orchestrator can detect stuck agents.

    Attributes:
        timestamp: Unix timestamp when the heartbeat was written.
        files_changed: Number of files modified since the agent started.
        status: Agent self-reported status ("working", "idle", "stuck").
        current_file: File currently being edited, or empty string.
        phase: Optional richer phase label ("planning", "implementing", "testing").
        progress_pct: Optional rough completion percentage (0-100).
        message: Optional human-readable status message.
    """

    timestamp: float
    files_changed: int = 0
    status: str = "working"
    current_file: str = ""
    phase: str = ""
    progress_pct: int = 0
    message: str = ""


@dataclass
class AgentSession:
    """A running agent instance."""

    id: str
    role: str
    trace_id: str = ""  # Correlation ID across task -> agent -> gate -> merge
    pid: int | None = None
    task_ids: list[str] = field(default_factory=list[str])
    model_config: ModelConfig = field(default_factory=lambda: ModelConfig("sonnet", "high"))
    heartbeat_ts: float = 0.0
    spawn_ts: float = field(default_factory=time.time)
    status: Literal["starting", "working", "idle", "dead"] = "starting"
    exit_code: int | None = None  # Process exit code once known; None while still running
    cell_id: str | None = None  # Which cell this agent belongs to
    provider: str | None = None  # Provider selected by TierAwareRouter
    agent_source: str = "built-in"  # "catalog", "agency", or "built-in"
    timeout_s: int | None = None  # Per-agent wall-clock timeout; None = use OrchestratorConfig default
    log_path: str = ""  # Path to agent log file for live streaming
    tokens_used: int = 0  # Running total of input+output tokens consumed by this agent
    token_budget: int = 0  # Per-task token budget computed from scope (0 = unlimited)
    context_window_tokens: int = 0  # Provider/model max context window for utilization tracking
    context_utilization_pct: float = 0.0  # Percentage of the context window currently consumed
    context_utilization_alert: bool = False  # True when utilization crosses the warning threshold
    parent_id: str | None = None  # ID of the agent session that spawned this one (delegation tree)
    isolation: str = "none"  # "none", "worktree", or "container"
    container_id: str | None = None  # Container ID when isolation=container
    runtime_backend: Literal["local", "openclaw"] = "local"
    bridge_session_key: str | None = None
    bridge_run_id: str | None = None
    transition_reason: TransitionReason | None = None
    abort_reason: AbortReason | None = None
    abort_detail: str = ""
    finish_reason: str = ""
    meta_messages: list[str] = field(default_factory=list[str])  # Operational nudges/hints (T423)


class IsolationMode(StrEnum):
    """Agent isolation mode."""

    NONE = "none"
    WORKTREE = "worktree"
    CONTAINER = "container"


@dataclass(frozen=True)
class ContainerIsolationConfig:
    """Container isolation settings for the orchestrator.

    Attributes:
        enabled: Whether container isolation is active.
        runtime: Container runtime backend ("docker", "podman", "gvisor", "firecracker").
        image: Container image for agent execution.
        cpu_cores: CPU core limit per container. None = unlimited.
        memory_mb: Memory limit in MB per container. None = unlimited.
        pids_limit: Max processes per container. None = unlimited.
        network_mode: Network mode ("host", "bridge", "none").
        drop_capabilities: Linux capabilities to drop.
        read_only_rootfs: Mount root filesystem as read-only.
        auto_build_image: Build agent image if not found.
        two_phase_sandbox: Enable Codex-style two-phase execution.  Phase 1
            runs with network access to install deps; Phase 2 runs the agent
            with the network fully disabled.
        sandbox_setup_commands: Override auto-detected Phase 1 commands.
            Empty tuple (default) triggers auto-detection from the workspace.
    """

    enabled: bool = False
    runtime: str = "docker"
    image: str = "bernstein-agent:latest"
    cpu_cores: float | None = 2.0
    memory_mb: int | None = 4096
    pids_limit: int | None = 256
    network_mode: str = "host"
    drop_capabilities: tuple[str, ...] = (
        "NET_RAW",
        "SYS_ADMIN",
        "SYS_PTRACE",
        "MKNOD",
    )
    read_only_rootfs: bool = False
    auto_build_image: bool = True
    two_phase_sandbox: bool = False
    sandbox_setup_commands: tuple[str, ...] = ()


@dataclass
class Cell:
    """A self-contained team unit: 1 manager + N workers."""

    id: str
    name: str
    manager: AgentSession | None = None
    workers: list[AgentSession] = field(default_factory=list[AgentSession])
    max_workers: int = 6
    task_queue: list[Task] = field(default_factory=list[Task])


@dataclass(frozen=True)
class TelemetryConfig:
    """OpenTelemetry configuration.

    Attributes:
        otlp_endpoint: Target OTLP collector URL (e.g. http://localhost:4317).
            If None, telemetry is disabled.
    """

    otlp_endpoint: str | None = None


@dataclass(frozen=True)
class RAGConfig:
    """Smart context injection configuration.

    Attributes:
        max_files: Maximum number of relevant files to inject.
        max_tokens: Maximum tokens to use for injected context.
    """

    max_files: int = 5
    max_tokens: int = 50000


@dataclass
class CostAnomalyConfig:
    """Configuration for cost anomaly detection.

    Args:
        enabled: Whether cost anomaly detection is active.
        per_task_multiplier: Alert when task cost exceeds this multiple of tier median.
        per_task_critical_multiplier: Kill agent when cost exceeds this multiple.
        budget_warn_pct: Log warning when spend exceeds this % of budget.
        budget_stop_pct: Stop spawning when spend exceeds this %.
        token_ratio_max: Flag when output/input token ratio exceeds this.
        token_ratio_min_tokens: Minimum total tokens before ratio check applies.
        retry_cost_multiplier: Stop retrying when cumulative retry cost exceeds
            this multiple of the original attempt.
        baseline_window: Number of recent tasks to keep for baseline statistics.
        baseline_min_samples: Minimum samples per tier before ceiling checks activate.
    """

    enabled: bool = True
    per_task_multiplier: float = 3.0
    per_task_critical_multiplier: float = 6.0
    budget_warn_pct: float = 60.0
    budget_stop_pct: float = 90.0
    token_ratio_max: float = 5.0
    token_ratio_min_tokens: int = 5000
    retry_cost_multiplier: float = 2.0
    baseline_window: int = 50
    baseline_min_samples: int = 5


@dataclass(frozen=True)
class BatchConfig:
    """Provider batch execution configuration."""

    enabled: bool = False
    eligible: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class TestAgentConfig:
    """Configuration for auto-spawning paired test tasks."""

    always_spawn: bool = False
    model: str = "sonnet"
    trigger: Literal["on_task_complete"] = "on_task_complete"


@dataclass(frozen=True)
class OpenClawBridgeConfig:
    """Typed OpenClaw Gateway bridge configuration.

    Attributes:
        enabled: Whether the OpenClaw bridge should be considered for spawns.
        url: Gateway WebSocket URL.
        api_key: Gateway bearer token.
        agent_id: Target OpenClaw agent identifier.
        workspace_mode: Supported deployment mode for this bridge ticket.
        fallback_to_local: Whether Bernstein may fall back to the local CLI
            adapter before the remote run is accepted by the gateway.
        connect_timeout_s: WebSocket connect timeout.
        request_timeout_s: Per-request timeout for bridge RPC calls.
        session_prefix: Prefix used when deriving Bernstein-owned session keys.
        max_log_bytes: Maximum bytes served by logs().
        model_override: Optional model override passed to the remote agent.
    """

    enabled: bool = False
    url: str = ""
    api_key: str = field(default="", repr=False)
    agent_id: str = ""
    workspace_mode: Literal["shared_workspace"] = "shared_workspace"
    fallback_to_local: bool = True
    connect_timeout_s: float = 10.0
    request_timeout_s: float = 30.0
    session_prefix: str = "bernstein-"
    max_log_bytes: int = 1_048_576
    model_override: str | None = None


@dataclass(frozen=True)
class BridgeConfigSet:
    """Optional runtime bridge configuration set."""

    openclaw: OpenClawBridgeConfig | None = None


@dataclass
class ApprovalWorkflowConfig:
    """Risk-based approval workflow configuration.

    Attributes:
        enabled: Whether the approval workflow is active.
        high_risk: Gate for high-risk tasks ("auto", "pr", "review").
        medium_risk: Gate for medium-risk tasks.
        low_risk: Gate for low-risk tasks.
        timeout_hours: Auto-reject tasks if approval takes longer than this.
        notify_channels: Where to send approval notifications.
    """

    enabled: bool = True
    high_risk: str = "review"
    medium_risk: str = "pr"
    low_risk: str = "auto"
    timeout_hours: int = 24
    notify_channels: list[str] = field(default_factory=lambda: ["slack", "email"])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalWorkflowConfig:
        return cls(
            enabled=bool(data.get("enabled", True)),
            high_risk=str(data.get("high_risk", "review")),
            medium_risk=str(data.get("medium_risk", "pr")),
            low_risk=str(data.get("low_risk", "auto")),
            timeout_hours=int(data.get("timeout_hours", 24)),
            notify_channels=list(data.get("notify_channels", ["slack", "email"])),
        )


@dataclass
class SmtpConfig:
    """SMTP configuration for email notifications (T207)."""

    host: str
    port: int
    username: str
    password: str
    from_address: str
    to_addresses: list[str]


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator main loop.

    Args:
        max_agents: Maximum concurrent agent processes.
        poll_interval_s: Seconds between orchestrator ticks.
        heartbeat_timeout_s: Seconds before an agent is considered stale.
        heartbeat_enabled: Whether the file-based heartbeat protocol is enabled.
        max_tasks_per_agent: Maximum tasks batched into one agent spawn.
        server_url: Base URL of the Bernstein task server.
        evolution_enabled: Whether the self-evolution feedback loop is active.
        evolution_tick_interval: Run evolution analysis every N ticks (~1.5 min at 3s poll).
        max_task_retries: Max times a task is re-queued after agent crash (0 = no retry).
        cross_model_verify: Cross-model verification config (None = disabled).
        telemetry: OpenTelemetry configuration.
        smtp: SMTP configuration for email notifications.
    """

    max_agents: int = 6
    poll_interval_s: int = 3
    smtp: SmtpConfig | None = None
    heartbeat_timeout_s: int = 900  # 15 min — generous until agents implement heartbeat writes
    heartbeat_enabled: bool = True
    max_agent_runtime_s: int = 1800  # 30 min wall-clock kill (agents need time for complex tasks)
    max_tasks_per_agent: int = 1  # one task per agent = focused, fast
    server_url: str = "http://localhost:8052"
    evolution_enabled: bool = True
    evolution_tick_interval: int = 30
    max_task_retries: int = 2
    kill_on_memory_leak: bool = False
    evolve_mode: bool = False
    budget_usd: float = 0.0  # Stop spawning when cumulative cost reaches this (0 = unlimited)
    dry_run: bool = False  # Preview planned spawns without actually spawning agents
    auth_token: str | None = None  # Bearer token for authenticated API calls
    merge_strategy: str = "pr"  # "pr" | "direct" — how agent work reaches the main branch
    auto_merge: bool = True  # Auto-merge PR after code review passes (requires gh CLI)
    pr_labels: list[str] = field(default_factory=lambda: ["bernstein", "auto-generated"])
    approval: Any = "auto"  # "auto" | "review" | "pr" or dict for workflow
    approval_workflow: ApprovalWorkflowConfig | None = field(default=None, repr=False)
    recovery: str = "resume"  # "resume" | "restart" | "escalate" — crash recovery strategy
    max_crash_retries: int = 2  # Max times to resume in same worktree before escalating
    cross_model_verify: Any | None = None  # CrossModelVerifierConfig | None
    force_parallel: bool = False  # Skip complexity advisor — always decompose/parallelize
    plan_mode: bool = False  # When True, tasks start as PLANNED and require approval before execution
    workflow: str | None = None  # "governed" activates governed workflow mode; None = adaptive (default)
    container_isolation: ContainerIsolationConfig = field(
        default_factory=ContainerIsolationConfig,
    )  # Container-based agent isolation settings
    compliance: Any | None = None  # ComplianceConfig | None — compliance preset configuration
    max_tokens_per_task: dict[str, int] = field(
        default_factory=lambda: {"small": 10_000, "medium": 50_000, "large": 200_000},
    )  # Per-task token budget by scope; agents warned at 80%, hard-killed at 2x
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    ab_test: bool = False
    rag: RAGConfig = field(default_factory=RAGConfig)
    cost_anomaly: CostAnomalyConfig = field(default_factory=CostAnomalyConfig)
    batch: BatchConfig = field(default_factory=BatchConfig)
    max_cost_per_agent: float = 0.0  # Hard per-agent spend cap (0 = unlimited)
    test_agent: TestAgentConfig = field(default_factory=TestAgentConfig)

    def __post_init__(self) -> None:
        """Parse nested workflow config if dict provided."""
        if isinstance(self.approval, dict):
            self.approval_workflow = ApprovalWorkflowConfig.from_dict(self.approval)
            self.approval = "workflow"


# ---------------------------------------------------------------------------
# Plan mode models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskCostEstimate:
    """Estimated cost and risk for a single task before execution.

    Attributes:
        task_id: Server-assigned task ID.
        title: Task title.
        role: Agent role.
        model: Estimated model to use.
        estimated_tokens: Estimated total tokens (input + output).
        estimated_cost_usd: Estimated cost in USD.
        risk_level: Risk classification (low, medium, high, critical).
        risk_reasons: Why this task was classified at this risk level.
    """

    task_id: str
    title: str
    role: str
    model: str = "sonnet"
    estimated_tokens: int = 0
    estimated_cost_usd: float = 0.0
    risk_level: Literal["low", "medium", "high", "critical"] = "low"
    risk_reasons: list[str] = field(default_factory=list[str])


class PlanStatus(Enum):
    """Status of a task execution plan."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class TaskPlan:
    """A plan grouping tasks with cost estimates for human approval.

    Created when plan_mode is enabled. Tasks start as PLANNED and are
    promoted to OPEN only after the human approves the plan.

    Attributes:
        id: Unique plan identifier.
        goal: The original goal that generated this plan.
        task_estimates: Per-task cost and risk estimates.
        total_estimated_cost_usd: Sum of all task cost estimates.
        total_estimated_minutes: Sum of all task time estimates.
        high_risk_tasks: IDs of tasks classified as high or critical risk.
        status: Current plan status.
        created_at: Unix timestamp when the plan was created.
        decided_at: Unix timestamp when the plan was approved/rejected.
        decision_reason: Optional reason provided by the human.
    """

    id: str
    goal: str
    task_estimates: list[TaskCostEstimate] = field(default_factory=list[TaskCostEstimate])
    total_estimated_cost_usd: float = 0.0
    total_estimated_minutes: int = 0
    high_risk_tasks: list[str] = field(default_factory=list[str])
    status: PlanStatus = PlanStatus.PENDING
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None
    decision_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "id": self.id,
            "goal": self.goal,
            "task_estimates": [
                {
                    "task_id": e.task_id,
                    "title": e.title,
                    "role": e.role,
                    "model": e.model,
                    "estimated_tokens": e.estimated_tokens,
                    "estimated_cost_usd": round(e.estimated_cost_usd, 6),
                    "risk_level": e.risk_level,
                    "risk_reasons": list(e.risk_reasons),
                }
                for e in self.task_estimates
            ],
            "total_estimated_cost_usd": round(self.total_estimated_cost_usd, 6),
            "total_estimated_minutes": self.total_estimated_minutes,
            "high_risk_tasks": self.high_risk_tasks,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision_reason": self.decision_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskPlan:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        estimates = [
            TaskCostEstimate(
                task_id=e["task_id"],
                title=e["title"],
                role=e["role"],
                model=e.get("model", "sonnet"),
                estimated_tokens=int(e.get("estimated_tokens", 0)),
                estimated_cost_usd=float(e.get("estimated_cost_usd", 0.0)),
                risk_level=e.get("risk_level", "low"),
                risk_reasons=list(e.get("risk_reasons", [])),
            )
            for e in d.get("task_estimates", [])
        ]
        return cls(
            id=d["id"],
            goal=d.get("goal", ""),
            task_estimates=estimates,
            total_estimated_cost_usd=float(d.get("total_estimated_cost_usd", 0.0)),
            total_estimated_minutes=int(d.get("total_estimated_minutes", 0)),
            high_risk_tasks=list(d.get("high_risk_tasks", [])),
            status=PlanStatus(d.get("status", "pending")),
            created_at=float(d.get("created_at", 0.0)),
            decided_at=d.get("decided_at"),
            decision_reason=d.get("decision_reason", ""),
        )


# ---------------------------------------------------------------------------
# Cluster / distributed coordination models
# ---------------------------------------------------------------------------


class NodeStatus(Enum):
    """Status of a cluster node."""

    ONLINE = "online"
    DEGRADED = "degraded"  # Responding but over capacity / errors
    OFFLINE = "offline"


class ClusterTopology(Enum):
    """Cluster topology mode."""

    STAR = "star"  # One central server, N worker nodes (default)
    MESH = "mesh"  # Any node can serve tasks, gossip sync
    HIERARCHICAL = "hierarchical"  # VP -> cell-leaders -> workers


@dataclass
class NodeCapacity:
    """Advertised capacity of a cluster node."""

    max_agents: int = 6
    available_slots: int = 6
    active_agents: int = 0
    gpu_available: bool = False
    supported_models: list[str] = field(default_factory=lambda: ["sonnet", "opus", "haiku"])


@dataclass
class NodeInfo:
    """A registered node in the Bernstein cluster."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    url: str = ""  # Base URL of this node's orchestrator (for callbacks)
    capacity: NodeCapacity = field(default_factory=NodeCapacity)
    status: NodeStatus = NodeStatus.ONLINE
    last_heartbeat: float = field(default_factory=time.time)
    registered_at: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict[str, str])  # e.g. {"gpu": "true", "region": "us-east"}
    cell_ids: list[str] = field(default_factory=list[str])  # Cells running on this node

    def is_alive(self, timeout_s: float = 60.0) -> bool:
        """Check if the node has sent a heartbeat within timeout."""
        return time.time() - self.last_heartbeat < timeout_s


@dataclass(frozen=True)
class ClusterConfig:
    """Configuration for distributed cluster mode.

    Attributes:
        enabled: Whether cluster mode is active.
        topology: Cluster topology mode.
        auth_token: Shared bearer token for inter-node auth.
        node_heartbeat_interval_s: Seconds between node heartbeats.
        node_timeout_s: Seconds before a node is considered offline.
        server_url: URL of the central task server (for worker nodes).
        bind_host: Host to bind the server to (0.0.0.0 for remote access).
    """

    enabled: bool = False
    topology: ClusterTopology = ClusterTopology.STAR
    auth_token: str | None = None
    node_heartbeat_interval_s: int = 15
    node_timeout_s: int = 60
    server_url: str | None = None  # Central server URL (worker nodes connect here)
    bind_host: str = "127.0.0.1"  # Default: localhost only


# ---------------------------------------------------------------------------
# Trigger / event-driven models
# ---------------------------------------------------------------------------


@dataclass
class TriggerEvent:
    """A normalized event from any trigger source (GitHub, Slack, cron, etc.)."""

    source: str  # "github", "slack", "cron", "webhook", etc.
    timestamp: float = field(default_factory=time.time)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    repo: str = ""  # Repository identifier (e.g. "owner/repo")
    branch: str = ""  # Git branch name
    sha: str = ""  # Git commit SHA
    sender: str = ""  # User or actor that triggered the event
    changed_files: tuple[str, ...] = ()  # Files affected by this event
    message: str = ""  # Human-readable event summary
    metadata: dict[str, Any] = field(default_factory=dict)  # Source-specific extra data


@dataclass
class TriggerTaskTemplate:
    """Template for the task to create when a trigger fires."""

    title: str = "Triggered task"
    role: str = "backend"
    priority: int = 2
    scope: str = "small"
    task_type: str = "standard"
    description_template: str = ""
    model: str | None = None
    effort: str | None = None
    model_escalation: dict[int, dict[str, str]] = field(default_factory=dict)


@dataclass
class TriggerConfig:
    """A single trigger rule loaded from triggers.yaml."""

    name: str
    source: str  # "github", "slack", "cron", "webhook"
    enabled: bool = True
    filters: dict[str, Any] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    task: TriggerTaskTemplate = field(default_factory=TriggerTaskTemplate)
    schedule: str | None = None  # Cron expression (source=cron only)


@dataclass
class TriggerFireRecord:
    """Audit record written when a trigger fires and creates a task."""

    trigger_name: str
    source: str
    fired_at: float
    task_id: str
    dedup_key: str
    event_summary: str = ""


@dataclass(frozen=True)
class LifecycleEvent:
    """Typed event emitted on every task or agent status transition.

    This is the single source of truth for replay, audit, and metrics.
    Every status change — task or agent — produces exactly one event.

    Attributes:
        timestamp: Unix epoch when the transition occurred.
        entity_type: "task" or "agent".
        entity_id: ID of the task or agent session.
        from_status: Status before the transition.
        to_status: Status after the transition.
        actor: Who/what triggered the transition (e.g. "task_store", "spawner").
        reason: Human-readable explanation of why the transition happened.
    """

    timestamp: float
    entity_type: Literal["task", "agent"]
    entity_id: str
    from_status: str
    to_status: str
    actor: str = ""
    reason: str = ""
    transition_reason: TransitionReason | None = None
    abort_reason: AbortReason | None = None


@dataclass(frozen=True)
class WorkflowPhaseEvent:
    """Event emitted on workflow phase transitions in governed mode.

    Extends the lifecycle event concept for workflow-level state changes.
    The full sequence of LifecycleEvent + WorkflowPhaseEvent records is
    sufficient to replay any governed run.

    Attributes:
        timestamp: Unix epoch when the phase transition occurred.
        workflow_hash: SHA-256 hash of the workflow definition.
        run_id: Orchestration run identifier.
        from_phase: Phase before the transition (empty string for initial phase).
        to_phase: Phase after the transition.
        reason: Human-readable explanation (e.g. "all phase tasks completed").
        tasks_completed: Task IDs that completed to trigger this transition.
    """

    timestamp: float
    workflow_hash: str
    run_id: str
    from_phase: str
    to_phase: str
    reason: str = ""
    tasks_completed: tuple[str, ...] = ()


@dataclass(frozen=True)
class VoteEvent:
    """Event emitted for each vote cast and for the final voting result.

    Attributes:
        timestamp: Unix epoch when this event was created.
        task_id: ID of the task being reviewed.
        voter_model: Model that cast this vote (empty string for final-result events).
        verdict: Individual vote verdict or final consensus verdict.
        confidence: Confidence score 0.0-1.0.
        reasoning: One-sentence rationale.
        is_final: True when this event records the aggregated voting result.
        strategy: VotingStrategy value used (e.g. "quorum").
    """

    timestamp: float
    task_id: str
    voter_model: str
    verdict: Literal["approve", "request_changes", "abstain"]
    confidence: float
    reasoning: str
    is_final: bool = False
    strategy: str = ""
