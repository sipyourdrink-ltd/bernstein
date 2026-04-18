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
_GENERIC_WEBHOOK_SIGNATURE_HEADER = "x-bernstein-webhook-signature-256"
_GENERIC_WEBHOOK_TIMESTAMP_HEADER = "x-bernstein-timestamp"
# Replay window: reject requests whose timestamp drifts more than this
# many seconds from the server clock (audit-121).  Five minutes matches
# the Slack v0 and AWS SigV4 recommendations — short enough to bound
# replay risk while tolerating modest clock skew between sender and
# receiver.
_WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS = 300


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _parse_timestamp_header(raw: str) -> int | None:
    """Parse a decimal Unix-seconds timestamp header, or return ``None``.

    Accepts only non-negative integers — leading whitespace is stripped
    but decimals, scientific notation, or signs are rejected so a
    malformed header cannot be confused with a missing one.
    """

    stripped = raw.strip()
    if not stripped or not stripped.isdigit():
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _verify_generic_webhook_secret(request: Request, body: bytes) -> JSONResponse | None:
    """Verify the HMAC signature + timestamp freshness for POST ``/webhook``.

    Fail-closed semantics (audit-042 + audit-121): when
    ``BERNSTEIN_WEBHOOK_SECRET`` is not configured the endpoint is
    disabled and every POST returns 503.  When a secret *is*
    configured, callers MUST supply:

    * ``X-Bernstein-Timestamp`` — Unix seconds; rejected if the skew
      from the server clock exceeds five minutes (replay protection).
    * ``X-Bernstein-Webhook-Signature-256`` — HMAC-SHA256 of
      ``f"{timestamp}.".encode() + body`` using the shared secret,
      prefixed with ``sha256=``.  The timestamp is bound into the
      signature so an attacker cannot rewrite the header after
      capturing a valid pair.

    The plaintext ``X-Bernstein-Webhook-Secret`` fallback has been
    removed (audit-121) — there is no remaining code path that
    compares the raw secret against a request header.
    """

    configured_secret = os.environ.get(_GENERIC_WEBHOOK_SECRET_ENV, "")
    if not configured_secret:
        logger.error(
            "Rejecting POST /webhook: %s is not configured. "
            "Set the env var to enable the endpoint; unsigned "
            "webhooks are not accepted.",
            _GENERIC_WEBHOOK_SECRET_ENV,
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Webhook endpoint is not configured: set "
                    f"{_GENERIC_WEBHOOK_SECRET_ENV} to the shared "
                    "secret used by the caller."
                ),
            },
        )

    timestamp_header = request.headers.get(_GENERIC_WEBHOOK_TIMESTAMP_HEADER, "")
    timestamp = _parse_timestamp_header(timestamp_header)
    if timestamp is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or malformed X-Bernstein-Timestamp header"},
        )
    if abs(int(time.time()) - timestamp) > _WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS:
        return JSONResponse(
            status_code=401,
            content={"detail": "Stale or future-dated X-Bernstein-Timestamp header"},
        )

    provided_signature = request.headers.get(_GENERIC_WEBHOOK_SIGNATURE_HEADER, "")
    if not provided_signature:
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing X-Bernstein-Webhook-Signature-256 header"},
        )

    signed_payload = f"{timestamp}.".encode() + body
    if verify_hmac_sha256(signed_payload, provided_signature, configured_secret, prefix="sha256="):
        return None
    return JSONResponse(status_code=401, content={"detail": "Invalid webhook signature"})


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/alerts")
def get_alerts(request: Request) -> JSONResponse:
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
    ``BERNSTEIN_WEBHOOK_SECRET`` must be configured (fail-closed; audit-042)
    and each request must carry a fresh ``X-Bernstein-Timestamp`` header
    plus a matching ``X-Bernstein-Webhook-Signature-256`` HMAC over
    ``f"{timestamp}.".encode() + body`` (audit-121).  The plaintext
    ``X-Bernstein-Webhook-Secret`` fallback has been removed; callers
    relying on it must upgrade to the HMAC + timestamp flow.
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


def _handle_issue_opened(event: Any) -> list[dict[str, Any]]:
    """Map a GitHub ``issues/opened`` event to task payloads."""
    from bernstein.github_app.mapper import issue_to_tasks

    return list(issue_to_tasks(event))


def _handle_issue_labeled(event: Any) -> list[dict[str, Any]]:
    """Map a GitHub ``issues/labeled`` event to task payloads."""
    from bernstein.github_app.mapper import label_to_action, trigger_label_to_task

    trigger_task = trigger_label_to_task(event)
    if trigger_task is not None:
        return [trigger_task]
    action_task = label_to_action(event)
    return [action_task] if action_task is not None else []


def _handle_comment(event: Any) -> list[dict[str, Any]]:
    """Map a PR review / issue comment event to task payloads."""
    from bernstein.github_app.mapper import SlashCommandHandler, pr_review_to_task

    comment: dict[str, Any] = event.payload.get("comment", {})
    comment_body = comment.get("body", "") or ""
    slash_task = SlashCommandHandler().handle(event, comment_body)
    if slash_task is not None:
        return [slash_task]
    review_task = pr_review_to_task(event)
    return [review_task] if review_task is not None else []


def _handle_workflow_run(event: Any, store: TaskStore) -> list[dict[str, Any]] | JSONResponse:
    """Map a GitHub ``workflow_run/completed`` event to task payloads.

    Returns a JSONResponse early when the retry cap is reached.
    """
    from bernstein.github_app.ci_router import MAX_CI_RETRIES
    from bernstein.github_app.mapper import workflow_run_to_task

    run: dict[str, Any] = event.payload.get("workflow_run", {})
    if run.get("conclusion") != "failure":
        return []

    head_branch: str = run.get("head_branch", "")
    retry_count = _count_ci_fix_attempts(store, head_branch)
    if retry_count >= MAX_CI_RETRIES:
        safe_branch = head_branch.replace("\n", "").replace("\r", "")[:200]
        logger.warning(
            "CI fix retry cap reached for branch %r (%d/%d) — skipping",
            safe_branch,
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
    return list(workflow_run_to_task(event, retry_count=retry_count))


def _dispatch_github_event(event: Any, store: TaskStore) -> list[dict[str, Any]] | JSONResponse:
    """Route a parsed GitHub webhook event to the appropriate handler."""
    from bernstein.github_app.mapper import push_to_tasks

    match (event.event_type, event.action):
        case ("issues", "opened"):
            return _handle_issue_opened(event)
        case ("issues", "labeled"):
            return _handle_issue_labeled(event)
        case ("pull_request_review_comment", _) | ("issue_comment", _):
            return _handle_comment(event)
        case ("push", _):
            return list(push_to_tasks(event))
        case ("workflow_run", "completed"):
            return _handle_workflow_run(event, store)
        case _:
            return []


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
    Fail-closed (audit-042): when the secret is not configured the
    endpoint is disabled and returns 503; unsigned GitHub webhooks are
    never accepted.
    Replay protection (audit-121): if the caller includes an
    ``X-Bernstein-Timestamp`` header the request is additionally
    checked for freshness — drift greater than five minutes returns
    401.  Real GitHub deliveries omit this header and continue to
    work; the check is there so bernstein-internal relays cannot be
    replayed after capture.
    Returns 200 on success, 401 on bad/missing signature or stale
    timestamp, 400 on parse error, 503 when the endpoint is not
    configured.
    """
    from bernstein.github_app.webhooks import parse_webhook, verify_signature

    store = _get_store(request)
    body = await request.body()

    # Verify HMAC signature — secret MUST be configured (audit-042).
    gh_webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not gh_webhook_secret:
        logger.error(
            "Rejecting POST /webhooks/github: GITHUB_WEBHOOK_SECRET is "
            "not configured. Set the env var to enable the endpoint; "
            "unsigned webhooks are not accepted.",
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "GitHub webhook endpoint is not configured: set "
                    "GITHUB_WEBHOOK_SECRET to the shared secret "
                    "registered with the GitHub App."
                ),
            },
        )
    # audit-121: opt-in timestamp freshness check.  GitHub itself does
    # not send ``X-Bernstein-Timestamp``, but bernstein-internal relays
    # and test harnesses can, and when they do we enforce the same
    # five-minute skew window as the generic webhook.
    ts_raw = request.headers.get(_GENERIC_WEBHOOK_TIMESTAMP_HEADER, "")
    if ts_raw:
        timestamp = _parse_timestamp_header(ts_raw)
        if timestamp is None or abs(int(time.time()) - timestamp) > _WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS:
            return JSONResponse(
                status_code=401,
                content={"detail": "Stale or malformed X-Bernstein-Timestamp header"},
            )
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

    result = _dispatch_github_event(event, store)
    if isinstance(result, JSONResponse):
        return result
    task_payloads = result

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


def _verify_gitlab_token(request: Request) -> JSONResponse | None:
    """Verify the GitLab webhook token and optional timestamp freshness.

    Fail-closed semantics (audit-042): when ``GITLAB_WEBHOOK_TOKEN`` is
    not configured the endpoint is disabled and every POST returns 503;
    unsigned / unauthenticated GitLab webhooks are never accepted.
    Missing / mismatched tokens return 401.  Replay protection
    (audit-121): when the caller includes ``X-Bernstein-Timestamp`` the
    request is rejected if its drift exceeds five minutes — GitLab
    itself never sends this header, so real deliveries are unaffected;
    the check hardens bernstein-internal relays.
    """
    gitlab_token = os.environ.get("GITLAB_WEBHOOK_TOKEN", "")
    if not gitlab_token:
        logger.error(
            "Rejecting POST /webhooks/gitlab: GITLAB_WEBHOOK_TOKEN is "
            "not configured. Set the env var to enable the endpoint; "
            "unauthenticated webhooks are not accepted.",
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "GitLab webhook endpoint is not configured: set "
                    "GITLAB_WEBHOOK_TOKEN to the shared token "
                    "registered with the GitLab project."
                ),
            },
        )
    provided_token = request.headers.get("x-gitlab-token", "")
    if not provided_token:
        return JSONResponse(status_code=401, content={"detail": "Missing GitLab webhook token"})
    if not hmac.compare_digest(provided_token, gitlab_token):
        return JSONResponse(status_code=401, content={"detail": "Invalid GitLab webhook token"})
    # audit-121: opt-in timestamp freshness check — mirrors the generic
    # webhook.  Real GitLab deliveries never send this header; internal
    # relays may and, when they do, we fail closed on stale timestamps.
    ts_raw = request.headers.get(_GENERIC_WEBHOOK_TIMESTAMP_HEADER, "")
    if ts_raw:
        timestamp = _parse_timestamp_header(ts_raw)
        if timestamp is None or abs(int(time.time()) - timestamp) > _WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS:
            return JSONResponse(
                status_code=401,
                content={"detail": "Stale or malformed X-Bernstein-Timestamp header"},
            )
    return None


def _handle_gitlab_pipeline(data: dict[str, Any], store: TaskStore) -> list[dict[str, Any]] | JSONResponse:
    """Handle a GitLab pipeline-failed event, enforcing the retry cap."""
    from bernstein.github_app.ci_router import MAX_CI_RETRIES

    status = data.get("object_attributes", {}).get("status", "")
    if status != "failed":
        return []

    ref = data.get("object_attributes", {}).get("ref", "")
    retry_count = _count_gitlab_ci_fix_attempts(store, ref)
    if retry_count >= MAX_CI_RETRIES:
        safe_ref = ref.replace("\n", "").replace("\r", "")[:200]
        logger.warning(
            "CI fix retry cap reached for ref %r (%d/%d) — skipping",
            safe_ref,
            retry_count,
            MAX_CI_RETRIES,
        )
        return JSONResponse(
            status_code=200,
            content={
                "event_type": "pipeline",
                "tasks_created": 0,
                "task_ids": [],
                "skipped_reason": f"max_retries_reached ({retry_count}/{MAX_CI_RETRIES})",
            },
        )
    task = _gitlab_pipeline_to_task(data, retry_count=retry_count)
    return [task] if task is not None else []


def _handle_gitlab_job(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Handle a GitLab job-failed event."""
    if data.get("build_status", "") != "failed":
        return []
    task = _gitlab_pipeline_to_task(data, retry_count=0)
    return [task] if task is not None else []


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
    store = _get_store(request)
    body_bytes = await request.body()
    body = body_bytes.decode("utf-8") if body_bytes else ""

    denied = _verify_gitlab_token(request)
    if denied is not None:
        return denied

    try:
        import json

        data: dict[str, Any] = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "Bad JSON payload"})

    event_type = data.get("object_kind", "")

    match event_type:
        case "pipeline":
            result = _handle_gitlab_pipeline(data, store)
        case "job":
            result = _handle_gitlab_job(data)
        case _:
            result = []

    if isinstance(result, JSONResponse):
        return result
    task_payloads = result

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
