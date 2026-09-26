"""Cooperative mailbox rendezvous wiring for #3450."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.communication.rendezvous import (
    RENDEZVOUS_CLOSED_KIND,
    RENDEZVOUS_OPEN_KIND,
    RendezvousOpen,
    encode_close_body,
    encode_open_body,
)
from bernstein.core.communication.task_mailbox import MailboxAuthorizationError, MailboxFull, TaskMailbox
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.suspension import blocking_ask, post_rendezvous_reply
from bernstein.core.tasks.task_store import TaskStore

if TYPE_CHECKING:
    from pathlib import Path


def _mailbox(tmp_path: Path) -> TaskMailbox:
    return TaskMailbox(
        tmp_path / "mailbox.jsonl",
        hmac_key=b"blocking-ask-test-key",
        identity_dir=tmp_path / "identity",
    )


def _stored_status(store: TaskStore, task_id: str) -> TaskStatus:
    task = store.get_task(task_id)
    assert task is not None
    return task.status


@pytest.mark.asyncio
async def test_answered_ask_suspends_then_resumes_with_the_stored_answer_bytes(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    store = TaskStore(tmp_path / "tasks.jsonl")
    waiter = await store.create(TaskCreate(title="A", description="ask B", role="backend"))
    waiter = await store.claim_by_id(waiter.id)

    waiting = asyncio.create_task(
        blocking_ask(
            task_store=store,
            task_id=waiter.id,
            awaited_task_id="task-b",
            question=b"Which schema should I use?",
            mailbox=mailbox,
            sender="session-a",
            authorized_task_ids=[waiter.id],
            timeout_s=2.0,
            poll_interval_s=0.001,
        )
    )

    open_message = None
    for _ in range(100):
        open_message = next((m for m in mailbox.all_messages() if m.kind == RENDEZVOUS_OPEN_KIND), None)
        if open_message is not None and _stored_status(store, waiter.id) is TaskStatus.SUSPENDED:
            break
        await asyncio.sleep(0.001)

    assert open_message is not None
    assert _stored_status(store, waiter.id) is TaskStatus.SUSPENDED
    opened = RendezvousOpen.from_message(open_message)
    reply, close = post_rendezvous_reply(
        mailbox=mailbox,
        open_entry_hash=opened.entry_hash,
        answer=b"Use schema v2.",
        sender="session-b",
        acting_task_id="task-b",
        authorized_task_ids=["task-b"],
    )

    assert await waiting == b"Use schema v2."
    assert _stored_status(store, waiter.id) is TaskStatus.CLAIMED
    assert [task.id for task in store.list_tasks(status="claimed")] == [waiter.id]
    assert store.list_tasks(status="suspended") == []
    assert close.seq > reply.seq > open_message.seq
    assert mailbox.message_by_hash(reply.entry_hash) is reply


@pytest.mark.asyncio
async def test_failed_timeout_close_restores_task_without_fabricating_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mailbox = _mailbox(tmp_path)
    store = TaskStore(tmp_path / "tasks.jsonl")
    waiter = await store.create(TaskCreate(title="A", description="ask B", role="backend"))
    waiter = await store.claim_by_id(waiter.id)
    original_post = mailbox.post

    def fail_timeout_close(**kwargs: Any) -> Any:
        if kwargs["task_id"] == waiter.id and kwargs["kind"] == RENDEZVOUS_CLOSED_KIND:
            raise MailboxFull("waiter mailbox is full")
        return original_post(**kwargs)

    monkeypatch.setattr(mailbox, "post", fail_timeout_close)

    with pytest.raises(MailboxFull, match="waiter mailbox is full"):
        await blocking_ask(
            task_store=store,
            task_id=waiter.id,
            awaited_task_id="task-b",
            question=b"Which schema should I use?",
            mailbox=mailbox,
            sender="session-a",
            authorized_task_ids=[waiter.id],
            timeout_s=0,
            poll_interval_s=0.001,
        )

    assert _stored_status(store, waiter.id) is TaskStatus.CLAIMED
    assert not any(message.kind == RENDEZVOUS_CLOSED_KIND for message in mailbox.all_messages())


@pytest.mark.asyncio
async def test_cancelled_live_wait_restores_task_without_fabricating_resolution(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    store = TaskStore(tmp_path / "tasks.jsonl")
    waiter = await store.create(TaskCreate(title="A", description="ask B", role="backend"))
    waiter = await store.claim_by_id(waiter.id)
    waiting = asyncio.create_task(
        blocking_ask(
            task_store=store,
            task_id=waiter.id,
            awaited_task_id="task-b",
            question=b"Which schema should I use?",
            mailbox=mailbox,
            sender="session-a",
            authorized_task_ids=[waiter.id],
            timeout_s=60,
            poll_interval_s=0.001,
        )
    )

    for _ in range(100):
        if _stored_status(store, waiter.id) is TaskStatus.SUSPENDED:
            break
        await asyncio.sleep(0.001)
    assert _stored_status(store, waiter.id) is TaskStatus.SUSPENDED

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert _stored_status(store, waiter.id) is TaskStatus.CLAIMED
    assert not any(message.kind == RENDEZVOUS_CLOSED_KIND for message in mailbox.all_messages())


@pytest.mark.asyncio
async def test_operator_can_cancel_a_stranded_cooperative_suspension(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    store = TaskStore(tmp_path / "tasks.jsonl")
    waiter = await store.create(TaskCreate(title="A", description="ask B", role="backend"))
    waiter = await store.claim_by_id(waiter.id)
    question = mailbox.post(
        task_id="task-b",
        sender="session-a",
        kind="question",
        body="Which schema should I use?",
        acting_task_id=waiter.id,
        authorized_task_ids=[waiter.id],
    )
    opened = mailbox.post(
        task_id="task-b",
        sender="session-a",
        kind=RENDEZVOUS_OPEN_KIND,
        body=encode_open_body(
            question_entry_hash=question.entry_hash,
            waiter_task_id=waiter.id,
            awaited_task_id="task-b",
        ),
        acting_task_id=waiter.id,
        authorized_task_ids=[waiter.id],
    )
    await store.suspend_for_rendezvous(waiter.id, opened.entry_hash)

    cancelled = await store.cancel(waiter.id, reason="operator recovery")

    assert cancelled.status is TaskStatus.CANCELLED
    assert not any(message.kind == RENDEZVOUS_CLOSED_KIND for message in mailbox.all_messages())


def test_unauthorized_credential_cannot_open_another_tasks_wait(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    question = mailbox.post(
        task_id="task-b",
        sender="session-c",
        kind="question",
        body="question",
        acting_task_id="task-c",
        authorized_task_ids=["task-c"],
    )

    with pytest.raises(MailboxAuthorizationError, match="acting task"):
        mailbox.post(
            task_id="task-b",
            sender="session-c",
            kind=RENDEZVOUS_OPEN_KIND,
            body=encode_open_body(
                question_entry_hash=question.entry_hash,
                waiter_task_id="task-a",
                awaited_task_id="task-b",
            ),
            acting_task_id="task-a",
            authorized_task_ids=["task-c"],
        )


def test_only_the_awaited_task_can_close_an_answered_wait(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)
    question = mailbox.post(
        task_id="task-b",
        sender="session-a",
        kind="question",
        body="question",
        acting_task_id="task-a",
        authorized_task_ids=["task-a"],
    )
    opened = mailbox.post(
        task_id="task-b",
        sender="session-a",
        kind=RENDEZVOUS_OPEN_KIND,
        body=encode_open_body(
            question_entry_hash=question.entry_hash,
            waiter_task_id="task-a",
            awaited_task_id="task-b",
        ),
        acting_task_id="task-a",
        authorized_task_ids=["task-a"],
    )
    forged_reply = mailbox.post(
        task_id="task-a",
        sender="session-c",
        kind="question",
        body="forged answer",
        acting_task_id="task-c",
        authorized_task_ids=["task-c"],
    )

    with pytest.raises(MailboxAuthorizationError, match="may not close"):
        mailbox.post(
            task_id="task-a",
            sender="session-c",
            kind=RENDEZVOUS_CLOSED_KIND,
            body=encode_close_body(
                open_entry_hash=opened.entry_hash,
                reply_entry_hash=forged_reply.entry_hash,
                resolution="answered",
            ),
            acting_task_id="task-c",
            authorized_task_ids=["task-c"],
        )


def test_unrelated_cross_task_message_kind_remains_forbidden(tmp_path: Path) -> None:
    mailbox = _mailbox(tmp_path)

    with pytest.raises(MailboxAuthorizationError, match="not permitted"):
        mailbox.post(
            task_id="task-b",
            sender="session-a",
            kind="finding",
            body="not a rendezvous step",
            acting_task_id="task-a",
            authorized_task_ids=["task-a"],
        )
