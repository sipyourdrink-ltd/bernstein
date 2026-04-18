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
import re
import signal
import threading
import time
from collections import defaultdict, deque
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
# audit-117: body-size cap middleware
# ---------------------------------------------------------------------------

# Default cap for request bodies.  Starlette accepts unlimited bodies by
# default — without a cap, a single POST with a 200MB JSON body will load the
# full payload into memory before pydantic validation even runs, wedging the
# server and bloating tasks.jsonl by 200MB per request.
_DEFAULT_MAX_BODY_BYTES = 1_048_576  # 1 MB


class ContentLengthMiddleware:
    """Reject requests with bodies larger than ``max_body_bytes`` (413).

    Protects /tasks, /webhook, /broadcast, /hooks/* and every other write
    endpoint from trivial memory exhaustion.

    Implemented as a raw ASGI middleware (rather than ``BaseHTTPMiddleware``)
    so the streaming body-size check can terminate the request *before*
    invoking the inner app, returning a clean 413 response.  ``BaseHTTPMiddleware``
    swallows exceptions raised inside its wrapped ``receive`` channel, which
    would let truncated bodies reach the route handler as malformed 400s.

    Behaviour:
    - If the ``Content-Length`` header is present and exceeds the cap, reject
      immediately with 413 — no body bytes are consumed.
    - Streaming / chunked requests without a ``Content-Length`` header are
      tallied on the ASGI receive channel and rejected with 413 the moment
      the cap is crossed.
    - GET/HEAD/OPTIONS requests pass through unchanged.
    """

    def __init__(self, app: Any, max_body_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self._max_body_bytes = max_body_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Only HTTP requests carry bodies we care about.  WebSocket and lifespan
        # scopes pass through unchanged.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        # Fast-path: reject when Content-Length header reports an oversized body.
        headers_list = scope.get("headers", [])
        content_length: int | None = None
        for name, value in headers_list:
            if name == b"content-length":
                try:
                    content_length = int(value.decode("latin-1"))
                except ValueError:
                    await self._send_json(send, 400, {"detail": "Invalid Content-Length header"})
                    return
                break

        if content_length is not None and content_length > self._max_body_bytes:
            await self._send_json(
                send,
                413,
                {
                    "detail": (f"Request body {content_length} bytes exceeds {self._max_body_bytes}-byte limit"),
                },
            )
            return

        # Streaming enforcement: when the client omits Content-Length (e.g.
        # chunked transfer encoding), wrap the receive channel so we tally
        # bytes and reject once the cap is crossed.  This guarantees the cap
        # cannot be bypassed by omitting the header.
        max_bytes = self._max_body_bytes
        seen_bytes = 0
        cap_exceeded = False

        async def limited_receive() -> Any:
            nonlocal seen_bytes, cap_exceeded
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    seen_bytes += len(chunk)
                    if seen_bytes > max_bytes:
                        cap_exceeded = True
            return message

        # Track whether the wrapped app has started its response so we don't
        # double-send if the body overflow is detected after headers were sent.
        response_started = False
        response_finished = False

        async def wrapped_send(message: Any) -> None:
            nonlocal response_started, response_finished
            if response_finished:
                # Inner app is still trying to send after we've already
                # emitted our 413 — drop its messages on the floor.
                return
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                if cap_exceeded:
                    # Body already crossed the cap — discard the inner app's
                    # response and emit our 413 instead.
                    await self._send_json(
                        send,
                        413,
                        {
                            "detail": (f"Request body {seen_bytes} bytes exceeds {self._max_body_bytes}-byte limit"),
                        },
                    )
                    response_started = True
                    response_finished = True
                    return
                response_started = True
            elif msg_type == "http.response.body" and cap_exceeded:
                # Shouldn't happen (we already finished), but guard anyway.
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, wrapped_send)
        finally:
            if cap_exceeded and not response_started:
                # Inner app finished without emitting a response — send 413.
                await self._send_json(
                    send,
                    413,
                    {
                        "detail": (f"Request body {seen_bytes} bytes exceeds {self._max_body_bytes}-byte limit"),
                    },
                )

    @staticmethod
    async def _send_json(send: Any, status: int, body: dict[str, Any]) -> None:
        """Emit a JSON response through the raw ASGI ``send`` channel."""
        payload = json.dumps(body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


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
        # audit-017: expose typed retry bookkeeping so clients read the
        # single source of truth rather than regex-ing the title.
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        retry_delay_s=task.retry_delay_s,
        terminal_reason=task.terminal_reason,
        max_output_tokens=task.max_output_tokens,
        meta_messages=list(task.meta_messages),
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
    - audit-122: reconnect-frequency limiter blocks IPs that churn
      subscribe/unsubscribe faster than ``RECONNECT_MAX_PER_WINDOW``
      inside ``RECONNECT_WINDOW_S``.
    - audit-122: per-IP buffer budget caps total events across all
      queues belonging to the same IP (defends slow-client DoS).
    - audit-122: dropped-event counter with warn logging to flag
      slow clients without killing the orchestrator.
    """

    # Maximum events buffered per subscriber before dropping
    MAX_BUFFER_SIZE: int = 256
    # Maximum total events buffered per IP across all its queues (audit-122)
    MAX_BUFFER_PER_IP: int = 1024
    # Seconds after which a subscriber with no reads is considered stale
    # (audit-122: lowered from 120s to 30s for /events slow-client DoS)
    STALE_TIMEOUT_S: float = 30.0
    # Heartbeat interval for SSE keep-alive pings
    HEARTBEAT_INTERVAL_S: float = 15.0
    # audit-122: reconnect-frequency limiter parameters. Three clean
    # reconnects inside RECONNECT_WINDOW_S tolerates a flaky wifi drop
    # (heartbeat timeout ~= 60s so one cycle per minute is realistic).
    RECONNECT_MAX_PER_WINDOW: int = 3
    RECONNECT_WINDOW_S: float = 60.0
    RECONNECT_COOLDOWN_S: float = 300.0

    def __init__(
        self,
        *,
        max_buffer: int = 256,
        stale_timeout_s: float = 30.0,
        max_buffer_per_ip: int = 1024,
        reconnect_max_per_window: int = 3,
        reconnect_window_s: float = 60.0,
        reconnect_cooldown_s: float = 300.0,
    ) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self._subscriber_last_read: dict[int, float] = {}
        # audit-122: map queue id -> owning IP so per-IP accounting works
        self._subscriber_ip: dict[int, str] = {}
        self._max_buffer = max_buffer
        self._stale_timeout_s = stale_timeout_s
        self._max_buffer_per_ip = max_buffer_per_ip
        # audit-122: sliding-window record of recent subscribe() timestamps
        # per IP. Bounded by RECONNECT_MAX_PER_WINDOW + 1 to avoid unbounded
        # growth for abusive clients.
        _max_hist = reconnect_max_per_window + 1
        self._recent_connects: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_max_hist))
        # audit-122: cooldown expiry per IP (monotonic timestamp)
        self._reconnect_cooldown_until: dict[str, float] = {}
        self._reconnect_max_per_window = reconnect_max_per_window
        self._reconnect_window_s = reconnect_window_s
        self._reconnect_cooldown_s = reconnect_cooldown_s
        # audit-122: dropped-event counters (total + last warn ts)
        self._dropped_events_total: int = 0
        self._last_drop_warning_ts: float = 0.0

    # ---- reconnect tracking (audit-122) -----------------------------------

    def is_blocked(self, ip: str, *, now: float | None = None) -> bool:
        """Return ``True`` when *ip* is inside its reconnect-cooldown window."""
        ts = now if now is not None else time.monotonic()
        expiry = self._reconnect_cooldown_until.get(ip)
        if expiry is None:
            return False
        if ts >= expiry:
            self._reconnect_cooldown_until.pop(ip, None)
            return False
        return True

    def _record_connect_attempt(self, ip: str, *, now: float | None = None) -> bool:
        """Record a subscribe attempt for *ip*.

        Returns ``True`` when the attempt is permitted, ``False`` when the
        caller has exceeded ``reconnect_max_per_window`` reconnects inside
        ``reconnect_window_s``; in that case the IP is parked in a
        ``reconnect_cooldown_s``-long penalty box.
        """
        ts = now if now is not None else time.monotonic()
        if self.is_blocked(ip, now=ts):
            return False
        window = self._recent_connects[ip]
        cutoff = ts - self._reconnect_window_s
        while window and window[0] <= cutoff:
            window.popleft()
        if len(window) >= self._reconnect_max_per_window:
            # Exceeded the window — trip the cooldown and reject.
            self._reconnect_cooldown_until[ip] = ts + self._reconnect_cooldown_s
            logger.warning(
                "SSE bus: blocking IP %s for %.0fs (>%d reconnects in %.0fs)",
                ip,
                self._reconnect_cooldown_s,
                self._reconnect_max_per_window,
                self._reconnect_window_s,
            )
            return False
        window.append(ts)
        return True

    # ---- subscribe / unsubscribe ------------------------------------------

    def subscribe(self, *, client_ip: str | None = None) -> asyncio.Queue[str]:
        """Create a new subscriber queue.

        Args:
            client_ip: Optional remote IP string. When provided, the
                connection is subject to the audit-122 reconnect-frequency
                limiter and the per-IP buffer budget. ``None`` opts out
                (used by synthetic callers — heartbeat loop, unit tests).

        Raises:
            PermissionError: If *client_ip* has exceeded the reconnect
                limit and is currently in cooldown.
        """
        if client_ip is not None and not self._record_connect_attempt(client_ip):
            raise PermissionError(f"SSE reconnect rate limit exceeded for {client_ip}")
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_buffer)
        self._subscribers.append(queue)
        self._subscriber_last_read[id(queue)] = time.time()
        if client_ip is not None:
            self._subscriber_ip[id(queue)] = client_ip
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        """Remove a subscriber queue."""
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)
        self._subscriber_last_read.pop(id(queue), None)
        self._subscriber_ip.pop(id(queue), None)

    def mark_read(self, queue: asyncio.Queue[str]) -> None:
        """Update the last-read timestamp for a subscriber."""
        self._subscriber_last_read[id(queue)] = time.time()

    @property
    def subscriber_count(self) -> int:
        """Number of active subscribers."""
        return len(self._subscribers)

    @property
    def dropped_events_total(self) -> int:
        """Return the cumulative count of events dropped due to buffer pressure."""
        return self._dropped_events_total

    def buffered_for_ip(self, ip: str) -> int:
        """Return the total number of buffered events across all *ip* queues."""
        total = 0
        for queue in self._subscribers:
            if self._subscriber_ip.get(id(queue)) == ip:
                total += queue.qsize()
        return total

    # ---- publish ----------------------------------------------------------

    def publish(self, event_type: str, data: str = "{}") -> None:
        """Push an event to all subscribers (non-blocking).

        If a subscriber's queue is full, or pushing would exceed the
        per-IP buffer budget (audit-122), the event is dropped for that
        subscriber. A warning is logged when drops are observed.
        """
        message = f"event: {event_type}\ndata: {data}\n\n"
        dropped = 0
        # Compute the per-IP buffered totals once per publish so we do not
        # pay O(n) lookups per queue inside the loop.
        per_ip_used: dict[str, int] = defaultdict(int)
        for queue in self._subscribers:
            owner = self._subscriber_ip.get(id(queue))
            if owner is not None:
                per_ip_used[owner] += queue.qsize()
        # Snapshot the subscriber list so mutations during publish (tests
        # exercise this) do not raise.
        for queue in list(self._subscribers):
            owner = self._subscriber_ip.get(id(queue))
            if owner is not None and per_ip_used[owner] >= self._max_buffer_per_ip:
                dropped += 1
                continue
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                dropped += 1
                continue
            if owner is not None:
                per_ip_used[owner] += 1
        if dropped:
            self._dropped_events_total += dropped
            now = time.monotonic()
            # Rate-limit the warn log to once per second to avoid spam.
            if now - self._last_drop_warning_ts >= 1.0:
                self._last_drop_warning_ts = now
                logger.warning(
                    "SSE bus: dropped %d events (total=%d) on %s publish",
                    dropped,
                    self._dropped_events_total,
                    event_type,
                )

    def cleanup_stale(self) -> int:
        """Remove subscribers that haven't read in ``stale_timeout_s``.

        Returns:
            Number of stale subscribers removed.
        """
        now = time.time()
        stale: list[asyncio.Queue[str]] = []
        for queue in self._subscribers:
            last_read = self._subscriber_last_read.get(id(queue), 0.0)
            if (now - last_read) > self._stale_timeout_s:
                stale.append(queue)
        for queue in stale:
            self.unsubscribe(queue)
        return len(stale)


# ---------------------------------------------------------------------------
# audit-122: SSE reconnect limiter middleware
# ---------------------------------------------------------------------------


class SSEReconnectLimiterMiddleware:
    """Front-door limiter for /events and /events/cost SSE endpoints.

    Rejects requests from IPs that have reconnected more than
    ``RECONNECT_MAX_PER_WINDOW`` times inside ``RECONNECT_WINDOW_S`` for
    ``RECONNECT_COOLDOWN_S`` seconds. Reconnect counters live on the shared
    ``SSEBus`` instance so per-IP state is consistent with subscribe().

    Tuned to tolerate realistic wifi blips: 3 reconnects / 60 s is >2x the
    expected churn rate given a 60 s read-timeout on the /events stream.
    """

    _SSE_PATHS = frozenset({"/events", "/events/cost"})

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method", "").upper() != "GET":
            await self.app(scope, receive, send)
            return
        if scope.get("path") not in self._SSE_PATHS:
            await self.app(scope, receive, send)
            return
        fastapi_app = scope.get("app")
        bus = getattr(getattr(fastapi_app, "state", None), "sse_bus", None)
        if not isinstance(bus, SSEBus):
            await self.app(scope, receive, send)
            return
        raw_client: Any = scope.get("client") or ("unknown", 0)
        if isinstance(raw_client, tuple) and raw_client:
            client_ip: str = str(raw_client[0])  # type: ignore[reportUnknownArgumentType]
        else:
            client_ip = "unknown"
        if not bus._record_connect_attempt(client_ip):  # pyright: ignore[reportPrivateUsage]
            body = json.dumps(
                {
                    "detail": "Too many SSE reconnects. Try again later.",
                    "bucket": "sse_reconnect",
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", b"300"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


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


def _split_cors_origins(
    allowed_origins: tuple[str, ...] | list[str],
) -> tuple[list[str], str | None]:
    """Separate literal CORS origins from glob patterns and build a regex.

    ``starlette.middleware.cors.CORSMiddleware`` compares ``allow_origins``
    literally — ``"http://localhost:*"`` never matches a real browser
    origin such as ``"http://localhost:3000"``.  audit-118 requires us to
    translate glob-style origins into an ``allow_origin_regex`` that
    CORSMiddleware actually honors.

    Args:
        allowed_origins: Origins as configured in bernstein.yaml, possibly
            mixing literal URLs (``https://app.example.com``) with glob
            patterns (``http://localhost:*``).

    Returns:
        A ``(literal_origins, origin_regex)`` tuple.  ``literal_origins`` is
        the subset containing no ``*`` — safe to pass to ``allow_origins``.
        ``origin_regex`` is ``None`` when no globs were present; otherwise
        it is a single combined regex matching any of the glob origins.
    """
    literal_origins: list[str] = []
    glob_origins: list[str] = []
    for origin in allowed_origins:
        if "*" in origin:
            glob_origins.append(origin)
        else:
            literal_origins.append(origin)

    if not glob_origins:
        return literal_origins, None

    # Translate each glob origin to a regex fragment.  Escape everything
    # except ``*`` — and translate ``*`` to either ``\d+`` (when it is the
    # port component, i.e. follows ``:``) or the generic ``[^/]*`` match.
    fragments: list[str] = []
    for origin in glob_origins:
        parts: list[str] = []
        i = 0
        while i < len(origin):
            ch = origin[i]
            if ch == "*":
                # A ``:*`` suffix means "any port" — restrict to digits so we
                # don't accept pathological inputs like ``http://localhost:evil``.
                if parts and parts[-1].endswith(":"):
                    parts.append(r"\d+")
                else:
                    parts.append(r"[^/]*")
            else:
                parts.append(re.escape(ch))
            i += 1
        fragments.append("".join(parts))

    combined = "^(?:" + "|".join(fragments) + ")$"
    return literal_origins, combined


def _do_reload_seed_config(workdir: Path, jsonl_path: Path, application: Any) -> dict[str, Any]:
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


def _resolve_configured_workers() -> int:
    """Resolve the requested uvicorn worker count from env vars.

    Reads ``BERNSTEIN_WORKERS`` first, falling back to ``WEB_CONCURRENCY``
    (the conventional uvicorn/gunicorn env var). Invalid or missing values
    resolve to ``1``.

    Returns:
        Worker count (minimum 1).
    """
    for var in ("BERNSTEIN_WORKERS", "WEB_CONCURRENCY"):
        raw = os.environ.get(var)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        return max(1, value)
    return 1


def preflight_multi_worker_guard() -> None:
    """Refuse to boot when multi-worker mode is requested.

    Bernstein's ``TaskStore`` coordinates mutations with an in-process
    ``asyncio.Lock`` and appends to JSONL without ``fcntl.flock`` — running
    under ``uvicorn --workers N`` (or ``WEB_CONCURRENCY>1``) causes torn
    JSONL lines and duplicate task claims (audit-025).

    The guard fires at app-factory time so each uvicorn worker subprocess
    re-runs it on import and bails out with a clear message instead of
    silently corrupting state.

    Raises:
        SystemExit: If the resolved worker count is greater than 1. The
            error message points operators to the single-supported
            configuration.
    """
    workers = _resolve_configured_workers()
    if workers > 1:
        raise SystemExit(
            "Bernstein TaskStore is single-process; refusing to boot with "
            f"workers={workers}. Set server.workers=1 in bernstein.yaml or "
            "use BERNSTEIN_WORKERS=1 (also clear WEB_CONCURRENCY). "
            "Multi-worker support is tracked as a separate ticket "
            "(fcntl.flock / SQLite WAL)."
        )


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

    Raises:
        SystemExit: Via ``preflight_multi_worker_guard`` when the operator
            requests more than one uvicorn worker — the ``TaskStore`` is
            single-process and multi-worker mode corrupts state
            (audit-025).
    """
    preflight_multi_worker_guard()
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

    # Auth is enabled by default.  Operators can opt out via the
    # BERNSTEIN_AUTH_DISABLED env var or the ``auth.enabled`` seed key —
    # both paths log a loud warning on startup.
    from bernstein.core.security.auth_middleware import auth_disabled_via_opt_out

    auth_disabled_flag = auth_disabled_via_opt_out()

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
        return _do_reload_seed_config(workdir, jsonl_path, application)

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
        # Close the persistent access-log file handle (audit-080).
        middleware_stack = getattr(app, "middleware_stack", None)
        while middleware_stack is not None:
            if isinstance(middleware_stack, StructuredAccessLogMiddleware):
                await middleware_stack.aclose()
                break
            middleware_stack = getattr(middleware_stack, "app", None)

    application = FastAPI(
        title="Bernstein Task Server",
        version="1.0.0",
        description=(
            "Bernstein REST API — multi-agent orchestration for CLI coding agents.\n\n"
            "## Authentication\n\n"
            "Authentication is ENABLED by default.  Include a Bearer token in "
            "all requests:\n\n"
            "```\nAuthorization: Bearer <token>\n```\n\n"
            "To run without auth (development only), set `BERNSTEIN_AUTH_DISABLED=1` "
            "— this logs a loud warning and passes every request through.\n\n"
            "Public endpoints (no auth required): `/health`, `/health/ready`, "
            "`/health/live`, `/ready`, `/alive`, `/.well-known/agent.json`, "
            "`/docs`, `/openapi.json`, and the auth-flow endpoints "
            "(`/auth/login`, `/auth/oidc/callback`, etc.).\n\n"
            "Webhook and hook endpoints (`/webhook`, `/webhooks/*`, "
            "`/hooks/{session_id}`) authenticate via HMAC-SHA256 signatures "
            "— they do NOT accept Bearer tokens.\n\n"
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

    # audit-117: body-size cap.  Added BEFORE auth/rate-limit so oversized bodies
    # are rejected with 413 without ever being buffered into memory.  Starlette
    # orders ``add_middleware`` calls from innermost (first to handle the
    # response) to outermost (first to see the request), so registering this
    # here places it outside the auth middleware layer.
    application.add_middleware(ContentLengthMiddleware)

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
        auth_disabled=auth_disabled_flag,
    )

    # Per-endpoint request rate limiting — reads buckets from app.state.seed_config.
    application.add_middleware(RequestRateLimitMiddleware)

    # audit-122: SSE reconnect-frequency limiter. Rejects /events clients that
    # reconnect faster than 3 times per 60 s for a 5-minute cooldown.
    application.add_middleware(SSEReconnectLimiterMiddleware)

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

    # audit-118: starlette.middleware.cors.CORSMiddleware compares
    # ``allow_origins`` LITERALLY — so ``http://localhost:*`` never matches
    # a real ``http://localhost:3000``.  Detect glob patterns, strip them
    # from the literal list, and translate them to a regex passed via
    # ``allow_origin_regex`` so wildcard ports actually work.
    literal_origins, origin_regex = _split_cors_origins(cors_config.allowed_origins)

    cors_kwargs: dict[str, Any] = {
        "allow_origins": literal_origins,
        "allow_methods": list(cors_config.allow_methods),
        "allow_headers": list(cors_config.allow_headers),
        "allow_credentials": cors_config.allow_credentials,
        "max_age": cors_config.max_age,
    }
    if origin_regex is not None:
        cors_kwargs["allow_origin_regex"] = origin_regex

    application.add_middleware(CORSMiddleware, **cors_kwargs)

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
    from bernstein.core.routes.acp import router as acp_router
    from bernstein.core.routes.agent_comparison import router as agent_comparison_router
    from bernstein.core.routes.api_v1 import router as api_v1_router
    from bernstein.core.routes.approvals import router as approvals_router
    from bernstein.core.routes.audit_log import router as audit_log_router
    from bernstein.core.routes.batch_ops import router as batch_ops_router
    from bernstein.core.routes.custom_metrics import router as custom_metrics_router
    from bernstein.core.routes.drain import router as drain_router
    from bernstein.core.routes.export import router as export_router
    from bernstein.core.routes.file_health import router as file_health_router
    from bernstein.core.routes.gateway import router as gateway_router
    from bernstein.core.routes.graduation import router as graduation_router
    from bernstein.core.routes.grafana import router as grafana_router
    from bernstein.core.routes.graphql_api import router as graphql_router
    from bernstein.core.routes.health import router as health_deps_router
    from bernstein.core.routes.hooks import router as hooks_router
    from bernstein.core.routes.identities import router as identities_router
    from bernstein.core.routes.paginated_tasks import router as paginated_tasks_router
    from bernstein.core.routes.plans import router as plans_router
    from bernstein.core.routes.predictive import router as predictive_router
    from bernstein.core.routes.provider_latency import router as provider_latency_router
    from bernstein.core.routes.sbom import router as sbom_router
    from bernstein.core.routes.slo import router as slo_router
    from bernstein.core.routes.task_detail import router as task_detail_router
    from bernstein.core.routes.team import router as team_router
    from bernstein.core.routes.websocket import router as ws_router

    # Full roster of application routers.
    #
    # AUDIT-126 — the /api/v1 mount used to receive only a hand-picked subset
    # (tasks, status, costs, export, grafana, health, batch_ops, etc.), so
    # newer routes silently lacked a versioned counterpart. Collecting every
    # router here and iterating once guarantees that adding a new router
    # mounts it under both the legacy root path and /api/v1 in lockstep.
    #
    # Order matters: paginated_tasks_router must precede tasks_router so that
    # /tasks/search is matched before the /tasks/{task_id} catch-all.
    all_routers = [
        paginated_tasks_router,
        agents_router,
        auth_router,
        tasks_router,
        status_router,
        workspace_router,
        webhooks_router,
        discord_router,
        slack_router,
        costs_router,
        dashboard_router,
        team_dashboard_router,
        graph_router,
        observability_router,
        quality_router,
        file_health_router,
        drain_router,
        identities_router,
        acp_router,
        approvals_router,
        plans_router,
        gateway_router,
        slo_router,
        custom_metrics_router,
        sbom_router,
        hooks_router,
        ws_router,
        export_router,
        grafana_router,
        task_detail_router,
        health_deps_router,
        batch_ops_router,
        agent_comparison_router,
        audit_log_router,
        graphql_router,
        graduation_router,
        team_router,
        provider_latency_router,
        predictive_router,
    ]

    for r in all_routers:
        application.include_router(r)
        # WEB-007 / AUDIT-126: expose the same router under /api/v1/* so the
        # versioned surface stays in parity with the legacy root paths.
        api_v1_router.include_router(r)

    # Gateway metrics — active only when a gateway session is running.
    application.state.mcp_gateway = None  # type: ignore[attr-defined]

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
