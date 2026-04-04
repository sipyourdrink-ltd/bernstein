"""FastAPI task server — central coordination point for all agents.

Agents pull tasks via HTTP, report completion, and send heartbeats.
State is held in-memory and flushed periodically to JSONL for persistence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.a2a import A2AHandler
from bernstein.core.access_log import StructuredAccessLogMiddleware
from bernstein.core.acp import ACPHandler
from bernstein.core.auth_rate_limiter import RequestRateLimitMiddleware
from bernstein.core.bulletin import BulletinBoard, MessageBoard, MessageType
from bernstein.core.cluster import NodeRegistry
from bernstein.core.json_logging import setup_json_logging
from bernstein.core.models import (
    ClusterConfig,
    NodeInfo,
    Task,
)
from bernstein.core.task_store import (
    ProgressEntry,
    TaskStore,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from starlette.responses import Response as StarletteResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth middleware — bearer token validation
# ---------------------------------------------------------------------------

# Paths that are always accessible without auth (health checks, agent card)
_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/live",
        "/ready",
        "/alive",
        "/.well-known/agent.json",
        "/.well-known/acp.json",
        "/acp/v0/agents",
        "/docs",
        "/openapi.json",
        "/webhook",
        "/webhooks/github",
        "/webhooks/slack/commands",
        "/webhooks/slack/events",
        "/dashboard",
        "/dashboard/data",
        "/dashboard/file_locks",
        "/events",
    }
)

# Path prefixes that are always accessible without auth.
# Used for routes with path parameters (e.g. /hooks/{session_id}).
_PUBLIC_PATH_PREFIXES = ("/hooks/",)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on all requests when auth is configured.

    When ``auth_token`` is set, every request must include a matching
    ``Authorization: Bearer <token>`` header. Health and discovery
    endpoints are exempt.
    """

    def __init__(self, app: Any, auth_token: str | None = None) -> None:
        super().__init__(app)
        self._token = auth_token

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        if self._token is None:
            response: StarletteResponse = await call_next(request)
            return response

        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES):
            response = await call_next(request)
            return response

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )
        token = auth_header[7:]  # Strip "Bearer "
        if token != self._token:
            return JSONResponse(
                status_code=403,
                content={"detail": "Invalid auth token"},
            )
        response = await call_next(request)
        return response


# Write methods that mutate state
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """Block all write operations when the server is in read-only mode.

    Useful for public demo deployments where the dashboard should be
    visible but task mutation must be disabled entirely.  All GET/HEAD/OPTIONS
    requests pass through; any write method returns 405.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        if request.method in _WRITE_METHODS:
            return JSONResponse(
                status_code=405,
                content={"detail": "Server is in read-only mode"},
                headers={"Allow": "GET, HEAD, OPTIONS"},
            )
        response: StarletteResponse = await call_next(request)
        return response


class CrashGuardMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions so they return 500 instead of crashing uvicorn.

    Without this, a single bad request (e.g. OOM in a route handler,
    unexpected None, missing key) can kill the entire server process.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        try:
            return await call_next(request)
        except Exception:
            import logging as _logging

            _logging.getLogger(__name__).exception("Unhandled exception in %s %s", request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error (crash guard caught)"},
            )


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Restrict task server access to allowed IP ranges.

    When ``allowed_ips`` is set, all requests must originate from
    an allowed IP range (CIDR notation). Localhost (127.0.0.1) is
    always allowed. Health and discovery endpoints are exempt.

    Args:
        app: FastAPI application.
        allowed_ips: List of allowed IP ranges in CIDR notation (e.g., ["10.0.0.0/8"]).
    """

    def __init__(self, app: Any, allowed_ips: list[str] | None = None) -> None:
        super().__init__(app)
        self._allowed_ips = allowed_ips
        self._allowed_networks: list[Any] = []
        if allowed_ips:
            import ipaddress
            from contextlib import suppress

            for ip_range in allowed_ips:
                with suppress(ValueError):
                    self._allowed_networks.append(ipaddress.ip_network(ip_range, strict=False))

    def _get_networks(self, request: Request) -> list[Any]:
        """Resolve allowed networks from constructor or seed_config."""
        if self._allowed_networks:
            return self._allowed_networks
        seed_config = getattr(request.app.state, "seed_config", None)
        network_cfg = getattr(seed_config, "network", None)
        allowed_ips = getattr(network_cfg, "allowed_ips", None)
        if not allowed_ips:
            return []
        import ipaddress
        from contextlib import suppress

        nets: list[Any] = []
        for ip_range in allowed_ips:
            with suppress(ValueError):
                nets.append(ipaddress.ip_network(ip_range, strict=False))
        return nets

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        networks = self._get_networks(request)
        if not networks:
            response: StarletteResponse = await call_next(request)
            return response

        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        if client_ip in ("127.0.0.1", "::1", "localhost"):
            response = await call_next(request)
            return response

        if path in _PUBLIC_PATHS:
            response = await call_next(request)
            return response

        try:
            import ipaddress

            client_addr = ipaddress.ip_address(client_ip)
            if any(client_addr in network for network in networks):
                response = await call_next(request)
                return response
        except ValueError:
            pass

        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )


# TypedDicts and related types are now imported from task_store module


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------

_SIGNAL_TYPE = Literal["path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"]


class CompletionSignalSchema(BaseModel):
    """Pydantic schema for a single completion signal in API requests."""

    type: _SIGNAL_TYPE
    value: str


class TaskCreate(BaseModel):
    """Body for POST /tasks."""

    title: str
    description: str
    role: str = "auto"
    tenant_id: str = "default"
    priority: int = 2
    scope: str = "medium"
    complexity: str = "medium"
    eu_ai_act_risk: str = "minimal"
    approval_required: bool = False
    risk_level: str = "low"
    estimated_minutes: int | None = None
    depends_on: list[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    depends_on_repo: str | None = None
    owned_files: list[str] = Field(default_factory=list)
    cell_id: str | None = None
    repo: str | None = None
    task_type: str = "standard"
    upgrade_details: dict[str, Any] | None = None
    model: str | None = None  # Manager hint: "opus", "sonnet", "haiku"
    effort: str | None = None  # Manager hint: "max", "high", "medium", "low"
    batch_eligible: bool = False  # Non-urgent: eligible for provider batch APIs at ~50% cost
    completion_signals: list[CompletionSignalSchema] = Field(default_factory=lambda: list[CompletionSignalSchema]())
    slack_context: dict[str, Any] | None = None  # Slack slash command metadata
    deadline: float | None = None  # Epoch timestamp when task must be complete


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
    created_at: float
    deadline: float | None = None
    progress_log: list[ProgressEntry] = Field(default_factory=lambda: list[ProgressEntry]())
    version: int = 1


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
    tests_passing: int | None = None
    errors: int | None = None
    last_file: str = ""


class TaskWaitForSubtasksRequest(BaseModel):
    """Body for POST /tasks/{task_id}/wait-for-subtasks."""

    subtask_count: int = 0


class BatchClaimRequest(BaseModel):
    """Body for POST /tasks/claim-batch."""

    task_ids: list[str]
    agent_id: str


class BatchClaimResponse(BaseModel):
    """Response for POST /tasks/claim-batch."""

    claimed: list[str]
    failed: list[str]


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


# TaskStore is now imported from task_store.py (facade pattern for existing imports)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# _parse_upgrade_dict is now imported from task_store.py


def a2a_task_to_response(task: Any) -> A2ATaskResponse:
    """Convert an A2ATask to its Pydantic response model."""
    return A2ATaskResponse(
        id=task.id,
        bernstein_task_id=task.bernstein_task_id,
        sender=task.sender,
        message=task.message,
        status=task.status.value,
        artifacts=[
            A2AArtifactResponse(
                name=a.name,
                content_type=a.content_type,
                data=a.data,
                created_at=a.created_at,
            )
            for a in task.artifacts
        ],
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def a2a_message_to_response(message: Any) -> A2AMessageResponse:
    """Convert an A2A message record to its response schema."""

    return A2AMessageResponse(
        id=message.id,
        sender=message.sender,
        recipient=message.recipient,
        content=message.content,
        task_id=message.task_id,
        direction=message.direction,
        delivered=message.delivered,
        external_endpoint=message.external_endpoint,
        created_at=message.created_at,
    )


def node_to_response(node: NodeInfo) -> NodeResponse:
    """Convert a NodeInfo to a Pydantic response model."""
    return NodeResponse(
        id=node.id,
        name=node.name,
        url=node.url,
        status=node.status.value,
        capacity=NodeCapacitySchema(
            max_agents=node.capacity.max_agents,
            available_slots=node.capacity.available_slots,
            active_agents=node.capacity.active_agents,
            gpu_available=node.capacity.gpu_available,
            supported_models=node.capacity.supported_models,
        ),
        last_heartbeat=node.last_heartbeat,
        registered_at=node.registered_at,
        labels=node.labels,
        cell_ids=node.cell_ids,
    )


def task_to_response(task: Task) -> TaskResponse:
    """Convert a domain Task to a Pydantic response model."""
    return TaskResponse(
        id=task.id,
        title=task.title,
        description=task.description,
        role=task.role,
        tenant_id=task.tenant_id,
        priority=task.priority,
        scope=task.scope.value,
        complexity=task.complexity.value,
        eu_ai_act_risk=task.eu_ai_act_risk,
        approval_required=task.approval_required,
        risk_level=task.risk_level,
        estimated_minutes=task.estimated_minutes,
        status=task.status.value,
        depends_on=task.depends_on,
        parent_task_id=task.parent_task_id,
        depends_on_repo=task.depends_on_repo,
        owned_files=task.owned_files,
        assigned_agent=task.assigned_agent,
        result_summary=task.result_summary,
        cell_id=task.cell_id,
        repo=task.repo,
        task_type=task.task_type.value,
        upgrade_details=asdict(task.upgrade_details) if task.upgrade_details else None,
        model=task.model,
        effort=task.effort,
        batch_eligible=task.batch_eligible,
        completion_signals=[{"type": s.type, "value": s.value} for s in task.completion_signals],
        slack_context=task.slack_context,
        created_at=task.created_at,
        progress_log=list(cast("list[ProgressEntry]", task.progress_log)),  # type: ignore[reportUnknownMemberType]
        version=task.version,
    )


# ---------------------------------------------------------------------------
# SSE event bus — fan-out to all connected dashboard clients
# ---------------------------------------------------------------------------


class SSEBus:
    """Fan-out event bus for Server-Sent Events.

    Each connected client gets its own asyncio.Queue.  Publishing an event
    pushes it to every queue.  Disconnected clients are cleaned up lazily.
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []

    def subscribe(self) -> asyncio.Queue[str]:
        """Create a new subscriber queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    def publish(self, event_type: str, data: str = "{}") -> None:
        """Push an event to all subscribers (non-blocking)."""
        message = f"event: {event_type}\ndata: {data}\n\n"
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)


# ---------------------------------------------------------------------------
# Background: stale-agent reaper
# ---------------------------------------------------------------------------


async def _reaper_loop(store: TaskStore, interval_s: float = 30.0) -> None:
    """Periodically mark stale agents as dead."""
    while True:
        await asyncio.sleep(interval_s)
        store.mark_stale_dead()


async def _node_reaper_loop(node_reg: NodeRegistry, interval_s: float = 15.0) -> None:
    """Periodically mark stale cluster nodes as offline."""
    while True:
        await asyncio.sleep(interval_s)
        node_reg.mark_stale()


async def _sse_heartbeat_loop(bus: SSEBus, interval_s: float = 15.0) -> None:
    """Send periodic heartbeat events to keep SSE connections alive."""
    while True:
        await asyncio.sleep(interval_s)
        bus.publish("heartbeat", json.dumps({"ts": time.time()}))


# ---------------------------------------------------------------------------
# Helpers used by route modules
# ---------------------------------------------------------------------------

DEFAULT_JSONL_PATH = Path(".sdd/runtime/tasks.jsonl")


def read_log_tail(path: Path, offset: int = 0) -> str:
    """Read a log file from *offset* bytes, skipping the partial first line.

    Args:
        path: Path to the log file.
        offset: Byte offset to start reading from.

    Returns:
        Log content as a string, with partial leading line stripped when
        offset is mid-line.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace")
    # When seeking into the middle of a file, the first partial line is
    # incomplete — strip it so callers only see whole lines.
    if offset > 0 and not text.startswith("\n"):
        idx = text.find("\n")
        if idx == -1:
            return ""
        text = text[idx + 1 :]
    return text


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    jsonl_path: Path = DEFAULT_JSONL_PATH,
    metrics_jsonl_path: Path | None = None,
    auth_token: str | None = None,
    cluster_config: ClusterConfig | None = None,
    plan_mode: bool = False,
    readonly: bool = False,
    slack_signing_secret: str | None = None,
) -> FastAPI:
    """Build and return the FastAPI application.

    Args:
        jsonl_path: Where to persist the JSONL task log.
        metrics_jsonl_path: Path to the metrics JSONL for cost reporting.
            Defaults to <jsonl_path.parent.parent>/metrics/tasks.jsonl.
        auth_token: If set, all API requests must include a matching
            ``Authorization: Bearer <token>`` header.
        cluster_config: Cluster mode configuration. If provided and
            enabled, node registration and cluster endpoints are active.
        readonly: If True, all write operations (POST/PUT/PATCH/DELETE) are
            rejected with 405.  The dashboard, events stream, and read
            endpoints remain fully accessible.  Useful for public demo
            deployments.
        slack_signing_secret: Slack app signing secret for verifying webhook
            request signatures.  Defaults to ``SLACK_SIGNING_SECRET`` env var.

    Returns:
        Configured FastAPI app with all routes registered.
    """
    setup_json_logging()
    from bernstein.core.auth import AuthService, AuthStore, SSOConfig
    from bernstein.core.auth_middleware import SSOAuthMiddleware
    from bernstein.core.routes.agents import router as agents_router
    from bernstein.core.routes.auth import router as auth_router
    from bernstein.core.routes.costs import router as costs_router
    from bernstein.core.routes.dashboard import router as dashboard_router
    from bernstein.core.routes.graph import router as graph_router
    from bernstein.core.routes.observability import router as observability_router
    from bernstein.core.routes.quality import router as quality_router
    from bernstein.core.routes.slack import router as slack_router
    from bernstein.core.routes.status import router as status_router
    from bernstein.core.routes.tasks import router as tasks_router
    from bernstein.core.routes.webhooks import router as webhooks_router
    from bernstein.core.routes.workspace import router as workspace_router

    # Resolve auth token: explicit arg > env var > None
    effective_token = auth_token or os.environ.get("BERNSTEIN_AUTH_TOKEN")

    # Cluster setup
    effective_cluster = cluster_config or ClusterConfig()
    node_registry = NodeRegistry(effective_cluster)

    store = TaskStore(jsonl_path, metrics_jsonl_path=metrics_jsonl_path)
    sse_bus = SSEBus()
    workdir = (
        jsonl_path.parent.parent.parent
        if jsonl_path.parent.name == "runtime" and jsonl_path.parent.parent.name == ".sdd"
        else Path.cwd()
    )
    sdd_dir = jsonl_path.parent.parent
    auth_config = SSOConfig()
    auth_enabled = auth_config.enabled or auth_config.oidc.enabled or auth_config.saml.enabled
    auth_service = AuthService(auth_config, AuthStore(sdd_dir)) if auth_enabled else None
    legacy_auth_token = effective_token or auth_config.legacy_token or None

    def _reload_seed_config() -> dict[str, Any]:
        """Reload and persist bernstein.yaml metadata without restarting."""
        from bernstein.core.config_diff import (
            diff_config_snapshots,
            load_redacted_config,
            read_config_snapshot,
            write_config_snapshot,
        )
        from bernstein.core.runtime_state import hash_file, write_config_state
        from bernstein.core.seed import SeedError, parse_seed
        from bernstein.core.tenanting import TenantRegistry, ensure_tenant_layout, tenant_registry_from_seed

        seed_path = workdir / "bernstein.yaml"
        sdd_dir = jsonl_path.parent.parent
        previous_snapshot = read_config_snapshot(sdd_dir)
        current_snapshot = load_redacted_config(seed_path if seed_path.exists() else None)
        diff = diff_config_snapshots(previous_snapshot, current_snapshot)
        config_hash = hash_file(seed_path if seed_path.exists() else None)
        payload: dict[str, Any] = {
            "seed_path": str(seed_path) if seed_path.exists() else None,
            "config_hash": config_hash,
            "reloaded_at": time.time(),
            "loaded": False,
            "config_last_diff": diff.to_dict(),
        }
        if seed_path.exists():
            try:
                application.state.seed_config = parse_seed(seed_path)  # type: ignore[attr-defined]
                application.state.tenant_registry = tenant_registry_from_seed(application.state.seed_config)  # type: ignore[attr-defined]
                for tenant in application.state.tenant_registry.tenants:  # type: ignore[attr-defined]
                    ensure_tenant_layout(sdd_dir, tenant.id)
                payload["loaded"] = True
            except SeedError as exc:
                payload["error"] = str(exc)
                application.state.tenant_registry = TenantRegistry()  # type: ignore[attr-defined]
        else:
            application.state.seed_config = None  # type: ignore[attr-defined]
            application.state.tenant_registry = TenantRegistry()  # type: ignore[attr-defined]
        write_config_state(
            sdd_dir,
            config_hash=config_hash,
            seed_path=payload["seed_path"],
            reloaded_at=float(payload["reloaded_at"]),
            last_diff=diff.to_dict(),
        )
        write_config_snapshot(sdd_dir, current_snapshot)
        return payload

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        # Startup: replay persisted state
        store.replay_jsonl()
        _reload_seed_config()
        previous_sighup = signal.getsignal(signal.SIGHUP) if hasattr(signal, "SIGHUP") else None
        if hasattr(signal, "SIGHUP") and threading.current_thread() is threading.main_thread():

            def _handle_sighup(_signum: int, _frame: object | None) -> None:
                _reload_seed_config()

            signal.signal(signal.SIGHUP, _handle_sighup)
        # Launch the stale-agent reaper
        reaper = asyncio.create_task(_reaper_loop(store))
        # Launch SSE heartbeat loop
        sse_heartbeat = asyncio.create_task(_sse_heartbeat_loop(sse_bus))
        # Launch node-stale reaper if cluster mode is on
        node_reaper: asyncio.Task[None] | None = None
        if effective_cluster.enabled:
            node_reaper = asyncio.create_task(
                _node_reaper_loop(node_registry, interval_s=effective_cluster.node_heartbeat_interval_s)
            )
        yield
        # Shutdown
        reaper.cancel()
        sse_heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reaper
        with contextlib.suppress(asyncio.CancelledError):
            await sse_heartbeat
        if node_reaper is not None:
            node_reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await node_reaper
        if (
            hasattr(signal, "SIGHUP")
            and previous_sighup is not None
            and threading.current_thread() is threading.main_thread()
        ):
            signal.signal(signal.SIGHUP, previous_sighup)
        await store.flush_buffer()

    application = FastAPI(title="Bernstein Task Server", version="0.1.0", lifespan=lifespan)

    # Crash guard — outermost middleware, catches unhandled exceptions
    application.add_middleware(CrashGuardMiddleware)

    # Structured request logging — logs after crash-guard normalization so the
    # final status code is always captured.
    application.add_middleware(
        StructuredAccessLogMiddleware,
        log_path=jsonl_path.parent / "access.jsonl",
    )

    # Read-only mode — blocks all writes before auth is even checked
    if readonly:
        application.add_middleware(ReadOnlyMiddleware)

    # Auth middleware — supports SSO JWTs plus legacy bearer tokens.
    application.add_middleware(
        SSOAuthMiddleware,
        auth_service=auth_service,
        legacy_token=legacy_auth_token,
    )

    # Per-endpoint request rate limiting — reads buckets from app.state.seed_config.
    application.add_middleware(RequestRateLimitMiddleware)

    # IP allowlist — reads allowed_ips from app.state.seed_config.network dynamically.
    application.add_middleware(IPAllowlistMiddleware)

    # Attach shared state for route modules to access via request.app.state
    bulletin = BulletinBoard()
    message_board = MessageBoard()
    a2a_handler = A2AHandler(server_url="http://localhost:8052")
    acp_handler = ACPHandler(server_url="http://localhost:8052")

    application.state.store = store  # type: ignore[attr-defined]
    application.state.bulletin = bulletin  # type: ignore[attr-defined]
    application.state.message_board = message_board  # type: ignore[attr-defined]
    application.state.a2a_handler = a2a_handler  # type: ignore[attr-defined]
    application.state.acp_handler = acp_handler  # type: ignore[attr-defined]
    application.state.node_registry = node_registry  # type: ignore[attr-defined]
    application.state.sse_bus = sse_bus  # type: ignore[attr-defined]
    application.state.runtime_dir = jsonl_path.parent  # type: ignore[attr-defined]  # .sdd/runtime/
    application.state.sdd_dir = sdd_dir  # type: ignore[attr-defined]  # .sdd/
    application.state.workdir = workdir  # type: ignore[attr-defined]
    application.state.seed_config = None  # type: ignore[attr-defined]
    application.state.tenant_registry = None  # type: ignore[attr-defined]
    application.state.reload_seed_config = _reload_seed_config  # type: ignore[attr-defined]
    application.state.draining = False  # type: ignore[attr-defined]
    application.state.readonly = readonly  # type: ignore[attr-defined]
    application.state.auth_service = auth_service  # type: ignore[attr-defined]
    application.state.legacy_auth_token = legacy_auth_token  # type: ignore[attr-defined]
    application.state.slack_signing_secret = (  # type: ignore[attr-defined]
        slack_signing_secret or os.environ.get("SLACK_SIGNING_SECRET") or ""
    )

    # Plan mode: initialize PlanStore when enabled
    if plan_mode:
        from bernstein.core.plan_approval import PlanStore

        application.state.plan_store = PlanStore(jsonl_path.parent.parent)  # type: ignore[attr-defined]
    else:
        application.state.plan_store = None  # type: ignore[attr-defined]

    # Root redirect → /status
    @application.get("/")
    async def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"name": "Bernstein Task Server", "status": "running", "docs": "/docs"}

    # Mount routers
    application.include_router(agents_router)
    application.include_router(auth_router)
    application.include_router(tasks_router)
    application.include_router(status_router)
    application.include_router(workspace_router)
    application.include_router(webhooks_router)
    application.include_router(slack_router)
    application.include_router(costs_router)
    application.include_router(dashboard_router)
    application.include_router(graph_router)
    application.include_router(observability_router)
    application.include_router(quality_router)

    # Graceful drain routes — freeze/unfreeze claim acceptance
    from bernstein.core.routes.drain import router as drain_router

    application.include_router(drain_router)

    # Agent identity lifecycle routes
    from bernstein.core.routes.identities import router as identities_router

    application.include_router(identities_router)

    # ACP (Agent Communication Protocol) routes — editor ecosystem visibility
    from bernstein.core.routes.acp import router as acp_router

    application.include_router(acp_router)

    # Approval routes for interactive TUI/HTTP approval gate management
    from bernstein.core.routes.approvals import router as approvals_router

    application.include_router(approvals_router)

    # Plan approval routes (always mounted; returns 503 if plan_mode is off)
    from bernstein.core.routes.plans import router as plans_router

    application.include_router(plans_router)

    # Gateway metrics — active only when a gateway session is running
    from bernstein.core.routes.gateway import router as gateway_router

    application.include_router(gateway_router)
    application.state.mcp_gateway = None  # type: ignore[attr-defined]

    # SLO and error budget endpoints
    from bernstein.core.routes.slo import router as slo_router

    application.include_router(slo_router)

    # Claude Code hook receiver — real-time tool-use and lifecycle events
    from bernstein.core.routes.hooks import router as hooks_router

    application.include_router(hooks_router)

    return application


# Default app instance for `uvicorn bernstein.core.server:app`
# Auth token and cluster config are read from environment at import time.
_default_cluster_enabled = os.environ.get("BERNSTEIN_CLUSTER_ENABLED", "").lower() in ("1", "true", "yes")
_default_cluster_config = (
    ClusterConfig(
        enabled=_default_cluster_enabled,
        auth_token=os.environ.get("BERNSTEIN_AUTH_TOKEN"),
        bind_host=os.environ.get("BERNSTEIN_BIND_HOST", "127.0.0.1"),
    )
    if _default_cluster_enabled
    else None
)


def get_app() -> FastAPI:
    """Get or create the default FastAPI app (lazy singleton)."""
    return create_app(
        auth_token=os.environ.get("BERNSTEIN_AUTH_TOKEN"),
        cluster_config=_default_cluster_config,
        readonly=os.environ.get("BERNSTEIN_READONLY", "").lower() in ("1", "true", "yes"),
        slack_signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    )


# Lazy app instance for uvicorn (bernstein.core.server:app).
# Uses __getattr__ to avoid circular import at module load time.
_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """Lazy module-level attribute for ``app``."""
    global _app
    if name == "app":
        if _app is None:
            _app = get_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Task notification protocol for agent status reports (T574)
# ---------------------------------------------------------------------------


@dataclass
class AgentStatusNotification:
    """Notification for agent status reports."""

    agent_id: str
    session_id: str
    role: str
    status: str  # "starting", "working", "completed", "failed", "stalled"
    task_id: str | None = None
    progress: float = 0.0  # 0.0 to 1.0
    message: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskNotificationManager:
    """Manages task notifications for agent status reports."""

    def __init__(self):
        self.notifications: list[AgentStatusNotification] = []
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._max_notifications = 1000  # Keep last 1000 notifications

    async def notify_agent_status(self, notification: AgentStatusNotification) -> None:
        """Notify agent status to all subscribers."""
        async with self._lock:
            # Add notification
            self.notifications.append(notification)

            # Keep only recent notifications
            if len(self.notifications) > self._max_notifications:
                self.notifications = self.notifications[-self._max_notifications :]

            # Notify subscribers
            for queue in self._subscribers:
                try:
                    await queue.put(notification)
                except Exception as e:
                    logger.warning(f"Failed to notify subscriber: {e}")

    async def subscribe(self) -> asyncio.Queue:
        """Subscribe to agent status notifications."""
        queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from agent status notifications."""
        async with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def get_recent_notifications(self, limit: int = 100) -> list[AgentStatusNotification]:
        """Get recent agent status notifications."""
        return self.notifications[-limit:]


# Global task notification manager
_task_notification_manager = TaskNotificationManager()


async def notify_agent_status(
    agent_id: str,
    session_id: str,
    role: str,
    status: str,
    task_id: str | None = None,
    progress: float = 0.0,
    message: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Send agent status notification (T574)."""
    notification = AgentStatusNotification(
        agent_id=agent_id,
        session_id=session_id,
        role=role,
        status=status,
        task_id=task_id,
        progress=progress,
        message=message,
        metadata=metadata or {},
    )

    await _task_notification_manager.notify_agent_status(notification)

    logger.info(
        f"Agent status notification: {agent_id} ({role}) - {status} (task: {task_id}, progress: {progress:.0%})"
    )
