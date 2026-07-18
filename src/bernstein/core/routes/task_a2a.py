"""A2A (Agent-to-Agent) federation routes."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from bernstein.core.a2a import AgentCard
from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level
from bernstein.core.server import (
    A2AAgentCardResponse,
    A2AArtifactRequest,
    A2AArtifactResponse,
    A2AMessageRequest,
    A2AMessageResponse,
    A2ATaskResponse,
    A2ATaskSendRequest,
    SSEBus,
    TaskCreate,
    TaskStore,
    a2a_message_to_response,
    a2a_task_to_response,
)
from bernstein.core.tenanting import request_tenant_id

if TYPE_CHECKING:
    from bernstein.core.a2a import A2AHandler
    from bernstein.core.a2a_federation import A2AFederation
    from bernstein.core.protocols.a2a.receipt import A2AReceiptIssuer

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_a2a_handler(request: Request) -> A2AHandler:
    return request.app.state.a2a_handler  # type: ignore[no-any-return]


def _get_a2a_federation(request: Request) -> A2AFederation:
    return request.app.state.a2a_federation  # type: ignore[no-any-return]


def _get_a2a_receipt_issuer(request: Request) -> A2AReceiptIssuer | None:
    """Return the receipt issuer, or ``None`` when the node cannot attest."""
    return getattr(request.app.state, "a2a_receipt_issuer", None)


def _attest(
    request: Request,
    *,
    a2a_task_id: str,
    response: A2ATaskResponse,
) -> dict[str, Any] | None:
    """Mint and persist the lineage receipt for an inbound A2A response.

    The receipt attests to the response body *excluding* the receipt itself -
    a receipt cannot cover its own bytes. Callers reconstruct the attested
    payload by dropping the ``receipt`` field, which is what
    ``bernstein a2a verify --receipt`` does.

    Returns ``None`` when the node has no issuer, in which case the response
    goes out unattested rather than failing.
    """
    issuer = _get_a2a_receipt_issuer(request)
    if issuer is None:
        return None
    try:
        receipt = issuer.issue(
            task_id=a2a_task_id,
            response=response.model_dump(exclude={"receipt"}),
        )
    except (OSError, ValueError) as exc:
        # An unattested answer is degraded but usable; a 500 is not.
        logger.warning("could not mint A2A receipt for %s: %s", a2a_task_id, exc)
        return None
    payload = receipt.to_dict()
    _get_a2a_handler(request).attach_receipt(a2a_task_id, payload)
    return payload


def _require_task_access(task: object, request: Request) -> None:
    """Reject access to a task outside the current tenant scope."""
    from bernstein.core.routes.task_crud import _require_task_access as _impl  # pyright: ignore[reportPrivateUsage]

    _impl(task, request)  # type: ignore[arg-type]


@router.get("/a2a/agent-card")
def agent_card(request: Request) -> A2AAgentCardResponse:
    """Publish the Bernstein orchestrator Agent Card (legacy A2A path).

    The richer service manifest at ``/.well-known/agent.json`` is served by
    ``routes.well_known``; this endpoint is preserved for callers that
    historically pulled the orchestrator's own A2A card.
    """
    d = _get_a2a_handler(request).orchestrator_card().to_dict()
    return A2AAgentCardResponse(**d)


@router.get("/a2a/agents")
def list_a2a_agents(request: Request) -> A2AAgentCardResponse:
    """Return Bernstein's A2A agent card via the task API namespace."""

    return agent_card(request)


@router.post(
    "/a2a/message",
    status_code=201,
    responses={404: {"description": "Task not found"}},
)
async def a2a_message(body: A2AMessageRequest, request: Request) -> A2AMessageResponse:
    """Receive an inbound A2A message and inject it into the target task context."""

    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    a2a_handler = _get_a2a_handler(request)

    task = store.get_task(body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{body.task_id}' not found")
    _require_task_access(task, request)

    message = a2a_handler.receive_message(
        sender=body.sender,
        recipient=body.recipient,
        content=body.content,
        task_id=body.task_id,
    )
    injected_context = f"[A2A:{body.sender}->{body.recipient}] {body.content}"
    await store.add_progress(body.task_id, injected_context, 0)
    sse_bus.publish(
        "a2a_message",
        json.dumps(
            {
                "id": message.id,
                "task_id": message.task_id,
                "sender": message.sender,
                "recipient": message.recipient,
            }
        ),
    )
    return a2a_message_to_response(message)


@router.post("/a2a/tasks/send", status_code=201)
async def a2a_send_task(body: A2ATaskSendRequest, request: Request) -> A2ATaskResponse:
    """Receive a task from an external A2A agent.

    Creates both an A2A task record and a corresponding Bernstein task,
    linking them together for lifecycle synchronisation.
    """
    store = _get_store(request)
    a2a_handler = _get_a2a_handler(request)
    a2a_task = a2a_handler.create_task(
        sender=body.sender,
        message=body.message,
        role=body.role,
    )
    # Create the corresponding Bernstein task.
    bernstein_task = await store.create(
        TaskCreate(
            title=f"[A2A] {body.message[:80]}",
            description=body.message,
            role=body.role,
            tenant_id=request_tenant_id(request),
            estimated_minutes=minutes_for_level(estimate_difficulty(body.message).level),
        )
    )
    a2a_handler.link_bernstein_task(a2a_task.id, bernstein_task.id)
    response = a2a_task_to_response(a2a_task)
    receipt = _attest(request, a2a_task_id=a2a_task.id, response=response)
    if receipt is not None:
        response = response.model_copy(update={"receipt": receipt})
    return response


@router.get(
    "/a2a/tasks/{a2a_task_id}",
    responses={404: {"description": "A2A task not found"}},
)
def a2a_get_task(a2a_task_id: str, request: Request) -> A2ATaskResponse:
    """Get an A2A task by ID, syncing status from the Bernstein task."""
    store = _get_store(request)
    a2a_handler = _get_a2a_handler(request)
    a2a_task = a2a_handler.get_task(a2a_task_id)
    if a2a_task is None:
        raise HTTPException(status_code=404, detail=f"A2A task '{a2a_task_id}' not found")
    # Sync status from the underlying Bernstein task.
    if a2a_task.bernstein_task_id is not None:
        bt = store.get_task(a2a_task.bernstein_task_id)
        if bt is not None:
            a2a_handler.sync_status(a2a_task.id, bt.status.value)
    return a2a_task_to_response(a2a_task)


@router.post(
    "/a2a/tasks/{a2a_task_id}/artifacts",
    status_code=201,
    responses={404: {"description": "A2A task not found"}},
)
def a2a_add_artifact(a2a_task_id: str, body: A2AArtifactRequest, request: Request) -> A2AArtifactResponse:
    """Attach an artifact to an A2A task."""
    a2a_handler = _get_a2a_handler(request)
    try:
        artifact = a2a_handler.add_artifact(
            a2a_task_id=a2a_task_id,
            name=body.name,
            data=body.data,
            content_type=body.content_type,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"A2A task '{a2a_task_id}' not found") from None
    return A2AArtifactResponse(
        name=artifact.name,
        content_type=artifact.content_type,
        data=artifact.data,
        created_at=artifact.created_at,
    )


@router.post(
    "/a2a/v0/tasks",
    status_code=202,
    responses={
        400: {"description": "Invalid sender Agent Card or task body"},
        409: {"description": "Task rejected (validation, capacity, etc.)"},
    },
)
async def a2a_v0_accept_task(request: Request) -> dict[str, Any]:
    """Accept a federated task delegated from a peer orchestrator.

    Wire format::

        {
          "sender": { ...AgentCard... },
          "task":   { "id": "...", "message": "...", "role": "..." }
        }

    Returns 202 with the local federated-task id and the remote task id
    that was offered. Validation errors return HTTP 409 so that the
    caller's retry policy treats them as terminal (the peer is reachable
    and authoritative, no point retrying with the same body).
    """
    a2a_federation = _get_a2a_federation(request)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from None

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    sender_raw = body.get("sender")
    task_raw = body.get("task")

    if not isinstance(sender_raw, dict) or not sender_raw:
        raise HTTPException(status_code=400, detail="Missing or invalid 'sender' Agent Card")
    if not isinstance(task_raw, dict) or not task_raw:
        raise HTTPException(status_code=400, detail="Missing or invalid 'task' body")

    # Validate the sender Agent Card upfront so malformed peers are
    # rejected at the boundary rather than silently logged.
    try:
        AgentCard.validate(sender_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid sender card: {exc}") from None

    sender_name = str(sender_raw.get("name") or "")
    remote_task_id = str(task_raw.get("id") or "")
    message = task_raw.get("message")
    role = str(task_raw.get("role") or "backend")

    if not remote_task_id:
        raise HTTPException(status_code=409, detail="Missing 'task.id'")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=409, detail="Missing or empty 'task.message'")

    # Auto-register inbound peer so the federation ledger has an ACTIVE
    # entry to refresh; explicit register_peer remains available for
    # callers that want to seed peers up-front with custom capabilities.
    if a2a_federation.get_peer(sender_name) is None and sender_name:
        endpoint = str(sender_raw.get("endpoint") or "")
        a2a_federation.register_peer(
            sender_name,
            endpoint,
            capabilities=list(sender_raw.get("capabilities") or []),
        )

    federated = a2a_federation.accept_inbound_task(
        peer_name=sender_name,
        remote_task_id=remote_task_id,
        message=message,
        role=role,
    )
    return {
        "id": federated.id,
        "remote_task_id": federated.remote_task_id,
        "status": federated.status.value,
        "peer_name": federated.peer_name,
    }
