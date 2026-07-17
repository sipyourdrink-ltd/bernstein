"""Operator steering route for the task server (#2508).

``POST /tasks/{task_id}/steer`` is the operator-outbound half of the task
mailbox: where ``/messages`` carries worker-to-worker coordination, this
carries an operator's mid-task intervention (pause, resume, guidance,
redirect, abort). Every action is a receipt first and an effect second -
the command is bound into the audit chain via ``record_steering_receipt``
before the effect runs, and the delivered effect (a ``steer.*`` mailbox
message plus, for abort/pause, a scheduler-enforced process signal)
references that receipt's chain HMAC. The response IS the receipt.

Authorisation rides the scoped dashboard-token registry: a request must
present an ``operator`` token to steer. A ``viewer`` token, or any request
that carried no valid credential once auth is configured, is recorded in the
denial tracker and rejected. On a loopback bind with no tokens configured
the route follows the dashboard's loopback posture and grants operator.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from bernstein.core.orchestration.steering import (
    InvalidSteeringCommand,
    SteeringCommand,
    SteeringController,
    SteeringPayloadMismatch,
    UnauthorizedSteering,
)
from bernstein.core.routes.task_crud import (
    _get_sse_bus,
    _get_store,
    _require_task_access,
)
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.server import TaskSteerPost, TaskSteerResponse
from bernstein.core.server.dashboard_tokens import SCOPE_OPERATOR

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.communication.task_mailbox import TaskMailbox
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

router = APIRouter()

_STEER_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "Scope is not authorised to steer"},
    404: {"description": "Task not found"},
    409: {"description": "Confirmed payload differs from the executed command"},
    422: {"description": "Malformed steering command"},
    503: {"description": "Task mailbox is not configured"},
}

_LOOPBACK_STEER_PRINCIPAL = "dashboard-operator"


def _get_mailbox(request: Request) -> TaskMailbox:
    mailbox = getattr(request.app.state, "task_mailbox", None)
    if mailbox is None:
        raise HTTPException(status_code=503, detail="Task mailbox is not configured")
    return mailbox


def _get_audit_chain(request: Request) -> AuditChainStore:
    chain = getattr(request.app.state, "audit_chain", None)
    if chain is None:
        raise HTTPException(status_code=503, detail="Audit chain is not configured")
    return chain


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    prefix = "bearer "
    if header.lower().startswith(prefix):
        return header[len(prefix) :].strip()
    return ""


def _resolve_steer_identity(request: Request, body: TaskSteerPost) -> tuple[str, str]:
    """Resolve the acting ``(principal, scope)`` for a steering request.

    A validated scoped token is authoritative for both principal and scope,
    so a client cannot spoof attribution. When scoped-token auth is not
    configured (loopback posture) the route grants operator and attributes to
    the client-declared principal, mirroring how the dashboard issues a
    loopback operator token at startup.
    """
    auth_state = getattr(request.app.state, "dashboard_auth_state", None)
    registry = getattr(auth_state, "token_registry", None) if auth_state is not None else None
    if registry is not None and registry.has_tokens():
        token = _bearer_token(request)
        if token:
            record = registry.validate(token)
            if record is not None:
                return record.principal, record.scope
        return "anonymous", ""
    return (body.principal or _LOOPBACK_STEER_PRINCIPAL), SCOPE_OPERATOR


def _resolve_sdd_dir(request: Request) -> Path | None:
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return None
    return sdd_dir


def _build_controller(request: Request, principal: str) -> SteeringController:
    from bernstein.core.security.denial_tracker import DenialTracker

    chain = _get_audit_chain(request)
    mailbox = _get_mailbox(request)
    sdd_dir = _resolve_sdd_dir(request)
    signals_dir = (sdd_dir / "runtime" / "signals") if sdd_dir is not None else None
    denial_path = (sdd_dir / "runtime" / "steering_denials.jsonl") if sdd_dir is not None else None
    return SteeringController(
        chain=chain,
        mailbox=mailbox,
        signals_dir=signals_dir,
        sdd_dir=sdd_dir,
        denial_tracker=DenialTracker(persist_path=denial_path),
        sender=f"operator:{principal}",
    )


@router.post(
    "/tasks/{task_id}/steer",
    status_code=201,
    responses=_STEER_RESPONSES,
)
async def post_task_steer(task_id: str, body: TaskSteerPost, request: Request) -> TaskSteerResponse:
    """Record a steering receipt for a running worker and apply its effect.

    The receipt is bound into the audit chain before the effect executes; the
    ``steer.*`` mailbox message and any process signal reference the receipt
    hash returned here. An effect can never precede its receipt.
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    principal, scope = _resolve_steer_identity(request, body)
    command = SteeringCommand(
        kind=body.kind,
        task_id=task_id,
        principal=principal,
        guidance=body.guidance,
        redirect_target=body.redirect_target,
        reason=body.reason,
        session_id=body.session_id,
        adapter=body.adapter,
        worktree=body.worktree,
    )
    controller = _build_controller(request, principal)

    try:
        outcome = controller.steer(command, scope=scope, displayed_payload_hash=body.displayed_payload_hash)
    except UnauthorizedSteering as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except SteeringPayloadMismatch as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidSteeringCommand as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    _get_sse_bus(request).publish(
        "task_steer",
        json.dumps(
            {
                "task_id": task_id,
                "kind": command.kind,
                "seq": outcome.message.seq,
                "receipt_hash": outcome.receipt.receipt_hash,
            }
        ),
    )
    logger.info(
        "task.steer recorded: task_id=%s kind=%s principal=%s scope=%s seq=%d",
        sanitize_log(task_id),
        sanitize_log(command.kind),
        sanitize_log(principal),
        sanitize_log(scope),
        outcome.message.seq,
    )
    return TaskSteerResponse(
        kind=outcome.receipt.kind,
        task_id=outcome.receipt.task_id,
        principal=outcome.receipt.principal,
        scope=outcome.receipt.scope,
        payload_hash=outcome.receipt.payload_hash,
        receipt_hash=outcome.receipt.receipt_hash,
        timestamp=outcome.receipt.timestamp,
        mailbox_seq=outcome.message.seq,
        mailbox_entry_hash=outcome.message.entry_hash,
        checkpoint_event_hash=(outcome.checkpoint.event_hash if outcome.checkpoint is not None else ""),
        abort_signal_written=outcome.abort_signal_path is not None,
    )
