"""GitHub and GitLab webhook routes and alerts endpoint."""

from __future__ import annotations

import hmac
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level
from bernstein.core.server import (
    TaskCreate,
    TaskStore,
    WebhookTaskCreate,
    WebhookTaskResponse,
    task_to_response,
)
from bernstein.core.tenanting import request_tenant_id
from bernstein.core.webhook_signatures import verify_hmac_sha256

logger = logging.getLogger(__name__)

router = APIRouter()
_GENERIC_WEBHOOK_SECRET_ENV = "BERNSTEIN_WEBHOOK_SECRET"
_GENERIC_WEBHOOK_SECRET_HEADER = "x-bernstein-webhook-secret"
_GENERIC_WEBHOOK_SIGNATURE_HEADER = "x-bernstein-webhook-signature-256"


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _verify_generic_webhook_secret(request: Request, body: bytes) -> JSONResponse | None:
    """Validate the optional shared secret or HMAC for POST /webhook."""

    configured_secret = os.environ.get(_GENERIC_WEBHOOK_SECRET_ENV, "")
    if not configured_secret:
        return None
    provided_signature = request.headers.get(_GENERIC_WEBHOOK_SIGNATURE_HEADER, "")
    if provided_signature:
        if verify_hmac_sha256(body, provided_signature, configured_secret, prefix="sha256="):
            return None
        return JSONResponse(status_code=401, content={"detail": "Invalid webhook signature"})
    provided_secret = request.headers.get(_GENERIC_WEBHOOK_SECRET_HEADER, "")
    if provided_secret and hmac.compare_digest(provided_secret, configured_secret):
        return None
    return JSONResponse(status_code=401, content={"detail": "Invalid webhook secret"})


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


@router.post("/webhook", response_model=WebhookTaskResponse, status_code=201)
async def generic_webhook(body: WebhookTaskCreate, request: Request) -> WebhookTaskResponse | JSONResponse:
    """Create a task directly from a generic inbound webhook payload.

    The endpoint is intentionally small and separate from the trigger-manager
    flow: callers POST a task-shaped payload and Bernstein creates one task.
    When ``BERNSTEIN_WEBHOOK_SECRET`` is configured, callers must also send
    the same value in ``X-Bernstein-Webhook-Secret``.
    """
    raw_body = await request.body()
    denied = _verify_generic_webhook_secret(request, raw_body)
    if denied is not None:
        return denied

    store = _get_store(request)
    effective_body = body.model_copy(update={"tenant_id": request_tenant_id(request)})
    if effective_body.estimated_minutes is None:
        score = estimate_difficulty(effective_body.description)
        effective_body.estimated_minutes = minutes_for_level(score.level)
    task = await store.create(effective_body)
    return WebhookTaskResponse(task=task_to_response(task))


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
        logger.debug("Bad GitHub webhook payload", exc_info=exc)
        return JSONResponse(
            status_code=400,
            content={"detail": "Bad webhook payload"},
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
    tenant_id = request_tenant_id(request)
    for payload in task_payloads:
        task = await store.create(TaskCreate(**payload, tenant_id=tenant_id))
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


# ---------------------------------------------------------------------------
# GitLab CI webhooks
# ---------------------------------------------------------------------------


def _count_gitlab_ci_fix_attempts(store: TaskStore, ref: str) -> int:
    """Count active ci-fix tasks for *ref* to enforce the retry cap.

    Args:
        store: Task store.
        ref: Git branch/ref name from the GitLab pipeline payload.

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
    return sum(1 for t in tasks if t.title.startswith("[ci-fix]") and ref in t.description and t.status in _ACTIVE)


def _gitlab_pipeline_to_task(payload: dict[str, Any], retry_count: int) -> dict[str, Any] | None:
    """Convert a failed GitLab pipeline webhook into a ci-fix task payload.

    Args:
        payload: Raw GitLab pipeline webhook JSON payload.
        retry_count: Current number of active ci-fix attempts for this ref.

    Returns:
        Task dict for store.create(), or None if no actionable failure found.
    """
    attrs = payload.get("object_attributes", {})
    pipeline_id = attrs.get("id", "?")
    ref = attrs.get("ref", "main")
    sha = attrs.get("sha", "")
    project = payload.get("project", {})
    repo_name = project.get("path_with_namespace", project.get("name", "unknown"))

    # Attempt to extract failure details from build traces.
    builds = payload.get("builds", [])
    failed_builds = [b for b in builds if b.get("status") in ("failed", "canceled")]

    summaries: list[str] = []
    for build in failed_builds[:5]:
        build_name = build.get("name", "unknown")
        stage = build.get("stage", "unknown")
        summaries.append(f"- Job **{build_name}** (stage: {stage}) failed")

    if not summaries:
        summaries.append(f"- Pipeline {pipeline_id} failed (no detailed job info in webhook)")

    # Escalate model on retries.
    from bernstein.github_app.ci_router import MAX_CI_RETRIES

    if retry_count >= 2:
        model = "opus"
        effort = "max"
    else:
        model = "sonnet"
        effort = "high"

    description = (
        f"GitLab CI pipeline failed on ``{ref}`` in ``{repo_name}``.\n\n"
        f"## Failed jobs\n" + "\n".join(summaries) + f"\n\n"
        f"Pipeline: {attrs.get('url', 'N/A')}\n"
        f"Commit: {sha}\n"
        f"Retry attempt: {retry_count + 1}/{MAX_CI_RETRIES}\n\n"
        f"Review the pipeline logs, identify root causes, and apply fixes.\n"
    )

    return {
        "title": f"[ci-fix] GitLab pipeline {pipeline_id} on {ref}",
        "description": description,
        "role": "qa",
        "priority": "1",
        "model": model,
        "effort": effort,
        "require_review": True,
    }


@router.post("/webhooks/gitlab", status_code=200)
async def gitlab_webhook(request: Request) -> JSONResponse:
    """Receive a GitLab CI webhook, verify token, and create ci-fix tasks.

    Handles the following event types:
    - ``pipeline`` (failed) — creates a ci-fix task, capped at
      ``MAX_CI_RETRIES`` active attempts per branch.
    - ``job`` (failed) — creates a ci-fix task for the specific job.

    Reads ``GITLAB_WEBHOOK_TOKEN`` from environment. GitLab sends a simple
    plaintext token in the ``x-gitlab-token`` header.
    Returns 200 on success, 401 on bad/missing token.
    """
    from bernstein.github_app.ci_router import MAX_CI_RETRIES

    store = _get_store(request)
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8") if body_bytes else ""

    # Verify GitLab webhook token.
    gitlab_token = os.environ.get("GITLAB_WEBHOOK_TOKEN", "")
    provided_token = request.headers.get("x-gitlab-token", "")
    if gitlab_token and provided_token:
        if not hmac.compare_digest(provided_token, gitlab_token):
            return JSONResponse(status_code=401, content={"detail": "Invalid GitLab webhook token"})
    elif gitlab_token and not provided_token:
        return JSONResponse(status_code=401, content={"detail": "Missing GitLab webhook token"})

    try:
        import json

        data: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "Bad JSON payload"})

    event_type = data.get("object_kind", "")
    task_payloads: list[dict[str, Any]] = []

    if event_type == "pipeline":
        status = data.get("object_attributes", {}).get("status", "")
        if status == "failed":
            ref = data.get("object_attributes", {}).get("ref", "")
            retry_count = _count_gitlab_ci_fix_attempts(store, ref)
            if retry_count >= MAX_CI_RETRIES:
                logger.warning(
                    "CI fix retry cap reached for ref %r (%d/%d) — skipping",
                    ref,
                    retry_count,
                    MAX_CI_RETRIES,
                )
                return JSONResponse(
                    status_code=200,
                    content={
                        "event_type": event_type,
                        "tasks_created": 0,
                        "task_ids": [],
                        "skipped_reason": f"max_retries_reached ({retry_count}/{MAX_CI_RETRIES})",
                    },
                )
            task = _gitlab_pipeline_to_task(data, retry_count=retry_count)
            if task is not None:
                task_payloads.append(task)

    elif event_type == "job":
        build_status = data.get("build_status", "")
        if build_status == "failed":
            task = _gitlab_pipeline_to_task(data, retry_count=0)
            if task is not None:
                task_payloads.append(task)

    if not task_payloads:
        return JSONResponse(
            status_code=200,
            content={"event_type": event_type, "tasks_created": 0, "task_ids": []},
        )

    created_ids: list[str] = []
    tenant_id = request_tenant_id(request)
    for payload_dict in task_payloads:
        task = await store.create(TaskCreate(**payload_dict, tenant_id=tenant_id))
        created_ids.append(task.id)

    return JSONResponse(
        status_code=200,
        content={
            "event_type": event_type,
            "tasks_created": len(created_ids),
            "task_ids": created_ids,
        },
    )
