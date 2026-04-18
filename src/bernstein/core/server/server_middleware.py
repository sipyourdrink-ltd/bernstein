"""HTTP middleware for the Bernstein task server.

Bearer auth, read-only mode, crash guard, and IP allowlist middleware.
The parent ``server`` module re-exports every name for backward compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

# ---------------------------------------------------------------------------
# Auth middleware — bearer token validation
# ---------------------------------------------------------------------------

# Paths that are always accessible without auth (health checks, agent card,
# API docs, and auth/discovery endpoints).  Keep this list minimal — anything
# that mutates state or exposes operational data must go through bearer auth
# (or HMAC alternative auth, for webhook-style endpoints whose handlers
# verify their own shared secret).
_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/health/ready",
        "/health/live",
        "/health/deps",
        "/ready",
        "/alive",
        "/.well-known/agent.json",
        "/.well-known/acp.json",
        "/acp/v0/agents",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/openapi.yaml",
    }
)

# HMAC-authenticated paths: handler verifies a shared-secret HMAC and rejects
# unsigned requests with 401.  Listed here so the bearer middleware does not
# reject them before the handler runs.
_HMAC_AUTH_PATHS = frozenset(
    {
        "/webhook",
        "/webhooks/github",
        "/webhooks/gitlab",
        "/webhooks/slack/commands",
        "/webhooks/slack/events",
    }
)

# Path prefixes whose handler verifies an HMAC signature (e.g.
# ``/hooks/{session_id}``).
_HMAC_AUTH_PATH_PREFIXES = ("/hooks/",)


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
        if path in _PUBLIC_PATHS or path in _HMAC_AUTH_PATHS or path.startswith(_HMAC_AUTH_PATH_PREFIXES):
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


def _is_sse_request(request: Request) -> bool:
    """Return True when the request is a Server-Sent Events stream.

    Detection checks both the ``Accept`` header and the request path so
    that SSE endpoints remain detectable even when a client omits the
    content-negotiation header.
    """
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept.lower():
        return True
    path = request.url.path
    return path == "/events" or path.startswith("/events/")


class CrashGuardMiddleware(BaseHTTPMiddleware):
    """Catch unhandled exceptions so they return 500 instead of crashing uvicorn.

    Without this, a single bad request (e.g. OOM in a route handler,
    unexpected None, missing key) can kill the entire server process.

    Server-Sent Events (SSE) streams are intentionally excluded: wrapping
    a streaming response in JSON 500 produces unparseable output for SSE
    clients that expect ``event:``/``data:`` lines.  For SSE the exception
    is re-raised so Uvicorn closes the connection cleanly.

    In production the traceback is redacted to a one-line summary plus a
    SHA256 digest of the full traceback.  Setting ``BERNSTEIN_DEBUG=1``
    emits the full traceback via ``logger.exception`` for triage.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        # Do not wrap streaming responses — re-raising lets Uvicorn close
        # the SSE connection without corrupting the wire format.
        if _is_sse_request(request):
            return await call_next(request)
        try:
            return await call_next(request)
        except Exception as exc:
            import hashlib
            import logging as _logging
            import os
            import traceback

            logger = _logging.getLogger(__name__)
            user_agent = request.headers.get("user-agent", "-")
            client_ip = request.client.host if request.client else "-"
            debug_enabled = bool(os.environ.get("BERNSTEIN_DEBUG"))
            if debug_enabled:
                logger.exception(
                    "Unhandled exception in %s %s (client=%s ua=%s)",
                    request.method,
                    request.url.path,
                    client_ip,
                    user_agent,
                )
            else:
                tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                tb_hash = hashlib.sha256(tb_text.encode("utf-8", errors="replace")).hexdigest()[:16]
                logger.error(
                    "Unhandled exception in %s %s: %s: %s (client=%s ua=%s tb_sha256=%s)",
                    request.method,
                    request.url.path,
                    type(exc).__name__,
                    str(exc).splitlines()[0] if str(exc) else "",
                    client_ip,
                    user_agent,
                    tb_hash,
                )
                # Stash full traceback in a debug-only sink so operators
                # with shell access can still correlate by hash.
                logger.debug("Full traceback for %s: %s", tb_hash, tb_text)
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
            pass  # Invalid IP address format; deny request

        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )
