"""Authentication middleware for the Bernstein task server.

Replaces the simple BearerAuthMiddleware with a multi-strategy middleware
that supports:
- JWT tokens (from SSO login)
- Agent identity JWT tokens (per-agent, task-scoped, zero-trust)
- Legacy bearer tokens (backwards compatible)
- Public path exemptions (health, discovery, login flow)
- HMAC-authenticated path exemptions (webhooks/hooks validate their own HMAC)
- User context injection into request.state
- RFC 8707 resource-indicator validation (audience binding for OAuth tokens)

Secure-by-default
-----------------
Authentication is REQUIRED by default.  A request to any protected path
without a valid Bearer token (and without a matching HMAC signature for
HMAC-authenticated paths) returns HTTP 401.  To run without authentication
(development convenience only) set ``BERNSTEIN_AUTH_DISABLED=1`` or put
``auth.enabled: false`` in ``bernstein.yaml`` - this logs a loud warning
once per process.

Zero-trust enforcement
----------------------
When an agent presents a task-scoped JWT, the middleware resolves the task
the request is about to act on and validates that its id appears in the
token's ``task_ids`` claim.  The rule is applied to the task identity, not
to a list of blessed URLs, so it holds wherever that identity arrives from:

* **Path-addressed.**  Deny-by-default over the whole ``/tasks/{id}/...``
  surface (and its ``/api/v<n>`` mirror), whatever the action segment below
  it, plus every other registered route whose path template declares a
  ``{task_id}`` parameter - ``/approvals/{task_id}/approve``, the review
  board's per-task decision route, and any per-task route added later under
  any prefix.  That second set is compiled from the app's own route table
  (:func:`task_id_route_patterns`), so it cannot drift from the routes the
  app registers.
* **Body-addressed and batch-addressed.**  The collection routes under
  ``/tasks/`` that name existing tasks in their request body
  (``TASK_BODY_SCOPED_SEGMENTS``) have no id in the path for the gate above
  to read, so their handlers apply the same rule through
  :func:`enforce_agent_task_scope_for_ids`.
* **Indirectly resolved.**  Handlers that reach a task through some other
  key - an ACP run, a plan, a cluster steal decision - call the same
  function with the ids they resolved, before mutating them.

Only the registered ``/tasks/`` collection routes are exempt from the path
gate, because they address the collection rather than one task.  That
exemption is keyed on the route a path resolves to
(:func:`task_collection_route_patterns`), not on the text of its first
segment, so a task whose id equals a collection segment name cannot borrow
the exemption.  A token without a task scope (``task_ids == []``) is treated
as unrestricted (manager / orchestrator tokens), and non-agent credentials
(SSO users, the legacy operator bearer, the cluster secret) never reach this
check at all.

RFC 8707 resource indicators
----------------------------
When ``expected_resource`` is configured (single string or list passed to
:class:`SSOAuthMiddleware`, or ``BERNSTEIN_AUTH_EXPECTED_RESOURCE`` env var,
or ``auth.expected_resource`` in :class:`SSOConfig`), bearer JWTs that
carry a ``resource`` claim must match one of the configured values.
Mismatches are rejected with HTTP 401 and the RFC 6750 ``WWW-Authenticate``
challenge ``Bearer error="invalid_token", error_description="resource indicator mismatch"``.
Tokens that omit the claim entirely pass through - the middleware does
not retroactively require an indicator on legacy tokens. The check is
skipped wholesale when ``expected_resource`` is unset so upgrading does
not break deployments minting opaque non-OAuth tokens.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from fastapi import Request
    from starlette.responses import Response as StarletteResponse

    from bernstein.core.agents.agent_identity import AgentIdentityStore
    from bernstein.core.security.auth import AuthService

_PERM_TASKS_WRITE = "tasks:write"
_PERM_ADMIN_MANAGE = "admin:manage"

type _ExpectedResourceConfig = str | Sequence[str] | None

# One registered ``/tasks/`` collection route: its anchored path matcher and
# the methods it accepts.  Both must match for a path to be exempt.
type _CollectionRoutePatterns = Sequence[tuple[re.Pattern[str], frozenset[str]]]

_EMPTY_EXPECTED_RESOURCES: Final[tuple[str, ...]] = ()

logger = logging.getLogger(__name__)

# Regex to extract the addressed task ID from any per-task path.
#
# Deny-by-default: this matches ``/tasks/<id>`` and every sub-path below it,
# on the root mount and on the versioned mirrors the app also registers
# (``/api/v1/tasks/...``).  It deliberately does NOT enumerate action names -
# the previous alternation (``complete|fail|progress|cancel|block|steal``)
# was hand-maintained, drifted from the route table as routes were added,
# and still named ``steal``, which is not a task route at all (the real one
# is ``POST /cluster/steal``).  Anything that addresses a single task by id
# is now scope-checked; only the collection segments listed in
# ``TASK_COLLECTION_SEGMENTS`` are exempt.
_TASK_ID_PATH_RE = re.compile(r"^(?:/api/v\d+)?/tasks/(?P<task_id>[^/]+)(?:/.*)?$")

# Collection routes registered directly under ``/tasks/`` that name an
# EXISTING task id in the request body rather than in the path.  The
# path-level gate cannot see a request body, so these routes carry the same
# rule at the handler by calling :func:`enforce_agent_task_scope_for_ids` on
# the ids they are about to act on.  Without that call a token scoped to task
# A could cancel task B through ``POST /tasks/batch-ops`` while
# ``POST /tasks/B/cancel`` denied it - the same operation, one path over.
#
#   batch-ops    - ``ids``: the tasks the bulk action mutates
#   claim-batch  - ``task_ids``: the tasks claimed for the caller
#   self-create  - ``parent_task_id``: the NEW task is unconstrained, but the
#                  named parent is an existing task this route transitions to
#                  ``waiting_for_subtasks``, which is exactly what
#                  ``POST /tasks/{parent}/wait-for-subtasks`` does
TASK_BODY_SCOPED_SEGMENTS: Final[frozenset[str]] = frozenset(
    {
        "batch-ops",
        "claim-batch",
        "self-create",
    }
)

# Segments that sit where a task id would but are NOT task ids: collection
# routes registered directly under ``/tasks/``.  Each entry is exempt from
# the path-level check because it addresses the task collection rather than
# one task, so there is no task id in the path to bind the identity to:
#
#   archive, counts, graph, search  - read-only collection queries
#   next                            - ``GET /tasks/next/{role}`` claim-next;
#                                     the server picks the row, the caller
#                                     cannot name one
#   batch                           - creates NEW tasks, so no existing id
#                                     can be in scope yet
#   batch-ops, claim-batch,         - name existing ids in the body; scoped
#   self-create                       at the handler, see
#                                     ``TASK_BODY_SCOPED_SEGMENTS`` above
#   claim-receipt                   - claims the next eligible backlog row
#                                     for the caller; like ``next``, the
#                                     caller cannot name a task
#
# Membership here is necessary but not sufficient: the exemption applies to
# the registered collection ROUTES these segments name, not to the segment
# text wherever it appears.  ``/tasks/archive`` is exempt;
# ``/tasks/archive/cancel`` is a per-task path that happens to carry
# ``archive`` as the id and stays gated.  See
# :func:`task_collection_route_patterns`.
#
# ``tests/unit/test_auth_middleware_task_scope_routes.py`` pins this set to
# the literal segments actually registered under ``/tasks/``: a new
# collection route fails that test until it is exempted deliberately, and a
# new ``/tasks/{task_id}/...`` route needs no change here because it is
# covered by default.
TASK_COLLECTION_SEGMENTS: Final[frozenset[str]] = (
    frozenset(
        {
            "archive",
            "batch",
            "claim-receipt",
            "counts",
            "graph",
            "next",
            "search",
        }
    )
    | TASK_BODY_SCOPED_SEGMENTS
)

# Path templates carry ``{task_id}`` for the task they address.  Per-task
# routes exist outside ``/tasks/`` too (``/approvals/{task_id}/approve``,
# ``/dashboard/review-board/runs/{run_id}/tasks/{task_id}/review``), and the
# ``/tasks/``-anchored pattern above cannot see them.  Rather than name those
# prefixes here - the enumeration that #3036 was filed about - the matchers
# are compiled from the route table the app actually registers.
_TASK_ID_TEMPLATE_PARAM: Final[str] = "task_id"

# One ``{name}`` or ``{name:convertor}`` placeholder in a path template.
_TEMPLATE_PARAM_RE = re.compile(r"\{(?P<name>[^{}:]+)(?::(?P<convertor>[^{}]+))?\}")

# A path template whose first segment under ``/tasks/`` is a literal rather
# than a placeholder, on the root mount or an ``/api/v<n>`` mirror.  Used to
# pick the registered collection routes out of the route table.
_TASK_COLLECTION_TEMPLATE_RE = re.compile(r"^(?:/api/v\d+)?/tasks/(?P<segment>[^/{}]+)(?:/.*)?$")

# Where the compiled matchers are memoised on ``app.state``.
_TASK_ROUTE_PATTERNS_ATTR: Final[str] = "_agent_task_scope_route_patterns"
_TASK_COLLECTION_ROUTE_PATTERNS_ATTR: Final[str] = "_agent_task_scope_collection_route_patterns"

# ---------------------------------------------------------------------------
# Public and HMAC-authenticated paths
# ---------------------------------------------------------------------------

# Paths that are always accessible without any authentication.
# Keep this list tiny - only trivially public endpoints (health probes,
# discovery metadata, login flow) belong here.  API docs and the OpenAPI
# schema are gated via ``AUTH_DEV_ONLY_PUBLIC_PATHS`` below so that they
# require viewer auth whenever the server is running with a configured
# auth backend (see ``_compute_auth_configured`` and ``dispatch``).
AUTH_PUBLIC_PATHS = frozenset(
    {
        # Health / readiness probes (k8s / load-balancer probes)
        "/health",
        "/health/ready",
        "/health/live",
        "/health/deps",
        "/ready",
        "/alive",
        # Agent / protocol discovery
        "/.well-known/agent.json",
        # A2A v1.0 canonical discovery path (#2609): served identical to
        # ``/.well-known/agent.json`` so shipped clients that fetch either the
        # v1.0 name or the legacy name both resolve the signed card.
        "/.well-known/agent-card.json",
        "/.well-known/agent.json/keys",
        "/.well-known/http-message-signatures-directory",
        "/.well-known/acp.json",
        "/.well-known/mcp-tools",
        "/llms.txt",
        "/acp/v0/agents",
        # A2A JSON-RPC server surface (#2609). These endpoints run their OWN
        # auth (card-declared API key + OAuth2 client-credentials) and reject
        # unauthenticated calls per spec, so the server-wide bearer check must
        # let them reach the handler. When the surface is disabled (the
        # default) the handler answers 404, exposing nothing.
        "/a2a/v1",
        "/a2a/v1/oauth/token",
        # Auth flow endpoints (must be public for login to work)
        "/auth/login",
        "/auth/oidc/callback",
        "/auth/saml/acs",
        "/auth/saml/metadata",
        "/auth/cli/device",
        "/auth/cli/token",
        "/auth/providers",
        # Dashboard auth flow (#2366). Login requires a credential in the
        # body, status only reports whether auth is required, and logout is
        # an idempotent session drop - none of them expose run data. The
        # /api/v1 mirrors are listed too because this set matches exact
        # paths.
        "/dashboard/auth/login",
        "/dashboard/auth/logout",
        "/dashboard/auth/status",
        "/api/v1/dashboard/auth/login",
        "/api/v1/dashboard/auth/logout",
        "/api/v1/dashboard/auth/status",
        # Static operator GUI shell + PWA assets. The single-page-app bundle
        # carries no run data and authenticates its OWN API calls with a
        # Bearer token it reads from localStorage, so the shell is safe to
        # serve anonymously - every data route (/tasks, /dashboard/*, the
        # /api/v1/* data endpoints, /status, ...) stays behind the bearer
        # check in ``dispatch``. Without this, a plain browser navigation to
        # /ui dead-ends on a bare 401 instead of loading the app (and its
        # token-entry screen), and the browser's automatic /favicon.ico probe
        # 401s on every page load. ``/ui`` itself is the exact SPA entry; its
        # sub-paths are covered by ``AUTH_PUBLIC_PATH_PREFIXES`` below.
        "/ui",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/sw.js",
        "/offline.html",
    }
)

# Paths that are anonymous ONLY in true dev mode (no auth backend
# configured).  When any auth backend is present (SSO service, legacy
# bearer token, or agent identity store) these require a valid token with
# at least viewer permissions.  This avoids leaking the API attack surface
# in production while keeping ``uvicorn …`` hello-world runs friendly.
AUTH_DEV_ONLY_PUBLIC_PATHS = frozenset(
    {
        "/docs",
        "/redoc",
        "/openapi.json",
        "/openapi.yaml",
    }
)

# Paths whose handlers perform their own HMAC-based verification.  The
# bearer-token middleware lets these pass; the route itself rejects
# unsigned / badly-signed requests with 401.
#
# IMPORTANT: do NOT add paths here unless their handler actually verifies
# a shared-secret HMAC signature.  An entry here bypasses bearer auth.
AUTH_HMAC_PATHS = frozenset(
    {
        "/webhook",
        "/webhooks/github",
        "/webhooks/gitlab",
        "/webhooks/slack/commands",
        "/webhooks/slack/events",
    }
)

# Path prefixes whose handlers perform their own HMAC verification.
# Used for routes with path parameters (e.g. /hooks/{session_id}).
AUTH_HMAC_PATH_PREFIXES = ("/hooks/",)

# Public path prefixes served without auth: the static GUI shell's own assets
# under ``/ui/`` (hashed JS/CSS bundles, PWA icons, service worker, offline
# page). This matches ``/ui/...`` but deliberately NOT the bare ``/ui`` (that
# exact path is in ``AUTH_PUBLIC_PATHS``) and NOT sibling paths such as
# ``/uitasks`` - only true descendants of ``/ui/`` are public. See the
# ``AUTH_PUBLIC_PATHS`` comment for why the shell is safe to serve anonymously.
AUTH_PUBLIC_PATH_PREFIXES = ("/ui/",)

# Opt-out flag: when set to a truthy value, auth is disabled and the
# middleware passes every request through (with a loud warning on startup).
AUTH_DISABLED_ENV = "BERNSTEIN_AUTH_DISABLED"

_AUTH_DISABLED_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Read-only methods that viewers can access
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Environment override for the configured RFC 8707 resource indicator(s).
# Comma-separated; empty disables the check entirely (legacy behaviour).
AUTH_EXPECTED_RESOURCE_ENV = "BERNSTEIN_AUTH_EXPECTED_RESOURCE"

# RFC 6750 §3 challenge for an audience-mismatched token. Issuer and
# wording match RFC 8707 §3 ("resource indicator mismatch") so well-known
# OAuth clients can react with a deterministic error reason.
_RESOURCE_MISMATCH_CHALLENGE = 'Bearer error="invalid_token", error_description="resource indicator mismatch"'
_RESOURCE_MALFORMED_CHALLENGE = 'Bearer error="invalid_token", error_description="malformed resource indicator"'

# Route → required permission mapping for write operations.
#
# Every write endpoint that Bernstein exposes MUST have an explicit entry
# here.  Any request that falls through without a match is treated as
# operator-only (``admin:manage``) - fail closed, not open.
#
# Operator-sensitive endpoints (``/shutdown``, ``/broadcast``, ``/drain``,
# ``/config``) require ``admin:manage``, which is only granted to the
# ``admin`` role - operator and agent tokens cannot reach them.
_ROUTE_PERMISSIONS: dict[str, str] = {
    "/tasks": _PERM_TASKS_WRITE,
    "/agents": "agents:write",
    "/cluster": "cluster:write",
    "/bulletin": "bulletin:write",
    "/auth": "auth:manage",
    "/config": _PERM_ADMIN_MANAGE,
    "/webhooks": "webhooks:manage",
    "/shutdown": _PERM_ADMIN_MANAGE,
    "/broadcast": _PERM_ADMIN_MANAGE,
    "/drain": _PERM_ADMIN_MANAGE,
}


def _normalise_expected_resource(raw: _ExpectedResourceConfig) -> tuple[str, ...]:
    """Coerce ``expected_resource`` into a tuple of trimmed values.

    Accepts:
        - ``None`` or empty string → ``()`` (disables the check).
        - Single non-empty string → ``(value,)``.
        - Comma-separated string (e.g. ``"https://a,https://b"``) → tuple
          of trimmed entries with empty segments stripped.
        - ``list``/``tuple`` of strings → trimmed tuple.

    The tuple is then matched any-of against the token's ``resource``
    claim by :func:`_resource_indicator_check`.
    """
    if raw is None:
        return _EMPTY_EXPECTED_RESOURCES
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return _EMPTY_EXPECTED_RESOURCES
        if "," in text:
            return tuple(part.strip() for part in text.split(",") if part.strip())
        return (text,)
    return tuple(item.strip() for item in raw if item and item.strip())


def expected_resource_from_env() -> tuple[str, ...]:
    """Resolve the env-var override for the configured resource indicator."""
    return _normalise_expected_resource(os.environ.get(AUTH_EXPECTED_RESOURCE_ENV, ""))


def _resource_indicator_check(
    claims: dict[str, Any],
    expected: tuple[str, ...],
) -> JSONResponse | None:
    """Validate the JWT's ``resource`` claim against the configured indicators.

    Returns:
        * ``None`` when the check passes (claim missing, or claim matches
          one of the expected values, or the indicator is unconfigured).
        * A :class:`JSONResponse` (HTTP 401) when the claim is malformed or
          mismatches every configured value. The response carries the
          RFC 6750 ``WWW-Authenticate`` challenge so OAuth clients can
          react deterministically.

    Args:
        claims: Decoded JWT claims dict.
        expected: Tuple of acceptable resource URIs.  Empty disables the
            check entirely (legacy behaviour).
    """
    if not expected:
        return None

    resource = claims.get("resource")
    if resource is None:
        # Legacy tokens that pre-date RFC 8707 stay valid; the orchestrator
        # only enforces the audience binding when the claim is actually
        # present. Enforcing it on every token would lock out existing
        # legacy bearer flows on upgrade.
        return None

    # RFC 8707 §2 allows ``resource`` to be a single URI string or a JSON
    # array of URI strings. Anything else is malformed.
    if isinstance(resource, str):
        candidates: tuple[str, ...] = (resource,)
    elif isinstance(resource, list):
        resource_items = cast("list[object]", resource)
        if not all(isinstance(item, str) for item in resource_items):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Token resource indicator is not a string or array of strings",
                },
                headers={"WWW-Authenticate": _RESOURCE_MALFORMED_CHALLENGE},
            )
        candidates = tuple(cast("list[str]", resource_items))
    else:
        return JSONResponse(
            status_code=401,
            content={
                "detail": "Token resource indicator is not a string or array of strings",
            },
            headers={"WWW-Authenticate": _RESOURCE_MALFORMED_CHALLENGE},
        )

    if any(candidate in expected for candidate in candidates):
        return None

    return JSONResponse(
        status_code=401,
        content={
            "detail": "Token resource indicator does not match this orchestrator",
            "expected": list(expected),
            "actual": list(candidates),
        },
        headers={"WWW-Authenticate": _RESOURCE_MISMATCH_CHALLENGE},
    )


def auth_disabled_via_opt_out() -> bool:
    """Return True when auth has been explicitly opted out for the process.

    The only supported opt-out signal is the ``BERNSTEIN_AUTH_DISABLED``
    environment variable set to a truthy value (``1``, ``true``, ``yes``,
    ``on``).  Config-based opt-out (``auth.enabled: false`` in
    ``bernstein.yaml``) is handled at the app factory layer, which passes
    the resolved flag into :class:`SSOAuthMiddleware` via ``auth_disabled``.
    """
    return os.environ.get(AUTH_DISABLED_ENV, "").strip().lower() in _AUTH_DISABLED_TRUTHY


def _get_required_permission(path: str, method: str) -> str | None:
    """Determine the required permission for a request.

    Returns None if no specific permission is needed (public/read).
    """
    # Check specific path patterns first (before prefix matching)
    if "/kill" in path:
        return "agents:read" if method in _READ_METHODS else "agents:kill"

    if method in _READ_METHODS:
        # Read operations need basic read permission on the resource
        for prefix, perm in _ROUTE_PERMISSIONS.items():
            if path.startswith(prefix):
                return perm.replace(":write", ":read").replace(":manage", ":read")
        return "status:read"  # Default read permission

    # Admin-only prefixes always win over substring heuristics so that, e.g.,
    # ``/drain/cancel`` does not fall through to ``tasks:write`` just because
    # it contains ``/cancel``.
    for prefix, perm in _ROUTE_PERMISSIONS.items():
        if perm == _PERM_ADMIN_MANAGE and path.startswith(prefix):
            return perm

    # Write operations - check specific action paths before prefix
    if "/complete" in path or "/fail" in path or "/cancel" in path or "/block" in path:
        return _PERM_TASKS_WRITE

    for prefix, perm in _ROUTE_PERMISSIONS.items():
        if path.startswith(prefix):
            return perm

    # Fail CLOSED: unknown write routes require operator-level
    # ``admin:manage`` rather than the old ``tasks:write`` open fallback.
    # Any new endpoint MUST be added to ``_ROUTE_PERMISSIONS`` explicitly.
    return _PERM_ADMIN_MANAGE


class SSOAuthMiddleware(BaseHTTPMiddleware):
    """Multi-strategy authentication middleware.

    Authentication strategies (tried in order for Bearer-authenticated
    paths):

    1. SSO JWT token in ``Authorization: Bearer <jwt>``
    2. Agent identity JWT (per-agent, task-scoped - zero-trust enforcement)
    3. Legacy static bearer token
    4. 401 if no strategy accepts the token

    HMAC-authenticated paths (``AUTH_HMAC_PATHS``, ``AUTH_HMAC_PATH_PREFIXES``)
    bypass bearer auth - their route handlers verify a shared-secret HMAC
    signature and reject invalid / missing signatures with 401.

    Truly-public paths (``AUTH_PUBLIC_PATHS``) require no auth at all.

    On successful auth, injects ``request.state.user`` (AuthUser or None)
    and ``request.state.auth_claims`` (dict) for downstream routes.

    For agent identity JWTs, ``request.state.agent_identity`` is also set
    (``AgentIdentity``) so that route handlers can perform finer-grained
    checks if needed.
    """

    # Log the "auth disabled" warning at most once per process to keep logs
    # readable while still making the misconfiguration loud.
    _warned_disabled: bool = False

    def __init__(
        self,
        app: Any,
        auth_service: AuthService | None = None,
        legacy_token: str | None = None,
        agent_identity_store: AgentIdentityStore | None = None,
        auth_disabled: bool | None = None,
        expected_resource: _ExpectedResourceConfig = None,
        cluster_secret: str | None = None,
    ) -> None:
        super().__init__(app)
        self._auth_service = auth_service
        self._legacy_token = legacy_token
        self._agent_identity_store = agent_identity_store
        # The cluster shared secret is accepted as a worker credential so a
        # single token clears both this outer layer and the inner cluster
        # route layer (#2805). It is barred from operator-only endpoints
        # below, mirroring the agent-identity restriction.
        self._cluster_secret = cluster_secret or None
        # Resolve opt-out from explicit arg > env var. Config-based opt-out
        # should be passed in via ``auth_disabled=True`` from the factory.
        resolved_disabled = bool(auth_disabled) or auth_disabled_via_opt_out()
        self._auth_disabled = resolved_disabled
        self._auth_configured = self._compute_auth_configured()
        # RFC 8707 resource-indicator binding. Explicit constructor arg
        # wins; env var falls back. Empty tuple disables the check.
        self._expected_resource: tuple[str, ...] = (
            _normalise_expected_resource(expected_resource) or expected_resource_from_env()
        )
        if resolved_disabled and not SSOAuthMiddleware._warned_disabled:
            logger.warning(
                "SECURITY: Bernstein auth is DISABLED - every request is "
                "accepted without a Bearer token (opt-out via "
                "BERNSTEIN_AUTH_DISABLED or auth.enabled=false).  "
                "Do NOT run this configuration on any network-exposed host.",
            )
            SSOAuthMiddleware._warned_disabled = True

    def _compute_auth_configured(self) -> bool:
        """Return True when any auth backend is available.

        ``/docs``, ``/openapi.json`` and friends stay anonymous only when no
        authenticator is wired up - i.e. true dev mode (developer runs the
        server by hand with no ``BERNSTEIN_AUTH_TOKEN``, no SSO, no agent
        identity store).  As soon as any backend is configured the server is
        assumed to face a real network and these paths require a bearer
        token with viewer permissions.
        """
        if self._auth_service is not None:
            return True
        if self._legacy_token:
            return True
        if self._cluster_secret:
            return True
        if self._agent_identity_store is not None:
            return True
        # Fallback: if an env-level legacy token is set somewhere outside the
        # middleware's own init path (e.g. the server factory reads it from
        # the environment but hasn't threaded it here), treat auth as
        # configured to fail closed.
        return bool(os.environ.get("BERNSTEIN_AUTH_TOKEN", "").strip())

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
    ) -> StarletteResponse:
        path = request.url.path

        # Opt-out: pass every request through unauthenticated.
        if self._auth_disabled:
            response: StarletteResponse = await call_next(request)
            return response

        # Truly-public paths are always accessible.
        if path in AUTH_PUBLIC_PATHS:
            response = await call_next(request)
            return response

        # Static GUI shell assets under /ui/ are public (see
        # AUTH_PUBLIC_PATH_PREFIXES). The shell authenticates its own data
        # calls; the data routes themselves stay gated below.
        if path.startswith(AUTH_PUBLIC_PATH_PREFIXES):
            response = await call_next(request)
            return response

        # Dev-only public paths (API docs, OpenAPI schema) - anonymous
        # access only when no auth backend is configured.  When auth IS
        # configured we fall through to the normal bearer-token path so the
        # request is gated behind a viewer-level permission.
        if path in AUTH_DEV_ONLY_PUBLIC_PATHS and not self._auth_configured:
            response = await call_next(request)
            return response

        # HMAC-authenticated paths: the route handler verifies a shared
        # secret; the bearer middleware lets them through.
        if path in AUTH_HMAC_PATHS or path.startswith(AUTH_HMAC_PATH_PREFIXES):
            response = await call_next(request)
            return response

        # Dashboard requests already authenticated by the dashboard auth
        # layer (#2366). ``dashboard_principal`` is stamped only after the
        # outer DashboardAuthMiddleware validated a session or a signed
        # scoped token and journaled the authz decision, so honouring it
        # here does not widen the surface - unauthenticated dashboard
        # requests never carry it and fall through to the bearer checks.
        if path.startswith(("/dashboard", "/api/v1/dashboard")) and getattr(request.state, "dashboard_principal", ""):
            response = await call_next(request)
            return response

        auth_header = request.headers.get("authorization", "")
        has_bearer = auth_header.startswith("Bearer ")

        if not has_bearer:
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header[7:]  # Strip "Bearer "

        # Strategy 1: Try SSO JWT validation (if SSO auth service is available)
        if self._auth_service is not None:
            sso_result = self._try_sso_auth(request, token, path)
            if sso_result is not None:
                if isinstance(sso_result, JSONResponse):
                    return sso_result
                response = await call_next(request)
                return response

        # Strategy 2: Agent identity JWT (zero-trust, task-scoped)
        if self._agent_identity_store is not None:
            agent_result = await self._try_agent_jwt(request, call_next, path, token)
            if agent_result is not None:
                return agent_result

        # Strategy 3: Legacy bearer token
        if self._legacy_token:
            import hmac

            if hmac.compare_digest(token, self._legacy_token):
                # Legacy tokens get operator-level access
                request.state.user = None  # type: ignore[attr-defined]
                request.state.auth_claims = {"legacy": True}  # type: ignore[attr-defined]
                response = await call_next(request)
                return response

        # Strategy 3b: Cluster shared secret. A cluster worker presents the
        # cluster secret to register, heartbeat, and pull tasks; it must clear
        # this outer layer as well as the inner cluster route layer (#2805).
        # Accepted like the legacy operator token but - like agent tokens -
        # barred from operator-only (admin:manage) endpoints so it cannot
        # shut down or reconfigure the server.
        if self._cluster_secret:
            import hmac

            if hmac.compare_digest(token, self._cluster_secret):
                if (
                    request.method not in _READ_METHODS
                    and _get_required_permission(path, request.method) == _PERM_ADMIN_MANAGE
                ):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": "Cluster credential cannot access operator-only endpoints",
                            "required_permission": _PERM_ADMIN_MANAGE,
                        },
                    )
                request.state.user = None  # type: ignore[attr-defined]
                request.state.auth_claims = {"cluster": True}  # type: ignore[attr-defined]
                response = await call_next(request)
                return response

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or expired authentication token"},
        )

    def _try_sso_auth(
        self,
        request: Request,
        token: str,
        path: str,
    ) -> JSONResponse | bool | None:
        """Validate SSO JWT. Returns JSONResponse on RBAC fail, True on success, None on miss."""
        assert self._auth_service is not None
        result = self._auth_service.validate_token(token)
        if result is None:
            return None

        user, claims = result

        # RFC 8707: reject SSO tokens minted for a different audience before
        # the request reaches its handler. Skipped when ``expected_resource``
        # is unconfigured, or when the token omits the claim entirely.
        resource_error = _resource_indicator_check(claims, self._expected_resource)
        if resource_error is not None:
            # Only the request path and configured expected_resource are logged.
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logger.warning(
                "SSO token rejected: resource indicator mismatch (path=%s, expected=%s)",
                sanitize_log(path),
                self._expected_resource,
            )
            return resource_error

        request.state.user = user  # type: ignore[attr-defined]
        request.state.auth_claims = claims  # type: ignore[attr-defined]

        permission = _get_required_permission(path, request.method)
        if permission and not user.has_permission(permission):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": f"Insufficient permissions. Required: {permission}",
                    "role": user.role.value,
                },
            )
        return True

    async def _try_agent_jwt(
        self,
        request: Request,
        call_next: Callable[[Request], Any],
        path: str,
        token: str,
    ) -> StarletteResponse | None:
        """Attempt agent identity JWT validation. Returns response or None on miss."""
        assert self._agent_identity_store is not None
        agent_identity = self._agent_identity_store.authenticate(token)
        if agent_identity is None:
            return None

        # RFC 8707: enforce the resource-indicator binding on the agent JWT
        # itself. ``authenticate`` has already verified the signature, so
        # decoding the unverified body here is safe - the body bytes have
        # already been authenticated against the JWT secret.
        if self._expected_resource:
            from bernstein.core.security.auth import decode_jwt_unverified

            agent_claims = decode_jwt_unverified(token) or {}
            resource_error = _resource_indicator_check(agent_claims, self._expected_resource)
            if resource_error is not None:
                logger.warning(
                    "Agent %s denied: resource indicator mismatch (path=%s, expected=%s)",
                    sanitize_log(agent_identity.id),
                    sanitize_log(path),
                    self._expected_resource,
                )
                return resource_error

        request.state.user = None  # type: ignore[attr-defined]
        request.state.auth_claims = {  # type: ignore[attr-defined]
            "agent": True,
            "agent_id": agent_identity.id,
            "role": agent_identity.role,
            "task_ids": agent_identity.task_ids,
        }
        request.state.agent_identity = agent_identity  # type: ignore[attr-defined]

        # Agent identity JWTs - even manager-role / unrestricted ones - must
        # never reach operator-only endpoints (shutdown, broadcast, drain,
        # config writer).  These mutate process-wide state and require an
        # admin SSO user or the legacy operator bearer.
        if request.method not in _READ_METHODS and _get_required_permission(path, request.method) == _PERM_ADMIN_MANAGE:
            logger.warning(
                "Agent %s denied operator-only path %s (admin:manage required)",
                sanitize_log(agent_identity.id),
                sanitize_log(path),
            )
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Agent tokens cannot access operator-only endpoints",
                    "required_permission": _PERM_ADMIN_MANAGE,
                    "agent_id": agent_identity.id,
                },
            )

        # Zero-trust: enforce task scope for mutating task operations.
        # Agents with a non-empty task_ids list may only act on their assigned
        # tasks.  Agents with task_ids=[] are unrestricted (manager role).
        if agent_identity.task_ids and request.method not in _READ_METHODS:
            app = request.scope.get("app")
            task_scope_error = _check_agent_task_scope(
                path,
                agent_identity.task_ids,
                task_id_route_patterns(app),
                task_collection_route_patterns(app),
                request.method,
            )
            if task_scope_error is not None:
                logger.warning(
                    "Agent %s denied task-scope access to %s: %s",
                    sanitize_log(agent_identity.id),
                    sanitize_log(path),
                    sanitize_log(task_scope_error),
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": task_scope_error,
                        "agent_id": agent_identity.id,
                    },
                )

        response: StarletteResponse = await call_next(request)
        return response


def _template_to_pattern(template: str, capture: str | None = None) -> re.Pattern[str]:
    """Compile a path template into an anchored matcher.

    Placeholders become wildcards: a ``path`` convertor may span ``/``
    (``{file_path:path}``), every other convertor matches one segment.  The
    placeholder named *capture*, if any, becomes a named group instead.

    Args:
        template: Route path template, e.g. ``/approvals/{task_id}/approve``.
        capture: Placeholder name to capture, or None to wildcard them all.

    Returns:
        A compiled anchored pattern.
    """
    parts: list[str] = []
    cursor = 0
    for match in _TEMPLATE_PARAM_RE.finditer(template):
        parts.append(re.escape(template[cursor : match.start()]))
        if capture is not None and match.group("name") == capture:
            parts.append(f"(?P<{capture}>[^/]+)")
        elif match.group("convertor") == "path":
            parts.append(".+")
        else:
            parts.append("[^/]+")
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


def _compile_task_id_route_pattern(template: str) -> re.Pattern[str] | None:
    """Compile one path template into a matcher capturing its ``task_id``.

    Args:
        template: Route path template, e.g. ``/approvals/{task_id}/approve``.

    Returns:
        A compiled anchored pattern with a ``task_id`` group, or None for a
        template that does not address a task by id.
    """
    if f"{{{_TASK_ID_TEMPLATE_PARAM}}}" not in template:
        return None
    return _template_to_pattern(template, _TASK_ID_TEMPLATE_PARAM)


def _route_templates(app: Any) -> list[str]:
    """Return the app's registered path templates, sorted."""
    templates: set[str] = set()
    for route in getattr(getattr(app, "router", None), "routes", ()) or ():
        template = getattr(route, "path", "")
        if template:
            templates.add(template)
    return sorted(templates)


def _build_task_id_route_patterns(app: Any) -> tuple[re.Pattern[str], ...]:
    """Compile a ``task_id``-capturing matcher per per-task route template."""
    compiled = (_compile_task_id_route_pattern(t) for t in _route_templates(app))
    return tuple(pattern for pattern in compiled if pattern is not None)


def _build_task_collection_route_patterns(app: Any) -> _CollectionRoutePatterns:
    """Compile a ``(matcher, methods)`` pair per ``/tasks/`` collection route.

    A collection route is one whose first segment under ``/tasks/`` is a
    literal listed in :data:`TASK_COLLECTION_SEGMENTS` rather than a task id,
    for example ``/tasks/batch-ops`` or ``/tasks/next/{role}``.

    Two things make the exemption route-resolved rather than text-matched.
    The matcher covers the WHOLE template, so ``/tasks/archive/cancel`` never
    matches ``/tasks/archive`` and stays gated even though ``archive`` is a
    collection segment.  The methods are carried alongside, because a
    template can match a path the router would dispatch elsewhere for a
    different method: ``POST /tasks/next/cancel`` matches the GET-only
    ``/tasks/next/{role}`` on path, yet the router sends it to
    ``POST /tasks/{task_id}/cancel`` with the id ``next``.  Matching the
    method too keeps that request gated.

    Args:
        app: The FastAPI/Starlette application serving the request.

    Returns:
        Pairs of anchored pattern and the methods that route accepts, in a
        deterministic order derived from the sorted route table.
    """
    methods_by_template: dict[str, set[str]] = {}
    for route in getattr(getattr(app, "router", None), "routes", ()) or ():
        template = getattr(route, "path", "")
        if not template:
            continue
        match = _TASK_COLLECTION_TEMPLATE_RE.match(template)
        if match is None or match.group("segment") not in TASK_COLLECTION_SEGMENTS:
            continue
        methods_by_template.setdefault(template, set()).update(getattr(route, "methods", None) or ())
    return tuple(
        (_template_to_pattern(template), frozenset(methods_by_template[template]))
        for template in sorted(methods_by_template)
    )


def _memoise_on_app_state[T](app: Any | None, attr: str, build: Callable[[Any], T]) -> T | tuple[()]:
    """Return ``build(app)``, memoised on ``app.state`` under *attr*.

    The route table is fixed once the app is built, so the compiled result is
    cached for the life of the app.

    Args:
        app: The FastAPI/Starlette application, or None when the ASGI scope
            carries none.
        attr: ``app.state`` attribute the result is memoised under.
        build: Builds the value from the app.

    Returns:
        The built value, or an empty tuple when there is no app.
    """
    if app is None:
        return ()
    state = getattr(app, "state", None)
    cached = getattr(state, attr, None) if state is not None else None
    if cached is not None:
        return cast("T", cached)
    value = build(app)
    if state is not None:
        setattr(state, attr, value)
    return value


def task_id_route_patterns(app: Any | None) -> tuple[re.Pattern[str], ...]:
    """Return a matcher per registered route that addresses a task by id.

    Derived from the app's own route table, so a per-task route registered
    under a prefix other than ``/tasks/`` is covered the moment it exists and
    this module never carries a list of prefixes to keep in step.

    Args:
        app: The FastAPI/Starlette application serving the request, or None
            when the scope carries none.

    Returns:
        Compiled patterns, each capturing a ``task_id`` group.  Empty when
        the app exposes no route table (``None``, or a bare ASGI callable
        mounted directly in a test).  The ``/tasks/`` surface is matched by
        the anchored pattern either way, so an empty result narrows the gate
        to that surface rather than opening it.
    """
    return _memoise_on_app_state(app, _TASK_ROUTE_PATTERNS_ATTR, _build_task_id_route_patterns)


def task_collection_route_patterns(app: Any | None) -> _CollectionRoutePatterns:
    """Return a ``(matcher, methods)`` pair per registered collection route.

    These name the only paths exempt from the ``/tasks/``-anchored gate.  The
    exemption is keyed on the route a path resolves to, not on the literal
    text of its first segment, so a task whose id happens to equal a
    collection segment name cannot borrow that segment's exemption.

    Args:
        app: The FastAPI/Starlette application serving the request, or None
            when the scope carries none.

    Returns:
        Pairs of anchored pattern and accepted methods.  Empty when the app
        exposes no route table, which exempts nothing and so keeps the gate
        at its most restrictive.
    """
    return _memoise_on_app_state(
        app,
        _TASK_COLLECTION_ROUTE_PATTERNS_ATTR,
        _build_task_collection_route_patterns,
    )


def _addressed_task_id(
    path: str,
    route_patterns: Sequence[re.Pattern[str]] = (),
    collection_patterns: _CollectionRoutePatterns = (),
    method: str | None = None,
) -> str | None:
    """Return the single task id this path addresses, or None.

    ``/tasks/``-anchored paths are resolved first and their answer is final:
    a path below ``/tasks/{id}/`` addresses a task whether or not a route is
    registered for it, so an unrouted probe cannot slip past the gate.
    Everything else is matched against the route-table-derived patterns.

    The exemption for collection routes is keyed on the route the path and
    method resolve to, not on the literal first segment.  Checking the
    segment text alone would hand a task whose id equals a collection segment
    name (``archive``, ``next``, ...) that segment's exemption, leaving
    ``/tasks/archive/cancel`` ungated while ``/tasks/<other>/cancel`` was
    denied.

    Args:
        path: Request URL path.
        route_patterns: Matchers from :func:`task_id_route_patterns`.
        collection_patterns: Matchers from
            :func:`task_collection_route_patterns`.
        method: Request method, matched against the methods each collection
            route accepts.  None matches any, for callers that only care
            about the path surface.

    Returns:
        The addressed task id, or None when the path addresses no single task.
    """
    anchored = _TASK_ID_PATH_RE.match(path)
    if anchored is not None:
        for pattern, methods in collection_patterns:
            if pattern.match(path) and (method is None or method in methods):
                # A registered collection route under /tasks/, not a task id.
                return None
        return anchored.group(_TASK_ID_TEMPLATE_PARAM)
    for pattern in route_patterns:
        match = pattern.match(path)
        if match is not None:
            return match.group(_TASK_ID_TEMPLATE_PARAM)
    return None


def _check_agent_task_scope(
    path: str,
    allowed_task_ids: list[str],
    route_patterns: Sequence[re.Pattern[str]] = (),
    collection_patterns: _CollectionRoutePatterns = (),
    method: str | None = None,
) -> str | None:
    """Return an error message if the request path is out of the agent's task scope.

    Returns None when the request is permitted.

    The caller (:meth:`SSOAuthMiddleware._try_agent_jwt`) invokes this only
    for mutating requests carrying a task-scoped agent identity, so reads and
    every non-agent credential are unaffected.  Within that population the
    rule is deny-by-default: any path addressing a single task by id is
    checked, whatever the action segment below it, on both the root mount and
    the ``/api/v<n>`` mirrors, and on every other registered per-task route.
    Paths that are not task-addressed (bulletin, status, ...) and the
    registered ``/tasks/`` collection routes are allowed.

    Args:
        path: Request URL path.
        allowed_task_ids: Task IDs the agent token is scoped to.
        route_patterns: Per-task route matchers for prefixes other than
            ``/tasks/``, from :func:`task_id_route_patterns`.  Defaults to
            empty so the ``/tasks/`` surface can be checked without an app.
        collection_patterns: Collection-route matchers from
            :func:`task_collection_route_patterns`.  Defaults to empty, which
            exempts nothing.
        method: Request method, matched against the methods each collection
            route accepts.  None matches any.

    Returns:
        Error message string if access should be denied, None otherwise.
    """
    task_id = _addressed_task_id(path, route_patterns, collection_patterns, method)
    if task_id is None:
        return None
    if task_id not in allowed_task_ids:
        return f"Task {task_id!r} is not in this agent's task scope (allowed: {allowed_task_ids})"
    return None


def check_agent_task_scope_ids(
    allowed_task_ids: Sequence[str],
    requested_task_ids: Iterable[str],
) -> str | None:
    """Return an error message if any requested task id is out of scope.

    The body-carried counterpart of :func:`_check_agent_task_scope`, applying
    the same rule to ids a caller names in a request body instead of in the
    path.  An empty ``allowed_task_ids`` means an unrestricted (manager)
    token and permits everything, exactly as the path-level gate does.

    Args:
        allowed_task_ids: Task IDs the agent token is scoped to.
        requested_task_ids: Task IDs the request is about to act on.

    Returns:
        Error message string if access should be denied, None otherwise.
    """
    if not allowed_task_ids:
        return None
    allowed = set(allowed_task_ids)
    out_of_scope = sorted({task_id for task_id in requested_task_ids if task_id not in allowed})
    if not out_of_scope:
        return None
    return f"Tasks {out_of_scope} are not in this agent's task scope (allowed: {list(allowed_task_ids)})"


def enforce_agent_task_scope_for_ids(request: Request, requested_task_ids: Iterable[str]) -> None:
    """Deny a task-scoped agent that reaches a task outside its scope.

    The handler-side half of the task-scope rule, for every id the path-level
    gate in this module cannot see:

    * ids the caller names in a request body - the collection routes under
      ``/tasks/`` listed in :data:`TASK_BODY_SCOPED_SEGMENTS`, and the
      per-task id ``POST /a2a/message`` carries;
    * ids the handler resolves from some other key - the Bernstein task
      behind an ACP run, the tasks a plan decision promotes or cancels, the
      tasks a cluster steal is about to reassign.

    Calling it binds the identity to the same tasks whichever route reaches
    them, so ``POST /tasks/B/cancel`` and every operation equivalent to it
    answer the same way for the same token.

    Pass the ids the handler will actually use, after any normalisation or
    lookup it applies - checking the raw input while acting on a rewritten or
    indirectly resolved value would let that step carry an id past the check.

    No-op for every credential that is not a task-scoped agent identity:
    operator bearer tokens, SSO users, the cluster secret, unscoped manager
    agent tokens (``task_ids == []``), and requests served with auth disabled
    never set (or never populate) ``request.state.agent_identity``.

    Args:
        request: The active request, carrying the resolved agent identity.
        requested_task_ids: Task IDs the handler is about to act on.

    Raises:
        HTTPException: 403 when any requested id is outside the agent's scope.
    """
    identity = getattr(request.state, "agent_identity", None)
    if identity is None:
        return
    error = check_agent_task_scope_ids(getattr(identity, "task_ids", None) or [], requested_task_ids)
    if error is None:
        return
    from fastapi import HTTPException

    logger.warning(
        "Agent %s denied task-scope access to %s: %s",
        sanitize_log(str(getattr(identity, "id", "unknown"))),
        sanitize_log(request.url.path),
        sanitize_log(error),
    )
    raise HTTPException(status_code=403, detail=error)
