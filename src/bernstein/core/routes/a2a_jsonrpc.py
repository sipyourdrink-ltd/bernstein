"""A2A JSON-RPC 2.0 server surface - the callable, discoverable node (#2609).

This is the inbound *server* binding the design directive fixes: JSON-RPC 2.0
over HTTP, with SSE for ``message/stream``, guarded by two card-declared auth
schemes (API key + OAuth2 client-credentials). It complements the client and
federation code that already ships under ``core/protocols/a2a`` and the
REST-shaped ``/a2a/tasks/*`` routes.

Design constraints honoured here (all from the binding directive):

* **JSON-RPC first.** One endpoint (:data:`A2A_JSONRPC_PATH`) dispatches
  ``message/send``, ``tasks/get``, and ``message/stream``.
* **Degrade gracefully.** A client with neither streaming nor artifacts still
  reaches every result by polling ``tasks/get``; the completed task returns an
  ``Artifact`` whose parts carry the result text *and* a lineage-receipt
  reference, so the answer verifies offline.
* **Auth, rejected per spec.** Missing / invalid credentials get an HTTP 401
  with an RFC 6750 ``WWW-Authenticate`` challenge; the token endpoint returns
  OAuth2 §5.2 error bodies. The authenticated caller is anchored in the audit
  chain via the receipt issuer.
* **Off by default.** The whole surface is gated behind
  ``BERNSTEIN_A2A_SERVER_ENABLED``; when unset the endpoints answer ``404`` and
  the agent card advertises none of this, so the default deployment is
  unchanged.

The heavy lifting lives in pure modules (:mod:`..protocols.a2a.jsonrpc`,
:mod:`..protocols.a2a.server_auth`); this router is the thin ASGI adapter.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level
from bernstein.core.protocols.a2a.jsonrpc import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    JSONRPC_PARSE_ERROR,
    JSONRPCError,
    a2a_state_for_bernstein,
    attested_completion_payload,
    build_completed_artifact,
    build_task_object,
    extract_message_text,
    jsonrpc_error_response,
    jsonrpc_result_response,
    parse_jsonrpc_request,
)
from bernstein.core.protocols.a2a.server_auth import A2AAuthError, A2AServerAuth

if TYPE_CHECKING:
    from bernstein.core.protocols.a2a.a2a import A2AHandler
    from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer
    from bernstein.core.server import TaskStore

logger = logging.getLogger(__name__)

router = APIRouter()

#: JSON-RPC 2.0 endpoint. Advertised as the node's A2A ``url`` when enabled.
A2A_JSONRPC_PATH: Final[str] = "/a2a/v1"

#: OAuth2 client-credentials token endpoint (advertised in the card scheme).
A2A_TOKEN_PATH: Final[str] = "/a2a/v1/oauth/token"

#: Env flag gating the whole surface. Off unless explicitly truthy.
_ENABLE_ENV_VAR: Final[str] = "BERNSTEIN_A2A_SERVER_ENABLED"
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

#: Handler key suffix for the completion receipt, kept distinct from the
#: acceptance receipt stored under the bare task id.
_COMPLETED_RECEIPT_SUFFIX: Final[str] = "::completed"


def a2a_server_enabled() -> bool:
    """Return whether the inbound A2A JSON-RPC surface is enabled.

    Read live from the environment so the flag, the card advertisement, and
    the route all agree within a single process without a restart.
    """
    return os.environ.get(_ENABLE_ENV_VAR, "").strip().lower() in _TRUTHY


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _context_id(task_id: str) -> str:
    """Return the A2A context id for a task - deterministic from the id.

    A2A groups related tasks under a ``contextId``. We assign one per task so
    ``tasks/get`` reproduces it without persisting extra state.
    """
    return f"ctx-{task_id}"


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_handler(request: Request) -> A2AHandler:
    return request.app.state.a2a_handler  # type: ignore[no-any-return]


def _get_issuer(request: Request) -> A2AReceiptIssuer | None:
    return getattr(request.app.state, "a2a_receipt_issuer", None)


def _get_auth(request: Request) -> A2AServerAuth:
    """Return the request-scoped authenticator.

    Rebuilt from the environment per request: the config is a handful of
    strings and the OAuth signing secret is derived deterministically, so a
    fresh instance validates tokens an earlier one issued.
    """
    cached = getattr(request.app.state, "a2a_server_auth", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    return A2AServerAuth.from_env()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _auth_error_response(exc: A2AAuthError) -> JSONResponse:
    """Render an :class:`A2AAuthError` as an HTTP response with its headers."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.error, "error_description": exc.message},
        headers=exc.headers,
    )


# ---------------------------------------------------------------------------
# OAuth2 client-credentials token endpoint
# ---------------------------------------------------------------------------


@router.post(A2A_TOKEN_PATH, include_in_schema=False)
async def a2a_oauth_token(request: Request) -> JSONResponse:
    """Issue an OAuth2 client-credentials access token.

    Accepts ``client_id`` / ``client_secret`` from a form body or an HTTP
    Basic ``Authorization`` header (RFC 6749 §2.3.1). Returns the RFC 6749
    §5.1 token response, or a §5.2 error body on failure.
    """
    if not a2a_server_enabled():
        return JSONResponse(status_code=404, content={"detail": "A2A server surface is disabled"})

    auth = _get_auth(request)
    form: dict[str, str] = {}
    try:
        raw_form = await request.form()
        form = {k: str(v) for k, v in raw_form.items()}
    except Exception:  # pragma: no cover  # intentional-broad-except: malformed multipart is best-effort
        form = {}

    grant_type = form.get("grant_type", "")
    if grant_type != "client_credentials":
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_grant_type", "error_description": "only client_credentials is supported"},
        )

    client_id, client_secret = _extract_client_credentials(request, form)
    if not client_id or not client_secret:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "error_description": "client_id and client_secret are required"},
        )

    try:
        token = auth.issue_client_credentials_token(client_id=client_id, client_secret=client_secret)
    except A2AAuthError as exc:
        return _auth_error_response(exc)

    return JSONResponse(
        status_code=200,
        content=token.to_response(),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _extract_client_credentials(request: Request, form: dict[str, str]) -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` from Basic auth or the form."""
    import base64
    import binascii

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[len("Basic ") :].strip()).decode("utf-8")
            client_id, _, client_secret = decoded.partition(":")
            if client_id:
                return client_id, client_secret
        except (ValueError, binascii.Error, UnicodeDecodeError):
            pass
    return form.get("client_id", ""), form.get("client_secret", "")


# ---------------------------------------------------------------------------
# JSON-RPC endpoint
# ---------------------------------------------------------------------------


@router.post(A2A_JSONRPC_PATH, include_in_schema=False)
async def a2a_jsonrpc(request: Request) -> Any:
    """Dispatch a single A2A JSON-RPC 2.0 request.

    Auth is enforced before parsing so an unauthenticated caller cannot probe
    method behaviour. ``message/stream`` returns an SSE stream; the other
    methods return a JSON-RPC envelope.
    """
    if not a2a_server_enabled():
        return JSONResponse(status_code=404, content={"detail": "A2A server surface is disabled"})

    # 1. Authenticate (transport-level, per the A2A spec).
    try:
        caller = _get_auth(request).authenticate(dict(request.headers))
    except A2AAuthError as exc:
        return _auth_error_response(exc)

    # 2. Parse the JSON body into a JSON-RPC request.
    try:
        body = await request.json()
    except Exception:  # intentional-broad-except: malformed JSON body returns a JSON-RPC parse error
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error_response(None, JSONRPC_PARSE_ERROR, "request body is not valid JSON"),
        )
    try:
        rpc = parse_jsonrpc_request(body)
    except JSONRPCError as exc:
        return JSONResponse(status_code=200, content=jsonrpc_error_response(None, exc.code, exc.message))

    # 3. Dispatch.
    if rpc.method == "message/stream":
        return _stream_message(request, rpc.id, rpc.params, caller_id=caller.caller_id)

    try:
        if rpc.method == "message/send":
            result = await _handle_message_send(request, rpc.params, caller_id=caller.caller_id)
        elif rpc.method == "tasks/get":
            result = _handle_tasks_get(request, rpc.params)
        else:
            return JSONResponse(
                status_code=200,
                content=jsonrpc_error_response(rpc.id, JSONRPC_METHOD_NOT_FOUND, f"unknown method '{rpc.method}'"),
            )
    except JSONRPCError as exc:
        return JSONResponse(status_code=200, content=jsonrpc_error_response(rpc.id, exc.code, exc.message))
    except Exception as exc:  # pragma: no cover  # intentional-broad-except: dispatch failure -> JSON-RPC error
        logger.warning("A2A JSON-RPC %s failed: %s", rpc.method, exc)
        return JSONResponse(
            status_code=200,
            content=jsonrpc_error_response(rpc.id, JSONRPC_INTERNAL_ERROR, "internal error handling the request"),
        )

    return JSONResponse(status_code=200, content=jsonrpc_result_response(rpc.id, result))


# ---------------------------------------------------------------------------
# Method handlers
# ---------------------------------------------------------------------------


async def _handle_message_send(request: Request, params: dict[str, Any], *, caller_id: str) -> dict[str, Any]:
    """Accept an A2A message, create a task, and return the accepted ``Task``.

    The acceptance is anchored in the audit chain (with the caller) and the
    receipt rides in the task metadata, so even the acceptance is verifiable.
    The task's eventual completed result is reached by polling ``tasks/get``.
    """
    text = extract_message_text(params)
    role = str(params.get("role") or (params.get("message") or {}).get("role") or "backend")
    if role not in {"backend", "frontend", "qa", "security", "devops", "docs", "manager"}:
        role = "backend"

    handler = _get_handler(request)
    store = _get_store(request)

    a2a_task = handler.create_task(sender=caller_id or "a2a-caller", message=text, role=role)
    from bernstein.core.server import TaskCreate
    from bernstein.core.tenanting import request_tenant_id

    bernstein_task = await store.create(
        TaskCreate(
            title=f"[A2A] {text[:80]}",
            description=text,
            role=role,
            tenant_id=request_tenant_id(request),
            estimated_minutes=minutes_for_level(estimate_difficulty(text).level),
        )
    )
    handler.link_bernstein_task(a2a_task.id, bernstein_task.id)

    state = a2a_state_for_bernstein(bernstein_task.status.value)
    metadata = _attest_acceptance(request, a2a_task_id=a2a_task.id, text=text, caller_id=caller_id)
    return build_task_object(
        task_id=a2a_task.id,
        context_id=_context_id(a2a_task.id),
        state=state,
        timestamp=_now_iso(),
        metadata=metadata,
    )


def _attest_acceptance(request: Request, *, a2a_task_id: str, text: str, caller_id: str) -> dict[str, Any]:
    """Mint and persist the acceptance receipt, returning task metadata.

    Returns a metadata dict carrying the authenticated caller and, when the
    node can attest, the receipt plus the exact payload it covers. An
    unattested node still answers; the missing receipt is the signal.
    """
    metadata: dict[str, Any] = {"a2a": {"caller": caller_id or "anonymous"}}
    issuer = _get_issuer(request)
    if issuer is None:
        return metadata
    attested = {"taskId": a2a_task_id, "message": text}
    try:
        receipt = issuer.issue(task_id=a2a_task_id, response=attested, caller=caller_id)
    except (OSError, ValueError) as exc:
        logger.warning("could not mint A2A acceptance receipt for %s: %s", a2a_task_id, exc)
        return metadata
    payload = receipt.to_dict()
    _get_handler(request).attach_receipt(a2a_task_id, payload)
    metadata["lineageReceipt"] = payload
    metadata["attested"] = attested
    return metadata


def _handle_tasks_get(request: Request, params: dict[str, Any]) -> dict[str, Any]:
    """Return the current A2A ``Task`` for a task id, projecting Bernstein state.

    When the underlying task is done, the returned task carries an ``Artifact``
    whose parts hold the result text and a lineage-receipt reference. The
    completion receipt is minted once and reused on later polls, so repeated
    reads do not grow the audit chain.
    """
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise JSONRPCError(JSONRPC_INVALID_PARAMS, "params.id is required")

    handler = _get_handler(request)
    store = _get_store(request)
    a2a_task = handler.get_task(task_id)
    if a2a_task is None:
        # A2A surfaces an unknown task as an error, not a 404 - the transport
        # stays JSON-RPC. -32001 is a server-defined "task not found".
        raise JSONRPCError(-32001, f"task '{task_id}' not found")

    bernstein_status = "open"
    result_summary = ""
    if a2a_task.bernstein_task_id is not None:
        bt = store.get_task(a2a_task.bernstein_task_id)
        if bt is not None:
            bernstein_status = bt.status.value
            handler.sync_status(a2a_task.id, bernstein_status)
            result_summary = bt.result_summary or ""

    state = a2a_state_for_bernstein(bernstein_status)
    artifacts: list[dict[str, Any]] = []
    if state == "completed":
        receipt = _completion_receipt(request, a2a_task_id=task_id, result=result_summary)
        artifacts.append(build_completed_artifact(task_id=task_id, result=result_summary, receipt=receipt))

    return build_task_object(
        task_id=task_id,
        context_id=_context_id(task_id),
        state=state,
        timestamp=_now_iso(),
        artifacts=artifacts,
    )


def _completion_receipt(request: Request, *, a2a_task_id: str, result: str) -> dict[str, Any]:
    """Return the completion receipt, minting it once and caching it.

    The receipt attests the exact ``{taskId, result}`` payload the artifact
    carries, so a peer reconstructs it and verifies offline. Minted lazily on
    the first poll that observes completion and reused thereafter.
    """
    handler = _get_handler(request)
    key = f"{a2a_task_id}{_COMPLETED_RECEIPT_SUFFIX}"
    stored = handler.get_receipt(key)
    if stored is not None:
        return stored

    issuer = _get_issuer(request)
    if issuer is None:
        return {}
    attested = attested_completion_payload(task_id=a2a_task_id, result=result)
    try:
        receipt = issuer.issue(task_id=a2a_task_id, response=attested)
    except (OSError, ValueError) as exc:
        logger.warning("could not mint A2A completion receipt for %s: %s", a2a_task_id, exc)
        return {}
    payload = receipt.to_dict()
    handler.attach_receipt(key, payload)
    return payload


# ---------------------------------------------------------------------------
# message/stream (SSE)
# ---------------------------------------------------------------------------


def _stream_message(request: Request, request_id: Any, params: dict[str, Any], *, caller_id: str) -> StreamingResponse:
    """Stream task updates over SSE for ``message/stream``.

    A2A clients that support streaming receive the accepted task and a status
    snapshot as ``data:`` events. Terminal completion (and the receipt-bearing
    artifact) is delivered through ``tasks/get`` polling, which every client
    must support - streaming is an accelerator, not the only path.
    """
    import json as _json

    def _sse(envelope: dict[str, Any]) -> str:
        return f"data: {_json.dumps(envelope)}\n\n"

    async def _events() -> Any:
        try:
            accepted = await _handle_message_send(request, params, caller_id=caller_id)
        except JSONRPCError as exc:
            yield _sse(jsonrpc_error_response(request_id, exc.code, exc.message))
            return
        except Exception as exc:  # pragma: no cover  # intentional-broad-except: stream failure -> JSON-RPC error
            logger.warning("A2A message/stream failed: %s", exc)
            yield _sse(jsonrpc_error_response(request_id, JSONRPC_INTERNAL_ERROR, "internal error"))
            return

        # First event: the accepted task.
        yield _sse(jsonrpc_result_response(request_id, accepted))

        # Second event: a status snapshot the client can act on, marked final
        # so a streaming client knows to fall back to tasks/get for the result.
        snapshot = _handle_tasks_get(request, {"id": accepted["id"]})
        status_update = {
            "taskId": accepted["id"],
            "contextId": accepted["contextId"],
            "kind": "status-update",
            "status": snapshot["status"],
            "final": snapshot["status"]["state"] in {"completed", "failed", "canceled"},
        }
        yield _sse(jsonrpc_result_response(request_id, status_update))

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
