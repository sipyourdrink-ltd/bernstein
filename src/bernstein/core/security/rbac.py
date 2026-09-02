"""Role-Based Access Control (RBAC) for API endpoints.

Implements admin/operator/viewer roles with route-level enforcement.
Integrates with the existing :mod:`bernstein.core.auth` role and permission
system, providing a decorator/dependency for FastAPI route protection.

Roles (highest to lowest privilege):
- **admin**: Full access to all endpoints including config and user management.
- **operator**: Task/agent management, no config or user changes.
- **viewer**: Read-only access to dashboards, status, and logs.

Usage in FastAPI routes::

    from bernstein.core.security.rbac import require_role, require_permission

    @router.post(_PATH_TASKS)
    async def create_task(
        request: Request,
        _auth: None = Depends(require_permission(_PERM_TASKS_WRITE)),
    ):
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.security.auth import AuthUser

DEFAULT_ROLE = "viewer"
"""Role assigned when no group of a principal maps to a Bernstein role."""

_PERM_TASKS_WRITE = "tasks:write"

_PATH_TASKS = "/tasks"

_PATH_AGENTS = "/agents"

_PATH_CLUSTER = "/cluster"

_PATH_WEBHOOKS = "/webhooks"

_PERM_WEBHOOKS_MANAGE = "webhooks:manage"

_PERM_CONFIG_WRITE = "config:write"

_PATH_CONFIG = "/config"

_PERM_AUTH_MANAGE = "auth:manage"

_PATH_AUTH_USERS = "/auth/users"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Group-to-role mapping
# ---------------------------------------------------------------------------


def resolve_role_from_groups(
    groups: Iterable[str],
    role_mapping: Mapping[str, str],
    default: str = DEFAULT_ROLE,
) -> str:
    """Map an external group membership list onto a Bernstein role.

    The first group with an entry in ``role_mapping`` wins, so the caller
    controls precedence by the order it supplies the groups in.

    This is the single rule every external identity source resolves through:
    the dashboard's OIDC login (:mod:`bernstein.core.security.sso_oidc`) and
    the directory bridge (:mod:`bernstein.core.security.directory_bridge`)
    both call it rather than deciding a role themselves, so a group that
    grants ``admin`` on one path cannot grant something else on the other.

    Args:
        groups: Group names as the identity source reported them.
        role_mapping: Mapping from external group name to Bernstein role.
        default: Role to return when no group matches.

    Returns:
        The mapped Bernstein role, or ``default``.
    """
    for group in groups:
        role = role_mapping.get(group)
        if role:
            return role
    return default


# ---------------------------------------------------------------------------
# Route permission mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutePermission:
    """Permission requirement for an API route.

    Attributes:
        path_prefix: URL path prefix this rule applies to.
        method: HTTP method (or ``*`` for all methods).
        permission: Required permission string.
    """

    path_prefix: str
    method: str = "*"
    permission: str = ""


# Default route permission rules.  Order matters: first match wins.
_DEFAULT_ROUTE_RULES: Final[list[RoutePermission]] = [
    # Auth management - admin only
    RoutePermission(_PATH_AUTH_USERS, "POST", _PERM_AUTH_MANAGE),
    RoutePermission(_PATH_AUTH_USERS, "DELETE", _PERM_AUTH_MANAGE),
    RoutePermission(_PATH_AUTH_USERS, "PUT", _PERM_AUTH_MANAGE),
    RoutePermission("/auth/roles", "*", _PERM_AUTH_MANAGE),
    # Config - admin only for writes
    RoutePermission(_PATH_CONFIG, "POST", _PERM_CONFIG_WRITE),
    RoutePermission(_PATH_CONFIG, "PUT", _PERM_CONFIG_WRITE),
    RoutePermission(_PATH_CONFIG, "DELETE", _PERM_CONFIG_WRITE),
    RoutePermission(_PATH_CONFIG, "GET", "config:read"),
    # Webhooks - admin only
    RoutePermission(_PATH_WEBHOOKS, "POST", _PERM_WEBHOOKS_MANAGE),
    RoutePermission(_PATH_WEBHOOKS, "PUT", _PERM_WEBHOOKS_MANAGE),
    RoutePermission(_PATH_WEBHOOKS, "DELETE", _PERM_WEBHOOKS_MANAGE),
    # Cluster management
    RoutePermission(_PATH_CLUSTER, "POST", "cluster:write"),
    RoutePermission(_PATH_CLUSTER, "PUT", "cluster:write"),
    RoutePermission(_PATH_CLUSTER, "GET", "cluster:read"),
    # Agent management
    RoutePermission(_PATH_AGENTS, "POST", "agents:write"),
    RoutePermission(_PATH_AGENTS, "DELETE", "agents:kill"),
    RoutePermission(_PATH_AGENTS, "GET", "agents:read"),
    # Task management
    RoutePermission(_PATH_TASKS, "POST", _PERM_TASKS_WRITE),
    RoutePermission(_PATH_TASKS, "PUT", _PERM_TASKS_WRITE),
    RoutePermission(_PATH_TASKS, "DELETE", "tasks:delete"),
    RoutePermission(_PATH_TASKS, "GET", "tasks:read"),
    # Bulletin board
    RoutePermission("/bulletin", "POST", "bulletin:write"),
    RoutePermission("/bulletin", "GET", "bulletin:read"),
    # Cost tracking
    RoutePermission("/costs", "GET", "costs:read"),
    # Status - lowest permission level
    RoutePermission("/status", "GET", "status:read"),
    RoutePermission("/health", "*", ""),  # No auth needed
]


class RBACEnforcer:
    """Enforce role-based access control on API requests.

    Uses the route permission rules to determine what permission is needed
    for each request, then checks the authenticated user's role.

    Args:
        extra_rules: Additional RoutePermission rules (checked before defaults).
    """

    def __init__(
        self,
        extra_rules: list[RoutePermission] | None = None,
    ) -> None:
        self._rules: list[RoutePermission] = []
        if extra_rules:
            self._rules.extend(extra_rules)
        self._rules.extend(_DEFAULT_ROUTE_RULES)

    def get_required_permission(
        self,
        path: str,
        method: str,
    ) -> str | None:
        """Determine the permission required for a request.

        Args:
            path: The request URL path.
            method: The HTTP method (GET, POST, etc.).

        Returns:
            The required permission string, or None if no rule matches
            (meaning the route is unrestricted).
        """
        method_upper = method.upper()
        for rule in self._rules:
            if not path.startswith(rule.path_prefix):
                continue
            if rule.method != "*" and rule.method.upper() != method_upper:
                continue
            # Empty permission means no auth needed
            return rule.permission or None

        # Default: read for GET/HEAD, write for others
        if method_upper in ("GET", "HEAD", "OPTIONS"):
            return "status:read"
        return _PERM_TASKS_WRITE

    def check_access(
        self,
        user: AuthUser | None,
        path: str,
        method: str,
    ) -> tuple[bool, str]:
        """Check whether a user has access to a route.

        Args:
            user: The authenticated user (None for unauthenticated).
            path: The request URL path.
            method: The HTTP method.

        Returns:
            Tuple of (allowed, reason).  ``reason`` is empty when allowed.
        """
        permission = self.get_required_permission(path, method)

        # No permission required
        if permission is None:
            return True, ""

        # No user = no access (unless route is unrestricted)
        if user is None:
            return False, f"Authentication required for {method} {path}"

        if user.has_permission(permission):
            return True, ""

        return (
            False,
            f"Role '{user.role}' lacks permission '{permission}' for {method} {path}",
        )


def require_permission(permission: str) -> Any:
    """FastAPI dependency that requires a specific permission.

    Use as a dependency in route definitions to enforce RBAC::

        @router.get("/admin/config")
        async def get_config(
            request: Request,
            _: None = Depends(require_permission("config:read")),
        ):
            ...

    Args:
        permission: The permission string required.

    Returns:
        A FastAPI dependency callable.

    Raises:
        HTTPException: 401 if no user, 403 if insufficient permissions.
    """

    def _check(request: Request) -> None:
        user: AuthUser | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
            )
        if not user.has_permission(permission):
            raise HTTPException(
                status_code=403,
                detail=f"Insufficient permissions. Required: {permission}, role: {user.role}",
            )

    return _check


def require_role(role_name: str) -> Any:
    """FastAPI dependency that requires a minimum role level.

    Role hierarchy: admin > operator > viewer.

    Args:
        role_name: Minimum role required (e.g. "operator").

    Returns:
        A FastAPI dependency callable.

    Raises:
        HTTPException: 401 if no user, 403 if role is too low.
    """
    _ROLE_LEVEL: dict[str, int] = {
        "admin": 3,
        "operator": 2,
        "viewer": 1,
    }

    required_level = _ROLE_LEVEL.get(role_name, 0)

    def _check(request: Request) -> None:
        user: AuthUser | None = getattr(request.state, "user", None)
        if user is None:
            raise HTTPException(
                status_code=401,
                detail="Authentication required",
            )
        user_level = _ROLE_LEVEL.get(user.role.value, 0)
        if user_level < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Minimum role '{role_name}' required, current role: {user.role.value}",
            )

    return _check
