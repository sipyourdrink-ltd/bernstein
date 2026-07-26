"""Authentication routes for SSO / SAML / OIDC flows.

Provides endpoints for:
- OIDC authorization code flow (web dashboard)
- SAML 2.0 SP-initiated SSO (enterprise IdPs)
- Device authorization flow (CLI authentication)
- Session management (profile, logout)
- Auth provider discovery
"""

from __future__ import annotations

import html
import json
import logging
import secrets
import time
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class LoginProvider(StrEnum):
    """SSO providers accepted by ``GET /auth/login``.

    Typing the ``provider`` query param with this enum lets FastAPI
    reject unknown values with a ``422`` at the validation layer instead
    of the handler falling through to a generic error for an input it
    was never going to support.
    """

    OIDC = "oidc"
    SAML = "saml"


class AuthProvidersResponse(BaseModel):
    """Available authentication providers."""

    oidc_enabled: bool = False
    saml_enabled: bool = False
    legacy_token_enabled: bool = False
    device_flow_enabled: bool = True


class DeviceCodeRequest(BaseModel):
    """Body for POST /auth/cli/device - initiate device auth flow."""

    client_name: str = "bernstein-cli"


class DeviceCodeResponse(BaseModel):
    """Response for device code request."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollRequest(BaseModel):
    """Body for POST /auth/cli/token - poll for device authorization."""

    device_code: str
    grant_type: str = "urn:ietf:params:oauth:grant-type:device_code"


class DevicePollResponse(BaseModel):
    """Response for device token poll."""

    access_token: str = ""
    expires_at: float | None = None
    refresh_token: str | None = None
    token_type: str = "Bearer"
    status: str = "pending"  # "pending", "complete", "expired"


class DeviceAuthorizeRequest(BaseModel):
    """Body for POST /auth/cli/authorize - authorize a device code."""

    user_code: str


class UserProfileResponse(BaseModel):
    """Response for GET /auth/me."""

    id: str
    email: str
    display_name: str
    role: str
    sso_provider: str
    sso_groups: list[str]
    permissions: list[str]


class GroupMappingEntry(BaseModel):
    """A single group → role mapping."""

    group: str
    role: str


class GroupMappingsResponse(BaseModel):
    """Response for GET /auth/group-mappings."""

    mappings: list[GroupMappingEntry]


class GroupMappingsUpdateRequest(BaseModel):
    """Body for PUT /auth/group-mappings."""

    mappings: list[GroupMappingEntry]


class LogoutRequest(BaseModel):
    """Body for POST /auth/logout."""

    pass  # Session ID extracted from JWT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Answer for every SSO route when no provider is configured on this
#: deployment. Deliberately 4xx, not 5xx: nothing on the server failed,
#: and the condition is permanent until an operator changes the config.
#: A 5xx would tell the client to retry a request that can never start
#: succeeding on its own, and this repo's own HTTP retry policy treats
#: 503 as retryable. 404 says what is true: this deployment serves no
#: such resource.
SSO_NOT_CONFIGURED_STATUS = 404
SSO_NOT_CONFIGURED_DETAIL = "SSO authentication is not configured on this server"


def _get_auth_service(request: Request) -> Any:
    """Get the AuthService from app state."""
    svc = getattr(request.app.state, "auth_service", None)
    if svc is None:
        raise HTTPException(status_code=SSO_NOT_CONFIGURED_STATUS, detail=SSO_NOT_CONFIGURED_DETAIL)
    return svc


def _get_current_user(request: Request) -> Any:
    """Get the authenticated user from request state (set by middleware)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


# ---------------------------------------------------------------------------
# Provider discovery
# ---------------------------------------------------------------------------


@router.get("/providers")
def auth_providers(request: Request) -> AuthProvidersResponse:
    """List available authentication providers."""
    svc = getattr(request.app.state, "auth_service", None)
    legacy = getattr(request.app.state, "legacy_auth_token", None)

    oidc_enabled = False
    saml_enabled = False
    if svc is not None:
        oidc_enabled = svc.config.oidc.enabled
        saml_enabled = svc.config.saml.enabled

    return AuthProvidersResponse(
        oidc_enabled=oidc_enabled,
        saml_enabled=saml_enabled,
        legacy_token_enabled=bool(legacy),
        device_flow_enabled=svc is not None,
    )


# ---------------------------------------------------------------------------
# OIDC flow
# ---------------------------------------------------------------------------


@router.get(
    "/login",
    responses={
        400: {"description": "Authentication provider not enabled"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
async def login(request: Request, provider: LoginProvider = LoginProvider.OIDC) -> Response:
    """Initiate SSO login. Redirects to IdP."""
    svc = _get_auth_service(request)

    if provider == LoginProvider.OIDC and svc.config.oidc.enabled:
        state = secrets.token_urlsafe(32)
        # Store state for CSRF validation (in-memory is fine for this)
        if not hasattr(request.app.state, "_oidc_states"):
            request.app.state._oidc_states = {}  # type: ignore[attr-defined]
        request.app.state._oidc_states[state] = time.time()  # type: ignore[attr-defined]

        discovery = await svc.oidc_discover()
        auth_url = svc.get_oidc_auth_url(state=state, discovery=discovery)
        return RedirectResponse(url=auth_url, status_code=302)

    if provider == LoginProvider.SAML and svc.config.saml.enabled:
        relay_state = secrets.token_urlsafe(16)
        redirect_url = svc.get_saml_auth_redirect_url(relay_state=relay_state)
        return RedirectResponse(url=redirect_url, status_code=302)

    raise HTTPException(
        status_code=400,
        detail=f"Authentication provider '{provider.value}' is not enabled",
    )


@router.get(
    "/oidc/callback",
    responses={
        400: {"description": "Missing or invalid authorization code or state"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
async def oidc_callback(request: Request) -> Response:
    """OIDC authorization code callback."""
    svc = _get_auth_service(request)

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    if error:
        error_desc = html.escape(request.query_params.get("error_description", error))
        return HTMLResponse(
            content=f'<h2>Authentication Failed</h2><p>{error_desc}</p><p><a href="/auth/login">Try again</a></p>',
            status_code=400,
        )

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    # Validate state (CSRF protection)
    oidc_states: dict[str, float] = getattr(request.app.state, "_oidc_states", {})
    if state not in oidc_states:
        raise HTTPException(status_code=400, detail="Invalid state parameter")
    del oidc_states[state]

    # Exchange code for tokens
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    result = await svc.handle_oidc_callback(code, ip_address=ip, user_agent=ua)
    if result is None:
        return HTMLResponse(
            content="<h2>Authentication Failed</h2>"
            "<p>Could not complete OIDC authentication.</p>"
            '<p><a href="/auth/login">Try again</a></p>',
            status_code=401,
        )

    user, token = result
    # Return a page that stores the token and redirects to dashboard
    return _login_success_page(user.display_name, user.role.value, token)


def _login_success_page(display_name: str, role: str, token: str) -> HTMLResponse:
    """Render the post-login page that stashes the session token.

    ``display_name`` and ``role`` originate from IdP-supplied claims, so
    they are HTML-escaped before being placed in element text. ``token``
    is embedded inside a JavaScript string literal inside a ``<script>``
    block, so it is JSON-encoded (a safe, fully quoted JS string) and the
    HTML-significant characters ``<``, ``>`` and ``&`` are then escaped to
    ``\\uXXXX`` so a value containing ``</script>`` cannot break out of
    the script element.
    """
    safe_name = html.escape(display_name)
    safe_role = html.escape(role)
    js_token = json.dumps(token).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><title>Login Successful</title></head>
<body>
<h2>Welcome, {safe_name}!</h2>
<p>Role: {safe_role} | Redirecting to dashboard...</p>
<script>
localStorage.setItem('bernstein_token', {js_token});
window.location.href = '/dashboard';
</script>
</body></html>"""
    )


# ---------------------------------------------------------------------------
# SAML flow
# ---------------------------------------------------------------------------


@router.post(
    "/saml/acs",
    responses={
        400: {"description": "Missing SAMLResponse"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
async def saml_acs(request: Request) -> Response:
    """SAML Assertion Consumer Service (ACS) endpoint.

    Receives the SAML Response from the IdP via HTTP-POST binding.
    """
    svc = _get_auth_service(request)

    form = await request.form()
    saml_response = str(form.get("SAMLResponse", ""))
    if not saml_response:
        raise HTTPException(status_code=400, detail="Missing SAMLResponse")

    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")
    result = svc.handle_saml_response(saml_response, ip_address=ip, user_agent=ua)
    if result is None:
        return HTMLResponse(
            content="<h2>SAML Authentication Failed</h2>"
            "<p>Could not validate SAML assertion.</p>"
            '<p><a href="/auth/login?provider=saml">Try again</a></p>',
            status_code=401,
        )

    user, token = result
    return _login_success_page(user.display_name, user.role.value, token)


@router.get("/saml/metadata", responses={404: {"description": "SSO authentication is not configured on this server"}})
def saml_metadata(request: Request) -> Response:
    """SAML SP metadata endpoint for IdP configuration."""
    metadata = _get_auth_service(request).get_saml_sp_metadata()
    return Response(content=metadata, media_type="application/xml")


# ---------------------------------------------------------------------------
# Device authorization flow (for CLI)
# ---------------------------------------------------------------------------


@router.post(
    "/cli/device",
    responses={404: {"description": "SSO authentication is not configured on this server"}},
)
def device_code_request(request: Request, body: DeviceCodeRequest) -> DeviceCodeResponse:
    """Initiate device authorization flow for CLI login.

    The CLI calls this to get a device_code and user_code.
    The user enters the user_code in the web dashboard after SSO login
    to authorize the CLI session.
    """
    req = _get_auth_service(request).create_device_request()

    server_url = str(request.base_url).rstrip("/")
    return DeviceCodeResponse(
        device_code=req.device_code,
        user_code=req.user_code,
        verification_uri=f"{server_url}/auth/login",
        expires_in=int(req.expires_at - req.created_at),
        interval=req.poll_interval_s,
    )


@router.post(
    "/cli/token",
    responses={404: {"description": "SSO authentication is not configured on this server"}},
)
def device_token_poll(request: Request, body: DevicePollRequest) -> DevicePollResponse:
    """Poll for device authorization status.

    Returns the access token once the user has authorized the device code.
    """
    svc = _get_auth_service(request)
    result = svc.poll_device_token(body.device_code)

    if result is None:
        # Check if it's expired vs still pending
        req = svc.store.get_device_request(body.device_code)
        if req is None or time.time() > req.expires_at:
            return DevicePollResponse(status="expired")
        return DevicePollResponse(status="pending")

    token, status = result
    return DevicePollResponse(
        access_token=token,
        expires_at=time.time() + svc.config.jwt_expiry_seconds,
        status=status,
    )


@router.post(
    "/cli/authorize",
    responses={
        400: {"description": "Invalid or expired user code"},
        401: {"description": "Authentication required"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
def device_authorize(request: Request, body: DeviceAuthorizeRequest) -> JSONResponse:
    """Authorize a device code (called from web dashboard after SSO login).

    Requires an authenticated user session.
    """
    svc = _get_auth_service(request)
    user = _get_current_user(request)

    if svc.authorize_device(body.user_code, user):
        return JSONResponse(content={"status": "authorized", "user_code": body.user_code})

    raise HTTPException(
        status_code=400,
        detail="Invalid or expired user code",
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@router.get("/me", responses={401: {"description": "Authentication required"}})
def get_profile(request: Request) -> UserProfileResponse:
    """Get the current authenticated user's profile."""
    user = _get_current_user(request)
    from bernstein.core.auth import _ROLE_PERMISSIONS

    permissions = _ROLE_PERMISSIONS.get(user.role, frozenset())

    return UserProfileResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        sso_provider=user.sso_provider,
        sso_groups=user.sso_groups,
        permissions=sorted(permissions),
    )


@router.post("/logout", responses={404: {"description": "SSO authentication is not configured on this server"}})
def logout(request: Request) -> JSONResponse:
    """Logout and revoke the current session."""
    session_id = getattr(request.state, "auth_claims", {}).get("session_id", "")

    if not session_id:
        return JSONResponse(content={"status": "ok", "detail": "No active session"})

    _get_auth_service(request).logout(session_id)
    return JSONResponse(content={"status": "ok", "detail": "Session revoked"})


# ---------------------------------------------------------------------------
# Group mappings management (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/group-mappings",
    responses={404: {"description": "SSO authentication is not configured on this server"}},
)
def get_group_mappings(request: Request) -> GroupMappingsResponse:
    """Get current SSO group → role mappings."""
    mappings = _get_auth_service(request).group_role_map
    return GroupMappingsResponse(mappings=[GroupMappingEntry(group=g, role=r.value) for g, r in mappings.items()])


@router.put(
    "/group-mappings",
    responses={
        400: {"description": "Invalid role value"},
        401: {"description": "Authentication required"},
        403: {"description": "Admin role required"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
def update_group_mappings(request: Request, body: GroupMappingsUpdateRequest) -> JSONResponse:
    """Update SSO group → role mappings (admin only)."""
    user = _get_current_user(request)
    if not user.has_permission("auth:manage"):
        raise HTTPException(status_code=403, detail="Admin role required")

    svc = _get_auth_service(request)
    from bernstein.core.auth import AuthRole

    mapping_dict = {}
    for entry in body.mappings:
        try:
            role = AuthRole(entry.role)
            mapping_dict[entry.group] = role
        except ValueError:
            raise HTTPException(  # noqa: B904
                status_code=400,
                detail=f"Invalid role: {entry.role}. Must be admin, operator, or viewer",
            )

    svc.store.save_group_mappings(mapping_dict)
    svc._load_group_mappings()  # Reload

    return JSONResponse(
        content={
            "status": "ok",
            "mappings_count": len(mapping_dict),
        }
    )


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    responses={
        401: {"description": "Authentication required"},
        403: {"description": "Admin role required"},
        404: {"description": "SSO authentication is not configured on this server"},
    },
)
def list_users(request: Request) -> JSONResponse:
    """List all users (admin only)."""
    user = _get_current_user(request)
    if not user.has_permission("auth:manage"):
        raise HTTPException(status_code=403, detail="Admin role required")

    users = _get_auth_service(request).store.list_users()
    return JSONResponse(
        content={
            "users": [u.to_dict() for u in users],
            "total": len(users),
        }
    )
