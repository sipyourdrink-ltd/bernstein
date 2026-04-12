"""A2A (Agent-to-Agent) federation routes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

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

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_a2a_handler(request: Request) -> A2AHandler:
    return request.app.state.a2a_handler  # type: ignore[no-any-return]


def _require_task_access(task: object, request: Request) -> None:
    """Reject access to a task outside the current tenant scope."""
    from bernstein.core.routes.task_crud import _require_task_access as _impl  # pyright: ignore[reportPrivateUsage]

    _impl(task, request)  # type: ignore[arg-type]


@router.get("/.well-known/agent.json")
def agent_card(request: Request) -> A2AAgentCardResponse:
    """Publish the Bernstein orchestrator Agent Card (A2A spec)."""
    a2a_handler = _get_a2a_handler(request)
    card = a2a_handler.orchestrator_card()
    d = card.to_dict()
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
    return a2a_task_to_response(a2a_task)


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
