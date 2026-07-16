"""Dashboard-specific routes - file locks and the auth endpoints (#2366)."""

from __future__ import annotations

import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.server.dashboard_auth import (
    ANONYMOUS_PRINCIPAL,
    PASSWORD_PRINCIPAL,
    SESSION_COOKIE,
    DashboardAuthState,
    verify_password,
)
from bernstein.core.server.dashboard_tokens import ACTION_LOGIN, SCOPE_OPERATOR

if TYPE_CHECKING:
    from pathlib import Path

router = APIRouter()


def _runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _cookie_secure(request: Request) -> bool:
    """Whether the session cookie should carry the ``Secure`` flag.

    ``Secure`` is set whenever the request arrived over TLS so the session
    cookie is never returned in clear text on an HTTPS deployment. A plain
    HTTP bind (the common loopback dev case) keeps working because a
    ``Secure`` cookie would never be sent back over HTTP; the forwarded-proto
    header lets the flag follow through a TLS-terminating proxy.
    """
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",", 1)[0].strip().lower() == "https"


def _auth_state(request: Request) -> DashboardAuthState:
    """Fetch the shared dashboard auth state (created in ``create_app``)."""
    state = getattr(request.app.state, "dashboard_auth_state", None)
    if isinstance(state, DashboardAuthState):
        return state
    # Minimal embeddings without create_app wiring: an empty state means
    # auth is not configured and the endpoints degrade gracefully.
    return DashboardAuthState()


async def _request_json(request: Request) -> dict[str, Any]:
    """Parse the request body as a JSON object, tolerating garbage."""
    try:
        parsed: object = await request.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.get("/dashboard/auth/status")
async def dashboard_auth_status(request: Request) -> JSONResponse:
    """Report whether dashboard auth is required and who is logged in."""
    state = _auth_state(request)
    required = state.auth_required()
    credential = state.resolve_credential(request) if required else None
    return JSONResponse(
        {
            "auth_required": required,
            "authenticated": (credential is not None) or not required,
            "principal": credential[0] if credential else None,
            "scope": credential[1] if credential else None,
        }
    )


@router.post("/dashboard/auth/login")
async def dashboard_auth_login(request: Request) -> JSONResponse:
    """Open a dashboard session from a password or a scoped token.

    The session cookie wraps exactly the principal and scope the credential
    carried; a viewer token can never log into an operator session. Every
    attempt -- success or failure -- is journaled as a signed governance
    decision (``dashboard.login``).
    """
    state = _auth_state(request)
    if not state.auth_required():
        return JSONResponse({"authenticated": True, "token": None, "principal": None, "scope": None})

    body = await _request_json(request)
    principal = ""
    scope = ""

    raw_token = body.get("token")
    if isinstance(raw_token, str) and raw_token and state.token_registry is not None:
        record = state.token_registry.validate(raw_token)
        if record is not None:
            principal, scope = record.principal, record.scope

    provided_password = body.get("password")
    if (
        not scope
        and isinstance(provided_password, str)
        and provided_password
        and verify_password(provided_password, state.effective_password())
    ):
        principal, scope = PASSWORD_PRINCIPAL, SCOPE_OPERATOR

    verdict = state.record_decision(
        subject=principal or ANONYMOUS_PRINCIPAL,
        scope=scope,
        action=ACTION_LOGIN,
    )
    if verdict != "allow":
        return JSONResponse(status_code=401, content={"detail": "Invalid dashboard credentials"})

    session_token = state.session_store.create_session(principal=principal, scope=scope)
    response = JSONResponse(
        {
            "authenticated": True,
            "token": session_token,
            "principal": principal,
            "scope": scope,
        }
    )
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
    )
    return response


@router.post("/dashboard/auth/logout")
async def dashboard_auth_logout(request: Request) -> JSONResponse:
    """Close the current dashboard session (idempotent)."""
    state = _auth_state(request)
    cookie_token = request.cookies.get(SESSION_COOKIE, "")
    if cookie_token:
        state.session_store.revoke_session(cookie_token)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        state.session_store.revoke_session(auth_header[7:])
    response = JSONResponse({"message": "Logged out"})
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="lax", secure=_cookie_secure(request))
    return response


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
        with suppress(OSError, KeyError, ValueError):
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

    return JSONResponse(
        {
            "ts": now,
            "all_locks": sorted(all_locks, key=lambda x: str(x.get("file_path", ""))),
            "locks_by_agent": locks_by_agent,
            "count": len(all_locks),
        }
    )
