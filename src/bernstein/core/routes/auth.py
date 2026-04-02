"""Authentication routes for SSO / SAML / OIDC flows.

Provides endpoints for:
- OIDC authorization code flow (web dashboard)
- SAML 2.0 SP-initiated SSO (enterprise IdPs)
- Device authorization flow (CLI authentication)
- Session management (profile, logout)
- Auth provider discovery
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from bernstein.core.auth_rate_limiter import check_auth_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth",
    tags=["authentication"],
    dependencies=[Depends(check_auth_rate_limit)],
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AuthProvidersResponse(BaseModel):
    """Available authentication providers."""

    oidc_enabled: bool = False
    saml_enabled: bool = False
    legacy_token_enabled: bool = False
    device_flow_enabled: bool = True


class DeviceCodeRequest(BaseModel):
    """Body for POST /auth/cli/device — initiate device auth flow."""

    client_name: str = "bernstein-cli"


class DeviceCodeResponse(BaseModel):
    """Response for device code request."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DevicePollRequest(BaseModel):
    """Body for POST /auth/cli/token — poll for device authorization."""

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
    """Body for POST /auth/cli/authorize — authorize a device code."""

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


def _get_auth_service(request: Request) -> Any:
    """Get the AuthService from app state."""
    svc = getattr(request.app.state, "auth_service", None)
    if svc is None:
        raise HTTPException(status_code=503, detail="SSO authentication is not configured")
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


@router.get("/providers", response_model=AuthProvidersResponse)
async def auth_providers(request: Request) -> AuthProvidersResponse:
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


@router.get("/login")
async def login(request: Request, provider: str = "oidc") -> Response:
    """Initiate SSO login. Redirects to IdP."""
    svc = _get_auth_service(request)

    if provider == "oidc" and svc.config.oidc.enabled:
        state = secrets.token_urlsafe(32)
        # Store state for CSRF validation (in-memory is fine for this)
        if not hasattr(request.app.state, "_oidc_states"):
            request.app.state._oidc_states = {}  # type: ignore[attr-defined]
        request.app.state._oidc_states[state] = time.time()  # type: ignore[attr-defined]

        discovery = await svc.oidc_discover()
        auth_url = svc.get_oidc_auth_url(state=state, discovery=discovery)
        return RedirectResponse(url=auth_url, status_code=302)

    if provider == "saml" and svc.config.saml.enabled:
        relay_state = secrets.token_urlsafe(16)
        redirect_url = svc.get_saml_auth_redirect_url(relay_state=relay_state)
        return RedirectResponse(url=redirect_url, status_code=302)

    raise HTTPException(
        status_code=400,
        detail=f"Authentication provider '{provider}' is not enabled",
    )


@router.get("/oidc/callback")
async def oidc_callback(request: Request) -> Response:
    """OIDC authorization code callback."""
    svc = _get_auth_service(request)

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    error = request.query_params.get("error", "")

    if error:
        error_desc = request.query_params.get("error_description", error)
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
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><title>Login Successful</title></head>
<body>
<h2>Welcome, {user.display_name}!</h2>
<p>Role: {user.role.value} | Redirecting to dashboard...</p>
<script>
localStorage.setItem('bernstein_token', '{token}');
window.location.href = '/dashboard';
</script>
</body></html>"""
    )


# ---------------------------------------------------------------------------
# SAML flow
# ---------------------------------------------------------------------------


@router.post("/saml/acs")
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
    return HTMLResponse(
        content=f"""<!DOCTYPE html>
<html><head><title>Login Successful</title></head>
<body>
<h2>Welcome, {user.display_name}!</h2>
<p>Role: {user.role.value} | Redirecting to dashboard...</p>
<script>
localStorage.setItem('bernstein_token', '{token}');
window.location.href = '/dashboard';
</script>
</body></html>"""
    )


@router.get("/saml/metadata")
async def saml_metadata(request: Request) -> Response:
    """SAML SP metadata endpoint for IdP configuration."""
    svc = _get_auth_service(request)
    metadata = svc.get_saml_sp_metadata()
    return Response(content=metadata, media_type="application/xml")


# ---------------------------------------------------------------------------
# Device authorization flow (for CLI)
# ---------------------------------------------------------------------------


@router.post("/cli/device", response_model=DeviceCodeResponse)
async def device_code_request(request: Request, body: DeviceCodeRequest) -> DeviceCodeResponse:
    """Initiate device authorization flow for CLI login.

    The CLI calls this to get a device_code and user_code.
    The user enters the user_code in the web dashboard after SSO login
    to authorize the CLI session.
    """
    svc = _get_auth_service(request)
    req = svc.create_device_request()

    server_url = str(request.base_url).rstrip("/")
    return DeviceCodeResponse(
        device_code=req.device_code,
        user_code=req.user_code,
        verification_uri=f"{server_url}/auth/login",
        expires_in=int(req.expires_at - req.created_at),
        interval=req.poll_interval_s,
    )


@router.post("/cli/token", response_model=DevicePollResponse)
async def device_token_poll(request: Request, body: DevicePollRequest) -> DevicePollResponse:
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


@router.post("/cli/authorize")
async def device_authorize(request: Request, body: DeviceAuthorizeRequest) -> JSONResponse:
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


@router.get("/me", response_model=UserProfileResponse)
async def get_profile(request: Request) -> UserProfileResponse:
    """Get the current authenticated user's profile."""
    user = _get_current_user(request)
    from bernstein.core.auth import _ROLE_PERMISSIONS

    permissions = list(_ROLE_PERMISSIONS.get(user.role, frozenset()))

    return UserProfileResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        sso_provider=user.sso_provider,
        sso_groups=user.sso_groups,
        permissions=sorted(permissions),
    )


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Logout and revoke the current session."""
    claims = getattr(request.state, "auth_claims", {})
    session_id = claims.get("session_id", "")

    if not session_id:
        return JSONResponse(content={"status": "ok", "detail": "No active session"})

    svc = _get_auth_service(request)
    svc.logout(session_id)
    return JSONResponse(content={"status": "ok", "detail": "Session revoked"})


# ---------------------------------------------------------------------------
# Group mappings management (admin only)
# ---------------------------------------------------------------------------


@router.get("/group-mappings", response_model=GroupMappingsResponse)
async def get_group_mappings(request: Request) -> GroupMappingsResponse:
    """Get current SSO group → role mappings."""
    svc = _get_auth_service(request)
    mappings = svc.group_role_map
    return GroupMappingsResponse(mappings=[GroupMappingEntry(group=g, role=r.value) for g, r in mappings.items()])


@router.put("/group-mappings")
async def update_group_mappings(request: Request, body: GroupMappingsUpdateRequest) -> JSONResponse:
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


@router.get("/users")
async def list_users(request: Request) -> JSONResponse:
    """List all users (admin only)."""
    user = _get_current_user(request)
    if not user.has_permission("auth:manage"):
        raise HTTPException(status_code=403, detail="Admin role required")

    svc = _get_auth_service(request)
    users = svc.store.list_users()
    return JSONResponse(
        content={
            "users": [u.to_dict() for u in users],
            "total": len(users),
        }
    )
