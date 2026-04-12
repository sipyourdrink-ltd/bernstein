"""IP allowlist middleware for network policy enforcement."""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Any, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_allowed_networks(allowed_ips: Sequence[str]) -> tuple[_Network, ...]:
    """Parse a sequence of CIDR strings into IP network objects."""
    networks: list[_Network] = []
    for ip_range in allowed_ips:
        try:
            networks.append(ipaddress.ip_network(ip_range, strict=False))
        except ValueError as exc:
            logger.warning("Invalid IP range %s: %s", ip_range, exc)
    return tuple(networks)


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
        allowed_ips: Sequence[str] | None = None,
        public_paths: Sequence[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._configured_allowed_ips = tuple(allowed_ips) if allowed_ips is not None else None
        self._configured_networks = (
            _parse_allowed_networks(self._configured_allowed_ips) if self._configured_allowed_ips is not None else ()
        )
        self._cached_dynamic_allowed_ips: tuple[str, ...] = ()
        self._cached_dynamic_networks: tuple[_Network, ...] = ()
        self._active_public_paths = frozenset(public_paths or self._PUBLIC_PATHS)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        """Process request and check IP allowlist.

        Args:
            request: Incoming request.
            call_next: Next middleware/handler.

        Returns:
            Response from next handler or 403 if IP not allowed.
        """
        path = request.url.path

        # Public paths always allowed
        if path in self._active_public_paths:
            return await call_next(request)

        allowed_networks = self._resolve_allowed_networks(request)

        # If no allowlist configured, pass through
        if not allowed_networks:
            return await call_next(request)

        # Get client IP
        client_ip = self._get_client_ip(request)

        # Localhost always allowed
        if client_ip in _LOOPBACK_HOSTS:
            return await call_next(request)

        # Check if client IP is in allowed ranges
        try:
            client_addr = ipaddress.ip_address(client_ip)
            if any(client_addr in network for network in allowed_networks):
                return await call_next(request)
        except ValueError:
            logger.warning("Invalid client IP: %s", client_ip)

        # IP not in allowlist
        logger.warning("Blocked request from IP %s to %s", client_ip, path)
        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )

    def _resolve_allowed_networks(self, request: Request) -> tuple[_Network, ...]:
        """Resolve the active allowlist from static config or app state."""
        if self._configured_allowed_ips is not None:
            return self._configured_networks

        allowed_ips = self._allowed_ips_from_seed(request)
        if not allowed_ips:
            return ()
        if allowed_ips != self._cached_dynamic_allowed_ips:
            self._cached_dynamic_allowed_ips = allowed_ips
            self._cached_dynamic_networks = _parse_allowed_networks(allowed_ips)
        return self._cached_dynamic_networks

    def _allowed_ips_from_seed(self, request: Request) -> tuple[str, ...]:
        """Read allowlist CIDRs from the current app seed config."""
        seed_config = getattr(request.app.state, "seed_config", None)
        network_config = getattr(seed_config, "network", None)
        allowed_ips_raw: object = getattr(network_config, "allowed_ips", ())
        if not isinstance(allowed_ips_raw, tuple):
            return ()
        allowed_ips_tuple = cast("tuple[object, ...]", allowed_ips_raw)
        for value in allowed_ips_tuple:
            if not isinstance(value, str):
                return ()
        return cast("tuple[str, ...]", allowed_ips_tuple)

    def _get_client_ip(self, request: Request) -> str:
        """Get client IP from request.

        Args:
            request: Incoming request.

        Returns:
            Client IP address string.
        """
        direct_client_ip = request.client.host if request.client else "unknown"
        if direct_client_ip in _LOOPBACK_HOSTS:
            forwarded_ip = self._trusted_forwarded_ip(request)
            if forwarded_ip:
                return forwarded_ip

        return direct_client_ip

    def _trusted_forwarded_ip(self, request: Request) -> str | None:
        """Extract a forwarded client IP when the proxy itself is trusted."""
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",", maxsplit=1)[0].strip()

        forwarded = request.headers.get("Forwarded")
        if not forwarded:
            return None
        first_segment = forwarded.split(",", maxsplit=1)[0]
        for part in first_segment.split(";"):
            key, separator, value = part.partition("=")
            if separator and key.strip().lower() == "for":
                return value.strip().strip('"')
        return None


def check_ip_allowed(client_ip: str, allowed_ips: Sequence[str]) -> bool:
    """Check if an IP address is in the allowed list.

    Args:
        client_ip: Client IP address to check.
        allowed_ips: List of allowed IP ranges in CIDR notation.

    Returns:
        True if IP is allowed, False otherwise.
    """
    # Localhost always allowed
    if client_ip in _LOOPBACK_HOSTS:
        return True

    try:
        client_addr = ipaddress.ip_address(client_ip)
        return any(client_addr in network for network in _parse_allowed_networks(allowed_ips))
    except ValueError:
        return False
