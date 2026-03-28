"""Core data models for tasks, agents, and cells."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    """Supported API provider types."""

    CLAUDE = "claude"
    GEMINI = "gemini"
    CODEX = "codex"
    QWEN = "qwen"


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
    OPEN = "open"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


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
    completion_signals: list[CompletionSignal] = field(default_factory=list[CompletionSignal])
    owned_files: list[str] = field(default_factory=list[str])
    assigned_agent: str | None = None
    result_summary: str | None = None
    cell_id: str | None = None  # Which cell this task belongs to
    repo: str | None = None  # Target repo in a multi-repo workspace
    # Manager-specified routing hints (override auto-routing when set)
    model: str | None = None  # "opus", "sonnet", "haiku"
    effort: str | None = None  # "max", "high", "medium", "low"
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
            completion_signals=signals,
            owned_files=raw.get("owned_files", []),
            assigned_agent=raw.get("assigned_agent"),
            result_summary=raw.get("result_summary"),
            cell_id=raw.get("cell_id"),
            repo=raw.get("repo"),
            model=raw.get("model"),
            effort=raw.get("effort"),
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
class JanitorResult:
    """Result of a janitor run for a single task."""

    task_id: str
    passed: bool
    signal_results: list[tuple[str, bool, str]]  # (signal_desc, passed, detail)
    fix_tasks_created: list[str] = field(default_factory=list[str])  # IDs of created fix tasks
    judge_verdict: JudgeVerdict | None = None  # Set when llm_judge signal was evaluated


@dataclass(frozen=True)
class ModelConfig:
    """Which model and effort to use for a task."""

    model: str  # e.g. "opus", "sonnet", "gpt-4.1"
    effort: str  # e.g. "max", "high", "normal"
    max_tokens: int = 200_000


@dataclass
class AgentSession:
    """A running agent instance."""

    id: str
    role: str
    pid: int | None = None
    task_ids: list[str] = field(default_factory=list[str])
    model_config: ModelConfig = field(default_factory=lambda: ModelConfig("sonnet", "high"))
    heartbeat_ts: float = 0.0
    spawn_ts: float = field(default_factory=time.time)
    status: Literal["starting", "working", "idle", "dead"] = "starting"
    cell_id: str | None = None  # Which cell this agent belongs to
    provider: str | None = None  # Provider selected by TierAwareRouter
    agent_source: str = "built-in"  # "catalog", "agency", or "built-in"
    timeout_s: int | None = None  # Per-agent wall-clock timeout; None = use OrchestratorConfig default


@dataclass
class Cell:
    """A self-contained team unit: 1 manager + N workers."""

    id: str
    name: str
    manager: AgentSession | None = None
    workers: list[AgentSession] = field(default_factory=list[AgentSession])
    max_workers: int = 6
    task_queue: list[Task] = field(default_factory=list[Task])


@dataclass
class OrchestratorConfig:
    """Configuration for the orchestrator main loop.

    Args:
        max_agents: Maximum concurrent agent processes.
        poll_interval_s: Seconds between orchestrator ticks.
        heartbeat_timeout_s: Seconds before an agent is considered stale.
        max_tasks_per_agent: Maximum tasks batched into one agent spawn.
        server_url: Base URL of the Bernstein task server.
        evolution_enabled: Whether the self-evolution feedback loop is active.
        evolution_tick_interval: Run evolution analysis every N ticks (~1.5 min at 3s poll).
        max_task_retries: Max times a task is re-queued after agent crash (0 = no retry).
    """

    max_agents: int = 6
    poll_interval_s: int = 3
    heartbeat_timeout_s: int = 900  # effectively disabled — agents can't heartbeat
    max_agent_runtime_s: int = 600  # 10 min wall-clock kill
    max_tasks_per_agent: int = 1  # one task per agent = focused, fast
    server_url: str = "http://localhost:8052"
    evolution_enabled: bool = True
    evolution_tick_interval: int = 30
    max_task_retries: int = 2
    evolve_mode: bool = False
    budget_usd: float = 0.0  # Stop spawning when cumulative cost reaches this (0 = unlimited)
    dry_run: bool = False  # Preview planned spawns without actually spawning agents
    auth_token: str | None = None  # Bearer token for authenticated API calls


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
