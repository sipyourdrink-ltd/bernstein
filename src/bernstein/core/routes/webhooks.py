"""GitHub webhook route."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.server import TaskCreate, TaskStore

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


@router.post("/webhooks/github", status_code=200)
async def github_webhook(request: Request) -> JSONResponse:
    """Receive a GitHub App webhook, verify signature, and create tasks.

    Reads ``GITHUB_WEBHOOK_SECRET`` from environment for HMAC verification.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse error.
    """
    from bernstein.github_app.mapper import (
        issue_to_tasks,
        label_to_action,
        pr_review_to_task,
        push_to_tasks,
    )
    from bernstein.github_app.webhooks import parse_webhook, verify_signature

    store = _get_store(request)
    body = await request.body()

    # Verify HMAC signature if a webhook secret is configured
    gh_webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if gh_webhook_secret:
        signature = request.headers.get("x-hub-signature-256", "")
        if not signature or not verify_signature(body, signature, gh_webhook_secret):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid webhook signature"},
            )

    # Parse the webhook event
    headers = dict(request.headers)
    try:
        event = parse_webhook(headers, body)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Bad webhook payload: {exc}"},
        )

    # Map event to tasks based on event type
    task_payloads: list[dict[str, Any]] = []

    if event.event_type == "issues" and event.action == "opened":
        task_payloads.extend(issue_to_tasks(event))
    elif event.event_type == "issues" and event.action == "labeled":
        action_task = label_to_action(event)
        if action_task is not None:
            task_payloads.append(action_task)
    elif event.event_type in ("pull_request_review_comment", "issue_comment"):
        review_task = pr_review_to_task(event)
        if review_task is not None:
            task_payloads.append(review_task)
    elif event.event_type == "push":
        task_payloads.extend(push_to_tasks(event))

    # Create tasks in the store
    created_ids: list[str] = []
    for payload in task_payloads:
        task = await store.create(TaskCreate(**payload))
        created_ids.append(task.id)

    return JSONResponse(
        status_code=200,
        content={
            "event_type": event.event_type,
            "action": event.action,
            "tasks_created": len(created_ids),
            "task_ids": created_ids,
        },
    )
