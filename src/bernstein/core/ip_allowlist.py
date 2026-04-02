"""IP allowlist middleware for network policy enforcement."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Restrict task server access to allowed IP ranges.

    When configured, all requests must originate from an allowed IP range.
    Localhost (127.0.0.1) is always allowed. Health and discovery endpoints
    are exempt.

    Args:
        app: ASGI application.
        allowed_ips: List of allowed IP ranges in CIDR notation.
    """

    # Paths that are always accessible without IP check
    _PUBLIC_PATHS = frozenset(
        {
            "/health",
            "/health/ready",
            "/health/live",
            "/ready",
            "/alive",
            "/.well-known/agent.json",
            "/docs",
            "/openapi.json",
        }
    )

    def __init__(
        self,
        app: ASGIApp,
        allowed_ips: list[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        if allowed_ips:
            for ip_range in allowed_ips:
                try:
                    network = ipaddress.ip_network(ip_range, strict=False)
                    self._allowed_networks.append(network)
                except ValueError as exc:
                    logger.warning("Invalid IP range %s: %s", ip_range, exc)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        """Process request and check IP allowlist.

        Args:
            request: Incoming request.
            call_next: Next middleware/handler.

        Returns:
            Response from next handler or 403 if IP not allowed.
        """
        # If no allowlist configured, pass through
        if not self._allowed_networks:
            return await call_next(request)

        path = request.url.path

        # Public paths always allowed
        if path in self._PUBLIC_PATHS:
            return await call_next(request)

        # Get client IP
        client_ip = self._get_client_ip(request)

        # Localhost always allowed
        if client_ip in ("127.0.0.1", "::1", "localhost"):
            return await call_next(request)

        # Check if client IP is in allowed ranges
        try:
            client_addr = ipaddress.ip_address(client_ip)
            if any(client_addr in network for network in self._allowed_networks):
                return await call_next(request)
        except ValueError:
            logger.warning("Invalid client IP: %s", client_ip)

        # IP not in allowlist
        logger.warning("Blocked request from IP %s to %s", client_ip, path)
        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )

    def _get_client_ip(self, request: Request) -> str:
        """Get client IP from request.

        Args:
            request: Incoming request.

        Returns:
            Client IP address string.
        """
        # Check X-Forwarded-For header first
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Take the first IP in the chain
            return forwarded.split(",")[0].strip()

        # Fall back to direct client
        if request.client:
            return request.client.host

        return "unknown"


def check_ip_allowed(client_ip: str, allowed_ips: list[str]) -> bool:
    """Check if an IP address is in the allowed list.

    Args:
        client_ip: Client IP address to check.
        allowed_ips: List of allowed IP ranges in CIDR notation.

    Returns:
        True if IP is allowed, False otherwise.
    """
    # Localhost always allowed
    if client_ip in ("127.0.0.1", "::1", "localhost"):
        return True

    try:
        client_addr = ipaddress.ip_address(client_ip)
        for ip_range in allowed_ips:
            network = ipaddress.ip_network(ip_range, strict=False)
            if client_addr in network:
                return True
    except ValueError:
        return False

    return False
