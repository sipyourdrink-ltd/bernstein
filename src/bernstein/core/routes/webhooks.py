"""GitHub webhook route and alerts endpoint."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.server import TaskCreate, TaskStore

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/alerts")
async def get_alerts(request: Request) -> JSONResponse:
    """Return current dashboard alerts as JSON.

    Builds alerts from the live task/agent state — failed tasks, blocked
    tasks, stale agents, and budget thresholds.  Intended for dashboard
    polling or external monitoring.

    Returns a JSON object with keys:
    - ``alerts``: list of alert dicts (``level``, ``message``, ``detail``)
    - ``count``: total number of alerts
    - ``ts``: server timestamp (Unix seconds)
    """
    from bernstein.core.routes.status import build_alerts

    store = _get_store(request)
    agents = store.agents
    alive_agents = [a for a in agents.values() if a.status != "dead"]
    cost_by_role = store.cost_by_role()
    total_cost = sum(cost_by_role.values())
    now = time.time()

    alerts = build_alerts(store, alive_agents, total_cost, now)
    return JSONResponse(content={"alerts": alerts, "count": len(alerts), "ts": now})


def _count_ci_fix_attempts(store: TaskStore, head_branch: str) -> int:
    """Count active ci-fix tasks for *head_branch* to enforce the retry cap.

    A task is "active" (counts toward the retry budget) when it is in any
    non-terminal status: ``open``, ``claimed``, ``in_progress``, or ``failed``.
    Tasks that are ``done`` or ``cancelled`` are excluded — a successful fix
    clears the budget so the branch can accumulate failures again.

    Args:
        store: Task store.
        head_branch: Branch name from the ``workflow_run`` payload.

    Returns:
        Number of ci-fix tasks still consuming the retry budget.
    """
    from bernstein.core.models import TaskStatus

    _ACTIVE = {
        TaskStatus.OPEN,
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.FAILED,
    }
    tasks = store.list_tasks()
    return sum(
        1 for t in tasks if t.title.startswith("[ci-fix]") and head_branch in t.description and t.status in _ACTIVE
    )


@router.post("/webhooks/github", status_code=200)
async def github_webhook(request: Request) -> JSONResponse:
    """Receive a GitHub App webhook, verify signature, and create tasks.

    Handles the following event types:
    - ``issues`` (opened / labeled)
    - ``pull_request_review_comment`` / ``issue_comment``
    - ``push``
    - ``workflow_run`` (completed + failure) — creates a ci-fix task, capped at
      ``MAX_CI_RETRIES`` active attempts per branch.

    Reads ``GITHUB_WEBHOOK_SECRET`` from environment for HMAC verification.
    Returns 200 on success, 401 on bad/missing signature, 400 on parse error.
    """
    from bernstein.github_app.ci_router import MAX_CI_RETRIES
    from bernstein.github_app.mapper import (
        SlashCommandHandler,
        issue_to_tasks,
        label_to_action,
        pr_review_to_task,
        push_to_tasks,
        trigger_label_to_task,
        workflow_run_to_task,
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

    # Map event to tasks based on event type — use handler classes for new events,
    # keep direct calls for legacy event types that already have tests.
    task_payloads: list[dict[str, Any]] = []

    if event.event_type == "issues" and event.action == "opened":
        task_payloads.extend(issue_to_tasks(event))
    elif event.event_type == "issues" and event.action == "labeled":
        # Handle bernstein/agent-fix trigger labels first, then evolve-candidate
        trigger_task = trigger_label_to_task(event)
        if trigger_task is not None:
            task_payloads.append(trigger_task)
        else:
            action_task = label_to_action(event)
            if action_task is not None:
                task_payloads.append(action_task)
    elif event.event_type in ("pull_request_review_comment", "issue_comment"):
        # Check for slash commands first, then fall back to actionable review heuristic
        comment: dict[str, Any] = event.payload.get("comment", {})
        comment_body = comment.get("body", "") or ""
        slash_task = SlashCommandHandler().handle(event, comment_body)
        if slash_task is not None:
            task_payloads.append(slash_task)
        else:
            review_task = pr_review_to_task(event)
            if review_task is not None:
                task_payloads.append(review_task)
    elif event.event_type == "push":
        task_payloads.extend(push_to_tasks(event))
    elif event.event_type == "workflow_run" and event.action == "completed":
        run: dict[str, Any] = event.payload.get("workflow_run", {})
        if run.get("conclusion") == "failure":
            head_branch: str = run.get("head_branch", "")
            retry_count = _count_ci_fix_attempts(store, head_branch)
            if retry_count >= MAX_CI_RETRIES:
                logger.warning(
                    "CI fix retry cap reached for branch %r (%d/%d) — skipping",
                    head_branch,
                    retry_count,
                    MAX_CI_RETRIES,
                )
                return JSONResponse(
                    status_code=200,
                    content={
                        "event_type": event.event_type,
                        "action": event.action,
                        "tasks_created": 0,
                        "task_ids": [],
                        "skipped_reason": f"max_retries_reached ({retry_count}/{MAX_CI_RETRIES})",
                    },
                )
            task_payloads.extend(workflow_run_to_task(event, retry_count=retry_count))

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
