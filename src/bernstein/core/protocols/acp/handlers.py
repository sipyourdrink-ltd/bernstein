"""ACP request handlers wired onto the existing Bernstein primitives.

The handler layer is deliberately *pure*: it takes injected dependencies
(task creator, cancel hook, audit emitter, permission gate) and never
constructs them.  This lets unit tests substitute lightweight fakes
while production code wires in the real task store, drain pipeline,
HMAC audit chain, and janitor approval gate.

A single :class:`ACPHandlerRegistry` holds bound coroutines for every
ACP method.  The transport layer dispatches against this registry; it
does NOT introspect the protocol surface itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from bernstein.core.protocols.acp.metrics import (
    record_acp_message,
    set_active_sessions,
)
from bernstein.core.protocols.acp.schema import (
    ACP_PROTOCOL_VERSION,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    PERMISSION_DENIED,
    SESSION_NOT_FOUND,
    ACPSchemaError,
)
from bernstein.core.protocols.acp.session import ACPSession, ACPSessionStore

logger = logging.getLogger(__name__)


class SessionMode(StrEnum):
    """Allowed approval-gate modes for an ACP session."""

    AUTO = "auto"
    MANUAL = "manual"


# Permission round-trip default timeout.  Long enough for a human to read
# the prompt; short enough that a hung IDE does not stall the agent
# forever.  Surfaced in defaults so it is configurable without touching
# this module.
_PERMISSION_TIMEOUT_S: Final[float] = 60.0


# ---------------------------------------------------------------------------
# Injected-dependency protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptResult:
    """Outcome of opening a Bernstein task for an ACP prompt.

    Attributes:
        session_id: Bernstein task id (also returned to the IDE as the
            ACP session id).
        accepted: ``True`` when the task was created.  ``False`` is used
            to surface deferred errors that the prompt handler converts
            to a JSON-RPC error envelope.
        message: Optional diagnostic, surfaced to the IDE when
            ``accepted=False``.
    """

    session_id: str
    accepted: bool = True
    message: str = ""


# Type aliases for injected callbacks.  They are async because the real
# task store is async; tests can wrap sync helpers with ``asyncio.coroutine``
# semantics by writing ``async def``.
TaskCreator = Callable[[str, str, str], Awaitable[PromptResult]]
"""Signature: ``async (prompt, cwd, role) -> PromptResult``."""

TaskCanceller = Callable[[str, str], Awaitable[bool]]
"""Signature: ``async (session_id, reason) -> bool``."""

StreamPublisher = Callable[[dict[str, Any]], Awaitable[None]]
"""Signature: ``async (frame) -> None`` — publishes a JSON-RPC frame to the IDE."""

PermissionAsker = Callable[[str, str, str], Awaitable[str]]
"""Signature: ``async (session_id, tool, detail) -> "approved"|"rejected"``.

Default implementation routes through the ACP transport so the IDE sees
a ``requestPermission`` notification.  Tests can substitute a stub that
returns instantly.
"""

AuditEmitter = Callable[[str, str, dict[str, Any]], None]
"""Signature: ``(event_type, resource_id, details) -> None``.

The handler layer calls this before returning a response so the HMAC
chain captures every ACP-driven mutation.  ACP-initiated audit entries
are byte-identical to CLI-initiated entries because both call sites pass
the same ``event_type`` strings.
"""


# ---------------------------------------------------------------------------
# Default no-op dependencies (production wires in the real ones).
# ---------------------------------------------------------------------------


async def _default_task_creator(prompt: str, cwd: str, role: str) -> PromptResult:
    """Fallback creator used when no task store is wired in.

    The session id is a deterministic hash of the prompt + timestamp so
    tests can assert against it.

    Args:
        prompt: The prompt text.
        cwd: Working directory.
        role: Bernstein role hint.

    Returns:
        A :class:`PromptResult` with ``accepted=True``.
    """
    del prompt, cwd, role
    sid = f"acp-{int(time.time() * 1000):x}"
    return PromptResult(session_id=sid, accepted=True)


async def _default_task_canceller(session_id: str, reason: str) -> bool:
    """Fallback canceller — logs the cancellation and returns ``True``."""
    logger.info("acp.cancel session=%s reason=%s (no canceller wired)", session_id, reason)
    return True


async def _default_stream_publisher(frame: dict[str, Any]) -> None:
    """Fallback publisher — drops the frame on the floor."""
    logger.debug("acp.stream dropped frame method=%s", frame.get("method"))


def _default_audit_emitter(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
    """Fallback audit emitter — logs at INFO level for visibility."""
    logger.info("acp.audit event=%s resource=%s details=%s", event_type, resource_id, details)


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ACPRequestContext:
    """Context passed to every handler invocation.

    Attributes:
        method: ACP method name.
        request_id: JSON-RPC ``id`` (or ``None`` for notifications).
        peer: Free-form transport identifier (e.g. ``"stdio"``,
            ``"http://1.2.3.4"``).  Recorded in audit events.
    """

    method: str
    request_id: str | int | None
    peer: str = "stdio"


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


@dataclass
class ACPHandlerRegistry:
    """Bundle of ACP method handlers.

    The registry owns the :class:`ACPSessionStore` and dispatches ACP
    methods against it.  All side-effecting operations (task creation,
    cancellation, stream publication, audit) go through injected
    callables so production wiring (real task store + HMAC audit) and
    tests (in-memory fakes) share the same code path.

    Attributes:
        sessions: Per-IDE session store.
        adapters: Sorted list of available adapter names (surfaced from
            ``initialize``).
        sandbox_backends: Sorted list of configured sandbox backends.
        task_creator: Async callable that opens a Bernstein task.
        task_canceller: Async callable that walks the drain pipeline.
        stream_publisher: Async callable that pushes a frame to the IDE.
        permission_asker: Async callable that surfaces a permission
            prompt to the IDE; defaults to a transport-driven prompt.
        audit_emitter: Sync callable that appends to the HMAC chain.
        permission_timeout_s: Per-request timeout for permission
            round-trips.
    """

    sessions: ACPSessionStore = field(default_factory=ACPSessionStore)
    adapters: tuple[str, ...] = ()
    sandbox_backends: tuple[str, ...] = ()
    task_creator: TaskCreator = field(default=_default_task_creator)
    task_canceller: TaskCanceller = field(default=_default_task_canceller)
    stream_publisher: StreamPublisher = field(default=_default_stream_publisher)
    permission_asker: PermissionAsker | None = None
    audit_emitter: AuditEmitter = field(default=_default_audit_emitter)
    permission_timeout_s: float = _PERMISSION_TIMEOUT_S

    # Bookkeeping for permission round-trips routed through the transport.
    # Maps ``promptId -> (session_id, asyncio.Event placeholder)``.

    def __post_init__(self) -> None:
        if self.permission_asker is None:
            object.__setattr__(self, "permission_asker", self._default_permission_asker)

    # -- Public dispatch ----------------------------------------------------

    async def dispatch(self, ctx: ACPRequestContext, params: dict[str, Any]) -> Any:
        """Dispatch *ctx.method* and return the JSON-serialisable result.

        Args:
            ctx: Validated request context.
            params: Already-validated parameters.

        Returns:
            A JSON-serialisable result for response methods, or ``None``
            for notifications.

        Raises:
            ACPSchemaError: For mapped errors that the transport should
                surface as JSON-RPC errors.
        """
        try:
            handler = self._handlers[ctx.method]
        except KeyError as exc:  # pragma: no cover — schema layer rejects first
            raise ACPSchemaError(INTERNAL_ERROR, f"no handler for {ctx.method!r}") from exc

        try:
            result = await handler(ctx, params)
        except ACPSchemaError as exc:
            outcome = "rejected" if exc.code == PERMISSION_DENIED else "error"
            record_acp_message(ctx.method, outcome)
            raise
        except Exception as exc:
            record_acp_message(ctx.method, "error")
            logger.exception("acp.handler crashed method=%s", ctx.method)
            raise ACPSchemaError(INTERNAL_ERROR, f"handler crashed: {exc}") from exc

        record_acp_message(ctx.method, "ok")
        return result

    @property
    def _handlers(self) -> dict[str, Callable[[ACPRequestContext, dict[str, Any]], Awaitable[Any]]]:
        return {
            "initialize": self._handle_initialize,
            "initialized": self._handle_initialized,
            "prompt": self._handle_prompt,
            "cancel": self._handle_cancel,
            "setMode": self._handle_set_mode,
            "requestPermission": self._handle_request_permission,
            # streamUpdate is server -> client; if we receive one we ack
            # silently to keep parity with the client/server symmetry that
            # some IDE clients expect.
            "streamUpdate": self._handle_stream_update,
        }

    # -- Capability negotiation --------------------------------------------

    async def _handle_initialize(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Reply with Bernstein capabilities, adapters, and sandbox backends.

        Honours the IDE's requested ``protocolVersion`` when compatible;
        otherwise downgrades to the version this server speaks.
        """
        requested = params.get("protocolVersion") or ACP_PROTOCOL_VERSION
        # Even if the requested version differs, we report ours.  An IDE
        # is free to refuse the handshake by closing the transport.
        result: dict[str, Any] = {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "negotiatedProtocolVersion": ACP_PROTOCOL_VERSION,
            "clientRequestedVersion": requested,
            "serverInfo": {
                "name": "bernstein",
                "description": "Multi-agent orchestration system for CLI coding agents",
            },
            "capabilities": {
                "prompts": True,
                "streaming": True,
                "cancellation": True,
                "modes": ["auto", "manual"],
                "permissions": True,
            },
            "adapters": list(self.adapters),
            "sandboxBackends": list(self.sandbox_backends),
        }
        self.audit_emitter("acp.initialize", "session", {"peer": ctx.peer, "version": requested})
        return result

    async def _handle_initialized(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Acknowledge the IDE's ``initialized`` notification.

        Notifications return ``None`` so the transport suppresses the
        response envelope.
        """
        del params
        logger.debug("acp.initialized peer=%s", ctx.peer)
        return None

    # -- Prompt -> task -----------------------------------------------------

    async def _handle_prompt(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Open a Bernstein task and register the corresponding ACP session."""
        prompt = params["prompt"]
        cwd = params.get("cwd") or "."
        role = params.get("role") or "backend"
        mode_param = params.get("mode")
        if mode_param is not None and mode_param not in {"auto", "manual"}:
            raise ACPSchemaError(INVALID_PARAMS, f"invalid mode {mode_param!r}")

        outcome = await self.task_creator(prompt, cwd, role)
        if not outcome.accepted:
            raise ACPSchemaError(INTERNAL_ERROR, outcome.message or "task creation rejected")

        session = ACPSession(
            session_id=outcome.session_id,
            cwd=cwd,
            role=role,
            mode=mode_param or "manual",
        )
        await self.sessions.add(session)
        set_active_sessions(await self.sessions.count())
        self.audit_emitter(
            "acp.prompt",
            outcome.session_id,
            {"peer": ctx.peer, "cwd": cwd, "role": role, "mode": session.mode},
        )
        return {
            "sessionId": outcome.session_id,
            "mode": session.mode,
            "cwd": cwd,
            "role": role,
        }

    # -- Cancel -------------------------------------------------------------

    async def _handle_cancel(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Walk the drain + shutdown pipeline for *sessionId*."""
        session_id = params["sessionId"]
        reason = params.get("reason") or "client_cancel"
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")

        ok = await self.task_canceller(session_id, reason)
        await self.sessions.remove(session_id)
        set_active_sessions(await self.sessions.count())
        self.audit_emitter(
            "acp.cancel",
            session_id,
            {"peer": ctx.peer, "reason": reason, "ok": ok},
        )
        if not ok:
            return {"sessionId": session_id, "cancelled": False, "reason": reason}
        return {"sessionId": session_id, "cancelled": True, "reason": reason}

    # -- Mode toggle --------------------------------------------------------

    async def _handle_set_mode(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Toggle ``auto`` <-> ``manual`` and persist on the session."""
        session_id = params["sessionId"]
        mode = params["mode"]
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")
        try:
            session.set_mode(mode)
        except ValueError as exc:
            raise ACPSchemaError(INVALID_PARAMS, str(exc)) from exc
        self.audit_emitter("acp.set_mode", session_id, {"peer": ctx.peer, "mode": mode})
        return {"sessionId": session_id, "mode": session.mode}

    # -- Request permission round-trip -------------------------------------

    async def _handle_request_permission(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve an outstanding permission waiter with the IDE's decision."""
        session_id = params["sessionId"]
        prompt_id = params["promptId"]
        decision = params.get("decision") or "approved"
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")
        if not session.resolve_permission(prompt_id, decision):
            raise ACPSchemaError(
                PERMISSION_DENIED,
                f"no pending permission with id {prompt_id!r}",
            )
        self.audit_emitter(
            "acp.permission",
            session_id,
            {"peer": ctx.peer, "decision": decision, "promptId": prompt_id},
        )
        return {"sessionId": session_id, "promptId": prompt_id, "decision": decision}

    # -- Inbound streamUpdate (rare; mostly server-emitted) ----------------

    async def _handle_stream_update(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Accept inbound stream updates (no-op).

        ACP defines ``streamUpdate`` as server -> client; some test
        clients echo it.  We accept the frame, record metrics, and drop
        it.
        """
        del ctx, params
        return None

    # -- Stream emission ----------------------------------------------------

    async def emit_stream_update(self, session_id: str, delta: dict[str, Any] | str) -> None:
        """Push a ``streamUpdate`` notification to the IDE.

        Args:
            session_id: ACP session the update is associated with.
            delta: Token delta (string or structured payload).
        """
        from bernstein.core.protocols.acp.schema import make_notification

        frame = make_notification("streamUpdate", {"sessionId": session_id, "delta": delta})
        await self.stream_publisher(frame)

    # -- Default permission asker ------------------------------------------

    async def _default_permission_asker(
        self,
        session_id: str,
        tool: str,
        detail: str,
    ) -> str:
        """Surface a permission prompt to the IDE and await its decision.

        Looks up the session, opens a waiter, publishes a
        ``requestPermission`` notification, and awaits the
        ``requestPermission`` response that the IDE sends back.

        Args:
            session_id: Session id.
            tool: Tool the agent wants to invoke.
            detail: Human-readable description.

        Returns:
            ``"approved"`` or ``"rejected"``; on timeout returns
            ``"rejected"`` to fail closed.
        """
        from bernstein.core.protocols.acp.schema import make_notification

        session = await self.sessions.get(session_id)
        if session is None:
            return "rejected"
        if session.mode == "auto":
            return "approved"

        waiter = session.open_permission_waiter(tool, detail)
        frame = make_notification(
            "requestPermission",
            {
                "sessionId": session_id,
                "promptId": waiter.prompt_id,
                "tool": tool,
                "detail": detail,
            },
        )
        await self.stream_publisher(frame)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=self.permission_timeout_s)
        except TimeoutError:
            session.discard_waiter(waiter.prompt_id)
            return "rejected"
        decision = waiter.decision or "rejected"
        session.discard_waiter(waiter.prompt_id)
        return decision
