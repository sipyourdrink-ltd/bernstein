"""ACP request handlers wired onto the existing Bernstein primitives.

Supports official ACP v1 (``protocolVersion = 1``: ``initialize``,
``authenticate``, ``session/new``, ``session/load``, ``session/set_mode``,
blocking ``session/prompt`` with ``stopReason`` and ``usage``, notification
``session/cancel``, ``session/update``, and Agent -> Client
``session/request_permission`` RPCs) alongside the legacy ``"2025-04-01"``
methods so existing tests and integrations stay dual-compatible.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any, Final, cast

from bernstein.core.protocols.acp.metrics import (
    record_acp_message,
    set_active_sessions,
)
from bernstein.core.protocols.acp.projection import project_journal_to_acp_updates
from bernstein.core.protocols.acp.schema import (
    ACP_AUTH_EXEMPT_METHODS,
    ACP_LEGACY_METHODS,
    ACP_LEGACY_PROTOCOL_VERSION,
    ACP_V1_PRE_INIT_BLOCKED_METHODS,
    ACP_V1_PROTOCOL_VERSION,
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PERMISSION_DENIED,
    RESOURCE_NOT_FOUND,
    SESSION_NOT_FOUND,
    VALID_MODES,
    VALID_STOP_REASONS,
    ACPSchemaError,
    make_notification,
)
from bernstein.core.protocols.acp.session import (
    ACPSession,
    ACPSessionStore,
    load_persisted_session,
    resolve_session_journal_path,
    sanitize_session_run_id,
    save_persisted_session,
)

logger = logging.getLogger(__name__)


class SessionMode(StrEnum):
    """Allowed approval-gate modes for an ACP session."""

    AUTO = "auto"
    MANUAL = "manual"


_PERMISSION_TIMEOUT_S: Final[float] = 60.0
_MAX_PEER_STATES: Final[int] = 1024

_AVAILABLE_SESSION_MODES: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "manual",
        "name": "Manual approval",
        "description": "Prompt before sensitive tool actions",
    },
    {
        "id": "auto",
        "name": "Auto approve",
        "description": "Run governed tasks without interactive permission prompts",
    },
)

_DEFAULT_PERMISSION_OPTIONS: Final[tuple[dict[str, str], ...]] = (
    {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
)


def _resolve_agent_version() -> str:
    """Resolve installed Bernstein version without importing top-level package."""
    try:
        return _pkg_version("bernstein")
    except Exception:  # pragma: no cover - bare checkout fallback
        return "0.0.0"


# ---------------------------------------------------------------------------
# Injected-dependency protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptResult:
    """Outcome of opening a Bernstein task for an ACP prompt."""

    session_id: str
    accepted: bool = True
    message: str = ""


@dataclass(frozen=True)
class PromptRunOutcome:
    """Structured return value from a :data:`PromptRunner` execution."""

    stop_reason: str
    usage: dict[str, int] | None = None


type TaskCreator = Callable[[str, str, str], Awaitable[PromptResult]]
type TaskCanceller = Callable[[str, str], Awaitable[bool]]
type StreamPublisher = Callable[[dict[str, Any]], Awaitable[None]]
type ClientRequester = Callable[[str, dict[str, Any], float | None], Awaitable[dict[str, Any]]]
type PromptRunner = Callable[[ACPSession, list[dict[str, Any]], asyncio.Event], Awaitable[str | PromptRunOutcome]]
type SessionLoader = Callable[[str, str], Awaitable[tuple[ACPSession | None, list[dict[str, Any]]]]]
type AuthHandler = Callable[[str], Awaitable[bool]]
type PermissionAsker = Callable[[str, str, str], Awaitable[str]]
type AuditEmitter = Callable[[str, str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Default no-op dependencies
# ---------------------------------------------------------------------------


async def _default_task_creator(prompt: str, cwd: str, role: str) -> PromptResult:
    """Fallback creator used when no task store is wired in."""
    del prompt, cwd, role
    sid = f"acp-{int(time.time() * 1000):x}"
    return PromptResult(session_id=sid, accepted=True)


async def _default_task_canceller(session_id: str, reason: str) -> bool:
    """Fallback canceller - logs the cancellation and returns ``True``."""
    logger.info("acp.cancel session=%s reason=%s (no canceller wired)", session_id, reason)
    return True


async def _default_stream_publisher(frame: dict[str, Any]) -> None:
    """Fallback publisher - drops the frame on the floor."""
    logger.debug("acp.stream dropped frame method=%s", frame.get("method"))


def _default_audit_emitter(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
    """Fallback audit emitter - logs at INFO level for visibility."""
    logger.info("acp.audit event=%s resource=%s details=%s", event_type, resource_id, details)


def extract_prompt_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten ACP v1 ContentBlock list into plain prompt text."""
    parts: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
        elif btype == "resource_link":
            name = block.get("name", "")
            uri = block.get("uri", "")
            parts.append(f"[{name}]({uri})")
        elif btype == "resource":
            res_raw = block.get("resource")
            if isinstance(res_raw, dict):
                res = cast("dict[str, Any]", res_raw)
                res_text = res.get("text")
                if isinstance(res_text, str) and res_text:
                    parts.append(res_text)
                else:
                    res_uri = res.get("uri")
                    if isinstance(res_uri, str):
                        parts.append(f"[resource]({res_uri})")
    return "\n".join(parts)


def _resolve_validated_cwd(cwd: Any) -> Path:
    """Validate *cwd* as an absolute path without NUL bytes and return its canonical ``Path``."""
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        raise ACPSchemaError(INVALID_PARAMS, "cwd must be a valid non-empty path string", {"field": "cwd"})
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        raise ACPSchemaError(INVALID_PARAMS, "cwd must be an absolute path", {"field": "cwd"})
    try:
        return cwd_path.resolve()
    except (OSError, ValueError) as exc:
        raise ACPSchemaError(INVALID_PARAMS, f"invalid cwd path: {exc}", {"field": "cwd"}) from exc


def _client_supports_terminal_auth(client_caps: dict[str, Any]) -> bool:
    """Return True if the client advertises terminal auth via spec or Registry CI flag."""
    auth_caps_raw = client_caps.get("auth")
    if isinstance(auth_caps_raw, dict):
        auth_caps = cast("dict[str, Any]", auth_caps_raw)
        if auth_caps.get("terminal") is True:
            return True
    meta_raw = client_caps.get("_meta")
    if isinstance(meta_raw, dict):
        meta = cast("dict[str, Any]", meta_raw)
        return meta.get("terminal-auth") is True
    return False


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ACPRequestContext:
    """Context passed to every handler invocation.

    Attributes:
        method: Validated ACP method name.
        request_id: JSON-RPC request ``id`` (``None`` for notifications).
        peer: Peer identifier (``"stdio"`` or ``"http://<ip>"``).
        connection_scoped: ``True`` for stateful single-connection transports
            such as stdio where connection-wide legacy/v1 lockouts apply;
            ``False`` for stateless HTTP POSTs where multiple clients may share
            a loopback or reverse-proxy IP and protocol isolation is enforced
            per session instead.
    """

    method: str
    request_id: str | int | None
    peer: str = "stdio"
    connection_scoped: bool = True


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


@dataclass
class _PeerState:
    """Per-peer protocol negotiation and authentication state."""

    initialized: bool = False
    negotiated_version: int | str | None = None
    client_capabilities: dict[str, Any] = field(default_factory=dict[str, Any])
    client_info: dict[str, Any] | None = None
    advertised_auth_method_ids: set[str] = field(default_factory=set[str])
    authenticated: bool = False


@dataclass
class ACPHandlerRegistry:
    """Bundle of ACP method handlers supporting ACP v1 and legacy 2025-04-01.

    Connection-level handshake and authentication state is stored per peer in a
    bounded LRU map (:data:`_MAX_PEER_STATES`).  Re-calling ``initialize`` on an
    existing peer resets ``authenticated = False`` for that peer only, so a
    re-initialized stdio connection must call ``authenticate`` again when
    ``require_auth=True``.
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
    agent_version: str = field(default_factory=_resolve_agent_version)
    prompt_runner: PromptRunner | None = None
    session_loader: SessionLoader | None = None
    client_requester: ClientRequester | None = None
    auth_handler: AuthHandler | None = None
    terminal_auth_method: dict[str, Any] | None = None
    extra_auth_methods: tuple[dict[str, Any], ...] = ()
    require_auth: bool = False

    _peer_states: OrderedDict[str, _PeerState] = field(
        default_factory=OrderedDict[str, _PeerState],
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.permission_asker is None:
            object.__setattr__(self, "permission_asker", self._default_permission_asker)

    def _get_peer_state(self, peer: str) -> _PeerState | None:
        """Return existing per-peer state and refresh its LRU recency."""
        state = self._peer_states.get(peer)
        if state is not None:
            self._peer_states.move_to_end(peer)
        return state

    def _get_or_create_peer_state(self, peer: str) -> _PeerState:
        """Return existing per-peer state (MRU) or allocate a bounded LRU entry for *peer*."""
        state = self._get_peer_state(peer)
        if state is None:
            while len(self._peer_states) >= _MAX_PEER_STATES:
                self._peer_states.popitem(last=False)
            state = _PeerState()
            self._peer_states[peer] = state
        return state

    def client_name_for_peer(self, peer: str = "stdio") -> str:
        """Return negotiated client name for *peer* (or ``'acp-client'`` fallback)."""
        state = self._peer_states.get(peer)
        if state is not None and isinstance(state.client_info, dict):
            name = state.client_info.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "acp-client"

    @property
    def client_name(self) -> str:
        """Return negotiated client name (preferring ``stdio``, else latest peer, else ``'acp-client'``)."""
        stdio_name = self.client_name_for_peer("stdio")
        if stdio_name != "acp-client" or "stdio" in self._peer_states:
            return stdio_name
        for peer in reversed(list(self._peer_states)):
            candidate = self.client_name_for_peer(peer)
            if candidate != "acp-client":
                return candidate
        return "acp-client"

    # -- Public dispatch ----------------------------------------------------

    async def dispatch(self, ctx: ACPRequestContext, params: dict[str, Any]) -> Any:
        """Dispatch *ctx.method* and return the JSON-serialisable result."""
        peer_state = self._get_peer_state(ctx.peer)
        is_initialized = peer_state.initialized if peer_state is not None else False
        negotiated_version = peer_state.negotiated_version if peer_state is not None else None
        is_authenticated = peer_state.authenticated if peer_state is not None else False

        if ctx.method in ACP_V1_PRE_INIT_BLOCKED_METHODS and not is_initialized:
            record_acp_message(ctx.method, "error")
            raise ACPSchemaError(
                INVALID_REQUEST,
                "connection not initialized; call 'initialize' first",
                {"method": ctx.method},
            )

        if ctx.connection_scoped and negotiated_version == ACP_V1_PROTOCOL_VERSION and ctx.method in ACP_LEGACY_METHODS:
            record_acp_message(ctx.method, "error")
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"legacy method {ctx.method!r} cannot be used on an ACP v1 connection",
                {"method": ctx.method},
            )

        if self.require_auth and not is_authenticated and ctx.method not in ACP_AUTH_EXEMPT_METHODS:
            record_acp_message(ctx.method, "error")
            raise ACPSchemaError(
                AUTH_REQUIRED,
                "authentication required",
                {"method": ctx.method},
            )

        try:
            handler = self._handlers[ctx.method]
        except KeyError as exc:
            record_acp_message(ctx.method, "error")
            raise ACPSchemaError(
                METHOD_NOT_FOUND,
                f"method {ctx.method!r} is not accepted from client",
                {"method": ctx.method},
            ) from exc

        try:
            result = await handler(ctx, params)
        except ACPSchemaError as exc:
            outcome = "rejected" if (exc.code == PERMISSION_DENIED and ctx.method == "requestPermission") else "error"
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
            # ACP v1
            "initialize": self._handle_initialize,
            "authenticate": self._handle_authenticate,
            "session/new": self._handle_session_new,
            "session/load": self._handle_session_load,
            "session/set_mode": self._handle_session_set_mode,
            "session/prompt": self._handle_session_prompt,
            "session/cancel": self._handle_session_cancel,
            "session/update": self._handle_session_update,
            # Legacy 2025-04-01
            "initialized": self._handle_initialized,
            "prompt": self._handle_prompt,
            "cancel": self._handle_cancel,
            "setMode": self._handle_set_mode,
            "requestPermission": self._handle_request_permission,
            "streamUpdate": self._handle_stream_update,
        }

    # -- Capability negotiation --------------------------------------------

    async def _handle_initialize(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Negotiate ACP v1 (integer ``protocolVersion``) or legacy string dialect.

        Re-calling ``initialize`` on an existing peer resets ``authenticated`` to
        ``False`` for that peer only, so a stdio connection must call
        ``authenticate`` again after re-initializing when ``require_auth=True``.
        """
        raw_version = params.get("protocolVersion")
        client_caps_raw = params.get("clientCapabilities")
        client_caps: dict[str, Any] = (
            dict(cast("dict[str, Any]", client_caps_raw)) if isinstance(client_caps_raw, dict) else {}
        )
        client_info_raw = params.get("clientInfo")
        client_info: dict[str, Any] | None = (
            dict(cast("dict[str, Any]", client_info_raw)) if isinstance(client_info_raw, dict) else None
        )

        state = self._get_or_create_peer_state(ctx.peer)
        state.initialized = True
        state.client_capabilities = client_caps
        state.client_info = client_info
        state.authenticated = False

        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            state.negotiated_version = ACP_V1_PROTOCOL_VERSION
            auth_methods: list[dict[str, Any]] = []
            if self.terminal_auth_method is not None and _client_supports_terminal_auth(client_caps):
                auth_methods.append(dict(self.terminal_auth_method))
            for method_def in self.extra_auth_methods:
                auth_methods.append(dict(method_def))
            state.advertised_auth_method_ids = {str(m["id"]) for m in auth_methods if isinstance(m.get("id"), str)}

            result: dict[str, Any] = {
                "protocolVersion": ACP_V1_PROTOCOL_VERSION,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {
                        "audio": False,
                        "embeddedContext": False,
                        "image": False,
                    },
                    "mcpCapabilities": {
                        "acp": False,
                        "http": False,
                        "sse": False,
                    },
                    "sessionCapabilities": {},
                    "auth": {},
                },
                "agentInfo": {
                    "name": "bernstein",
                    "title": "Bernstein",
                    "version": self.agent_version,
                },
                "authMethods": auth_methods,
                "_meta": {
                    "adapters": list(self.adapters),
                    "sandboxBackends": list(self.sandbox_backends),
                },
            }
            self.audit_emitter("acp.initialize", "session", {"peer": ctx.peer, "version": raw_version})
            return result

        # Legacy 2025-04-01 handshake (string protocolVersion or omitted)
        requested = raw_version if isinstance(raw_version, str) and raw_version else ACP_LEGACY_PROTOCOL_VERSION
        state.negotiated_version = ACP_LEGACY_PROTOCOL_VERSION
        state.advertised_auth_method_ids = set()
        legacy_result: dict[str, Any] = {
            "protocolVersion": ACP_LEGACY_PROTOCOL_VERSION,
            "negotiatedProtocolVersion": ACP_LEGACY_PROTOCOL_VERSION,
            "clientRequestedVersion": requested,
            "serverInfo": {
                "name": "bernstein",
                "description": "An open-source governance layer for AI agents, one git worktree per task",
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
        return legacy_result

    # -- ACP v1: authenticate -----------------------------------------------

    async def _handle_authenticate(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle ACP v1 ``authenticate({methodId}) -> {}``."""
        method_id: str = params["methodId"]
        state = self._get_peer_state(ctx.peer)
        advertised: set[str] = state.advertised_auth_method_ids if state is not None else set()
        if method_id not in advertised:
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"unknown authentication method {method_id!r}",
                {"field": "methodId", "methodId": method_id},
            )
        if self.auth_handler is None:
            raise ACPSchemaError(
                AUTH_REQUIRED,
                "no authentication handler is configured",
                {"methodId": method_id},
            )
        ok = await self.auth_handler(method_id)
        if not ok:
            raise ACPSchemaError(
                AUTH_REQUIRED,
                f"authentication failed for method {method_id!r}",
                {"methodId": method_id},
            )
        assert state is not None
        state.authenticated = True
        self.audit_emitter("acp.authenticate", "session", {"peer": ctx.peer, "methodId": method_id})
        return {}

    # -- ACP v1: session/new & session/load ---------------------------------

    async def _handle_session_new(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle ACP v1 ``session/new`` and return ``{sessionId, modes}``."""
        state = self._get_peer_state(ctx.peer)
        if self.require_auth and not (state is not None and state.authenticated):
            raise ACPSchemaError(
                AUTH_REQUIRED,
                "authentication required before creating a session",
            )
        cwd: str = params["cwd"]
        _resolve_validated_cwd(cwd)
        mcp_servers_raw: list[dict[str, Any]] = params["mcpServers"]
        additional_dirs_raw = params.get("additionalDirectories")
        additional_dirs: tuple[str, ...] = (
            tuple(str(d) for d in cast("list[Any]", additional_dirs_raw))
            if isinstance(additional_dirs_raw, list)
            else ()
        )
        system_prompt = params.get("systemPrompt")

        session_id = f"sess-{uuid.uuid4().hex[:16]}"
        session = ACPSession(
            session_id=session_id,
            cwd=cwd,
            mode="manual",
            mcp_servers=tuple(dict(s) for s in mcp_servers_raw),
            additional_directories=additional_dirs,
            system_prompt=system_prompt,
            protocol_version=ACP_V1_PROTOCOL_VERSION,
            peer=ctx.peer,
        )
        await self.sessions.add(session)
        save_persisted_session(session)
        set_active_sessions(await self.sessions.count())
        self.audit_emitter(
            "acp.session_new",
            session_id,
            {"peer": ctx.peer, "cwd": cwd, "mode": session.mode},
        )
        return {
            "sessionId": session_id,
            "modes": {
                "currentModeId": session.mode,
                "availableModes": [dict(m) for m in _AVAILABLE_SESSION_MODES],
            },
        }

    async def _default_session_loader(
        self,
        cwd: str,
        session_id: str,
    ) -> tuple[ACPSession | None, list[dict[str, Any]]]:
        """Load session state and replayable journal updates from memory or disk."""
        req_cwd = _resolve_validated_cwd(cwd)
        in_mem = await self.sessions.get(session_id)
        if in_mem is not None:
            if in_mem.protocol_version != ACP_V1_PROTOCOL_VERSION:
                return None, []
            if "\x00" in in_mem.cwd or not Path(in_mem.cwd).is_absolute():
                return None, []
            if Path(in_mem.cwd).resolve() != req_cwd:
                return None, []
            if in_mem.run_id != sanitize_session_run_id(session_id):
                return None, []
            jpath, journal_ok = resolve_session_journal_path(req_cwd / ".sdd", in_mem.run_id)
            if not journal_ok:
                return None, []
            updates = project_journal_to_acp_updates(jpath, include_user_messages=True) if jpath is not None else []
            return in_mem, updates

        loaded, jpath = load_persisted_session(cwd, session_id)
        if loaded is None:
            return None, []
        updates = project_journal_to_acp_updates(jpath, include_user_messages=True) if jpath is not None else []
        return loaded, updates

    async def _handle_session_load(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle ACP v1 ``session/load`` by streaming journal history then returning ``{modes}``."""
        state = self._get_peer_state(ctx.peer)
        if self.require_auth and not (state is not None and state.authenticated):
            raise ACPSchemaError(
                AUTH_REQUIRED,
                "authentication required before loading a session",
            )
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ACPSchemaError(INVALID_PARAMS, "sessionId must be a valid non-empty string", {"field": "sessionId"})
        cwd = params.get("cwd")
        req_cwd = _resolve_validated_cwd(cwd)
        assert isinstance(cwd, str)
        mcp_servers_raw: list[dict[str, Any]] = params["mcpServers"]
        additional_dirs_raw = params.get("additionalDirectories")
        additional_dirs: tuple[str, ...] = (
            tuple(str(d) for d in cast("list[Any]", additional_dirs_raw))
            if isinstance(additional_dirs_raw, list)
            else ()
        )
        system_prompt = params.get("systemPrompt")

        existing = await self.sessions.get(session_id)
        if existing is not None:
            if (
                existing.protocol_version != ACP_V1_PROTOCOL_VERSION
                or "\x00" in existing.cwd
                or not Path(existing.cwd).is_absolute()
                or Path(existing.cwd).resolve() != req_cwd
            ):
                raise ACPSchemaError(
                    RESOURCE_NOT_FOUND,
                    f"unknown session {session_id!r}",
                    {"sessionId": session_id},
                )
            if existing.has_active_turn:
                raise ACPSchemaError(
                    INVALID_REQUEST,
                    f"session {session_id!r} already has an active prompt turn",
                    {"sessionId": session_id},
                )

        loader = self.session_loader or self._default_session_loader
        session, replayed_updates = await loader(cwd, session_id)
        if (
            session is None
            or session.session_id != session_id
            or session.protocol_version != ACP_V1_PROTOCOL_VERSION
            or "\x00" in session.cwd
            or not Path(session.cwd).is_absolute()
            or Path(session.cwd).resolve() != req_cwd
        ):
            raise ACPSchemaError(
                RESOURCE_NOT_FOUND,
                f"unknown session {session_id!r}",
                {"sessionId": session_id},
            )
        if session.has_active_turn:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} already has an active prompt turn",
                {"sessionId": session_id},
            )

        latest = await self.sessions.get(session_id)
        if latest is not None:
            if latest.protocol_version != ACP_V1_PROTOCOL_VERSION or Path(latest.cwd).resolve() != req_cwd:
                raise ACPSchemaError(
                    RESOURCE_NOT_FOUND,
                    f"unknown session {session_id!r}",
                    {"sessionId": session_id},
                )
            if latest.has_active_turn:
                raise ACPSchemaError(
                    INVALID_REQUEST,
                    f"session {session_id!r} already has an active prompt turn",
                    {"sessionId": session_id},
                )

        session.peer = ctx.peer
        session.mcp_servers = tuple(dict(s) for s in mcp_servers_raw)
        session.additional_directories = additional_dirs
        if system_prompt is not None:
            session.system_prompt = system_prompt
        session.protocol_version = ACP_V1_PROTOCOL_VERSION
        session.touch()

        await self.sessions.upsert(session)
        set_active_sessions(await self.sessions.count())

        for update in replayed_updates:
            await self.emit_session_update(session_id, update)

        self.audit_emitter(
            "acp.session_load",
            session_id,
            {"peer": ctx.peer, "cwd": cwd, "replayedUpdates": len(replayed_updates)},
        )
        return {
            "modes": {
                "currentModeId": session.mode,
                "availableModes": [dict(m) for m in _AVAILABLE_SESSION_MODES],
            },
        }

    # -- ACP v1: session/set_mode -------------------------------------------

    async def _handle_session_set_mode(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle ACP v1 ``session/set_mode({sessionId, modeId}) -> {}``."""
        session_id: str = params["sessionId"]
        mode_id: str = params["modeId"]
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(
                RESOURCE_NOT_FOUND,
                f"unknown session {session_id!r}",
                {"sessionId": session_id},
            )
        if session.protocol_version != ACP_V1_PROTOCOL_VERSION:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} is not an ACP v1 session",
                {"sessionId": session_id},
            )
        if mode_id not in VALID_MODES:
            raise ACPSchemaError(
                INVALID_PARAMS,
                f"modeId must be one of {sorted(VALID_MODES)}",
                {"field": "modeId", "got": mode_id},
            )
        session.set_mode(mode_id)
        save_persisted_session(session)
        self.audit_emitter("acp.set_mode", session_id, {"peer": ctx.peer, "mode": mode_id})
        return {}

    # -- ACP v1: session/prompt ---------------------------------------------

    async def _default_prompt_runner(
        self,
        session: ACPSession,
        blocks: list[dict[str, Any]],
        cancel_event: asyncio.Event,
    ) -> str:
        """Default runner delegating to ``task_creator`` (used with ``--server-url``)."""
        if cancel_event.is_set():
            return "cancelled"
        prompt_text = extract_prompt_text(blocks) or "(empty prompt)"
        outcome = await self.task_creator(prompt_text, session.cwd, session.role)
        if not outcome.accepted:
            raise ACPSchemaError(INTERNAL_ERROR, outcome.message or "task creation rejected")
        session.set_active_task_id(outcome.session_id)
        if cancel_event.is_set():
            task_id = session.claim_cancel_task_id()
            if task_id is not None:
                ok = await self.task_canceller(task_id, session.cancel_reason or "client_cancel")
                return "cancelled" if ok else "end_turn"
            return "cancelled"
        return "end_turn"

    async def _handle_session_prompt(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle ACP v1 blocking ``session/prompt`` returning ``{stopReason, usage?}``."""
        session_id: str = params["sessionId"]
        blocks: list[dict[str, Any]] = params["prompt"]
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(
                RESOURCE_NOT_FOUND,
                f"unknown session {session_id!r}",
                {"sessionId": session_id},
            )
        if session.protocol_version != ACP_V1_PROTOCOL_VERSION:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} is not an ACP v1 session",
                {"sessionId": session_id},
            )
        if session.has_active_turn:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} already has an active prompt turn",
                {"sessionId": session_id},
            )

        session.peer = ctx.peer
        cancel_event = session.begin_turn()
        self.audit_emitter(
            "acp.prompt",
            session_id,
            {"peer": ctx.peer, "cwd": session.cwd, "role": session.role, "mode": session.mode},
        )
        runner = self.prompt_runner or self._default_prompt_runner
        usage_payload: dict[str, int] | None = None
        try:
            raw_outcome = await runner(session, blocks, cancel_event)
            if isinstance(raw_outcome, PromptRunOutcome):
                stop_reason = raw_outcome.stop_reason
                usage_payload = raw_outcome.usage
            else:
                stop_reason = raw_outcome
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
            stop_reason = "cancelled"
        finally:
            session.end_turn()
            save_persisted_session(session)

        if stop_reason not in VALID_STOP_REASONS:
            raise ACPSchemaError(
                INTERNAL_ERROR,
                f"prompt runner returned invalid stopReason {stop_reason!r}",
            )
        response: dict[str, Any] = {"stopReason": stop_reason}
        if usage_payload is not None:
            response["usage"] = usage_payload
        return response

    # -- ACP v1: session/cancel (notification) ------------------------------

    async def _handle_session_cancel(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Handle ACP v1 one-way ``session/cancel`` notification."""
        session_id: str = params["sessionId"]
        session = await self.sessions.get(session_id)
        if session is None:
            logger.debug("acp.session_cancel ignored for unknown session=%s", session_id)
            return None

        had_active = session.request_cancel("client_cancel")
        ok = False
        if had_active:
            task_id = session.claim_cancel_task_id()
            if task_id is not None:
                ok = await self.task_canceller(task_id, "client_cancel")
        self.audit_emitter(
            "acp.cancel",
            session_id,
            {"peer": ctx.peer, "reason": "client_cancel", "ok": ok, "active": had_active},
        )
        return None

    async def cancel_active_sessions(self, reason: str = "transport_eof") -> None:
        """Signal cancellation on all sessions with an active turn (e.g. on EOF)."""
        for session in list(self.sessions):
            if not session.has_active_turn:
                continue
            session.request_cancel(reason)
            task_id = session.claim_cancel_task_id()
            if task_id is not None:
                with contextlib.suppress(Exception):
                    await self.task_canceller(task_id, reason)

    async def _handle_session_update(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Accept echoed ``session/update`` notifications as a no-op."""
        del ctx, params
        return None

    # -- ACP v1 outbound notifications & Agent -> Client RPCs ---------------

    async def emit_session_update(self, session_id: str, update: dict[str, Any]) -> None:
        """Push an ACP v1 ``session/update`` notification to the client."""
        frame = make_notification(
            "session/update",
            {"sessionId": session_id, "update": update},
        )
        await self.stream_publisher(frame)

    async def emit_current_mode_update(self, session_id: str, mode_id: str) -> None:
        """Push an ACP v1 ``current_mode_update`` notification to the client."""
        await self.emit_session_update(
            session_id,
            {"sessionUpdate": "current_mode_update", "currentModeId": mode_id},
        )

    async def ask_permission_outcome(
        self,
        session_id: str,
        tool: str,
        detail: str,
        *,
        tool_call_id: str | None = None,
        kind: str = "other",
        locations: list[dict[str, Any]] | None = None,
        options: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """Request tool permission and return ``(decision, selected_option_kind)``."""
        session = await self.sessions.get(session_id)
        if session is None:
            return "rejected", "cancelled"
        if session.mode == "auto":
            return "approved", "auto"
        if tool in session.always_allowed_tools:
            return "approved", "allow_always"

        if self.client_requester is not None and session.protocol_version == ACP_V1_PROTOCOL_VERSION:
            tc_id = tool_call_id or f"call-{uuid.uuid4().hex[:12]}"
            perm_options = (
                [dict(opt) for opt in options]
                if options is not None
                else [dict(opt) for opt in _DEFAULT_PERMISSION_OPTIONS]
            )
            tool_call_obj: dict[str, Any] = {
                "toolCallId": tc_id,
                "title": detail or tool,
                "name": tool,
                "kind": kind,
                "status": "pending",
            }
            if locations:
                tool_call_obj["locations"] = locations
            params: dict[str, Any] = {
                "sessionId": session_id,
                "toolCall": tool_call_obj,
                "options": perm_options,
            }
            try:
                response = await _await_permission_or_cancel(
                    self.client_requester(
                        "session/request_permission",
                        params,
                        self.permission_timeout_s,
                    ),
                    session.active_cancel_event,
                )
            except ConnectionError as exc:
                logger.warning("acp.request_permission transport closed: %s", exc)
                session.request_cancel("transport_eof")
                decision, selected_kind = "rejected", "cancelled"
            except (TimeoutError, ACPSchemaError) as exc:
                logger.warning("acp.request_permission failed closed: %s", exc)
                decision, selected_kind = "rejected", "cancelled"
            else:
                decision, selected_kind = _interpret_permission_outcome(response, perm_options)
                if selected_kind == "allow_always":
                    session.always_allowed_tools.add(tool)
                    save_persisted_session(session)

            self.audit_emitter(
                "acp.permission",
                session_id,
                {
                    "peer": "acp-v1",
                    "decision": decision,
                    "optionKind": selected_kind,
                    "toolCallId": tc_id,
                    "tool": tool,
                },
            )
            return decision, selected_kind

        assert self.permission_asker is not None
        legacy_decision = await self.permission_asker(session_id, tool, detail)
        return legacy_decision, ("allow_once" if legacy_decision == "approved" else "reject_once")

    async def ask_permission(
        self,
        session_id: str,
        tool: str,
        detail: str,
        *,
        tool_call_id: str | None = None,
        kind: str = "other",
        options: list[dict[str, Any]] | None = None,
    ) -> str:
        """Request tool permission and return ``'approved'`` or ``'rejected'``."""
        decision, _ = await self.ask_permission_outcome(
            session_id,
            tool,
            detail,
            tool_call_id=tool_call_id,
            kind=kind,
            options=options,
        )
        return decision

    # -- Legacy 2025-04-01 handlers (preserved for dual-compatibility) ------

    async def _handle_initialized(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Acknowledge the legacy ``initialized`` notification."""
        del params
        logger.debug("acp.initialized peer=%s", ctx.peer)
        return None

    async def _handle_prompt(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Legacy ``prompt``: open a Bernstein task and register a one-shot session."""
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
            protocol_version=ACP_LEGACY_PROTOCOL_VERSION,
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

    async def _handle_cancel(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Legacy ``cancel``: walk drain pipeline and remove session."""
        session_id = params["sessionId"]
        reason = params.get("reason") or "client_cancel"
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")
        if session.protocol_version == ACP_V1_PROTOCOL_VERSION:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} is an ACP v1 session; use 'session/cancel'",
                {"sessionId": session_id},
            )

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

    async def _handle_set_mode(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Legacy ``setMode``: toggle ``auto`` <-> ``manual``."""
        session_id = params["sessionId"]
        mode = params["mode"]
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")
        if session.protocol_version == ACP_V1_PROTOCOL_VERSION:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} is an ACP v1 session; use 'session/set_mode'",
                {"sessionId": session_id},
            )
        try:
            session.set_mode(mode)
        except ValueError as exc:
            raise ACPSchemaError(INVALID_PARAMS, str(exc)) from exc
        self.audit_emitter("acp.set_mode", session_id, {"peer": ctx.peer, "mode": mode})
        return {"sessionId": session_id, "mode": session.mode}

    async def _handle_request_permission(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Legacy ``requestPermission``: resolve an outstanding permission waiter."""
        session_id = params["sessionId"]
        prompt_id = params["promptId"]
        decision = params.get("decision") or "approved"
        session = await self.sessions.get(session_id)
        if session is None:
            raise ACPSchemaError(SESSION_NOT_FOUND, f"unknown session {session_id!r}")
        if session.protocol_version == ACP_V1_PROTOCOL_VERSION:
            raise ACPSchemaError(
                INVALID_REQUEST,
                f"session {session_id!r} is an ACP v1 session; use bidirectional 'session/request_permission'",
                {"sessionId": session_id},
            )
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

    async def _handle_stream_update(
        self,
        ctx: ACPRequestContext,
        params: dict[str, Any],
    ) -> None:
        """Accept legacy inbound ``streamUpdate`` notifications as a no-op."""
        del ctx, params
        return None

    async def emit_stream_update(self, session_id: str, delta: dict[str, Any] | str) -> None:
        """Push a legacy ``streamUpdate`` notification to the IDE."""
        frame = make_notification("streamUpdate", {"sessionId": session_id, "delta": delta})
        await self.stream_publisher(frame)

    async def _default_permission_asker(
        self,
        session_id: str,
        tool: str,
        detail: str,
    ) -> str:
        """Legacy permission round-trip over ``stream_publisher``."""
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


async def _await_permission_or_cancel(
    req_coro: Awaitable[dict[str, Any]],
    cancel_event: asyncio.Event | None,
) -> dict[str, Any]:
    """Await a client permission response while respecting mid-turn cancellation."""
    if cancel_event is None:
        return await req_coro
    if cancel_event.is_set():
        if asyncio.iscoroutine(req_coro):
            req_coro.close()
        return {"outcome": {"outcome": "cancelled"}}

    req_task: asyncio.Task[dict[str, Any]] = asyncio.ensure_future(req_coro)
    cancel_task = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            {req_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if req_task in done:
            return req_task.result()
        req_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await req_task
        return {"outcome": {"outcome": "cancelled"}}
    finally:
        if not cancel_task.done():
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
        if not req_task.done():
            req_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await req_task


def _interpret_permission_outcome(
    response: dict[str, Any],
    options: list[dict[str, Any]],
) -> tuple[str, str]:
    """Map an ACP v1 ``RequestPermissionResponse`` result dict onto ``(decision, option_kind)``."""
    outcome_raw = response.get("outcome")
    if not isinstance(outcome_raw, dict):
        return "rejected", "cancelled"
    outcome = cast("dict[str, Any]", outcome_raw)
    if outcome.get("outcome") != "selected":
        return "rejected", "cancelled"
    option_id = outcome.get("optionId")
    if not isinstance(option_id, str):
        return "rejected", "cancelled"
    by_id = {str(opt.get("optionId")): str(opt.get("kind", "")) for opt in options}
    kind = by_id.get(option_id)
    if kind in {"allow_once", "allow_always"}:
        return "approved", kind
    if kind in {"reject_once", "reject_always"}:
        return "rejected", kind
    return "rejected", "cancelled"
