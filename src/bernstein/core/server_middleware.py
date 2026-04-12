"""HTTP middleware for the Bernstein task server.

Bearer auth, read-only mode, crash guard, and IP allowlist middleware.
The parent ``server`` module re-exports every name for backward compatibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.responses import Response as StarletteResponse

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
        "/ws",
        "/health/deps",
        "/grafana/dashboard",
    }
)

# Path prefixes that are always accessible without auth.
# Used for routes with path parameters (e.g. /hooks/{session_id}).
_PUBLIC_PATH_PREFIXES = ("/hooks/", "/export/", "/dashboard/tasks/")


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
            pass  # Invalid IP address format; deny request

        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )
