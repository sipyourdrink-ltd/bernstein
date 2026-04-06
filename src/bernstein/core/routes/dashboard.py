"""Dashboard-specific routes — file lock inspection and session auth endpoints."""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.dashboard_auth import DashboardAuthMiddleware

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models for dashboard auth endpoints
# ---------------------------------------------------------------------------


class DashboardLoginRequest(BaseModel):
    """Body for POST /dashboard/auth/login."""

    password: str


class DashboardLoginResponse(BaseModel):
    """Response for POST /dashboard/auth/login."""

    authenticated: bool
    message: str
    token: str | None = None


class DashboardAuthStatusResponse(BaseModel):
    """Response for GET /dashboard/auth/status."""

    auth_required: bool
    authenticated: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _get_dashboard_middleware(request: Request) -> DashboardAuthMiddleware | None:
    """Retrieve the DashboardAuthMiddleware from app state if configured."""
    return getattr(request.app.state, "dashboard_auth_middleware", None)


# ---------------------------------------------------------------------------
# File lock endpoint
# ---------------------------------------------------------------------------


@router.get("/dashboard/file_locks")
def file_locks_endpoint(request: Request) -> JSONResponse:
    """Return active file locks grouped by agent for the dashboard.

    Reads the persisted lock state from ``.sdd/runtime/file_locks.json`` and
    returns it in a dashboard-friendly format with both a flat list and an
    agent-grouped view.

    Returns:
        JSON with ``all_locks`` (flat list sorted by path), ``locks_by_agent``
        (dict keyed by agent_id with files list + task info + elapsed_s),
        ``count`` (total lock count), and ``ts`` (generation timestamp).
    """
    runtime_dir = _runtime_dir(request)
    locks_path = runtime_dir / "file_locks.json"

    now = time.time()
    all_locks: list[dict[str, Any]] = []
    locks_by_agent: dict[str, dict[str, Any]] = {}

    if locks_path.exists():
        try:
            raw = json.loads(locks_path.read_text(encoding="utf-8"))
            for entry in raw:
                file_path = str(entry.get("file_path", ""))
                agent_id = str(entry.get("agent_id", ""))
                task_id = str(entry.get("task_id", ""))
                task_title = str(entry.get("task_title", ""))
                locked_at = float(entry.get("locked_at", 0))
                elapsed_s = int(now - locked_at) if locked_at > 0 else 0

                all_locks.append(
                    {
                        "file_path": file_path,
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "task_title": task_title,
                        "locked_at": locked_at,
                        "elapsed_s": elapsed_s,
                    }
                )

                if agent_id not in locks_by_agent:
                    locks_by_agent[agent_id] = {
                        "agent_id": agent_id,
                        "task_id": task_id,
                        "task_title": task_title,
                        "locked_at": locked_at,
                        "elapsed_s": elapsed_s,
                        "files": [],
                    }
                cast("list[str]", locks_by_agent[agent_id]["files"]).append(file_path)
        except (OSError, KeyError, ValueError):
            pass

    return JSONResponse(
        {
            "ts": now,
            "all_locks": sorted(all_locks, key=lambda x: str(x.get("file_path", ""))),
            "locks_by_agent": locks_by_agent,
            "count": len(all_locks),
        }
    )


# ---------------------------------------------------------------------------
# Dashboard auth routes
# ---------------------------------------------------------------------------


@router.post("/dashboard/auth/login", response_model=DashboardLoginResponse)
def dashboard_login(body: DashboardLoginRequest, request: Request) -> JSONResponse:
    """Authenticate and create a dashboard session.

    Returns a session token as both a Set-Cookie header and in the
    JSON response body for API consumers.
    """
    from bernstein.core.dashboard_auth import SESSION_COOKIE, verify_password

    middleware = _get_dashboard_middleware(request)
    if middleware is None or not middleware.password:
        # Check env var fallback
        env_pw = os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")
        if not env_pw:
            return JSONResponse(
                content={"authenticated": True, "message": "No authentication required", "token": None},
            )
        # Env var password is set but no middleware — this shouldn't happen
        # with normal app setup, but handle it gracefully
        return JSONResponse(
            content={"authenticated": True, "message": "No authentication required", "token": None},
        )

    effective_password = middleware.password or os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")
    if not verify_password(body.password, effective_password):
        return JSONResponse(
            status_code=401,
            content={"authenticated": False, "message": "Invalid password", "token": None},
        )

    token = middleware.session_store.create_session()
    response = JSONResponse(
        content={"authenticated": True, "message": "Login successful", "token": token},
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=3600,
        path="/",
    )
    return response


@router.post("/dashboard/auth/logout")
def dashboard_logout(request: Request) -> JSONResponse:
    """Revoke the current dashboard session."""
    from bernstein.core.dashboard_auth import SESSION_COOKIE

    middleware = _get_dashboard_middleware(request)
    if middleware is not None:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            middleware.session_store.revoke_session(token)

    response = JSONResponse(content={"message": "Logged out"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/dashboard/auth/status", response_model=DashboardAuthStatusResponse)
def dashboard_auth_status(request: Request) -> DashboardAuthStatusResponse:
    """Check whether dashboard auth is required and if the current session is valid."""
    from bernstein.core.dashboard_auth import SESSION_COOKIE

    middleware = _get_dashboard_middleware(request)

    if middleware is None:
        password = os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")
        return DashboardAuthStatusResponse(auth_required=bool(password), authenticated=False)

    effective_password = middleware.password or os.environ.get("BERNSTEIN_DASHBOARD_PASSWORD", "")
    auth_required = bool(effective_password)

    if not auth_required:
        return DashboardAuthStatusResponse(auth_required=False, authenticated=True)

    token = request.cookies.get(SESSION_COOKIE, "")
    authenticated = bool(token and middleware.session_store.validate_session(token))

    return DashboardAuthStatusResponse(auth_required=auth_required, authenticated=authenticated)
