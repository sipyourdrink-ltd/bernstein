"""Helpers for tenant-aware request scoping, config, and file layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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


# A tenant ID is used verbatim as a single filesystem path segment (see
# `tenant_paths`), so it is restricted to characters that name one directory
# entry and nothing else. Equivalent to ``^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$``,
# spelled out to keep the check ASCII-only: `str.isalnum` would accept
# non-ASCII digits and letters that normalize unpredictably on some
# filesystems.
TENANT_ID_MAX_LENGTH = 64
_TENANT_ID_EXTRA_CHARS = frozenset("_.-")


def _is_ascii_alnum(char: str) -> bool:
    """Return whether *char* is an ASCII letter or digit."""

    return "a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9"


def is_valid_tenant_id(value: str) -> bool:
    """Return whether *value* is usable as a tenant path segment.

    The identifier must be non-empty, start with an ASCII alphanumeric, and
    otherwise contain only ASCII alphanumerics, underscore, dot, or hyphen.
    That excludes separators (``/``, ``\\``), relative segments (``.``,
    ``..``), and control characters, so joining it onto a directory always
    yields a child of that directory.
    """

    if not value or len(value) > TENANT_ID_MAX_LENGTH:
        return False
    if not _is_ascii_alnum(value[0]):
        return False
    return all(_is_ascii_alnum(char) or char in _TENANT_ID_EXTRA_CHARS for char in value)


def _describe_tenant_id(value: str) -> str:
    """Render a rejected identifier safely for an error message.

    Uses `repr` so control characters appear as escapes rather than as raw
    bytes in logs, and truncates so an oversized value cannot flood them.
    """

    shown = value if len(value) <= TENANT_ID_MAX_LENGTH else f"{value[:TENANT_ID_MAX_LENGTH]}..."
    return repr(shown)


def normalize_tenant_id(raw: str | None) -> str:
    """Normalize a raw tenant ID into a stable non-empty value.

    Absent or blank input keeps its established meaning and resolves to
    `DEFAULT_TENANT_ID`; callers that are simply tenant-unaware rely on that.
    A value that is present but not a usable path segment is a caller error
    rather than a default, so it is refused.

    Args:
        raw: Raw tenant identifier, possibly absent or padded.

    Returns:
        The normalized tenant ID, or `DEFAULT_TENANT_ID` when *raw* is blank.

    Raises:
        ValueError: If *raw* is non-blank and not a valid tenant ID.
    """

    value = (raw or "").strip()
    if not value:
        return DEFAULT_TENANT_ID
    if not is_valid_tenant_id(value):
        raise ValueError(
            f"invalid tenant id {_describe_tenant_id(value)}: expected 1-{TENANT_ID_MAX_LENGTH} characters "
            "starting with an ASCII letter or digit, followed by ASCII letters, digits, '_', '.', or '-'"
        )
    return value


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
) -> str:
    """Resolve the effective tenant for a request.

    Args:
        bound_tenant: Tenant derived from request/auth context.
        requested_tenant: Optional tenant query parameter.
        registry: Optional configured tenant registry.

    Returns:
        Effective tenant ID.

    Raises:
        PermissionError: If a non-default bound tenant requests another tenant.
        LookupError: If the resolved tenant is not configured in the registry.
    """

    effective_bound = normalize_tenant_id(bound_tenant)
    target = normalize_tenant_id(requested_tenant) if requested_tenant is not None else effective_bound
    if effective_bound != DEFAULT_TENANT_ID and target != effective_bound:
        raise PermissionError(f"tenant scope '{target}' is not accessible from '{effective_bound}'")
    if registry is not None and registry.is_configured and not registry.has(target):
        raise LookupError(f"unknown tenant '{target}'")
    return target


def tenant_paths(sdd_dir: Path, tenant_id: str) -> TenantPaths:
    """Return derived tenant paths inside `.sdd`.

    The derived root is asserted to be a strict descendant of *sdd_dir* after
    resolution. `normalize_tenant_id` already restricts the identifier to a
    single path segment, so this check is redundant by design: it keeps the
    layout invariant true on its own terms rather than as a consequence of
    the identifier rule, and it is what fails if that rule is ever widened.

    Args:
        sdd_dir: The `.sdd` directory the tenant subtree lives under.
        tenant_id: Tenant identifier used as the subtree's path segment.

    Returns:
        The derived tenant paths.

    Raises:
        ValueError: If *tenant_id* is not a valid identifier, or if the
            derived root does not resolve to a location under *sdd_dir*.
    """

    normalized = normalize_tenant_id(tenant_id)
    root = sdd_dir / normalized
    resolved_root = root.resolve()
    resolved_base = sdd_dir.resolve()
    if resolved_root == resolved_base or not resolved_root.is_relative_to(resolved_base):
        raise ValueError(f"tenant id {_describe_tenant_id(str(tenant_id))} resolves outside the tenant root")
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


def request_tenant_id(request: Request) -> str:
    """Return the normalized tenant ID for a request."""

    state_value = getattr(request.state, "tenant_id", None)
    if isinstance(state_value, str) and state_value.strip():
        return normalize_tenant_id(state_value)
    return normalize_tenant_id(request.headers.get("x-tenant-id"))
