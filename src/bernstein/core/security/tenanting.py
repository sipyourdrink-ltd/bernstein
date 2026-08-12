"""Helpers for tenant-aware request scoping, config, and file layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from fastapi import Request

DEFAULT_TENANT_ID = "default"


@dataclass(frozen=True)
class TenantConfig:
    """Configured tenant boundary.

    Attributes:
        id: Stable tenant identifier.
        budget_usd: Optional tenant-specific budget cap.
        allowed_agents: Adapter names allowed to work for this tenant.
    """

    id: str
    budget_usd: float | None = None
    allowed_agents: tuple[str, ...] = ()


@dataclass(frozen=True)
class TenantPaths:
    """Filesystem layout for a tenant-scoped `.sdd` subtree."""

    root: Path
    backlog_dir: Path
    metrics_dir: Path


@dataclass(frozen=True)
class TenantRegistry:
    """Typed lookup for configured tenants."""

    tenants: tuple[TenantConfig, ...] = ()

    def get(self, tenant_id: str) -> TenantConfig | None:
        """Return the configured tenant, if present."""

        normalized = normalize_tenant_id(tenant_id)
        for tenant in self.tenants:
            if tenant.id == normalized:
                return tenant
        return None

    def has(self, tenant_id: str) -> bool:
        """Return whether *tenant_id* is explicitly configured."""

        return self.get(tenant_id) is not None

    @property
    def is_configured(self) -> bool:
        """Return whether any explicit tenants are configured."""

        return bool(self.tenants)


def normalize_tenant_id(raw: str | None) -> str:
    """Normalize a raw tenant ID into a stable non-empty value."""

    value = (raw or "").strip()
    return value or DEFAULT_TENANT_ID


def build_tenant_registry(configs: Sequence[TenantConfig] | None) -> TenantRegistry:
    """Build a registry from parsed tenant configs."""

    if not configs:
        return TenantRegistry()
    normalized: list[TenantConfig] = []
    seen: set[str] = set()
    for config in configs:
        tenant_id = normalize_tenant_id(config.id)
        if tenant_id in seen:
            continue
        seen.add(tenant_id)
        normalized.append(
            TenantConfig(
                id=tenant_id,
                budget_usd=config.budget_usd,
                allowed_agents=tuple(sorted({agent.strip() for agent in config.allowed_agents if agent.strip()})),
            )
        )
    return TenantRegistry(tenants=tuple(normalized))


def tenant_registry_from_seed(seed_config: object | None) -> TenantRegistry:
    """Extract a tenant registry from a seed config-like object."""

    tenants = getattr(seed_config, "tenants", ())
    if not isinstance(tenants, tuple):
        return TenantRegistry()
    typed_tenants = [
        candidate for candidate in cast("tuple[object, ...]", tenants) if isinstance(candidate, TenantConfig)
    ]
    return build_tenant_registry(typed_tenants)


def resolve_tenant_scope(
    bound_tenant: str,
    requested_tenant: str | None = None,
    *,
    registry: TenantRegistry | None = None,
    allow_cross_tenant: bool = False,
) -> str:
    """Resolve the effective tenant for a request.

    The bound tenant is the scope the caller's credential was issued for and
    is the only scope reachable by default.  Selecting a different tenant
    requires *allow_cross_tenant*, which callers derive from the authenticated
    principal (see :func:`request_tenant_cross_scope`) - never from the
    request itself.  ``DEFAULT_TENANT_ID`` carries no implicit privilege: a
    credential bound to the default tenant reaches the default tenant and
    nothing else unless it also carries the operator scope.

    Args:
        bound_tenant: Tenant the credential is bound to.
        requested_tenant: Optional caller-supplied tenant selector.
        registry: Optional configured tenant registry.
        allow_cross_tenant: Whether the principal may select a tenant other
            than the one it is bound to.

    Returns:
        Effective tenant ID.

    Raises:
        PermissionError: If a tenant other than the bound one is requested
            without the operator scope that permits it.
        LookupError: If the resolved tenant is not configured in the registry.
    """

    effective_bound = normalize_tenant_id(bound_tenant)
    target = normalize_tenant_id(requested_tenant) if requested_tenant is not None else effective_bound
    if target != effective_bound and not allow_cross_tenant:
        raise PermissionError(f"tenant scope '{target}' is not accessible from '{effective_bound}'")
    if registry is not None and registry.is_configured and not registry.has(target):
        raise LookupError(f"unknown tenant '{target}'")
    return target


def tenant_paths(sdd_dir: Path, tenant_id: str) -> TenantPaths:
    """Return derived tenant paths inside `.sdd`."""

    normalized = normalize_tenant_id(tenant_id)
    root = sdd_dir / normalized
    return TenantPaths(
        root=root,
        backlog_dir=root / "backlog",
        metrics_dir=root / "metrics",
    )


def ensure_tenant_layout(sdd_dir: Path, tenant_id: str) -> TenantPaths:
    """Create and return the tenant-scoped `.sdd` layout."""

    paths = tenant_paths(sdd_dir, tenant_id)
    paths.backlog_dir.mkdir(parents=True, exist_ok=True)
    paths.metrics_dir.mkdir(parents=True, exist_ok=True)
    return paths


def tenant_metrics_dir(metrics_dir: Path, tenant_id: str) -> Path:
    """Return the tenant metrics directory derived from a shared metrics dir."""

    normalized = normalize_tenant_id(tenant_id)
    if metrics_dir.name == "metrics":
        return metrics_dir.parent / normalized / "metrics"
    return metrics_dir / normalized


# ``request.state`` attributes that carry the request's trusted tenant scope.
# Both are written by the authentication layer and read everywhere else; no
# other writer is legitimate, because anything that can write them decides
# which tenant's data the request reaches.
REQUEST_TENANT_ATTR: Final[str] = "tenant_id"
REQUEST_TENANT_CROSS_SCOPE_ATTR: Final[str] = "tenant_cross_scope"

# Caller-supplied tenant selector.  Read only as a *request*, never as an
# identity: it is authorized against the bound scope by
# :func:`resolve_tenant_scope` before it can take effect.
TENANT_OVERRIDE_HEADER: Final[str] = "x-tenant-id"


def bind_request_tenant(request: Request, tenant_id: str | None, *, cross_tenant: bool = False) -> str:
    """Bind the authenticated tenant scope onto *request*.

    Call this from the authentication layer only, once a credential has been
    validated, passing the tenant that credential is bound to.  Everything
    downstream reads the result through :func:`request_tenant_id`, so this is
    the single point at which a principal's tenant is established.

    Args:
        request: The request being authenticated.
        tenant_id: Tenant the validated credential is bound to.  ``None`` or
            blank normalizes to :data:`DEFAULT_TENANT_ID`.
        cross_tenant: Whether this principal holds the operator scope that
            permits selecting a tenant other than the bound one.

    Returns:
        The normalized tenant ID that was bound.
    """

    normalized = normalize_tenant_id(tenant_id)
    setattr(request.state, REQUEST_TENANT_ATTR, normalized)
    setattr(request.state, REQUEST_TENANT_CROSS_SCOPE_ATTR, bool(cross_tenant))
    return normalized


def request_tenant_id(request: Request) -> str:
    """Return the trusted tenant ID bound to *request* by authentication.

    The value comes from :func:`bind_request_tenant` and therefore from the
    authenticated principal.  Requests that were never authenticated - the
    unauthenticated development mode, truly public paths - fall back to
    :data:`DEFAULT_TENANT_ID`.

    A caller-supplied selector (the ``X-Tenant-Id`` header, a ``?tenant=``
    query parameter) is deliberately NOT consulted here: it is a request, not
    an identity, and reaches an effective scope only after
    :func:`resolve_tenant_scope` authorizes it against the bound one.
    """

    state_value = getattr(request.state, REQUEST_TENANT_ATTR, None)
    if isinstance(state_value, str) and state_value.strip():
        return normalize_tenant_id(state_value)
    return DEFAULT_TENANT_ID


def request_tenant_cross_scope(request: Request) -> bool:
    """Return whether *request*'s principal may select another tenant.

    Only credentials the authentication layer marked with an operator scope
    answer True; every other principal - and every unauthenticated request -
    is confined to the tenant it is bound to.
    """

    return bool(getattr(request.state, REQUEST_TENANT_CROSS_SCOPE_ATTR, False))


def requested_tenant_override(request: Request) -> str | None:
    """Return the caller-supplied tenant selector, or None when absent.

    The value is untrusted input.  Pass it to :func:`resolve_tenant_scope` as
    ``requested_tenant`` so it is authorized against the bound scope; never
    treat it as the request's tenant on its own.
    """

    raw = request.headers.get(TENANT_OVERRIDE_HEADER, "").strip()
    return raw or None
