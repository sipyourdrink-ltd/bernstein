"""Hook receiver routes for Claude Code hook events.

Receives HTTP POST callbacks from Claude Code's built-in hook system
(PostToolUse, Stop, PreCompact, SubagentStart, SubagentStop).  The
adapter injects these hooks into ``.claude/settings.local.json`` before
spawning so every tool invocation and lifecycle event is reported in
real time.

Authentication (audit-113)
--------------------------
The endpoint is mounted outside the bearer-auth gate because Claude Code's
hook runner cannot carry Bearer tokens.  Instead, each request is signed
with HMAC-SHA256 using the shared secret configured via
``BERNSTEIN_HOOK_SECRET`` (or ``BERNSTEIN_AUTH_TOKEN`` as a fallback).
Requests without a matching ``X-Bernstein-Hook-Signature-256`` header are
rejected with 401.

Security (audit-114)
--------------------
``session_id`` is strictly validated before any filesystem work.  Values
containing path separators, ``..``, null bytes, or anything outside
``^[A-Za-z0-9_-]{1,128}$`` are rejected with 400.  This blocks URL-encoded
traversal such as ``/hooks/..%2F..%2Fruntime%2Fsignals%2FSHUTDOWN`` from
forging completion markers or clobbering runtime state.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.hooks_receiver import (
    InvalidSessionIdError,
    parse_hook_event,
    process_hook_event,
    validate_session_id,
)
from bernstein.core.server.webhook_signatures import verify_hmac_sha256

router = APIRouter()
logger = logging.getLogger(__name__)

_HOOK_SECRET_ENV = "BERNSTEIN_HOOK_SECRET"
_HOOK_SECRET_FALLBACK_ENV = "BERNSTEIN_AUTH_TOKEN"
_HOOK_SIGNATURE_HEADER = "x-bernstein-hook-signature-256"


def _resolve_hook_secret() -> str:
    """Return the shared HMAC secret for hook requests.

    Prefers ``BERNSTEIN_HOOK_SECRET``; falls back to ``BERNSTEIN_AUTH_TOKEN``
    so existing deployments keep working without extra configuration.
    """
    secret = os.environ.get(_HOOK_SECRET_ENV, "").strip()
    if secret:
        return secret
    return os.environ.get(_HOOK_SECRET_FALLBACK_ENV, "").strip()


def _verify_hook_signature(request: Request, body: bytes) -> JSONResponse | None:
    """Validate the hook HMAC signature.

    Returns a 401 JSONResponse when the secret is missing or when the
    request carries no (or an invalid) signature.  Returns ``None`` when
    validation passes.

    When no secret is configured the endpoint is *disabled* (returns 401) —
    hooks cannot be authenticated and must not be trusted by default.
    Operators who deliberately run without auth set
    ``BERNSTEIN_AUTH_DISABLED=1`` at the app level; in that mode the SSO
    middleware is bypassed and this helper is not invoked because the
    caller never reaches it.
    """
    secret = _resolve_hook_secret()
    if not secret:
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    "Hook endpoint is not configured: set "
                    "BERNSTEIN_HOOK_SECRET (or BERNSTEIN_AUTH_TOKEN) "
                    "to the shared secret used by the Claude Code hook "
                    "runner."
                ),
            },
        )
    provided = request.headers.get(_HOOK_SIGNATURE_HEADER, "")
    if not provided or not verify_hmac_sha256(body, provided, secret, prefix="sha256="):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing hook signature"},
        )
    return None


def _get_workdir(request: Request) -> Path:
    """Return the repository root from application state."""
    workdir = getattr(request.app.state, "workdir", None)
    if isinstance(workdir, Path):
        return workdir
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if isinstance(sdd_dir, Path) and sdd_dir.name == ".sdd":
        return sdd_dir.parent
    return Path.cwd()


def _reject(detail: str, status_code: int = 400) -> JSONResponse:
    """Return a 400-style rejection with a consistent error envelope."""
    return JSONResponse(
        content={"status": "error", "detail": detail},
        status_code=status_code,
    )


@router.post("/hooks/{session_id}")
async def receive_hook(session_id: str, request: Request) -> JSONResponse:
    """Receive a hook event from Claude Code.

    Claude Code sends structured JSON with at minimum a ``hook_event_name``
    field.  The event is parsed, persisted to a JSONL sidecar, and triggers
    side effects (heartbeat touch, completion markers, etc.).

    The request body is verified against
    ``X-Bernstein-Hook-Signature-256`` (HMAC-SHA256 over the raw body,
    keyed with ``BERNSTEIN_HOOK_SECRET``) *before* any parsing or
    filesystem work — this is the authentication boundary for the
    endpoint (audit-113).  The ``session_id`` is then validated against
    a strict allowlist to prevent path traversal (audit-114).

    Args:
        session_id: Agent session identifier from the URL path.
        request: The incoming FastAPI request.

    Returns:
        JSON response with status and action taken, 401 if signature
        verification fails, or 400 if ``session_id`` is unsafe / body
        is not valid JSON.
    """
    raw_body = await request.body()
    denied = _verify_hook_signature(request, raw_body)
    if denied is not None:
        return denied

    try:
        validate_session_id(session_id)
    except InvalidSessionIdError as exc:
        logger.warning(
            "Rejected hook POST with unsafe session_id: %s",
            exc,
        )
        return _reject("invalid session_id")

    try:
        body: dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reject("invalid JSON body")

    workdir = _get_workdir(request)
    try:
        event = parse_hook_event(session_id, body)
        result = process_hook_event(event, workdir)
    except InvalidSessionIdError as exc:
        # Defense in depth — primary validation above should have
        # already caught this, but we re-map any downstream rejection
        # from the receiver to a 400 rather than a 500.
        logger.warning("Hook receiver rejected session_id downstream: %s", exc)
        return _reject("invalid session_id")
    return JSONResponse(content=result, status_code=200)
