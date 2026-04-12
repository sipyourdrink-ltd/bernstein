"""Application factory and runtime classes for the Bernstein task server.

SSE bus, background loops, helper converters, and ``create_app()`` live here.
The parent ``server`` module re-exports every name for backward compatibility.
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
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI

from bernstein.core.a2a import A2AHandler
from bernstein.core.acp import ACPHandler
from bernstein.core.auth_rate_limiter import RequestRateLimitMiddleware
from bernstein.core.bulletin import BulletinBoard, DirectChannel, MessageBoard
from bernstein.core.cluster import NodeRegistry
from bernstein.core.models import (
    ClusterConfig,
    NodeInfo,
    Task,
)
from bernstein.core.server.access_log import StructuredAccessLogMiddleware
from bernstein.core.server.json_logging import setup_json_logging
from bernstein.core.server.server_middleware import (
    CrashGuardMiddleware,
    IPAllowlistMiddleware,
    ReadOnlyMiddleware,
)
from bernstein.core.server.server_models import (
    A2AArtifactResponse,
    A2AMessageResponse,
    A2ATaskResponse,
    NodeCapacitySchema,
    NodeResponse,
    TaskResponse,
)
from bernstein.core.tasks.task_store import (
    ProgressEntry,
    TaskStore,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        metadata=task.metadata,
        created_at=task.created_at,
        claimed_at=task.claimed_at,
        progress_log=list(cast("list[ProgressEntry]", task.progress_log)),  # type: ignore[reportUnknownMemberType]
        version=task.version,
        parent_session_id=task.parent_session_id,
    )


# ---------------------------------------------------------------------------
# SSE event bus — fan-out to all connected dashboard clients
# ---------------------------------------------------------------------------


class SSEBus:
    """Fan-out event bus for Server-Sent Events.

    Each connected client gets its own asyncio.Queue.  Publishing an event
    pushes it to every queue.  Disconnected clients are cleaned up lazily.

    Features:
    - Queue buffer size limit prevents unbounded memory growth.
    - Heartbeat pings enable disconnect detection.
    - Stale subscriber cleanup prevents leaked queue references.
    """

    # Maximum events buffered per subscriber before dropping
    MAX_BUFFER_SIZE: int = 256
    # Seconds after which a subscriber with no reads is considered stale
    STALE_TIMEOUT_S: float = 120.0
    # Heartbeat interval for SSE keep-alive pings
    HEARTBEAT_INTERVAL_S: float = 15.0

    def __init__(self, *, max_buffer: int = 256, stale_timeout_s: float = 120.0) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self._subscriber_last_read: dict[int, float] = {}
        self._max_buffer = max_buffer
        self._stale_timeout_s = stale_timeout_s

    def subscribe(self) -> asyncio.Queue[str]:
        """Create a new subscriber queue."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_buffer)
        self._subscribers.append(queue)
        self._subscriber_last_read[id(queue)] = time.time()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)
        self._subscriber_last_read.pop(id(queue), None)

    def mark_read(self, queue: asyncio.Queue[str]) -> None:
        """Update the last-read timestamp for a subscriber."""
        self._subscriber_last_read[id(queue)] = time.time()

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    def publish(self, event_type: str, data: str = "{}") -> None:
        """Push an event to all subscribers (non-blocking).

        If a subscriber's queue is full, the event is dropped for that
        subscriber to prevent unbounded memory growth.
        """
        message = f"event: {event_type}\ndata: {data}\n\n"
        for queue in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)

    def cleanup_stale(self) -> int:
        """Remove subscribers that haven't read in ``stale_timeout_s``.

        Returns:
            Number of stale subscribers removed.
        """
        now = time.time()
        stale: list[asyncio.Queue[str]] = []
        for queue in list(self._subscribers):
            last_read = self._subscriber_last_read.get(id(queue), 0.0)
            if (now - last_read) > self._stale_timeout_s:
                stale.append(queue)
        for queue in stale:
            self.unsubscribe(queue)
        return len(stale)


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
    """Send periodic heartbeat events to keep SSE connections alive.

    Also cleans up stale subscribers that haven't consumed messages.
    """
    cleanup_counter = 0
    while True:
        await asyncio.sleep(interval_s)
        bus.publish("heartbeat", json.dumps({"ts": time.time()}))
        # Run stale subscriber cleanup every 4th heartbeat (~60s)
        cleanup_counter += 1
        if cleanup_counter % 4 == 0:
            removed = bus.cleanup_stale()
            if removed > 0:
                logger.info("SSE bus: cleaned up %d stale subscribers", removed)


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
    from bernstein.core.routes.discord import router as discord_router
    from bernstein.core.routes.graph import router as graph_router
    from bernstein.core.routes.observability import router as observability_router
    from bernstein.core.routes.quality import router as quality_router
    from bernstein.core.routes.slack import router as slack_router
    from bernstein.core.routes.status import router as status_router
    from bernstein.core.routes.tasks import router as tasks_router
    from bernstein.core.routes.team_dashboard import router as team_dashboard_router
    from bernstein.core.routes.webhooks import router as webhooks_router
    from bernstein.core.routes.workspace import router as workspace_router

    # Resolve auth token: explicit arg > env var > None
    effective_token = auth_token or os.environ.get("BERNSTEIN_AUTH_TOKEN")

    # Cluster setup
    effective_cluster = cluster_config or ClusterConfig()
    # Persist node registry alongside the task store when inside .sdd/
    _runtime_dir = jsonl_path.parent
    _nodes_persist: Path | None = None
    if _runtime_dir.name == "runtime" and _runtime_dir.parent.name == ".sdd":
        _nodes_persist = _runtime_dir / "nodes.json"
    node_registry = NodeRegistry(effective_cluster, persist_path=_nodes_persist)

    # Cluster JWT authentication (ENT-002)
    from bernstein.core.cluster_auth import ClusterAuthConfig, ClusterAuthenticator

    _cluster_auth_secret = effective_cluster.auth_token or ""
    cluster_authenticator: ClusterAuthenticator | None = None
    if effective_cluster.enabled and _cluster_auth_secret:
        cluster_authenticator = ClusterAuthenticator(
            ClusterAuthConfig(secret=_cluster_auth_secret, require_auth=True),
        )

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
        store.recover_stale_claimed_tasks()
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

    application = FastAPI(
        title="Bernstein Task Server",
        version="1.0.0",
        description=(
            "Bernstein REST API — multi-agent orchestration for CLI coding agents.\n\n"
            "## Authentication\n\n"
            "When auth is enabled (`BERNSTEIN_AUTH_ENABLED=true`), include a Bearer token "
            "in all requests:\n\n"
            "```\nAuthorization: Bearer <token>\n```\n\n"
            "Public endpoints (no auth required): `/health`, `/health/ready`, `/health/live`, "
            "`/.well-known/agent.json`, `/docs`, `/openapi.json`.\n\n"
            "## Base URL\n\n"
            "Default: `http://127.0.0.1:8052`. Override with env vars `BERNSTEIN_HOST` and "
            "`BERNSTEIN_PORT`.\n\n"
            "## Error Format\n\n"
            "All errors return JSON with a `detail` field:\n\n"
            '```json\n{"detail": "Task not found: task-xyz"}\n```\n\n'
            "| Status | Meaning |\n"
            "|--------|---------|\n"
            "| 400 | Bad request (validation error) |\n"
            "| 401 | Unauthorized (missing/invalid token) |\n"
            "| 403 | Forbidden (IP not in allowlist) |\n"
            "| 404 | Resource not found |\n"
            "| 409 | Conflict (task already in terminal state) |\n"
            "| 429 | Rate limited — respect the `Retry-After` header |\n"
            "| 500 | Internal server error |\n"
        ),
        lifespan=lifespan,
    )

    # Crash guard — outermost middleware, catches unhandled exceptions
    application.add_middleware(CrashGuardMiddleware)

    from bernstein.core.server.frame_headers import FrameHeadersMiddleware, load_frame_embedding_policy

    application.add_middleware(
        FrameHeadersMiddleware,
        policy=load_frame_embedding_policy(),
    )

    # Structured request logging — logs after crash-guard normalization so the
    # final status code is always captured.
    application.add_middleware(
        StructuredAccessLogMiddleware,
        log_path=jsonl_path.parent / "access.jsonl",
    )

    # WEB-010: Request/response logging middleware (method, path, status, duration)
    from bernstein.core.server.request_logging import RequestLoggingMiddleware

    application.add_middleware(RequestLoggingMiddleware)

    # Read-only mode — blocks all writes before auth is even checked
    if readonly:
        application.add_middleware(ReadOnlyMiddleware)

    # Auth middleware — supports SSO JWTs, agent identity JWTs (zero-trust),
    # and legacy bearer tokens.  The agent identity store is shared with
    # application state so spawned agents can authenticate per-request.
    from bernstein.core.agent_identity import AgentIdentityStore

    _auth_dir = sdd_dir / "auth"
    _agent_identity_store = AgentIdentityStore(_auth_dir)
    application.state.identity_store = _agent_identity_store  # type: ignore[attr-defined]

    application.add_middleware(
        SSOAuthMiddleware,
        auth_service=auth_service,
        legacy_token=legacy_auth_token,
        agent_identity_store=_agent_identity_store,
    )

    # Per-endpoint request rate limiting — reads buckets from app.state.seed_config.
    application.add_middleware(RequestRateLimitMiddleware)

    # IP allowlist — reads allowed_ips from app.state.seed_config.network dynamically.
    application.add_middleware(IPAllowlistMiddleware)

    # CORS middleware — configured from bernstein.yaml or defaults to localhost:*
    from bernstein.core.seed import CORSConfig

    cors_config = CORSConfig()  # default; overridden after seed_config loads
    seed_path = workdir / "bernstein.yaml"
    if seed_path.exists():
        try:
            from bernstein.core.seed import parse_seed

            _temp_seed = parse_seed(seed_path)
            if _temp_seed.cors is not None:
                cors_config = _temp_seed.cors
        except Exception:
            pass  # Use defaults on seed parse failure

    from starlette.middleware.cors import CORSMiddleware

    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_config.allowed_origins),
        allow_methods=list(cors_config.allow_methods),
        allow_headers=list(cors_config.allow_headers),
        allow_credentials=cors_config.allow_credentials,
        max_age=cors_config.max_age,
    )

    # Attach shared state for route modules to access via request.app.state
    bulletin = BulletinBoard()
    message_board = MessageBoard()
    direct_channel = DirectChannel()
    a2a_handler = A2AHandler(server_url="http://localhost:8052")
    acp_handler = ACPHandler(server_url="http://localhost:8052")

    application.state.store = store  # type: ignore[attr-defined]
    application.state.bulletin = bulletin  # type: ignore[attr-defined]
    application.state.message_board = message_board  # type: ignore[attr-defined]
    application.state.direct_channel = direct_channel  # type: ignore[attr-defined]
    application.state.a2a_handler = a2a_handler  # type: ignore[attr-defined]
    application.state.acp_handler = acp_handler  # type: ignore[attr-defined]
    application.state.node_registry = node_registry  # type: ignore[attr-defined]
    application.state.cluster_authenticator = cluster_authenticator  # type: ignore[attr-defined]
    application.state.sse_bus = sse_bus  # type: ignore[attr-defined]
    application.state.runtime_dir = jsonl_path.parent  # type: ignore[attr-defined]  # .sdd/runtime/
    application.state.sdd_dir = sdd_dir  # type: ignore[attr-defined]  # .sdd/
    application.state.workdir = workdir  # type: ignore[attr-defined]

    # Real-time behavior anomaly monitor — checks file access and output-size on
    # every progress update and writes kill signals for compromised sessions.
    from bernstein.core.behavior_anomaly import RealtimeBehaviorMonitor

    application.state.realtime_behavior_monitor = RealtimeBehaviorMonitor(workdir)  # type: ignore[attr-defined]
    application.state.seed_config = None  # type: ignore[attr-defined]
    application.state.tenant_registry = None  # type: ignore[attr-defined]

    # ENT-001: Multi-tenant task isolation manager
    from bernstein.core.tenant_isolation import TenantIsolationManager

    tenant_isolation_mgr = TenantIsolationManager(sdd_dir)
    tenant_isolation_mgr.load_state()
    application.state.tenant_isolation_manager = tenant_isolation_mgr  # type: ignore[attr-defined]
    application.state.reload_seed_config = _reload_seed_config  # type: ignore[attr-defined]
    application.state.draining = False  # type: ignore[attr-defined]
    application.state.readonly = readonly  # type: ignore[attr-defined]

    # Config drift watcher — snapshot current config file checksums
    from bernstein.core.config_watcher import ConfigWatcher

    application.state.config_watcher = ConfigWatcher.snapshot(workdir)  # type: ignore[attr-defined]
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

    # Root redirect -> /status
    @application.get("/")
    def root() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"name": "Bernstein Task Server", "status": "running", "docs": "/docs"}

    # WEB-011: Paginated task search — must precede tasks_router so /tasks/search
    # is matched before /tasks/{task_id}.
    from bernstein.core.routes.paginated_tasks import router as paginated_tasks_router

    application.include_router(paginated_tasks_router)

    # Mount routers
    application.include_router(agents_router)
    application.include_router(auth_router)
    application.include_router(tasks_router)
    application.include_router(status_router)
    application.include_router(workspace_router)
    application.include_router(webhooks_router)
    application.include_router(discord_router)
    application.include_router(slack_router)
    application.include_router(costs_router)
    application.include_router(dashboard_router)
    application.include_router(team_dashboard_router)
    application.include_router(graph_router)
    application.include_router(observability_router)
    application.include_router(quality_router)

    # Per-file code health score routes
    from bernstein.core.routes.file_health import router as file_health_router

    application.include_router(file_health_router)

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

    # Custom metrics (OBS-148): user-defined formula-based KPIs
    from bernstein.core.routes.custom_metrics import router as custom_metrics_router

    application.include_router(custom_metrics_router)

    # SBOM generation and artifact listing (supply-chain security)
    from bernstein.core.routes.sbom import router as sbom_router

    application.include_router(sbom_router)

    # Claude Code hook receiver — real-time tool-use and lifecycle events
    from bernstein.core.routes.hooks import router as hooks_router

    application.include_router(hooks_router)

    # WEB-006: WebSocket live dashboard
    from bernstein.core.routes.websocket import router as ws_router

    application.include_router(ws_router)

    # WEB-008: Data export endpoints
    from bernstein.core.routes.export import router as export_router

    application.include_router(export_router)

    # WEB-009: Grafana dashboard endpoint
    from bernstein.core.routes.grafana import router as grafana_router

    application.include_router(grafana_router)

    # WEB-012: Dashboard task detail with live log streaming
    from bernstein.core.routes.task_detail import router as task_detail_router

    application.include_router(task_detail_router)

    # WEB-013: Health endpoint with dependency status
    from bernstein.core.routes.health import router as health_deps_router

    application.include_router(health_deps_router)

    # WEB-017: Batch operations endpoint
    from bernstein.core.routes.batch_ops import router as batch_ops_router

    application.include_router(batch_ops_router)

    # WEB-018: Agent comparison view
    from bernstein.core.routes.agent_comparison import router as agent_comparison_router

    application.include_router(agent_comparison_router)

    # WEB-019: Audit log endpoint with search and filtering
    from bernstein.core.routes.audit_log import router as audit_log_router

    application.include_router(audit_log_router)

    # WEB-021: GraphQL API alongside REST
    from bernstein.core.routes.graphql_api import router as graphql_router

    application.include_router(graphql_router)

    # Graduation framework — stage inspection and promotion
    from bernstein.core.routes.graduation import router as graduation_router

    application.include_router(graduation_router)

    # Team state — current roster visibility for CLI/TUI
    from bernstein.core.routes.team import router as team_router

    application.include_router(team_router)

    # ROAD-155: Provider latency percentile tracker
    from bernstein.core.routes.provider_latency import router as provider_latency_router

    application.include_router(provider_latency_router)

    # ROAD-157: Predictive alerting — forecast issues before they impact the run
    from bernstein.core.routes.predictive import router as predictive_router

    application.include_router(predictive_router)

    # WEB-007: API v1 versioned routes — mount all existing routers under /api/v1/
    from bernstein.core.routes.api_v1 import router as api_v1_router

    api_v1_router.include_router(paginated_tasks_router)
    api_v1_router.include_router(tasks_router)
    api_v1_router.include_router(status_router)
    api_v1_router.include_router(agents_router)
    api_v1_router.include_router(costs_router)
    api_v1_router.include_router(export_router)
    api_v1_router.include_router(grafana_router)
    api_v1_router.include_router(health_deps_router)
    api_v1_router.include_router(batch_ops_router)
    api_v1_router.include_router(agent_comparison_router)
    api_v1_router.include_router(audit_log_router)
    api_v1_router.include_router(graphql_router)
    application.include_router(api_v1_router)

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
