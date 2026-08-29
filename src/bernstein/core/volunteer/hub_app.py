"""FastAPI web view for the volunteer hub lease store.

The hub exposes HTTP endpoints that donor workers reach over the network to
enroll, claim, heartbeat, submit, and release tasks.  Unlike the orchestrator's
own API surface, this is a small, standalone FastAPI app with its own auth
scope and trust boundary: a volunteer worker is self-enrolled, untrusted by
default, and approved individually by the operator.

The shape mirrors :mod:`bernstein.core.fleet.web` —
:func:`build_fleet_app` established the "small standalone FastAPI app served by
its own CLI subcommand" pattern — but the auth story is deliberately different.
Cluster scopes (``SCOPE_NODE_REGISTER``, ``SCOPE_NODE_HEARTBEAT``,
``SCOPE_NODE_ADMIN`` from :mod:`bernstein.core.protocols.cluster.cluster_auth`)
authorize operations for nodes the *operator* registered; there is no approval
step, registration itself is trusted.  A volunteer worker is the opposite:
self-enrolled, untrusted by default, approved individually.  Reusing
``SCOPE_NODE_ADMIN`` for "approve a worker" would conflate "this caller
administers my cluster" with "this caller may vet strangers," which is a wider
grant than the action needs.  This module therefore defines a parallel set of
scopes:

* ``SCOPE_VOLUNTEER_ENROLL`` — operator approval of a new worker.
* ``SCOPE_VOLUNTEER_CLAIM`` — claiming a task.
* ``SCOPE_VOLUNTEER_HEARTBEAT`` — heartbeating a held task.
* ``SCOPE_VOLUNTEER_SUBMIT`` — submitting a result.
* ``SCOPE_VOLUNTEER_RELEASE`` — releasing a lease.

Endpoints
---------

* ``POST /volunteer/enroll`` — body carries an Ed25519 public key; returns a
  worker id.  The enrollment record starts ``pending`` and a separate,
  operator-only endpoint or CLI flips it to ``approved``.
* ``POST /volunteer/tasks/{task_id}/claim``
* ``POST /volunteer/tasks/{task_id}/heartbeat``
* ``POST /volunteer/tasks/{task_id}/submit``
* ``POST /volunteer/tasks/{task_id}/release``

Each handler verifies a scoped bearer token, calls the matching
:class:`~bernstein.core.volunteer.lease_store.LeaseStore` method, and translates
its refusals to HTTP status codes:

* ``401`` — unknown worker / bad auth
* ``403`` — not the lease holder
* ``404`` — unknown task / no lease
* ``409`` — already leased / already submitted

A standalone app, not a router mounted into ``core.server:app``.
``core.server:app`` is the orchestrator's full API surface — cluster nodes,
tasks, A2A, MCP gateway, the works — and is not something a maintainer with no
interest in running an orchestrator should have to stand up to receive volunteer
submissions.
"""

from __future__ import annotations

import logging
import math

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from bernstein.adapters.capability_profile import UnknownProfileError, get_profile
from bernstein.core.volunteer.lease_store import (
    LeaseRefusal,
    LeaseRefusalReason,
    LeaseStore,
)

logger = logging.getLogger(__name__)

# Scope constants for volunteer JWT tokens.
SCOPE_VOLUNTEER_ENROLL = "volunteer:enroll"
SCOPE_VOLUNTEER_CLAIM = "volunteer:claim"
SCOPE_VOLUNTEER_HEARTBEAT = "volunteer:heartbeat"
SCOPE_VOLUNTEER_SUBMIT = "volunteer:submit"
SCOPE_VOLUNTEER_RELEASE = "volunteer:release"


class VolunteerAuthError(Exception):
    """Raised when volunteer authentication fails."""


class VolunteerAuthenticator:
    """Minimal authenticator for volunteer worker operations.

    Unlike :class:`bernstein.core.protocols.cluster.cluster_auth.ClusterAuthenticator`,
    this does not issue JWTs; it only verifies them.  The assumption is that a
    separate operator surface (CLI or another endpoint) issues bearer tokens
    carrying the volunteer scopes.
    """

    def __init__(self, require_auth: bool = True) -> None:
        self.require_auth = require_auth
        self._tokens: dict[str, tuple[str, ...]] = {}  # token -> scopes

    def add_token(self, token: str, scopes: tuple[str, ...]) -> None:
        """Add a bearer token with the given scopes."""
        self._tokens[token] = scopes

    def verify_request(
        self,
        authorization: str | None,
        required_scope: str,
    ) -> None:
        """Verify an incoming request's authorization header.

        Args:
            authorization: The ``Authorization`` header value.
            required_scope: The scope that must be present in the token.

        Raises:
            VolunteerAuthError: If auth is required and verification fails.
        """
        if not self.require_auth:
            return
        if not authorization:
            raise VolunteerAuthError("Missing Authorization header")

        parts = authorization.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise VolunteerAuthError("Invalid Authorization header format")

        token = parts[1]
        scopes = self._tokens.get(token)
        if scopes is None:
            raise VolunteerAuthError("Invalid or expired token")
        if required_scope not in scopes:
            raise VolunteerAuthError(f"Token lacks required scope '{required_scope}'")


def _get_lease_store(request: Request) -> LeaseStore:
    """Return the lease store from app state."""
    return request.app.state.lease_store  # type: ignore[no-any-return]


def _get_authenticator(request: Request) -> VolunteerAuthenticator | None:
    """Return the authenticator from app state, or None if not configured."""
    return getattr(request.app.state, "volunteer_authenticator", None)


def _verify_volunteer_auth(request: Request, required_scope: str) -> None:
    """Verify volunteer JWT authentication if a VolunteerAuthenticator is configured.

    Raises HTTPException 401 on auth failure.
    """
    authenticator = _get_authenticator(request)
    if authenticator is None or not authenticator.require_auth:
        return
    try:
        authenticator.verify_request(request.headers.get("Authorization"), required_scope)
    except VolunteerAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _refusal_to_http(refusal: LeaseRefusal) -> None:
    """Translate a LeaseRefusal to an HTTPException.

    Raises HTTPException with the appropriate status code.
    """
    status_map = {
        LeaseRefusalReason.ALREADY_LEASED: 409,
        LeaseRefusalReason.NOT_LEASE_HOLDER: 403,
        LeaseRefusalReason.NO_LEASE: 404,
        LeaseRefusalReason.ALREADY_SUBMITTED: 409,
        LeaseRefusalReason.UNKNOWN_WORKER: 401,
        LeaseRefusalReason.LEASE_REASSIGNED: 403,
        LeaseRefusalReason.TASK_BUDGET_EXHAUSTED: 409,
        LeaseRefusalReason.WALL_CLOCK_BUDGET_EXHAUSTED: 409,
        LeaseRefusalReason.TOKEN_BUDGET_EXHAUSTED: 409,
        LeaseRefusalReason.SIZE_CAP_EXCEEDED: 409,
        LeaseRefusalReason.TASK_SIZE_UNKNOWN: 422,
        LeaseRefusalReason.LOCAL_ONLY_ADAPTER_REQUIRED: 409,
    }
    status_code = status_map.get(refusal.reason, 400)
    raise HTTPException(status_code=status_code, detail=refusal.detail)


def build_hub_app(
    lease_store: LeaseStore,
    config: dict | None = None,
    authenticator: VolunteerAuthenticator | None = None,
) -> FastAPI:
    """Build the FastAPI application backing the volunteer hub.

    Args:
        lease_store: The :class:`LeaseStore` instance to back the endpoints.
        config: Optional configuration dict. Not used yet; reserved for
            future config (TLS, auth, etc.).
        authenticator: Optional :class:`VolunteerAuthenticator` for scoped
            bearer-token verification. When omitted, all auth-gated endpoints
            are open (no-op auth). Auth is intentionally deferred; wire this
            via the CLI once a token-issuance surface exists.

    Returns:
        Configured :class:`FastAPI` app.
    """
    app = FastAPI(title="Bernstein volunteer hub", version="1.0")

    # CORS middleware — allows the volunteer web UI (served from a different
    # origin) to call the hub API.  Mirrors the pattern established by
    # bernstein.core.fleet.web so the same configuration surface is used.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )

    app.state.lease_store = lease_store
    if authenticator is not None:
        app.state.volunteer_authenticator = authenticator

    # Health check
    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"ok": True}

    # Enrollment endpoint
    @app.post("/volunteer/enroll")
    async def enroll(request: Request) -> JSONResponse:
        """Enroll a new worker with an Ed25519 public key.

        Returns a worker id.  The enrollment starts as ``pending``; the worker
        must be approved before it can claim tasks.
        """
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            body = await request.json()
            pubkey_pem = body.get("public_key_pem")
            if not pubkey_pem:
                raise HTTPException(status_code=422, detail="public_key_pem is required")
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        # Parse the PEM back to a key object for id derivation
        from cryptography.hazmat.primitives import serialization

        pubkey = serialization.load_pem_public_key(pubkey_pem.encode("utf-8"))
        if not isinstance(pubkey, Ed25519PublicKey):
            raise HTTPException(status_code=422, detail="public_key_pem must be an Ed25519 public key")

        worker_id = await lease_store.enroll(pubkey)
        return JSONResponse({"worker_id": worker_id, "status": "pending"}, status_code=201)

    # Placeholder endpoint — verifies worker exists; full approval gating deferred
    @app.post("/volunteer/workers/{worker_id}/approve", status_code=204)
    async def approve_worker(worker_id: str) -> Response:
        """Check whether a worker is enrolled.

        Currently a no-op that verifies the worker exists in the store.
        A full implementation would gate on actual approval state once
        the store supports it.
        """
        if not lease_store.is_enrolled(worker_id):
            raise HTTPException(status_code=404, detail=f"Worker {worker_id} not found")
        return Response(status_code=204)

    # Claim endpoint
    @app.post("/volunteer/tasks/{task_id}/claim", status_code=201)
    async def claim_task(task_id: str, request: Request) -> JSONResponse:
        """Claim a task for processing.

        Requires the volunteer:claim scope.
        """
        _verify_volunteer_auth(request, SCOPE_VOLUNTEER_CLAIM)
        try:
            body = await request.json()
            worker_id = body.get("worker_id")
            ttl_seconds = int(body.get("ttl_seconds", 300))
            task_size = str(body.get("task_size", "s"))
            token_estimate = int(body.get("token_estimate", 0))
            wall_clock_hours_raw = body.get("wall_clock_hours")
            wall_clock_hours = float(wall_clock_hours_raw) if wall_clock_hours_raw is not None else None
            adapter_id = body.get("adapter_id")
            adapter_profile = get_profile(adapter_id) if isinstance(adapter_id, str) else None
        except UnknownProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        if not worker_id:
            raise HTTPException(status_code=422, detail="worker_id is required")
        if token_estimate < 0:
            raise HTTPException(status_code=422, detail="token_estimate must be non-negative")
        if wall_clock_hours is not None and (wall_clock_hours < 0 or not math.isfinite(wall_clock_hours)):
            raise HTTPException(status_code=422, detail="wall_clock_hours must be finite and non-negative")

        result = await lease_store.claim(
            task_id,
            worker_id,
            ttl_seconds,
            task_size=task_size,
            token_estimate=token_estimate,
            wall_clock_hours=wall_clock_hours,
            adapter_profile=adapter_profile,
        )
        if isinstance(result, LeaseRefusal):
            _refusal_to_http(result)
        return JSONResponse(result.to_dict(), status_code=201)

    # Heartbeat endpoint
    @app.post("/volunteer/tasks/{task_id}/heartbeat")
    async def heartbeat_task(task_id: str, request: Request) -> JSONResponse:
        """Heartbeat to extend the lease.

        Requires the volunteer:heartbeat scope.
        """
        _verify_volunteer_auth(request, SCOPE_VOLUNTEER_HEARTBEAT)
        try:
            body = await request.json()
            worker_id = body.get("worker_id")
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        if not worker_id:
            raise HTTPException(status_code=422, detail="worker_id is required")

        result = await lease_store.heartbeat(task_id, worker_id)
        if isinstance(result, LeaseRefusal):
            _refusal_to_http(result)
        return JSONResponse(result.to_dict())

    # Submit endpoint
    @app.post("/volunteer/tasks/{task_id}/submit", status_code=200)
    async def submit_task(task_id: str, request: Request) -> JSONResponse:
        """Submit a result for a leased task.

        Requires the volunteer:submit scope.
        """
        _verify_volunteer_auth(request, SCOPE_VOLUNTEER_SUBMIT)
        try:
            body = await request.json()
            worker_id = body.get("worker_id")
            bundle_digest = body.get("bundle_digest")
            location = body.get("location")
            actual_tokens_raw = body.get("actual_tokens")
            actual_tokens = int(actual_tokens_raw) if actual_tokens_raw is not None else None
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        if not worker_id or not bundle_digest or not location:
            raise HTTPException(
                status_code=422,
                detail="worker_id, bundle_digest, and location are required",
            )
        if actual_tokens is not None and actual_tokens < 0:
            raise HTTPException(status_code=422, detail="actual_tokens must be non-negative")

        result = await lease_store.submit(
            task_id,
            worker_id,
            bundle_digest,
            location,
            actual_tokens=actual_tokens,
        )
        if isinstance(result, LeaseRefusal):
            _refusal_to_http(result)
        return JSONResponse(result.to_dict())

    # Release endpoint
    @app.post("/volunteer/tasks/{task_id}/release", status_code=204)
    async def release_task(task_id: str, request: Request) -> Response:
        """Release a lease, making the task available for re-claim.

        Requires the volunteer:release scope.
        """
        _verify_volunteer_auth(request, SCOPE_VOLUNTEER_RELEASE)
        try:
            body = await request.json()
            worker_id = body.get("worker_id")
            actual_tokens_raw = body.get("actual_tokens")
            actual_tokens = int(actual_tokens_raw) if actual_tokens_raw is not None else None
        except Exception as exc:
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=422, detail=f"Invalid request body: {exc}") from exc

        if not worker_id:
            raise HTTPException(status_code=422, detail="worker_id is required")
        if actual_tokens is not None and actual_tokens < 0:
            raise HTTPException(status_code=422, detail="actual_tokens must be non-negative")

        result = await lease_store.release(task_id, worker_id, actual_tokens=actual_tokens)
        if isinstance(result, LeaseRefusal):
            _refusal_to_http(result)
        return Response(status_code=204)

    @app.exception_handler(HTTPException)
    async def _http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app
