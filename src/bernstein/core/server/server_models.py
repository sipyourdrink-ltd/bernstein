"""Pydantic request / response schemas for the Bernstein task server.

All BaseModel subclasses used by route handlers live here.
The parent ``server`` module re-exports every name for backward compatibility.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from bernstein.core.bulletin import MessageType  # noqa: TC001 - Pydantic needs at runtime
from bernstein.core.task_store import ProgressEntry  # noqa: TC001 - Pydantic needs at runtime

# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------

_SIGNAL_TYPE = Literal["path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"]


class CompletionSignalSchema(BaseModel):
    """Pydantic schema for a single completion signal in API requests."""

    type: _SIGNAL_TYPE
    value: str


# audit-117: input-size caps for TaskCreate (prevents OOM via 200MB descriptions).
# Titles are short human-readable summaries; descriptions can carry a plan but must
# stay below the per-request body cap (1MB) enforced by ContentLengthMiddleware.
_MAX_TITLE_LEN = 500  # Raised from 200 — real backlog/audit tickets use
# long descriptive titles (audit-169-… runs to 206 chars). 500 still caps
# abusive multi-MB titles but stops ingest_backlog batch POSTs 422-ing every
# 20 s whenever the backlog carries any title > 200.
_MAX_DESCRIPTION_LEN = 100_000
_MAX_SHORT_STR_LEN = 1_000  # role, scope, complexity, etc. — enum-like fields
_MAX_PATH_LEN = 4_096  # owned_files / parent_task_id / depends_on entries
_MAX_LIST_LEN = 100
_MAX_DICT_SERIALIZED_LEN = 50_000  # cap serialized size of dict[str, Any] fields
_MAX_META_MESSAGE_LEN = 10_000  # retry meta_messages — operational hints


def _enforce_dict_size(value: dict[str, Any] | None, *, field_name: str) -> dict[str, Any] | None:
    """Validate that a dict-of-any does not serialize beyond ``_MAX_DICT_SERIALIZED_LEN``.

    Raises ``ValueError`` (surfaced by pydantic as 422) when the JSON form would
    exceed the cap.  Protects slack_context / metadata / upgrade_details from
    unbounded memory usage without forcing callers onto a rigid TypedDict.
    """
    if value is None:
        return value
    import json as _json

    try:
        serialized = _json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serialisable") from exc
    if len(serialized) > _MAX_DICT_SERIALIZED_LEN:
        raise ValueError(
            f"{field_name} exceeds {_MAX_DICT_SERIALIZED_LEN} serialized chars",
        )
    return value


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    # audit-117: bounded string lengths prevent trivial memory exhaustion.
    title: str = Field(max_length=_MAX_TITLE_LEN)
    description: str = Field(max_length=_MAX_DESCRIPTION_LEN)
    role: str = Field(default="auto", max_length=_MAX_SHORT_STR_LEN)
    tenant_id: str = Field(default="default", max_length=_MAX_SHORT_STR_LEN)
    priority: int = 2
    scope: str = Field(default="medium", max_length=_MAX_SHORT_STR_LEN)
    complexity: str = Field(default="medium", max_length=_MAX_SHORT_STR_LEN)
    eu_ai_act_risk: str = Field(default="minimal", max_length=_MAX_SHORT_STR_LEN)
    approval_required: bool = False
    risk_level: str = Field(default="low", max_length=_MAX_SHORT_STR_LEN)
    estimated_minutes: int | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=_MAX_LIST_LEN)
    parent_task_id: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)
    depends_on_repo: str | None = Field(default=None, max_length=_MAX_PATH_LEN)
    owned_files: list[str] = Field(default_factory=list, max_length=_MAX_LIST_LEN)
    cell_id: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)
    repo: str | None = Field(default=None, max_length=_MAX_PATH_LEN)
    task_type: str = Field(default="standard", max_length=_MAX_SHORT_STR_LEN)
    upgrade_details: dict[str, Any] | None = None
    model: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)  # "opus", "sonnet", "haiku"
    effort: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)  # "max", "high", "medium", "low"
    batch_eligible: bool = False  # Non-urgent: eligible for provider batch APIs at ~50% cost
    completion_signals: list[CompletionSignalSchema] = Field(default_factory=list, max_length=_MAX_LIST_LEN)
    slack_context: dict[str, Any] | None = None  # Slack slash command metadata
    metadata: dict[str, Any] = Field(default_factory=dict)  # Trigger-source metadata (e.g. issue_number)
    deadline: float | None = None  # Epoch timestamp when task must be complete
    parent_session_id: str | None = Field(default=None, max_length=_MAX_SHORT_STR_LEN)
    parent_context: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    # Retry bookkeeping (audit-017): retry_count is the single source of truth.
    # When a retry task is created, the orchestrator sets retry_count=previous+1.
    retry_count: int | None = None  # Current retry attempt number (0 = first attempt)
    max_retries: int | None = None  # Per-task override of default retry limit
    retry_delay_s: float | None = None  # Delay between retries (exponential backoff base)
    terminal_reason: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_LEN)
    max_output_tokens: int | None = None  # Per-task output-token cap (escalated on retry)
    meta_messages: list[str] | None = Field(default=None, max_length=_MAX_LIST_LEN)

    # audit-117: cap serialized size of dict-of-any fields to block deeply-nested
    # or very wide payloads from wedging the server at pydantic-validation time.
    def model_post_init(self, _context: Any) -> None:
        """Enforce serialized-size caps on dict fields and meta_messages entries."""
        _enforce_dict_size(self.slack_context, field_name="slack_context")
        _enforce_dict_size(self.metadata, field_name="metadata")
        _enforce_dict_size(self.upgrade_details, field_name="upgrade_details")
        # Cap individual meta_messages strings so a 100-item list can't each be 1MB.
        if self.meta_messages is not None:
            for msg in self.meta_messages:
                if len(msg) > _MAX_META_MESSAGE_LEN:
                    raise ValueError(
                        f"meta_messages entry exceeds {_MAX_META_MESSAGE_LEN} chars",
                    )


class TaskSelfCreate(BaseModel):
    """Body for POST /tasks/self-create — agent-initiated subtask creation.

    Agents use this to decompose work into subtasks during execution.
    The parent_task_id is required and links the new subtask to the calling
    agent's current task.
    """

    parent_task_id: str
    title: str
    description: str
    role: str = "auto"
    priority: int = 2
    scope: str = "medium"
    complexity: str = "medium"
    estimated_minutes: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    owned_files: list[str] = Field(default_factory=list)


class WebhookTaskCreate(TaskCreate):
    """Body for POST /webhook."""

    role: str = "backend"


class TaskResponse(BaseModel):
    """Serialised task returned by every task endpoint."""

    id: str
    title: str
    description: str
    role: str
    tenant_id: str
    priority: int
    scope: str
    complexity: str
    eu_ai_act_risk: str
    approval_required: bool
    risk_level: str
    estimated_minutes: int | None
    status: str
    depends_on: list[str]
    parent_task_id: str | None
    depends_on_repo: str | None
    owned_files: list[str]
    assigned_agent: str | None
    result_summary: str | None
    cell_id: str | None
    repo: str | None
    task_type: str
    upgrade_details: dict[str, Any] | None
    model: str | None
    effort: str | None
    batch_eligible: bool = False
    completion_signals: list[dict[str, str]] = Field(default_factory=lambda: list[dict[str, str]]())
    slack_context: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    claimed_at: float | None = None
    deadline: float | None = None
    progress_log: list[ProgressEntry] = Field(default_factory=list)
    version: int = 1
    parent_session_id: str | None = None  # Coordinator session that owns this task
    # Retry bookkeeping (audit-017): typed fields are the single source of truth.
    retry_count: int = 0
    max_retries: int = 3
    retry_delay_s: float = 0.0
    terminal_reason: str | None = None
    max_output_tokens: int | None = None
    meta_messages: list[str] = Field(default_factory=list)


class WebhookTaskResponse(BaseModel):
    """Serialized task returned by POST /webhook."""

    task: TaskResponse


class TaskCompleteRequest(BaseModel):
    """Body for POST /tasks/{task_id}/complete."""

    result_summary: str


class TaskFailRequest(BaseModel):
    """Body for POST /tasks/{task_id}/fail."""

    reason: str = ""


class TaskCancelRequest(BaseModel):
    """Body for POST /tasks/{task_id}/cancel."""

    reason: str = ""


class TaskBlockRequest(BaseModel):
    """Body for POST /tasks/{task_id}/block."""

    reason: str = ""


class TaskPatchRequest(BaseModel):
    """Body for PATCH /tasks/{task_id} — manager corrections."""

    role: str | None = None
    priority: int | None = None
    model: str | None = None


class TaskProgressRequest(BaseModel):
    """Body for POST /tasks/{task_id}/progress."""

    message: str = ""
    percent: int = 0
    # Structured snapshot fields for stall detection (optional)
    files_changed: int | None = None
    lines_changed: int | None = None
    tests_passing: int | None = None
    errors: int | None = None
    last_file: str = ""
    # Last shell command executed by the agent — used for real-time anomaly detection.
    # Agents report this so the orchestrator can detect dangerous commands (exfiltration,
    # reverse shells, privilege escalation) before the task completes.
    last_command: str = ""


class PartialMergeRequest(BaseModel):
    """Body for POST /tasks/{task_id}/partial-merge.

    Requests an incremental merge of specific files from the agent's branch
    into the main branch before the task finishes.  Only files already
    committed in the agent's worktree branch are processed.
    """

    files: list[str]
    """Repo-relative file paths to merge (must be committed in the agent branch)."""

    message: str = ""
    """Optional commit message.  Auto-generated from session/file list if empty."""


class PartialMergeResponse(BaseModel):
    """Response for POST /tasks/{task_id}/partial-merge."""

    success: bool
    merged_files: list[str]
    skipped_already_merged: list[str]
    uncommitted_files: list[str]
    conflicting_files: list[str]
    commit_sha: str
    error: str


class TaskWaitForSubtasksRequest(BaseModel):
    """Body for POST /tasks/{task_id}/wait-for-subtasks."""

    subtask_count: int = 0


class BatchClaimRequest(BaseModel):
    """Body for POST /tasks/claim-batch."""

    task_ids: list[str]
    agent_id: str
    claimed_by_session: str | None = None


class BatchClaimResponse(BaseModel):
    """Response for POST /tasks/claim-batch."""

    claimed: list[str]
    failed: list[str]


class BatchCreateRequest(BaseModel):
    """Body for POST /tasks/batch."""

    tasks: list[TaskCreate]


class BatchCreateResponse(BaseModel):
    """Response for POST /tasks/batch."""

    created: list[TaskResponse]
    skipped_titles: list[str]


class RoleCounts(BaseModel):
    """Per-role open task counts."""

    role: str
    open: int
    claimed: int
    done: int
    failed: int
    cost_usd: float = 0.0


class StatusResponse(BaseModel):
    """Body for GET /status."""

    total: int
    open: int
    claimed: int
    done: int
    failed: int
    per_role: list[RoleCounts]
    total_cost_usd: float = 0.0


class HeartbeatRequest(BaseModel):
    """Body for POST /agents/{agent_id}/heartbeat."""

    role: str = ""
    status: Literal["starting", "working", "idle", "dead"] = "working"


class HeartbeatResponse(BaseModel):
    """Response for heartbeat."""

    agent_id: str
    acknowledged: bool
    server_ts: float


class ComponentStatus(BaseModel):
    """Status of an individual system component."""

    status: Literal["ok", "degraded", "down", "unknown"]
    detail: str = ""


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: str
    uptime_s: float
    task_count: int
    agent_count: int
    task_queue_depth: int = 0
    memory_mb: float = 0.0
    restart_count: int = 0
    is_readonly: bool = False
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BulletinPostRequest(BaseModel):
    """Body for POST /bulletin."""

    agent_id: str
    type: MessageType = "status"
    content: str
    cell_id: str | None = None


# -- Cluster schemas -------------------------------------------------------


class NodeCapacitySchema(BaseModel):
    """Advertised capacity of a cluster node."""

    max_agents: int = 6
    available_slots: int = 6
    active_agents: int = 0
    gpu_available: bool = False
    supported_models: list[str] = Field(default_factory=lambda: ["sonnet", "opus", "haiku"])


class NodeRegisterRequest(BaseModel):
    """Body for POST /cluster/nodes."""

    name: str = ""
    url: str = ""
    capacity: NodeCapacitySchema = Field(default_factory=NodeCapacitySchema)
    labels: dict[str, str] = Field(default_factory=dict)
    cell_ids: list[str] = Field(default_factory=list)


class NodeHeartbeatRequest(BaseModel):
    """Body for POST /cluster/nodes/{node_id}/heartbeat."""

    capacity: NodeCapacitySchema | None = None


class NodeResponse(BaseModel):
    """Serialised node in API responses."""

    id: str
    name: str
    url: str
    status: str
    capacity: NodeCapacitySchema
    last_heartbeat: float
    registered_at: float
    labels: dict[str, str]
    cell_ids: list[str]


class ClusterStatusResponse(BaseModel):
    """Response for GET /cluster/status."""

    topology: str
    total_nodes: int
    online_nodes: int
    offline_nodes: int
    total_capacity: int
    available_slots: int
    active_agents: int
    nodes: list[NodeResponse]


class TaskStealRequest(BaseModel):
    """Body for POST /cluster/steal — report queue depths and request rebalancing."""

    queue_depths: dict[str, int] = Field(default_factory=dict)


class TaskStealAction(BaseModel):
    """A single steal action: move tasks from donor to receiver."""

    donor_node_id: str
    receiver_node_id: str
    task_ids: list[str]


class TaskStealResponse(BaseModel):
    """Response for POST /cluster/steal."""

    actions: list[TaskStealAction]
    total_stolen: int


class TaskCountsResponse(BaseModel):
    """Lightweight status counts — no task bodies."""

    open: int = 0
    claimed: int = 0
    done: int = 0
    failed: int = 0
    blocked: int = 0
    cancelled: int = 0
    total: int = 0


class PaginatedTasksResponse(BaseModel):
    """Paginated list of tasks with total count for cursor math."""

    tasks: list[TaskResponse]
    total: int
    limit: int
    offset: int


class BulletinMessageResponse(BaseModel):
    """Single bulletin message in responses."""

    agent_id: str
    type: str
    content: str
    timestamp: float
    cell_id: str | None


class AgentLogsResponse(BaseModel):
    """Response for GET /agents/{session_id}/logs."""

    session_id: str
    content: str
    size: int


class AgentKillResponse(BaseModel):
    """Response for POST /agents/{session_id}/kill."""

    session_id: str
    kill_requested: bool


# -- Delegation schemas ----------------------------------------------------


class DelegationPostRequest(BaseModel):
    """Body for POST /delegations."""

    origin_agent: str
    target_role: str
    description: str
    deadline: float = 0.0
    cell_id: str | None = None


class DelegationClaimRequest(BaseModel):
    """Body for POST /delegations/{id}/claim."""

    agent_id: str


class DelegationResultRequest(BaseModel):
    """Body for POST /delegations/{id}/result."""

    agent_id: str
    result: str


class DelegationResponse(BaseModel):
    """Single delegation in API responses."""

    id: str
    origin_agent: str
    target_role: str
    description: str
    deadline: float
    status: str
    claimed_by: str | None
    result: str | None
    created_at: float
    cell_id: str | None


# -- Direct channel schemas ------------------------------------------------


class ChannelQueryRequest(BaseModel):
    """Body for POST /channel/query."""

    sender_agent: str
    topic: str
    content: str
    target_agent: str | None = None
    target_role: str | None = None
    ttl_seconds: float = 300


class ChannelResponseRequest(BaseModel):
    """Body for POST /channel/{query_id}/respond."""

    responder_agent: str
    content: str


class ChannelQueryResponse(BaseModel):
    """Single channel query in API responses."""

    id: str
    sender_agent: str
    topic: str
    content: str
    target_agent: str | None
    target_role: str | None
    timestamp: float
    expires_at: float
    resolved: bool


class ChannelResponseResponse(BaseModel):
    """Single channel response in API responses."""

    id: str
    query_id: str
    responder_agent: str
    content: str
    timestamp: float


# -- A2A protocol schemas --------------------------------------------------


class A2ATaskSendRequest(BaseModel):
    """Body for POST /a2a/tasks/send — receive a task from an external A2A agent."""

    sender: str
    message: str
    role: str = "backend"


class A2AArtifactRequest(BaseModel):
    """Body for POST /a2a/tasks/{id}/artifacts — attach an artifact."""

    name: str
    data: str = ""
    content_type: str = "text/plain"


class A2AArtifactResponse(BaseModel):
    """Single artifact in responses."""

    name: str
    content_type: str
    data: str
    created_at: float


class A2AMessageRequest(BaseModel):
    """Body for POST /a2a/message."""

    sender: str
    recipient: str
    content: str
    task_id: str


class A2AMessageResponse(BaseModel):
    """Serialized A2A message returned by Bernstein endpoints."""

    id: str
    sender: str
    recipient: str
    content: str
    task_id: str
    direction: str
    delivered: bool
    external_endpoint: str | None
    created_at: float


class A2ATaskResponse(BaseModel):
    """Serialised A2A task in responses."""

    id: str
    bernstein_task_id: str | None
    sender: str
    message: str
    status: str
    artifacts: list[A2AArtifactResponse]
    created_at: float
    updated_at: float


class A2AAgentCardResponse(BaseModel):
    """Agent Card response for /.well-known/agent.json."""

    name: str
    description: str
    capabilities: list[str]
    protocol_version: str
    endpoint: str
    provider: str
