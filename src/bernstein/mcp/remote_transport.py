"""Streamable HTTP transport for Bernstein MCP server.

Implements the MCP streamable HTTP transport spec for remote deployment.
Can be used with any ASGI server (uvicorn, Cloudflare Workers via Python worker).

Stateless serving (issue #2506): the transport keeps no per-client session
store. Every request is served from its body plus ``_meta`` alone, so any
transport instance can serve any request with no shared memory. Cross-call
continuity is anchored in the run journal and the audit chain (see
``anchor_stateless_call``), not in a session. A legacy client that still
sends the removed protocol-session header is accepted as a no-op behind the
compat shim until the shim's removal date, and never receives a session
header back.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx

from bernstein.core.protocols.mcp.stateless_core import (
    LEGACY_SESSION_HEADER,
    REMOVAL_DATE,
    anchor_stateless_call,
    compat_shim_active,
    legacy_session_header_value,
    months_since_deprecation,
)
from bernstein.mcp.approval_gate import (
    completion_refusal_payload,
    is_approvable,
    is_worker_completable,
    refusal_payload,
)
from bernstein.mcp.capability import package_repo_url, package_version
from bernstein.mcp.streaming import InFlightRegistry, cancelled_envelope

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

_DEFAULT_SERVER_URL = "http://127.0.0.1:8052"
_HTTP_TIMEOUT = 5.0

_package_version = package_version
_package_repo_url = package_repo_url


# JSON-RPC error codes per spec.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603
# MCP resource-not-found (spec reserved range).
_RESOURCE_NOT_FOUND = -32002
_CONTENT_TYPE_JSON = "application/json"

# The protocol-revision request header (MCP streamable HTTP transport spec).
_PROTOCOL_VERSION_HEADER = "mcp-protocol-version"

# Revision assumed when a client sends no version header, per spec.
_DEFAULT_PROTOCOL_VERSION = "2025-03-26"

# One-release opt-out for the Origin and MCP-Protocol-Version enforcement
# (#3084): both refuse requests that succeeded before, so an operator whose
# proxy strips or rewrites headers can disable the checks for one release
# while fixing the proxy. Named in the release notes; removed with them.
_HEADER_CHECKS_ENV = "BERNSTEIN_MCP_REMOTE_HEADER_CHECKS"


def _header_checks_enabled() -> bool:
    """Whether Origin and protocol-version enforcement are active."""
    raw = os.environ.get(_HEADER_CHECKS_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _supported_protocol_versions() -> tuple[str, ...]:
    """The protocol revisions this deployment serves, from the pinned SDK."""
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    return tuple(SUPPORTED_PROTOCOL_VERSIONS)


def _origin_allowed(origin: str, allowed: list[str]) -> bool:
    """Whether a browser ``Origin`` value is in the configured allow list.

    Matching is the enforcement half of ``cors_origins`` (#3084): the same
    list that used to only build the ``access-control-allow-origin`` response
    header now refuses the request outright, which is what actually stops a
    DNS-rebound page from reaching a loopback-bound server. Entries match
    exactly (case-insensitive), or by ``:*`` port glob, or ``*``.
    """
    origin_norm = origin.strip().lower().rstrip("/")
    for entry in allowed:
        entry_norm = entry.strip().lower().rstrip("/")
        if entry_norm == "*" or entry_norm == origin_norm:
            return True
        if entry_norm.endswith(":*"):
            base = entry_norm[:-2]
            if origin_norm == base:
                # Default-port origins carry no explicit port.
                return True
            rest = origin_norm.removeprefix(base + ":")
            if rest != origin_norm and rest.isdigit():
                return True
    return False


# Hostnames considered safe for listening without a configured auth token.
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Env var names used to pick up the bearer auth token if not provided explicitly.
_TOKEN_ENV_VARS = ("BERNSTEIN_MCP_TOKEN", "BERNSTEIN_MCP_AUTH_TOKEN")

# Characters a request authority may keep once it is echoed back into a
# response header or body: registered names, IPv4 and bracketed IPv6 literals,
# and a port. Everything else, CR and LF and the quote character included, is
# dropped by :func:`_sanitise_authority`.
_AUTHORITY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~:[]%")

# Clear-text scheme, spelled as a bare token so a browser origin can only be
# built from it deliberately. Any origin carrying this scheme has to prove it
# is loopback-pinned (see ``RemoteMCPConfig.__post_init__``).
_PLAINTEXT_SCHEME = "http"

# Every scheme that carries traffic without TLS. `ws` and `ftp` are not valid
# browser origins, but enumerating them keeps the enforced policy identical to
# the documented one: clear-text is loopback-only, everything else is TLS.
# Non-URL CORS tokens such as `*` and `null` have no scheme and are untouched.
_CLEAR_TEXT_SCHEMES = frozenset({_PLAINTEXT_SCHEME, "ws", "ftp"})

# Default browser origin. Clear-text is acceptable here only because the origin
# is pinned to a loopback host, so the traffic never leaves the machine; every
# non-loopback origin is required to be TLS.
_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (f"{_PLAINTEXT_SCHEME}://localhost:*",)

#: The only ``RemoteMCPConfig.auth_type`` values ``_authenticate`` implements.
#: An ``"oauth"`` value used to be documented here but ``_authenticate`` never
#: implemented it and fell through to deny (issue #3463): a server configured
#: with it refused every request, including correctly authenticated ones. The
#: OAuth-2 discovery surface (``bernstein.mcp.oauth``) is unaffected -- it
#: serves protected-resource metadata so a client can locate an external IdP,
#: which is orthogonal to how this transport authenticates a bearer token.
_VALID_AUTH_TYPES: tuple[str, ...] = ("none", "bearer")


class RemoteMCPConfigError(RuntimeError):
    """Raised when an MCP remote transport config is unsafe to start with.

    Examples: binding a non-loopback host without a configured auth token, or
    explicitly setting auth_type='none' on a non-loopback host.
    """


def _resolve_token_from_env() -> str:
    """Return the first non-empty token found in the well-known env vars."""
    for name in _TOKEN_ENV_VARS:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _is_localhost(host: str) -> bool:
    """Return True if ``host`` refers to the loopback interface only."""
    return host in _LOCALHOST_HOSTS


def _sanitise_authority(authority: str) -> str:
    """Filter a request authority down to host and port characters.

    The value reaches us from a client-supplied ``Host`` header and is
    interpolated into response headers and bodies, so anything outside the
    character set a host, an IPv6 literal, and a port may use is dropped. CR,
    LF, and the quote character are the ones that matter: they would otherwise
    terminate a header value or escape a quoted header parameter.

    Args:
        authority: Raw authority, for example ``bernstein.example.com:8053``.

    Returns:
        The filtered authority, possibly empty when nothing survived.
    """
    return "".join(c for c in authority.strip() if c in _AUTHORITY_CHARS)


def _origin_host(origin: str) -> str | None:
    """Return the host of a CORS ``origin``, or ``None`` if it does not parse.

    Delegates to :func:`urllib.parse.urlsplit` so bracketed IPv6 literals are
    unwrapped by the stdlib instead of by hand. Hand-rolled bracket stripping
    silently accepted malformed authorities: ``http://[::1]evil.test`` and
    ``http://[::1]@evil.test`` both collapsed to ``::1`` and were admitted as
    loopback. ``urlsplit`` raises on those, so they are refused.

    ``hostname`` never parses the port, so the ``host:*`` port glob the server
    accepts survives, and the host comes back already lower-cased. Userinfo is
    refused outright: a browser ``Origin`` never carries it, so its presence
    means the value is not an origin.
    """
    try:
        parts = urlsplit(origin)
        if "@" in parts.netloc:
            return None
        return parts.hostname
    except ValueError:
        return None


def _is_plaintext_non_loopback_origin(origin: str) -> bool:
    """Return True if ``origin`` is clear-text and not pinned to loopback.

    Every clear-text scheme is covered, not just ``http``, so the policy the
    docs state holds for anything that would put a bearer token on the wire.
    An origin that does not parse is not provably loopback, so it is refused.
    """
    # Scheme is case-insensitive per RFC 3986.
    scheme, sep, _rest = origin.partition("://")
    if not sep or scheme.lower() not in _CLEAR_TEXT_SCHEMES:
        return False
    host = _origin_host(origin)
    return host is None or not _is_localhost(host)


def _constant_time_eq(left: str, right: str) -> bool:
    """Constant-time string compare that tolerates length differences."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _task_server_auth_headers() -> dict[str, str]:
    """Return task-server auth headers when a bearer token is configured."""
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


@dataclass(frozen=True)
class RemoteMCPConfig:
    """Configuration for remote MCP server transport.

    Safe-by-default: binds to localhost only and requires a bearer token.
    When constructed without an explicit ``auth_token`` the value is pulled
    from ``BERNSTEIN_MCP_TOKEN`` (or ``BERNSTEIN_MCP_AUTH_TOKEN``).

    Validation (in ``__post_init__``) refuses any combination that would
    expose MCP JSON-RPC without authentication:

    * an ``auth_type`` outside :data:`_VALID_AUTH_TYPES` (``"none"``,
      ``"bearer"``) → :class:`RemoteMCPConfigError`, raised at construction
      time rather than left to deny every request at serve time
    * ``auth_type='none'`` on a non-loopback host → :class:`RemoteMCPConfigError`
    * ``auth_type='bearer'`` with an empty token on a non-loopback host →
      :class:`RemoteMCPConfigError`
    * a clear-text (``http://``) CORS origin that is not pinned to a loopback
      host → :class:`RemoteMCPConfigError`
    """

    host: str = "127.0.0.1"
    port: int = 8053
    path: str = "/mcp"
    auth_type: str = "bearer"  # "none", "bearer"
    auth_token: str = ""
    cors_origins: list[str] = field(default_factory=lambda: list(_DEFAULT_CORS_ORIGINS))

    def __post_init__(self) -> None:
        """Enforce safe-by-default policy and pick up env-provided tokens."""
        if self.auth_type not in _VALID_AUTH_TYPES:
            msg = (
                f"Refusing to start MCP remote transport: auth_type={self.auth_type!r} "
                f"is not one of {_VALID_AUTH_TYPES!r}. Fixing this at startup, rather "
                "than letting every request reach _authenticate and be denied, avoids "
                "shipping a server that looks configured but refuses all traffic."
            )
            raise RemoteMCPConfigError(msg)

        # Pull token from env when not explicitly provided. Use object.__setattr__
        # because the dataclass is frozen.
        if self.auth_type == "bearer" and not self.auth_token:
            env_token = _resolve_token_from_env()
            if env_token:
                object.__setattr__(self, "auth_token", env_token)

        localhost = _is_localhost(self.host)

        if self.auth_type == "none" and not localhost:
            msg = (
                f"Refusing to start MCP remote transport: host={self.host!r} is "
                "not loopback and auth_type='none'. Set auth_type='bearer' and "
                "provide a token via BERNSTEIN_MCP_TOKEN, or bind to 127.0.0.1."
            )
            raise RemoteMCPConfigError(msg)

        if self.auth_type == "bearer" and not self.auth_token and not localhost:
            msg = (
                f"Refusing to start MCP remote transport: host={self.host!r} is "
                "not loopback but no bearer token is configured. Set "
                "BERNSTEIN_MCP_TOKEN (or pass auth_token=...) before binding to "
                "a public interface."
            )
            raise RemoteMCPConfigError(msg)

        plaintext = [o for o in self.cors_origins if _is_plaintext_non_loopback_origin(o)]
        if plaintext:
            msg = (
                "Refusing to start MCP remote transport: clear-text CORS "
                f"origin(s) {plaintext!r} are not pinned to a loopback host. "
                "Bearer tokens travel on these origins, so use https:// or "
                "restrict the origin to 127.0.0.1, localhost or [::1]."
            )
            raise RemoteMCPConfigError(msg)


def _jsonrpc_error(
    code: int,
    message: str,
    req_id: int | str | None = None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 error response."""
    resp: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
    }
    if req_id is not None:
        resp["id"] = req_id
    else:
        resp["id"] = None
    return resp


def _jsonrpc_result(
    result: Any,
    req_id: int | str | None,
) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 success response."""
    return {
        "jsonrpc": "2.0",
        "result": result,
        "id": req_id,
    }


# -- Tool definitions (mirrors the FastMCP tools in server.py) ---------------

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "bernstein_run",
        "description": (
            "Start an orchestration run by posting a task. Pass "
            "parent_task_id to create the run as a subtask of an existing "
            "task."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "role": {"type": "string", "default": "backend"},
                "priority": {"type": "integer", "default": 2},
                "scope": {"type": "string", "default": "medium"},
                "complexity": {"type": "string", "default": "medium"},
                "estimated_minutes": {"type": "integer", "default": 30},
                "parent_task_id": {"type": "string"},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "bernstein_status",
        "description": (
            "Liveness, task counts, and cost in one read. Pass status to "
            "include the matching tasks; pass detail=true for full rows."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "detail": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "bernstein_cancel",
        "description": (
            "Cancel one task and its subtask tree; the orchestrator keeps "
            "running. An already-terminal task is reported with its state, "
            "and an unknown task id is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "bernstein_shutdown_orchestrator",
        "description": (
            "Shut down the ENTIRE Bernstein orchestrator for the project - "
            "every run, every worker - by writing the SHUTDOWN signal. To "
            "stop a single run, use bernstein_cancel instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {"type": "string", "default": "."},
            },
        },
    },
    {
        "name": "bernstein_approve",
        "description": (
            "Sign off a finished result that is waiting on a decision. Acts "
            "only on a task in 'pending_approval'; any other status is "
            "refused, including 'planned', which is released by approving "
            "the plan it belongs to. Not a way to finish work - use "
            "bernstein_complete for that."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "note": {"type": "string", "default": "Approved via MCP"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "bernstein_complete",
        "description": (
            "Report the result of work you are executing. Acts only on a task "
            "you hold ('open', 'claimed', 'in_progress'); a task waiting on "
            "its subtasks, one whose worker is gone, or one already awaiting "
            "a decision is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "result_summary": {"type": "string"},
            },
            "required": ["task_id", "result_summary"],
        },
    },
]

#: Deprecated remote tool names (#3087): callable for one minor release,
#: never advertised in ``tools/list``, answering with a payload that names
#: the replacement. The mapping mirrors the stdio alias table for the subset
#: of tools this transport serves.
_REMOTE_DEPRECATED_TOOLS: dict[str, str] = {
    "bernstein_health": "bernstein_status",
    "bernstein_tasks": "bernstein_status",
    "bernstein_cost": "bernstein_status",
    "bernstein_stop": "bernstein_shutdown_orchestrator",
    "bernstein_create_subtask": "bernstein_run",
}


def validation_scope_notice() -> str:
    """Return the interim notice about this transport's weaker argument checks.

    This transport does not route ``tools/call`` through
    ``bernstein.mcp.input_validation.validate_tool_call``, so the
    deny-by-default input firewall documented in ``docs/mcp/input-validation.md``
    covers the stdio and SSE servers only. It also exposes a subset of the
    server's tools, with schemas restated here rather than loaded from
    ``src/bernstein/mcp/tool_schemas/``.

    The tool list is derived from ``_TOOL_DEFS`` so the notice cannot drift
    from what the transport actually serves.

    This is a notice, not a fix. Delete this function, its call site, its
    tests and the matching doc sections in the same change that closes issue
    #3083.
    """
    names = ", ".join(str(defn["name"]) for defn in _TOOL_DEFS)
    return (
        f"Streamable HTTP transport: argument validation on this path is weaker than on stdio. "
        f"It exposes {len(_TOOL_DEFS)} tools ({names}) with schemas restated in this module, "
        f"and does not apply the deny-by-default input validation the stdio transport applies. "
        f"Tracked in issue #3083."
    )


_SERVER_INFO: dict[str, Any] = {
    "name": "bernstein",
    "version": _package_version(),
    "repo_url": _package_repo_url(),
}

_CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    # Server-side prompt templates surfaced via prompts/list and prompts/get.
    "prompts": {"listChanged": False},
    # The capability card, the skill index, and (when enabled) the lineage
    # records, served via resources/list and resources/read (#3084).
    "resources": {"listChanged": False, "subscribe": False},
    # The server honours notifications/cancelled for in-flight tool calls and
    # preserves partial output on cancel.
    "experimental": {"cancellation": {"partialResults": True}},
}


# Built-in prompt catalogue mirroring src/bernstein/mcp/prompts.py. Mirroring
# here keeps the streamable HTTP transport self-contained: it does not need
# to spin up a FastMCP instance to answer prompts/list and prompts/get.
_PROMPT_DEFS: list[dict[str, Any]] = [
    {
        "name": "orchestrate_goal",
        "description": "Plan a Bernstein orchestration run for a single goal.",
        "arguments": [
            {"name": "goal", "description": "What to accomplish.", "required": True},
            {"name": "role", "description": "Specialist role.", "required": False},
            {"name": "scope", "description": "Task scope.", "required": False},
        ],
    },
    {
        "name": "triage_failed_tasks",
        "description": "Triage the most recent failed tasks and propose next actions.",
        "arguments": [
            {"name": "limit", "description": "Max tasks to inspect.", "required": False},
        ],
    },
    {
        "name": "cost_recap",
        "description": "Summarise Bernstein cost by role for a stated window.",
        "arguments": [
            {"name": "window", "description": "Window label (e.g. today).", "required": False},
        ],
    },
]


class StreamableHTTPTransport:
    """MCP streamable HTTP transport implementation.

    Handles the HTTP request/response cycle for MCP messages using the
    streamable HTTP transport spec (POST for requests). No session store
    exists: every request is served from its body plus ``_meta`` alone, and
    served calls are anchored into the run journal / audit chain when those
    are wired in (issue #2506).

    Args:
        config: Transport configuration.
        server_url: Bernstein task server URL tool calls proxy to.
        journal: Optional run journal; every served ``tools/call`` becomes an
            ordered ``mcp.stateless_call`` journal entry.
        audit_chain: Optional audit chain store; requires ``journal``. Every
            served ``tools/call`` is anchored as an ``mcp.stateless_call``
            chain entry binding its content-derived ids to the journal head.
        today: Clock override for the legacy-session compat shim window
            (tests pin it; production uses the current date).
    """

    def __init__(
        self,
        config: RemoteMCPConfig,
        server_url: str = _DEFAULT_SERVER_URL,
        *,
        journal: EventJournal | None = None,
        audit_chain: AuditChainStore | None = None,
        today: Callable[[], date] | None = None,
        lineage_root: Any = None,
    ) -> None:
        # An audit chain with no journal to anchor against would silently
        # disable the auditing the caller asked for: every served call would
        # take the ``journal is None`` early return and never reach the chain.
        # Fail fast so a misconfigured transport cannot look audited (AC3).
        if audit_chain is not None and journal is None:
            msg = "audit_chain requires a journal; a chain without a journal silently disables anchoring"
            raise ValueError(msg)
        self._config = config
        self._server_url = server_url
        self._journal = journal
        self._audit_chain = audit_chain
        self._today = today
        self._lineage_root = lineage_root
        self._inflight = InFlightRegistry()

    def _legacy_session_shim_active(self) -> bool:
        """Whether the legacy protocol-session shim still accepts the header."""
        today = self._today() if self._today is not None else None
        return compat_shim_active("sessions", months_since_deprecation=months_since_deprecation(today))

    # -- public API ----------------------------------------------------------

    async def handle_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        """Route incoming HTTP request to appropriate MCP handler.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: Request path.
            headers: HTTP headers (lower-cased keys).
            body: Raw request body.

        Returns:
            Tuple of (status_code, response_headers, response_body).
        """
        # OAuth-2 / OIDC discovery metadata. Served without authentication so
        # a client can locate the IdP before it has a token; standard well-known
        # paths only return content when ``BERNSTEIN_MCP_OAUTH_ISSUER`` is set,
        # otherwise 404 (anonymous/static-bearer remain the advertised path).
        if method == "GET" and self._is_well_known(path):
            return self._handle_well_known(path, headers)

        # Normalise path.
        if not path.rstrip("/").endswith(self._config.path.rstrip("/")):
            return (404, {"content-type": _CONTENT_TYPE_JSON}, b'{"error":"not found"}')

        # Origin and protocol-version enforcement (#3084). Both run before
        # auth: a DNS-rebound page must be refused whether or not it guessed
        # a token, and a version mismatch is answerable without credentials
        # so the client can downgrade and retry.
        if _header_checks_enabled():
            origin = headers.get("origin")
            if origin is not None and not _origin_allowed(origin, self._config.cors_origins):
                return (
                    403,
                    {"content-type": _CONTENT_TYPE_JSON},
                    json.dumps({"error": "origin not allowed"}).encode(),
                )
            version = headers.get(_PROTOCOL_VERSION_HEADER)
            if version is not None and version not in _supported_protocol_versions():
                message = (
                    f"Unsupported MCP-Protocol-Version {version!r}; "
                    f"supported: {', '.join(_supported_protocol_versions())}"
                )
                return (
                    400,
                    {"content-type": _CONTENT_TYPE_JSON},
                    json.dumps({"error": message}).encode(),
                )

        # The revision this request is served under: the header when the
        # client sent one, the spec default otherwise. Threaded through the
        # dispatch so the capability card reports the live connection's
        # revision rather than a constant.
        negotiated_version = headers.get(_PROTOCOL_VERSION_HEADER) or _DEFAULT_PROTOCOL_VERSION

        # Auth check.
        if not self._authenticate(headers):
            return self._unauthorized_response(headers)

        # Legacy protocol-session shim: the removed header is accepted (and
        # ignored) for a bounded window, then refused with the removal date.
        if legacy_session_header_value(headers) is not None and not self._legacy_session_shim_active():
            message = (
                "protocol sessions were removed by the stateless MCP spec revision; "
                f"the {LEGACY_SESSION_HEADER} compatibility window ended on {REMOVAL_DATE.isoformat()}"
            )
            return (
                400,
                {"content-type": _CONTENT_TYPE_JSON},
                json.dumps({"error": message}).encode(),
            )

        if method == "POST":
            return await self._handle_post(body, negotiated_version=negotiated_version)
        if method == "GET":
            return self._handle_get()
        if method == "DELETE":
            return self._handle_delete()

        return (
            405,
            {"content-type": _CONTENT_TYPE_JSON, "allow": "GET, POST, OPTIONS"},
            b'{"error":"method not allowed"}',
        )

    # -- HTTP method handlers ------------------------------------------------

    async def _handle_post(
        self,
        body: bytes,
        *,
        negotiated_version: str = _DEFAULT_PROTOCOL_VERSION,
    ) -> tuple[int, dict[str, str], bytes]:
        """Handle POST: JSON-RPC request/notification.

        The request is served from its body alone; no session is looked up
        or created, and no session header is returned.
        """
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            err = _jsonrpc_error(_PARSE_ERROR, "Parse error")
            return (400, {"content-type": _CONTENT_TYPE_JSON}, json.dumps(err).encode())

        resp_headers: dict[str, str] = {"content-type": _CONTENT_TYPE_JSON}

        # JSON-RPC batching was removed from the MCP schema two revisions
        # ago; a JSON array body is dead surface and is refused (#3084).
        if isinstance(message, list):
            err = _jsonrpc_error(
                _INVALID_REQUEST,
                "JSON-RPC batching was removed from the MCP specification; send one request per POST",
            )
            return (400, resp_headers, json.dumps(err).encode())

        if not isinstance(message, dict):
            err = _jsonrpc_error(_INVALID_REQUEST, "Invalid request")
            return (200, resp_headers, json.dumps(err).encode())

        message_dict = cast("dict[str, Any]", message)
        result = await self._handle_jsonrpc(message_dict, negotiated_version=negotiated_version)
        if result is None:
            # Notification - no response.
            return (204, resp_headers, b"")
        return (200, resp_headers, json.dumps(result).encode())

    def _handle_get(self) -> tuple[int, dict[str, str], bytes]:
        """Handle GET: server-initiated SSE stream endpoint (stub - 501).

        Client-to-server streaming, cancellation, and partial-result
        preservation are handled over POST (``tools/call`` plus
        ``notifications/cancelled``). A server-initiated SSE push channel is
        not implemented, so GET still returns 501 with a pointer to POST.
        """
        return (
            501,
            {"content-type": _CONTENT_TYPE_JSON},
            b'{"error":"server-initiated SSE not implemented - use POST and notifications/cancelled"}',
        )

    def _handle_delete(self) -> tuple[int, dict[str, str], bytes]:
        """Handle DELETE: the removed session-close lifecycle.

        While the compat shim is active a legacy close request is
        acknowledged as a no-op (there is nothing to close); afterwards the
        method is gone. The shim-expiry path for a request that still
        carries the legacy header is handled in :meth:`handle_request`.
        """
        if self._legacy_session_shim_active():
            return (200, {"content-type": _CONTENT_TYPE_JSON}, b'{"status":"ok"}')
        return (
            405,
            {"content-type": _CONTENT_TYPE_JSON, "allow": "GET, POST, OPTIONS"},
            b'{"error":"method not allowed"}',
        )

    # -- JSON-RPC dispatch ---------------------------------------------------

    async def _handle_jsonrpc(
        self,
        message: dict[str, Any],
        *,
        negotiated_version: str = _DEFAULT_PROTOCOL_VERSION,
    ) -> dict[str, Any] | None:
        """Process a single JSON-RPC message from its body alone.

        Args:
            message: Parsed JSON-RPC message.
            negotiated_version: The protocol revision this request is served
                under (from the version header, or the spec default).

        Returns:
            JSON-RPC response dict, or None for notifications.
        """
        if message.get("jsonrpc") != "2.0":
            return _jsonrpc_error(_INVALID_REQUEST, "Invalid JSON-RPC version")

        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params", {})

        # Notifications have no id - fire and forget.
        is_notification = req_id is None and "id" not in message

        handler = self._get_method_handler(method)
        if handler is None:
            if is_notification:
                return None
            return _jsonrpc_error(_METHOD_NOT_FOUND, f"Method not found: {method}", req_id)

        try:
            # tools/call needs its JSON-RPC id so the call can be tracked for
            # cancellation; initialize and resources/read need the request's
            # negotiated revision; other handlers take only (params).
            if method == "tools/call":
                result = await self._method_tools_call(params, req_id)
            elif method == "initialize":
                result = await self._method_initialize(params, negotiated_version=negotiated_version)
            elif method == "resources/read":
                resolved = await self._method_resources_read(params, negotiated_version=negotiated_version)
                if resolved is None:
                    if is_notification:
                        return None
                    uri = params.get("uri", "") if isinstance(params, dict) else ""
                    return _jsonrpc_error(_RESOURCE_NOT_FOUND, f"Resource not found: {uri}", req_id)
                result = resolved
            else:
                result = await handler(params)
        except Exception as exc:
            logger.exception("Error handling method %s", method)
            if is_notification:
                return None
            return _jsonrpc_error(_INTERNAL_ERROR, str(exc), req_id)

        if is_notification:
            return None
        return _jsonrpc_result(result, req_id)

    def _anchor_served_call(self, method: str, params: dict[str, Any]) -> None:
        """Anchor a served call into the run journal and audit chain.

        The journal row and chain entry replace the deleted session store as
        the continuity record: ordering reconstructs from chain entries
        alone (issue #2506). Anchoring failures are logged, not raised, so a
        full audit volume cannot take request serving down; the gap is still
        visible to a verifier as a missing call index.
        """
        if self._journal is None:
            return
        try:
            anchor_stateless_call(
                journal=self._journal,
                method=method,
                params=params,
                chain=self._audit_chain,
            )
        except Exception:
            logger.exception("Failed to anchor mcp.stateless_call for %s", method)

    def _get_method_handler(self, method: str | None) -> Any:
        """Look up handler for a JSON-RPC method name."""
        handlers: dict[str, Any] = {
            "initialize": self._method_initialize,
            "tools/list": self._method_tools_list,
            "tools/call": self._method_tools_call,
            "prompts/list": self._method_prompts_list,
            "prompts/get": self._method_prompts_get,
            "resources/list": self._method_resources_list,
            "resources/templates/list": self._method_resources_templates_list,
            "resources/read": self._method_resources_read,
            "ping": self._method_ping,
            "notifications/initialized": self._method_noop,
            "notifications/cancelled": self._method_cancelled,
        }
        return handlers.get(method or "")

    # -- MCP method implementations ------------------------------------------

    async def _method_initialize(
        self,
        params: dict[str, Any],
        *,
        negotiated_version: str = _DEFAULT_PROTOCOL_VERSION,
    ) -> dict[str, Any]:
        """Handle 'initialize' - return server info and capabilities.

        Retained for clients that still send the legacy handshake; the
        result carries no session identity. The revision is negotiated, not
        constant (#3084): a supported ``protocolVersion`` in the params is
        echoed back, an unknown one answers with the spec default so the
        client can downgrade. Alongside the static spec ``capabilities``
        object, the result carries a runtime ``capabilityCard`` describing
        the live transports, auth modes, active tool tier, cost-meter state,
        and the revision actually negotiated for this connection.
        """
        from bernstein.mcp.capability import build_capability_card

        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        if isinstance(requested, str) and requested in _supported_protocol_versions():
            negotiated = requested
        elif requested is not None:
            negotiated = _DEFAULT_PROTOCOL_VERSION
        else:
            negotiated = negotiated_version
        return {
            "protocolVersion": negotiated,
            "serverInfo": _SERVER_INFO,
            "capabilities": _CAPABILITIES,
            "capabilityCard": build_capability_card(spec_revision=negotiated),
        }

    async def _method_tools_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'tools/list' - return available tools."""
        _ = params
        return {"tools": _TOOL_DEFS}

    async def _method_tools_call(
        self,
        params: dict[str, Any],
        req_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Handle 'tools/call' - execute a tool and return result.

        The tool runs inside a cancellable task tracked by its JSON-RPC id, so
        a ``notifications/cancelled`` for that id stops the work and the
        accumulated partial output is returned (``cancelled: true`` with the
        preserved ``partial`` chunks) rather than discarded.

        The tool's JSON payload is wrapped in the per-call cost-meter envelope
        (latency, cost, trace id, status) so the remote transport emits the
        same observable shape as the stdio/SSE server. The envelope is a no-op
        when the meter is disabled via ``BERNSTEIN_MCP_COST_METER``.

        When a journal / audit chain is wired in, the served call is
        anchored as an ``mcp.stateless_call`` entry before execution, so the
        chain records the call even if the tool itself fails (issue #2506).
        """
        from bernstein.mcp.cost_meter import measure_call, wrap_envelope

        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        self._anchor_served_call("tools/call", params)

        # Untracked calls (no id) still execute, just without cancel support.
        call = await self._inflight.register(req_id, tool_name) if req_id is not None else None

        meter_ctx = measure_call(tool_name)
        meter = meter_ctx.__enter__()
        meter_finalised = False
        text = ""
        try:
            if call is not None:
                assert req_id is not None
                # Seed a partial chunk so a cancel mid-flight preserves the
                # in-progress context rather than returning an empty result.
                call.append_partial(json.dumps({"status": "running", "tool": tool_name}))
                task: asyncio.Task[str] = asyncio.create_task(self._execute_tool(tool_name, arguments))
                await self._inflight.attach_task(req_id, task)
                task_result = (await asyncio.gather(task, return_exceptions=True))[0]
                if isinstance(task_result, asyncio.CancelledError):
                    current = await self._inflight.get(req_id)
                    if current is not None and current.cancelled:
                        meter_ctx.__exit__(None, None, None)
                        meter_finalised = True
                        return cancelled_envelope(current, meter.to_dict())
                    raise task_result
                if isinstance(task_result, BaseException):
                    raise task_result
                text = task_result
            else:
                text = await self._execute_tool(tool_name, arguments)
        except Exception as exc:
            # Finalise the meter for the failed call, then emit it alongside
            # the error so observability covers failures too.
            with suppress(Exception):
                meter_ctx.__exit__(type(exc), exc, exc.__traceback__)
            meter_finalised = True
            logger.warning("Tool %s failed: %s", tool_name, exc)
            error_payload = wrap_envelope(json.dumps({"error": str(exc)}), meter)
            return {
                "content": [{"type": "text", "text": error_payload}],
                "isError": True,
            }
        finally:
            if req_id is not None:
                await self._inflight.discard(req_id)
            if not meter_finalised:
                with suppress(Exception):
                    meter_ctx.__exit__(None, None, None)
        return {
            "content": [{"type": "text", "text": wrap_envelope(text, meter)}],
        }

    async def _method_prompts_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'prompts/list' - return the built-in prompt catalogue.

        Common auto-discovery hosts probe this surface to populate a prompt
        picker. The catalogue is the same one the FastMCP server registers,
        kept in sync via ``_PROMPT_DEFS`` on this transport.
        """
        _ = params
        return {"prompts": _PROMPT_DEFS}

    async def _method_prompts_get(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'prompts/get' - render a named prompt with arguments.

        Args:
            params: JSON-RPC params carrying ``name`` and optional ``arguments``.

        Returns:
            A prompt response with a single user-role text message.

        Raises:
            ValueError: When the requested prompt name is unknown.
        """
        from bernstein.mcp import prompts

        prompt_attrs = vars(prompts)
        orchestrate_goal_template = cast(
            "Callable[[str, str, str], str]",
            prompt_attrs["_orchestrate_goal_template"],
        )
        triage_failed_tasks_template = cast(
            "Callable[[int], str]",
            prompt_attrs["_triage_failed_tasks_template"],
        )
        cost_recap_template = cast(
            "Callable[[str], str]",
            prompt_attrs["_cost_recap_template"],
        )

        name_raw = params.get("name", "")
        name = name_raw if isinstance(name_raw, str) else ""
        arguments_raw = params.get("arguments", {})
        arguments = cast("dict[str, object]", arguments_raw) if isinstance(arguments_raw, dict) else {}
        if name == "orchestrate_goal":
            goal = arguments.get("goal", "")
            role = arguments.get("role", "backend")
            scope = arguments.get("scope", "medium")
            body = orchestrate_goal_template(
                goal if isinstance(goal, str) else "",
                role if isinstance(role, str) else "backend",
                scope if isinstance(scope, str) else "medium",
            )
        elif name == "triage_failed_tasks":
            limit_raw = arguments.get("limit", 5)
            if isinstance(limit_raw, int):
                limit = limit_raw
            elif isinstance(limit_raw, str):
                try:
                    limit = int(limit_raw)
                except ValueError:
                    limit = 5
            else:
                limit = 5
            body = triage_failed_tasks_template(limit)
        elif name == "cost_recap":
            window = arguments.get("window", "today")
            body = cost_recap_template(window if isinstance(window, str) else "today")
        else:
            msg = f"Unknown prompt: {name}"
            raise ValueError(msg)
        return {
            "description": next((p["description"] for p in _PROMPT_DEFS if p["name"] == name), ""),
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": body},
                }
            ],
        }

    # -- Resources (#3084) ---------------------------------------------------

    def _lineage_enabled(self) -> bool:
        """Whether the lineage resources are exposed on this remote surface.

        Default OFF for remote deployments (ADR-009 7.3), opt-in via
        ``BERNSTEIN_LINEAGE_MCP_ENABLED=1`` - the same gate the SSE server
        applies, so a resource read is tier-checked exactly like the stdio
        registrar decides registration.
        """
        from bernstein.mcp.server import _lineage_mcp_default  # pyright: ignore[reportPrivateUsage]

        return _lineage_mcp_default(default=False)

    def _lineage_root_path(self) -> Any:
        from pathlib import Path

        if self._lineage_root is not None:
            return self._lineage_root
        return Path.cwd() / ".sdd" / "lineage"

    @staticmethod
    def _skill_index_body() -> str:
        """Serialize the skill discovery index (same body stdio serves)."""
        from pathlib import Path

        from bernstein import get_templates_dir
        from bernstein.core.skills.index_builder import serialize_skill_discovery_index
        from bernstein.core.skills.loader import default_loader_from_templates

        templates_root = get_templates_dir(Path.cwd())
        return serialize_skill_discovery_index(default_loader_from_templates(templates_root / "roles"))

    async def _method_resources_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'resources/list' - the same resources stdio exposes.

        The capability card and the skill index are always served; the
        lineage resources appear only when the lineage exposure is enabled
        for remote transports, mirroring the stdio registrar's gate.
        """
        from bernstein.core.skills.index_builder import SKILL_INDEX_RESOURCE_URI

        _ = params
        resources: list[dict[str, Any]] = [
            {
                "uri": "bernstein://capability",
                "name": "bernstein_capability",
                "description": "Runtime capability card: transports, auth, tiers, meter, spec rev.",
                "mimeType": _CONTENT_TYPE_JSON,
            },
            {
                "uri": SKILL_INDEX_RESOURCE_URI,
                "name": "skill_index",
                "description": "Compact index of loadable Bernstein skills and their content hashes.",
                "mimeType": _CONTENT_TYPE_JSON,
            },
        ]
        if self._lineage_enabled():
            resources.append(
                {
                    "uri": "lineage://stats",
                    "name": "lineage_stats",
                    "description": "Summary counts over the entire lineage log.",
                    "mimeType": _CONTENT_TYPE_JSON,
                }
            )
        return {"resources": resources}

    async def _method_resources_templates_list(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'resources/templates/list' - lineage artefact template."""
        _ = params
        templates: list[dict[str, Any]] = []
        if self._lineage_enabled():
            templates.append(
                {
                    "uriTemplate": "lineage://artefact/{artefact_path}",
                    "name": "lineage_artefact",
                    "description": "JSONL chain of lineage entries for a single artefact.",
                    "mimeType": "application/x-ndjson",
                }
            )
        return {"resourceTemplates": templates}

    async def _method_resources_read(
        self,
        params: dict[str, Any],
        *,
        negotiated_version: str = _DEFAULT_PROTOCOL_VERSION,
    ) -> dict[str, Any] | None:
        """Handle 'resources/read' - serve one resource body by URI.

        Runs after the same auth (and header) checks as every tool call:
        this method is only reachable through :meth:`handle_request`. A
        lineage URI is refused while the lineage exposure is disabled, so a
        caller below that gate cannot read lineage remotely. Returns
        ``None`` for an unknown or refused URI; the dispatcher renders that
        as a resource-not-found error.
        """
        from bernstein.core.skills.index_builder import SKILL_INDEX_RESOURCE_URI

        uri = params.get("uri", "") if isinstance(params, dict) else ""
        if not isinstance(uri, str) or not uri:
            return None

        if uri == "bernstein://capability":
            from bernstein.mcp.capability import build_capability_card

            text = json.dumps(build_capability_card(spec_revision=negotiated_version), sort_keys=True)
            return {"contents": [{"uri": uri, "mimeType": _CONTENT_TYPE_JSON, "text": text}]}

        if uri == SKILL_INDEX_RESOURCE_URI:
            return {"contents": [{"uri": uri, "mimeType": _CONTENT_TYPE_JSON, "text": self._skill_index_body()}]}

        if uri.startswith("lineage://"):
            if not self._lineage_enabled():
                return None
            from bernstein.mcp.resources.lineage import lineage_artefact_body, lineage_stats_body

            if uri == "lineage://stats":
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": _CONTENT_TYPE_JSON,
                            "text": lineage_stats_body(self._lineage_root_path()),
                        }
                    ]
                }
            artefact_prefix = "lineage://artefact/"
            if uri.startswith(artefact_prefix):
                artefact_path = uri[len(artefact_prefix) :]
                return {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/x-ndjson",
                            "text": lineage_artefact_body(self._lineage_root_path(), artefact_path),
                        }
                    ]
                }
            return None

        return None

    async def _method_cancelled(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'notifications/cancelled' - stop an in-flight tool call.

        Per the MCP spec the notification carries ``requestId`` (the id of the
        ``tools/call`` to cancel). Cancelling marks the tracked call and
        cancels its task; the originating ``tools/call`` handler then returns
        the preserved partial output. Cancelling an unknown or already-settled
        id is a no-op, as the spec requires.
        """
        request_id = params.get("requestId")
        if request_id is not None:
            await self._inflight.cancel(request_id)
        return {}

    async def _method_ping(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle 'ping' - return empty result."""
        _ = params
        return {}

    async def _method_noop(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Handle notifications that need no response."""
        _ = params
        return {}

    # -- Tool execution (proxies to Bernstein task server) --------------------

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a Bernstein tool by proxying to the task server.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            JSON string result.

        Raises:
            ValueError: If the tool name is unknown.
        """
        if name in _REMOTE_DEPRECATED_TOOLS:
            from bernstein.core.protocols.mcp.tool_tiers import deprecated_aliases_enabled

            if not deprecated_aliases_enabled():
                msg = f"Unknown tool: {name}"
                raise ValueError(msg)
            from bernstein.mcp.server import _deprecated_alias_payload  # pyright: ignore[reportPrivateUsage]

            return _deprecated_alias_payload(name, await self._execute_deprecated_tool(name, arguments))

        if name == "bernstein_status":
            return await self._folded_status(arguments)

        if name == "bernstein_run":
            return await self._run_tool(arguments)

        if name == "bernstein_cancel":
            return await self._cancel_tool(arguments)

        if name == "bernstein_shutdown_orchestrator":
            return self._shutdown_tool(arguments)

        if name == "bernstein_approve":
            task_id = arguments["task_id"]
            note = arguments.get("note", "Approved via MCP")
            # The gate is the same one the in-process server enforces: read
            # the task first and refuse anything that is not holding a
            # finished result for sign-off, so the remote transport is not a
            # way around it.
            raw_task = await self._proxy_get(f"/tasks/{task_id}")
            current_status = str(json.loads(raw_task).get("status") or "")
            if not is_approvable(current_status):
                return json.dumps(refusal_payload(task_id, current_status), indent=2)
            return await self._proxy_post(
                f"/tasks/{task_id}/complete",
                {"result_summary": note},
            )

        if name == "bernstein_complete":
            # Same read-before-act rule as the in-process server: a worker
            # reports the result of a task it holds, and a task that is
            # waiting on its subtasks or whose worker is gone is refused
            # rather than marked done on request.
            task_id = arguments["task_id"]
            raw_task = await self._proxy_get(f"/tasks/{task_id}")
            current_status = str(json.loads(raw_task).get("status") or "")
            if not is_worker_completable(current_status):
                return json.dumps(completion_refusal_payload(task_id, current_status), indent=2)
            return await self._proxy_post(
                f"/tasks/{task_id}/complete",
                {"result_summary": arguments["result_summary"]},
            )

        msg = f"Unknown tool: {name}"
        raise ValueError(msg)

    async def _execute_deprecated_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Serve a deprecated tool name with its historical result body."""
        if name == "bernstein_health":
            return json.dumps({"status": "ok"})

        if name == "bernstein_tasks":
            params: dict[str, str] = {}
            if arguments.get("status"):
                params["status"] = arguments["status"]
            return await self._proxy_get("/tasks", params=params)

        if name == "bernstein_cost":
            raw = await self._proxy_get("/status")
            data = json.loads(raw)
            per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
            return json.dumps(
                {
                    "total_cost_usd": data.get("total_cost_usd", 0.0),
                    "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
                }
            )

        if name == "bernstein_stop":
            return self._shutdown_tool(arguments)

        # bernstein_create_subtask
        payload_sub: dict[str, Any] = {
            "parent_task_id": arguments["parent_task_id"],
            "title": arguments["goal"][:120],
            "description": arguments["goal"],
            "role": arguments.get("role", "auto"),
            "priority": arguments.get("priority", 2),
            "scope": arguments.get("scope", "medium"),
            "complexity": arguments.get("complexity", "medium"),
        }
        if arguments.get("estimated_minutes") is not None:
            payload_sub["estimated_minutes"] = arguments["estimated_minutes"]
        return await self._proxy_post("/tasks/self-create", payload_sub)

    async def _folded_status(self, arguments: dict[str, Any]) -> str:
        """The folded status body (#3087): liveness + counts + cost (+ tasks)."""
        status_filter = arguments.get("status") or None
        detail = bool(arguments.get("detail", False))
        data = json.loads(await self._proxy_get("/status"))
        tasks: list[dict[str, Any]] | None = None
        if status_filter:
            tasks = json.loads(await self._proxy_get("/tasks", params={"status": str(status_filter)}))
        per_role_raw: list[dict[str, Any]] = data.get("per_role", [])
        body: dict[str, Any] = {
            "live": True,
            "counts": {
                "total": data.get("total", 0),
                "open": data.get("open", 0),
                "claimed": data.get("claimed", 0),
                "done": data.get("done", 0),
                "failed": data.get("failed", 0),
            },
            "cost": {
                "total_cost_usd": data.get("total_cost_usd", 0.0),
                "per_role": [{"role": r["role"], "cost_usd": r.get("cost_usd", 0.0)} for r in per_role_raw],
            },
        }
        if detail:
            body["per_role"] = per_role_raw
        if tasks is not None:
            body["status_filter"] = status_filter
            if detail:
                body["tasks"] = tasks
            else:
                body["tasks"] = [{k: t.get(k) for k in ("id", "title", "role", "status")} for t in tasks]
        return json.dumps(body)

    async def _run_tool(self, arguments: dict[str, Any]) -> str:
        """Start a run, optionally as a subtask via ``parent_task_id``."""
        goal = arguments.get("goal", "")
        payload: dict[str, Any] = {
            "title": goal[:120],
            "description": goal,
            "role": arguments.get("role", "backend"),
            "priority": arguments.get("priority", 2),
            "scope": arguments.get("scope", "medium"),
            "complexity": arguments.get("complexity", "medium"),
            "estimated_minutes": arguments.get("estimated_minutes", 30),
        }
        endpoint = "/tasks"
        parent = arguments.get("parent_task_id")
        if parent:
            payload["parent_task_id"] = parent
            endpoint = "/tasks/self-create"
        return await self._proxy_post(endpoint, payload)

    async def _cancel_tool(self, arguments: dict[str, Any]) -> str:
        """Cancel one task tree; report terminal state instead of erroring (#3078).

        Read-before-act, like the approve and complete gates: an unknown id
        or a non-cancellable state is refused without a state-changing
        request being sent.
        """
        from bernstein.mcp.server import _CANCELLABLE_STATUSES  # pyright: ignore[reportPrivateUsage]

        task_id = str(arguments.get("task_id", ""))
        reason = str(arguments.get("reason", ""))
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            read = await client.get(
                f"{self._server_url}/tasks/{task_id}",
                headers=_task_server_auth_headers(),
            )
            if read.status_code == 404:
                return json.dumps(
                    {
                        "error": "unknown_task",
                        "task_id": task_id,
                        "message": f"No task with id {task_id!r}; nothing was cancelled.",
                    }
                )
            read.raise_for_status()
            current = str(read.json().get("status") or "unknown")
            if current not in _CANCELLABLE_STATUSES:
                return json.dumps(
                    {
                        "task_id": task_id,
                        "status": current,
                        "cancelled": False,
                        "message": (f"Task {task_id} is already in a terminal or non-cancellable state ({current})."),
                    }
                )
            resp = await client.post(
                f"{self._server_url}/tasks/{task_id}/cancel",
                json={"reason": reason},
                headers=_task_server_auth_headers(),
            )
            if resp.status_code == 409:
                reread = await client.get(
                    f"{self._server_url}/tasks/{task_id}",
                    headers=_task_server_auth_headers(),
                )
                moved = str(reread.json().get("status") or "unknown") if reread.is_success else "unknown"
                return json.dumps(
                    {
                        "task_id": task_id,
                        "status": moved,
                        "cancelled": False,
                        "message": (f"Task {task_id} is already in a terminal or non-cancellable state ({moved})."),
                    }
                )
            resp.raise_for_status()
            data = resp.json()
        return json.dumps({"task_id": data["id"], "status": data["status"], "cancelled": True})

    def _shutdown_tool(self, arguments: dict[str, Any]) -> str:
        """Write the SHUTDOWN signal for the whole orchestrator."""
        from bernstein.mcp.signal_paths import shutdown_signal_path

        # Same barrier as the stdio surface: the workdir must name an
        # existing project root and the signal path must stay inside it.
        # A refusal raises and is rendered as the structured tool error,
        # before any directory is created.
        shutdown_file = shutdown_signal_path(arguments.get("workdir", "."))
        shutdown_file.parent.mkdir(parents=True, exist_ok=True)
        shutdown_file.write_text("mcp-remote-stop\n", encoding="utf-8")
        return json.dumps({"status": "shutdown signal sent", "path": str(shutdown_file)})

    async def _proxy_get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> str:
        """GET request to Bernstein task server."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(
                f"{self._server_url}{path}",
                params=params,
                headers=_task_server_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

    async def _proxy_post(self, path: str, payload: dict[str, Any]) -> str:
        """POST request to Bernstein task server."""
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{self._server_url}{path}",
                json=payload,
                headers=_task_server_auth_headers(),
            )
            resp.raise_for_status()
            return resp.text

    # -- OAuth discovery -----------------------------------------------------

    def _request_base_url(self, headers: dict[str, str]) -> str:
        """Return the scheme and authority the client reached this server on.

        Both the protected-resource metadata document and the
        ``WWW-Authenticate`` challenge on a 401 are built from this, so the URL
        a client is pointed at cannot drift from the URL that serves the
        document.

        ``Host`` and ``X-Forwarded-Proto`` are supplied by the client or by a
        proxy in front of it, and the result is interpolated into a response
        header, so the authority is filtered down to the characters a host and
        port may contain. That drops CR, LF, and the quote character before
        they can terminate the header value or the quoted parameter.

        Args:
            headers: HTTP request headers (lower-cased keys).

        Returns:
            An absolute base URL with no trailing slash.
        """
        fallback = f"{self._config.host}:{self._config.port}"
        authority = _sanitise_authority(headers.get("host", "")) or _sanitise_authority(fallback)
        scheme = "https" if headers.get("x-forwarded-proto", "").lower() == "https" else "http"
        return f"{scheme}://{authority}"

    def _unauthorized_response(
        self,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        """Build the 401 response, including the discovery challenge.

        Every 401 the transport emits is built here. When an OAuth issuer is
        configured, the response carries a ``WWW-Authenticate`` challenge
        naming the protected-resource metadata URL, so a client that is refused
        can locate the document and start the authorization flow instead of
        stopping at the refusal. Without an issuer no challenge is emitted and
        the anonymous and static-bearer flows are unchanged.

        Args:
            headers: HTTP request headers (lower-cased keys).

        Returns:
            Tuple of (401, response_headers, response_body).
        """
        from bernstein.mcp.oauth import www_authenticate_challenge

        resp_headers = {"content-type": _CONTENT_TYPE_JSON}
        challenge = www_authenticate_challenge(self._request_base_url(headers))
        if challenge is not None:
            resp_headers["www-authenticate"] = challenge
        return (401, resp_headers, b'{"error":"unauthorized"}')

    @staticmethod
    def _is_well_known(path: str) -> bool:
        """Return True for the protected-resource discovery path.

        Bernstein only publishes RFC 9728 protected-resource metadata; the
        RFC 8414 authorization-server metadata is owned by the IdP itself.
        """
        from bernstein.mcp.oauth import PR_METADATA_PATH

        return path == PR_METADATA_PATH

    def _handle_well_known(
        self,
        path: str,
        headers: dict[str, str],
    ) -> tuple[int, dict[str, str], bytes]:
        """Serve protected-resource metadata (RFC 9728 / MCP draft).

        Returns:
            (200, headers, json-body) when discovery is enabled; 404 otherwise.
        """
        from bernstein.mcp.oauth import (
            PR_METADATA_PATH,
            protected_resource_metadata,
        )

        if path == PR_METADATA_PATH:
            # Build the absolute resource URL from the request base so the
            # advertised resource matches what the client called.
            resource_url = f"{self._request_base_url(headers)}{self._config.path}"
            meta = protected_resource_metadata(resource_url)
        else:
            meta = None

        if meta is None:
            return (
                404,
                {"content-type": _CONTENT_TYPE_JSON},
                b'{"error":"oauth discovery not configured"}',
            )
        return (
            200,
            {"content-type": _CONTENT_TYPE_JSON, "cache-control": "public, max-age=300"},
            json.dumps(meta).encode(),
        )

    # -- Auth ----------------------------------------------------------------

    def _authenticate(self, headers: dict[str, str]) -> bool:
        """Validate authentication credentials.

        Args:
            headers: HTTP request headers (lower-cased keys).

        Returns:
            True if the request is authenticated.
        """
        if self._config.auth_type == "none":
            return True

        if self._config.auth_type == "bearer":
            expected = self._config.auth_token
            if not expected:
                # Defence in depth: never treat a blank token as valid even
                # when callers have (incorrectly) reached this branch on a
                # localhost-only bind.
                return False
            auth_header = headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                return False
            token = auth_header[7:]
            return _constant_time_eq(token, expected)

        # Unreachable through normal construction: __post_init__ already
        # rejects any auth_type outside _VALID_AUTH_TYPES. Kept as defence in
        # depth for a config that reached this frozen dataclass by some other
        # path (e.g. object.__setattr__), so an unrecognised value still
        # denies rather than falling open.
        return False


def create_asgi_app(
    server_url: str = _DEFAULT_SERVER_URL,
    config: RemoteMCPConfig | None = None,
    *,
    journal: EventJournal | None = None,
    audit_chain: AuditChainStore | None = None,
    lineage_root: Any = None,
) -> Any:
    """Create ASGI application wrapping Bernstein MCP server with streamable HTTP transport.

    Args:
        server_url: Bernstein task server URL.
        config: Transport configuration. Uses defaults if None.
        journal: Optional run journal; served ``tools/call`` requests become
            ordered ``mcp.stateless_call`` journal entries.
        audit_chain: Optional audit chain store anchoring every served call.
        lineage_root: Lineage store root for the lineage resources (served
            only when ``BERNSTEIN_LINEAGE_MCP_ENABLED`` opts in); defaults
            to ``<cwd>/.sdd/lineage``.

    Returns:
        ASGI application callable.
    """
    cfg = config or RemoteMCPConfig()
    # Interim notice (issue #3088). Emitted at WARNING so an operator sees it
    # in ordinary startup output, not only with debug logging on. Remove with
    # issue #3083.
    logger.warning("%s", validation_scope_notice())
    transport = StreamableHTTPTransport(
        config=cfg,
        server_url=server_url,
        journal=journal,
        audit_chain=audit_chain,
        lineage_root=lineage_root,
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        """ASGI application entry point."""
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return

        if scope["type"] != "http":
            return

        # Read request body.
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        method = scope["method"]
        path = scope["path"]
        raw_headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}

        # CORS preflight.
        if method == "OPTIONS":
            cors_headers = _cors_headers(cfg)
            await _send_response(send, 204, cors_headers, b"")
            return

        status, resp_headers, resp_body = await transport.handle_request(method, path, headers, body)
        resp_headers.update(_cors_headers(cfg))
        await _send_response(send, status, resp_headers, resp_body)

    return app


def _cors_headers(config: RemoteMCPConfig) -> dict[str, str]:
    """Build CORS response headers.

    The legacy protocol-session header stays preflight-allowed so browsers
    running older clients can still send it during the compat window (the
    transport ignores it); it is never exposed because no response carries
    it.
    """
    origin = ", ".join(config.cors_origins)
    return {
        "access-control-allow-origin": origin,
        "access-control-allow-methods": "GET, POST, DELETE, OPTIONS",
        "access-control-allow-headers": f"content-type, authorization, {LEGACY_SESSION_HEADER}",
    }


async def _send_response(
    send: Any,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    """Send an ASGI HTTP response."""
    raw_headers = [(k.encode(), v.encode()) for k, v in headers.items()]
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": raw_headers,
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": body,
        }
    )


def run_remote(
    server_url: str = _DEFAULT_SERVER_URL,
    host: str = "127.0.0.1",
    port: int = 8053,
    auth_token: str | None = None,
) -> None:
    """Start MCP server with streamable HTTP transport for remote access.

    Args:
        server_url: Bernstein task server URL to proxy tool calls to.
        host: Host to bind to. Defaults to loopback; binding to ``0.0.0.0``
            requires a bearer token (passed via ``auth_token`` or the
            ``BERNSTEIN_MCP_TOKEN`` env var), otherwise a
            :class:`RemoteMCPConfigError` is raised at startup.
        port: Port to bind to.
        auth_token: Explicit bearer token. Falls back to
            ``BERNSTEIN_MCP_TOKEN`` / ``BERNSTEIN_MCP_AUTH_TOKEN`` env vars.

    Raises:
        RemoteMCPConfigError: When the host/token combination would expose
            the MCP endpoint without authentication.
    """
    import uvicorn

    token = auth_token if auth_token is not None else _resolve_token_from_env()
    config = RemoteMCPConfig(host=host, port=port, auth_token=token)
    app = create_asgi_app(server_url=server_url, config=config)
    uvicorn.run(app, host=host, port=port)
