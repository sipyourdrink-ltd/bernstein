"""IP allowlist middleware for network policy enforcement."""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING, Any, cast

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse
    from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_allowed_networks(allowed_ips: Sequence[str]) -> tuple[_Network, ...]:
    """Parse a sequence of CIDR strings into IP network objects.

    An entry that does not parse is dropped with a warning rather than
    raising: one bad range in a list of five should narrow the allowlist,
    not take the server down. Dropping narrows, so it errs towards denial -
    but only while at least one range survives. Callers must therefore
    distinguish "nothing was configured" from "everything configured was
    dropped"; see :func:`allowlist_is_unusable`.
    """
    networks: list[_Network] = []
    for ip_range in allowed_ips:
        try:
            networks.append(ipaddress.ip_network(ip_range, strict=False))
        except ValueError as exc:
            logger.warning("Invalid IP range %s: %s", sanitize_log(ip_range), sanitize_log(str(exc)))
    return tuple(networks)


def allowlist_is_unusable(configured: Sequence[str], parsed: Sequence[_Network]) -> bool:
    """Is this an allowlist that was asked for but cannot be enforced?

    The middleware treats an empty set of networks as "no allowlist", which
    is right for an operator who configured none and wrong for an operator
    who configured only ranges that fail to parse. The two states are
    identical downstream - an empty tuple either way - so they have to be
    told apart here, before the request is judged.

    ``configured`` empty is the first state: nothing was asked for, the
    middleware is inert, every request passes. ``configured`` non-empty with
    ``parsed`` empty is the second: an operator asked for a restriction and
    a typo silently removed it. That must not resolve to "allow everyone".
    """
    return bool(configured) and not parsed


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Restrict task server access to allowed IP ranges.

    When configured, all requests must originate from an allowed IP range.
    Localhost (127.0.0.1) is always allowed. Health and discovery endpoints
    are exempt.

    A configured allowlist in which *no* range parses is refused rather than
    ignored. Dropping unparseable ranges narrows the allowlist, which is safe
    while one survives; when none does, the same drop widens it to everything,
    and an allowlist that silently stops restricting is worse than one that
    stops the server. ``check_ip_allowed`` has always denied in that state -
    this is the middleware agreeing with it.

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
            "/.well-known/agent.json/keys",
            "/.well-known/mcp-tools",
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

        configured, allowed_networks = self._resolve_allowed_networks(request)

        # An allowlist that was configured but parsed to nothing is a
        # misconfiguration, not an absence. Passing through here is how a
        # single typo turns "only the office range may reach this server"
        # into "anyone may", with nothing in the response to show it.
        if allowlist_is_unusable(configured, allowed_networks):
            logger.error(
                "IP allowlist configured with %d range(s), none of them parseable; refusing %s",
                len(configured),
                sanitize_log(path),
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "IP allowlist is configured but unusable; no range parsed"},
            )

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
            logger.warning("Invalid client IP: %s", sanitize_log(client_ip))

        # IP not in allowlist
        # Both values can come straight from the request - the IP via a
        # forwarded header, the path from the request line - so neither
        # reaches the log without escaping.
        logger.warning("Blocked request from IP %s to %s", sanitize_log(client_ip), sanitize_log(path))
        return JSONResponse(
            status_code=403,
            content={"detail": f"IP {client_ip} not in allowed list"},
        )

    def _resolve_allowed_networks(self, request: Request) -> tuple[tuple[str, ...], tuple[_Network, ...]]:
        """Resolve the active allowlist from static config or app state.

        Returns both halves - the raw ranges the operator configured and the
        ones that parsed - because the caller cannot tell a missing allowlist
        from a wholly unparseable one from the parsed tuple alone.
        """
        if self._configured_allowed_ips is not None:
            return self._configured_allowed_ips, self._configured_networks

        allowed_ips = self._allowed_ips_from_seed(request)
        if not allowed_ips:
            return (), ()
        if allowed_ips != self._cached_dynamic_allowed_ips:
            self._cached_dynamic_allowed_ips = allowed_ips
            self._cached_dynamic_networks = _parse_allowed_networks(allowed_ips)
        return allowed_ips, self._cached_dynamic_networks

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
