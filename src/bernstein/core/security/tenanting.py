"""Helpers for tenant-aware request scoping, config, and file layout."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from bernstein.core.persistence.anchored_write import AnchoredDir, mkdir_anchored
from bernstein.core.security.path_containment import PathContainmentError, contained_path

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
    """Filesystem layout for a tenant-scoped `.sdd` subtree.

    The `Path` fields name the layout for callers that read it. `anchor` names
    the same layout as a root plus the components below it, which is what a
    caller that *writes* needs: joining `backlog_dir` at the moment of a write
    derives the directory a second time, and the result is the directory that
    was validated only while nothing replaced a component in between.

    Attributes:
        root: The tenant subtree.
        backlog_dir: Backlog files for this tenant.
        metrics_dir: Metrics files for this tenant.
        anchor: The same subtree, anchored on the `.sdd` directory it was
            derived from. Writers go through this; readers can use either.
    """

    root: Path
    backlog_dir: Path
    metrics_dir: Path
    anchor: AnchoredDir


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


class InvalidTenantIdError(ValueError, LookupError):
    """Raised when a tenant identifier cannot name a tenant.

    Subclasses both builtins on purpose. It is a `ValueError` because the
    identifier is malformed, and a `LookupError` because the practical
    consequence is the same as naming a tenant that does not exist: there is
    no such scope to resolve. The `LookupError` base is what lets the
    existing request surfaces report it as a client error, since they
    already map `LookupError` from `resolve_tenant_scope` to 404 - an
    unvalidated identifier reaching a route would otherwise surface as an
    unhandled 500.

    `LookupError` is deliberate where `PermissionError` is not: the latter
    derives from `OSError`, which several callers catch around filesystem
    work that also normalizes tenant IDs, so a refusal would be silently
    swallowed there.
    """


# A tenant ID is used verbatim as a single filesystem path segment (see
# `tenant_paths`), so it is restricted to characters that name one directory
# entry and nothing else. Equivalent to ``^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$``,
# spelled out to keep the check ASCII-only: `str.isalnum` would accept
# non-ASCII digits and letters that normalize unpredictably on some
# filesystems.
TENANT_ID_MAX_LENGTH = 64
_TENANT_ID_EXTRA_CHARS = frozenset("_.-")

# Names Windows resolves to character devices rather than to a directory,
# with or without an extension (`CON`, `CON.txt`). The suite runs on Windows,
# so these are rejected everywhere to keep one tenant ID meaning one thing on
# every supported platform.
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"} | {f"com{digit}" for digit in "123456789"} | {f"lpt{digit}" for digit in "123456789"}
)


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

    Two further rules keep one identifier from naming two different things
    across supported platforms: a trailing dot is rejected because Windows
    strips it (making ``acme.`` and ``acme`` the same directory), and names
    Windows reserves for character devices are rejected outright.
    """

    if not value or len(value) > TENANT_ID_MAX_LENGTH:
        return False
    if not _is_ascii_alnum(value[0]):
        return False
    if value.endswith("."):
        return False
    if value.split(".", 1)[0].lower() in _WINDOWS_RESERVED_STEMS:
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
        InvalidTenantIdError: If *raw* is non-blank and not a valid tenant ID.
    """

    value = (raw or "").strip()
    if not value:
        return DEFAULT_TENANT_ID
    if not is_valid_tenant_id(value):
        raise InvalidTenantIdError(
            f"invalid tenant id {_describe_tenant_id(value)}: expected 1-{TENANT_ID_MAX_LENGTH} characters "
            "starting with an ASCII letter or digit, followed by ASCII letters, digits, '_', '.', or '-', "
            "not ending in '.', and not a reserved device name (CON, PRN, AUX, NUL, COM1-9, LPT1-9)"
        )
    return value


def try_normalize_tenant_id(raw: object) -> str | None:
    """Normalize a tenant ID read from stored data, or None if unusable.

    For persisted records rather than request input. Rows written before
    these rules existed, or by an operator editing a file by hand, may carry
    a tenant ID that no longer normalizes. A reader scanning history should
    treat such a row as one it cannot attribute and move on, not abort the
    scan - archive reads and isolation checks must still return a result for
    the records they *can* read.

    Because `None` matches no valid tenant ID, callers filtering by tenant
    can compare the result directly and an unattributable row simply never
    matches.

    The parameter is `object` rather than `str | None` because the values
    arrive from JSON, where the field's type is whatever was written. A caller
    that coerces first hides the problem: `str(True)` is `"True"` and
    `str(123)` is `"123"`, both of which satisfy the identifier rules, so a row
    carrying a boolean would be attributed to a tenant named after it. A
    non-string is not a malformed identifier that can be repaired by coercion;
    it is a row this function cannot read, which is exactly what `None` says.

    Args:
        raw: Tenant identifier as stored, possibly absent, malformed, or of
            some type other than `str`.

    Returns:
        The normalized tenant ID, or None if it cannot be normalized.
    """

    if raw is not None and not isinstance(raw, str):
        return None
    try:
        return normalize_tenant_id(raw)
    except InvalidTenantIdError:
        return None


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


def _assert_contained(base: Path, *segments: str, tenant_id: str) -> None:
    """Refuse a derived directory that does not sit strictly under *base*.

    A cheap invariant check on the layout as it stands, run before anything is
    created. It is not what makes a write safe -- resolving a path says where
    it pointed when it was resolved, and the write happens later -- but it
    keeps the derivation honest on its own terms, and it is what fails first if
    the identifier rule is ever widened.

    Delegates the realpath-containment check itself to
    :func:`bernstein.core.security.path_containment.contained_path`, the same
    barrier the rest of the codebase asserts through (#3692), rather than
    re-deriving the check here. Its refusal is translated into
    `InvalidTenantIdError` so existing callers -- which already treat a bad
    tenant id as a `LookupError` via that type's `LookupError` base -- keep
    working unchanged.
    """

    try:
        contained_path(base, *segments, label="tenant id")
    except PathContainmentError as exc:
        raise InvalidTenantIdError(
            f"tenant id {_describe_tenant_id(str(tenant_id))} resolves outside the tenant root"
        ) from exc


def tenant_paths(sdd_dir: Path, tenant_id: str) -> TenantPaths:
    """Return derived tenant paths inside `.sdd`.

    The derived root is asserted to be a strict descendant of *sdd_dir* after
    resolution. `normalize_tenant_id` already restricts the identifier to a
    single path segment, so this check is redundant by design: it keeps the
    layout invariant true on its own terms rather than as a consequence of
    the identifier rule, and it is what fails if that rule is ever widened.

    The check answers where the layout points now. It cannot answer where a
    later write lands, because a caller that joins the returned paths derives
    the directory a second time, and the two derivations agree only while
    nothing replaces a component between them. `TenantPaths.anchor` carries
    the layout in the form that removes the second derivation; writers use it,
    and the containment of *their* directory is a property of the walk rather
    than of this check.

    Args:
        sdd_dir: The `.sdd` directory the tenant subtree lives under.
        tenant_id: Tenant identifier used as the subtree's path segment.

    Returns:
        The derived tenant paths.

    Raises:
        InvalidTenantIdError: If *tenant_id* is not a valid identifier, or if
            the derived root does not resolve to a location under *sdd_dir*.
    """

    normalized = normalize_tenant_id(tenant_id)
    root = sdd_dir / normalized
    _assert_contained(sdd_dir, normalized, tenant_id=tenant_id)
    return TenantPaths(
        root=root,
        backlog_dir=root / "backlog",
        metrics_dir=root / "metrics",
        anchor=AnchoredDir(root=sdd_dir, parts=(normalized,)),
    )


def ensure_tenant_layout(sdd_dir: Path, tenant_id: str) -> TenantPaths:
    """Create and return the tenant-scoped `.sdd` layout.

    Every directory is created through the anchored walk, so a component that
    turns out to be a symlink is refused at the `mkdir` rather than followed.
    That covers the tenant segment itself as well as the two below it: a
    tenant directory linked to a *sibling* tenant passes the containment check
    above -- it does resolve under `.sdd` -- and would otherwise alias one
    tenant's writes onto another's subtree.

    Raises:
        InvalidTenantIdError: If *tenant_id* is not usable, per `tenant_paths`.
        OSError: If a component of the layout is a symlink (`ELOOP`) or is not
            a directory (`ENOTDIR`), or if creation fails.
    """

    paths = tenant_paths(sdd_dir, tenant_id)
    # The anchored walk deliberately never creates its own root: an anchor is
    # the location everything below it is trusted relative to, so creating one
    # would vouch for a place nothing has vouched for. `.sdd` is that root here
    # and it is the caller's own base -- operator configuration, allowed to be
    # a symlink -- so it is created the ordinary way when absent. Leaving it to
    # the walk turned a first run in an empty directory into `FileNotFoundError`
    # on the anchor's `open`, which is not a containment refusal at all.
    sdd_dir.mkdir(parents=True, exist_ok=True)
    mkdir_anchored(paths.anchor.child("backlog"))
    mkdir_anchored(paths.anchor.child("metrics"))
    return paths


def _tenant_metrics_anchor(metrics_dir: Path, tenant_id: str) -> AnchoredDir:
    """Return the anchored form of the tenant metrics directory.

    A shared `.sdd/metrics` directory names the tenant subtree as a sibling
    (`.sdd/<tenant>/metrics`), so the anchor is its parent; anything else is
    treated as a base the tenant segment hangs directly off.

    The containment assert runs before the anchor is constructed, not after.
    `AnchoredDir` refuses a part that is not a single name, and that refusal is
    a plain `ValueError` about a programming error -- correct for its own
    callers, wrong here, where a bad tenant segment has to keep arriving as
    `InvalidTenantIdError` so the request surfaces still report it as a client
    error rather than as a 500.
    """

    normalized = normalize_tenant_id(tenant_id)
    base: Path
    parts: tuple[str, ...]
    if metrics_dir.name == "metrics":
        base, parts = metrics_dir.parent, (normalized, "metrics")
    else:
        base, parts = metrics_dir, (normalized,)
    _assert_contained(base, *parts, tenant_id=tenant_id)
    return AnchoredDir(root=base, parts=parts)


def tenant_metrics_dir(metrics_dir: Path, tenant_id: str) -> Path:
    """Return the tenant metrics directory derived from a shared metrics dir.

    Carries the same containment contract as `tenant_paths`: the derived
    directory must sit strictly under the base it was derived from. Callers
    that write to the result should use `tenant_metrics_target` instead, which
    returns the anchored form and does not require re-deriving the path.

    Raises:
        InvalidTenantIdError: If *tenant_id* is not a valid identifier, or if
            the derived directory does not resolve under its base.
    """

    return _tenant_metrics_anchor(metrics_dir, tenant_id).path


def tenant_metrics_target(metrics_dir: Path, tenant_id: str) -> AnchoredDir:
    """Return the tenant metrics directory as an anchored target.

    The write-side counterpart to `tenant_metrics_dir`. Holding the anchor
    rather than the joined path is what lets a caller defer the write -- a
    buffered metric point, flushed later -- without the directory it writes to
    being re-derived at flush time.

    Raises:
        InvalidTenantIdError: If *tenant_id* is not a valid identifier, or if
            the derived directory does not resolve under its base.
    """

    return _tenant_metrics_anchor(metrics_dir, tenant_id)


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
