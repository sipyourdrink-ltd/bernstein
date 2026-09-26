"""Worker mailbox routes for the task server (#2357).

``POST /tasks/{task_id}/messages`` appends one typed, size-capped message
to the HMAC-chained mailbox journal and mirrors it into the audit chain;
``GET /tasks/{task_id}/messages`` is the poll channel that delivers
pending messages in chain append order - a total, replay-stable order.
A worker that finds a cross-cutting problem hands it to the tasks still
in flight without waiting for a scheduler re-dispatch.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request

from bernstein.core.communication.rendezvous import RENDEZVOUS_CLOSED_KIND, RENDEZVOUS_OPEN_KIND
from bernstein.core.communication.task_mailbox import (
    MailboxAuthorizationError,
    MailboxError,
    MailboxFull,
    MailboxMessage,
    TaskMailbox,
)
from bernstein.core.log_safe import for_log
from bernstein.core.routes.task_crud import (
    _get_sse_bus,
    _get_store,
    _require_task_access,
)
from bernstein.core.security.audit_chain import AuditChainStore, record_task_mailbox_message
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.server import (
    TaskAskRequest,
    TaskAskResponse,
    TaskMessagePost,
    TaskMessageResponse,
    TaskRendezvousReplyRequest,
    TaskRendezvousReplyResponse,
)
from bernstein.core.tasks.suspension import RendezvousRefusedError, blocking_ask, post_rendezvous_reply

logger = logging.getLogger(__name__)

router = APIRouter()

_MAILBOX_RESPONSES: dict[int | str, dict[str, str]] = {
    404: {"description": "Task not found"},
    422: {"description": "Unknown message kind or body over the byte cap"},
    429: {"description": "Recipient task mailbox is full"},
}


def _get_mailbox(request: Request) -> TaskMailbox:
    mailbox = getattr(request.app.state, "task_mailbox", None)
    if mailbox is None:
        raise HTTPException(status_code=503, detail="Task mailbox is not configured")
    return mailbox


def _get_audit_chain(request: Request) -> AuditChainStore | None:
    return getattr(request.app.state, "audit_chain", None)


def _message_to_response(message: MailboxMessage) -> TaskMessageResponse:
    return TaskMessageResponse(
        seq=message.seq,
        task_id=message.task_id,
        sender=message.sender,
        sender_card_fingerprint=message.sender_card_fingerprint,
        kind=message.kind,
        body=message.body,
        body_hash=message.body_hash,
        redaction_count=message.redaction_count,
        timestamp=message.timestamp,
        prev_entry_hash=message.prev_entry_hash,
        entry_hash=message.entry_hash,
        signature=message.signature,
        signer_public_key_pem=message.signer_public_key_pem,
    )


def _record_message_effects(request: Request, message: MailboxMessage) -> None:
    """Mirror and publish an already-appended mailbox entry."""
    chain = _get_audit_chain(request)
    if chain is not None:
        try:
            record_task_mailbox_message(
                chain=chain,
                task_id=message.task_id,
                seq=message.seq,
                kind=message.kind,
                sender=message.sender,
                sender_card_fingerprint=message.sender_card_fingerprint,
                body_hash=message.body_hash,
                entry_hash=message.entry_hash,
                redaction_count=message.redaction_count,
            )
        except Exception as exc:  # intentional-broad-except: audit mirror is best-effort, never blocks the post
            logger.warning("task_mailbox: audit chain mirror failed: %s", type(exc).__name__)
    _get_sse_bus(request).publish("task_message", json.dumps({"task_id": message.task_id, "seq": message.seq}))


def _worker_mailbox_context(request: Request, task_id: str) -> tuple[str, list[str] | None]:
    """Return trusted sender and task scope for a worker-facing mailbox call."""
    identity = getattr(request.state, "agent_identity", None)
    if identity is None:
        return "operator", None
    authorized_task_ids = list(identity.task_ids)
    if authorized_task_ids and task_id not in authorized_task_ids:
        raise HTTPException(status_code=403, detail="acting task is not in this agent's task scope")
    return str(identity.id), authorized_task_ids


@router.post(
    "/tasks/{task_id}/messages",
    status_code=201,
    responses=_MAILBOX_RESPONSES,
)
async def post_task_message(task_id: str, body: TaskMessagePost, request: Request) -> TaskMessageResponse:
    """Append one typed message to the recipient task's mailbox.

    The message is DLP-redacted, HMAC-chained onto the mailbox journal,
    Ed25519-signed, and mirrored into the audit chain before the response
    is returned - the response IS the signed journal entry.
    """
    agent_identity = getattr(request.state, "agent_identity", None)
    sender = body.sender
    authorized_task_ids = None
    if agent_identity is not None:
        sender = str(agent_identity.id)
        if body.sender != sender:
            raise HTTPException(status_code=403, detail="mailbox sender must match the authenticated agent identity")
        authorized_task_ids = list(agent_identity.task_ids)
        if (
            authorized_task_ids
            and task_id not in authorized_task_ids
            and body.kind
            not in {
                "question",
                RENDEZVOUS_OPEN_KIND,
                RENDEZVOUS_CLOSED_KIND,
            }
        ):
            raise HTTPException(status_code=403, detail=f"cross-task message kind {body.kind!r} is not permitted")

    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    mailbox = _get_mailbox(request)
    try:
        message = mailbox.post(
            task_id=task_id,
            sender=sender,
            kind=body.kind,
            body=body.body,
            sender_card_fingerprint=body.sender_card_fingerprint or "unregistered",
            acting_task_id=body.acting_task_id,
            authorized_task_ids=authorized_task_ids,
        )
    except MailboxAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except MailboxFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except MailboxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    _record_message_effects(request, message)
    logger.info(
        "task.message posted: task_id=%s seq=%d kind=%s sender=%s redactions=%d",
        for_log(task_id),
        message.seq,
        sanitize_log(message.kind),
        sanitize_log(message.sender),
        message.redaction_count,
    )
    return _message_to_response(message)


@router.post(
    "/tasks/{task_id}/ask",
    responses={403: {"description": "Task scope mismatch"}, 404: {"description": "Task not found"}},
)
async def ask_task(task_id: str, body: TaskAskRequest, request: Request) -> TaskAskResponse:
    """Block ``task_id`` cooperatively until its mailbox rendezvous closes."""
    store = _get_store(request)
    waiter = store.get_task(task_id)
    awaited = store.get_task(body.awaited_task_id)
    if waiter is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    if awaited is None:
        raise HTTPException(status_code=404, detail=f"Task '{body.awaited_task_id}' not found")
    _require_task_access(waiter, request)
    _require_task_access(awaited, request)
    sender, authorized_task_ids = _worker_mailbox_context(request, task_id)
    try:
        answer = await blocking_ask(
            task_store=store,
            task_id=task_id,
            awaited_task_id=body.awaited_task_id,
            question=body.question.encode("utf-8"),
            mailbox=_get_mailbox(request),
            sender=sender,
            authorized_task_ids=authorized_task_ids or [],
            timeout_s=body.timeout_s,
            on_post=lambda message: _record_message_effects(request, message),
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from None
    except RendezvousRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (MailboxError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return TaskAskResponse(answer=answer.decode("utf-8"))


@router.post(
    "/tasks/{task_id}/rendezvous/reply",
    status_code=201,
    responses={403: {"description": "Task scope mismatch"}, 404: {"description": "Task not found"}},
)
async def reply_to_rendezvous(
    task_id: str,
    body: TaskRendezvousReplyRequest,
    request: Request,
) -> TaskRendezvousReplyResponse:
    """Append the answer and close for a rendezvous awaited by ``task_id``."""
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    sender, authorized_task_ids = _worker_mailbox_context(request, task_id)
    try:
        reply, close = post_rendezvous_reply(
            mailbox=_get_mailbox(request),
            open_entry_hash=body.open_entry_hash,
            answer=body.answer.encode("utf-8"),
            sender=sender,
            acting_task_id=task_id,
            authorized_task_ids=authorized_task_ids or [],
        )
    except MailboxAuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except (MailboxError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    _record_message_effects(request, reply)
    _record_message_effects(request, close)
    return TaskRendezvousReplyResponse(reply=_message_to_response(reply), close=_message_to_response(close))


@router.get(
    "/tasks/{task_id}/messages",
    responses={404: {"description": "Task not found"}},
)
def get_task_messages(task_id: str, request: Request, since_seq: int = -1) -> list[TaskMessageResponse]:
    """Deliver pending messages for a task, in chain append order.

    ``since_seq`` is a deterministic cursor: pass the highest ``seq``
    already processed to receive only newer messages. Replaying the same
    journal always reproduces the same delivery order.
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    mailbox = _get_mailbox(request)
    return [_message_to_response(m) for m in mailbox.pending(task_id, since_seq=since_seq)]
