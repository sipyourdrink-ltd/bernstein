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

from bernstein.core.communication.task_mailbox import (
    MailboxError,
    MailboxFull,
    MailboxMessage,
    TaskMailbox,
)
from bernstein.core.routes.task_crud import (
    _get_sse_bus,
    _get_store,
    _require_task_access,
)
from bernstein.core.security.audit_chain import AuditChainStore, record_task_mailbox_message
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.server import TaskMessagePost, TaskMessageResponse

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
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    mailbox = _get_mailbox(request)
    try:
        message = mailbox.post(
            task_id=task_id,
            sender=body.sender,
            kind=body.kind,
            body=body.body,
            sender_card_fingerprint=body.sender_card_fingerprint or "unregistered",
        )
    except MailboxFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    except MailboxError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

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

    _get_sse_bus(request).publish("task_message", json.dumps({"task_id": task_id, "seq": message.seq}))
    logger.info(
        "task.message posted: task_id=%s seq=%d kind=%s sender=%s redactions=%d",
        sanitize_log(task_id),
        message.seq,
        sanitize_log(message.kind),
        sanitize_log(message.sender),
        message.redaction_count,
    )
    return _message_to_response(message)


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
