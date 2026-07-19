"""GitHub and GitLab webhook routes, the automation bridge, and alerts endpoint."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.trigger_sources.receipt import TriggerAdmission

logger = logging.getLogger(__name__)

router = APIRouter()
_GENERIC_WEBHOOK_SECRET_ENV = "BERNSTEIN_WEBHOOK_SECRET"
_GENERIC_WEBHOOK_SIGNATURE_HEADER = "x-bernstein-webhook-signature-256"
_GENERIC_WEBHOOK_TIMESTAMP_HEADER = "x-bernstein-timestamp"
# Replay window: reject requests whose timestamp drifts more than this
# many seconds from the server clock. Five minutes matches
# the Slack v0 and AWS SigV4 recommendations - short enough to bound
# replay risk while tolerating modest clock skew between sender and
# receiver.
_WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS = 300


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _parse_timestamp_header(raw: str) -> int | None:
    """Parse a decimal Unix-seconds timestamp header, or return ``None``.

    Accepts only non-negative integers - leading whitespace is stripped
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


def _verify_generic_webhook_secret(request: Request, body: bytes) -> tuple[JSONResponse, str] | None:
    """Verify the HMAC signature + timestamp freshness for POST ``/webhook``.

    Fail-closed semantics ( + ): when
    ``BERNSTEIN_WEBHOOK_SECRET`` is not configured the endpoint is
    disabled and every POST returns 503.  When a secret *is*
    configured, callers MUST supply:

    * ``X-Bernstein-Timestamp`` - Unix seconds; rejected if the skew
      from the server clock exceeds five minutes (replay protection).
    * ``X-Bernstein-Webhook-Signature-256`` - HMAC-SHA256 of
      ``f"{timestamp}.".encode() + body`` using the shared secret,
      prefixed with ``sha256=``.  The timestamp is bound into the
      signature so an attacker cannot rewrite the header after
      capturing a valid pair.

    The plaintext ``X-Bernstein-Webhook-Secret`` fallback has been
    removed - there is no remaining code path that
    compares the raw secret against a request header.

    Returns:
        ``None`` when the request authenticates, else the refusal response
        paired with a canonical refusal reason for the trigger receipt (#2512).
        The unconfigured-endpoint case is a deployment fault, not a refused
        trigger, and carries an empty reason so no receipt is minted for it.
    """
    from bernstein.core.trigger_sources.receipt import (
        REFUSAL_STALE_TIMESTAMP,
        REFUSAL_UNAUTHENTICATED,
    )

    configured_secret = os.environ.get(_GENERIC_WEBHOOK_SECRET_ENV, "")
    if not configured_secret:
        logger.error(
            "Rejecting POST /webhook: %s is not configured. "
            "Set the env var to enable the endpoint; unsigned "
            "webhooks are not accepted.",
            _GENERIC_WEBHOOK_SECRET_ENV,
        )
        return (
            JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Webhook endpoint is not configured: set "
                        f"{_GENERIC_WEBHOOK_SECRET_ENV} to the shared "
                        "secret used by the caller."
                    ),
                },
            ),
            "",
        )

    timestamp_header = request.headers.get(_GENERIC_WEBHOOK_TIMESTAMP_HEADER, "")
    timestamp = _parse_timestamp_header(timestamp_header)
    if timestamp is None:
        return (
            JSONResponse(
                status_code=401,
                content={"detail": "Missing or malformed X-Bernstein-Timestamp header"},
            ),
            REFUSAL_STALE_TIMESTAMP,
        )
    if abs(int(time.time()) - timestamp) > _WEBHOOK_TIMESTAMP_MAX_SKEW_SECONDS:
        return (
            JSONResponse(
                status_code=401,
                content={"detail": "Stale or future-dated X-Bernstein-Timestamp header"},
            ),
            REFUSAL_STALE_TIMESTAMP,
        )

    provided_signature = request.headers.get(_GENERIC_WEBHOOK_SIGNATURE_HEADER, "")
    if not provided_signature:
        return (
            JSONResponse(
                status_code=401,
                content={"detail": "Missing X-Bernstein-Webhook-Signature-256 header"},
            ),
            REFUSAL_UNAUTHENTICATED,
        )

    signed_payload = f"{timestamp}.".encode() + body
    if verify_hmac_sha256(signed_payload, provided_signature, configured_secret, prefix="sha256="):
        return None
    return (
        JSONResponse(status_code=401, content={"detail": "Invalid webhook signature"}),
        REFUSAL_UNAUTHENTICATED,
    )


# ---------------------------------------------------------------------------
# Automation bridge (#2512)
# ---------------------------------------------------------------------------


def _bridge_paths(request: Request) -> tuple[Path, Path] | None:
    """Return ``(bridge_root, audit_dir)`` for this install, or ``None``.

    ``None`` means the app carries no ``.sdd`` layout, so there is nowhere to
    anchor a receipt; callers degrade to the pre-bridge behaviour rather than
    failing a request over bookkeeping.
    """
    from bernstein.core.trigger_sources.receipt import bridge_root

    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return None
    return bridge_root(sdd_dir / "automation-bridge"), sdd_dir / "audit"


def _fallback_trigger_id(request: Request, body: bytes) -> str:
    """Derive a replay nonce when the caller named none.

    Binds the signed timestamp to the payload digest, so a byte-identical
    request replayed inside the signature window collides with its own earlier
    admission and is refused -- which is exactly the request a captured
    delivery reproduces.
    """
    timestamp = request.headers.get(_GENERIC_WEBHOOK_TIMESTAMP_HEADER, "").strip()
    digest = hashlib.sha256(body).hexdigest()
    return f"derived-{hashlib.sha256(f'{timestamp}.{digest}'.encode()).hexdigest()[:32]}"


def _mint_trigger_receipt(
    request: Request,
    body: bytes,
    *,
    authenticated: bool,
    refusal_reason: str = "",
    task_ids: tuple[str, ...] = (),
) -> TriggerAdmission | None:
    """Admit or refuse the inbound trigger and mint its signed receipt.

    Returns ``None`` when the bridge has nowhere to anchor a receipt, or when
    minting failed; the caller keeps its existing behaviour in that case, and
    the failure is logged rather than swallowed.
    """
    from bernstein.core.trigger_sources.automation_platforms import normalise_trigger
    from bernstein.core.trigger_sources.receipt import RefusalBudget, admit_trigger

    paths = _bridge_paths(request)
    if paths is None:
        return None
    root, audit_dir = paths

    try:
        import json as _json

        decoded = _json.loads(body) if body else {}
    except (ValueError, UnicodeDecodeError):
        decoded = {}
    payload = decoded if isinstance(decoded, dict) else {}

    headers = dict(request.headers)
    platform, intent, trigger_id = normalise_trigger(payload=payload, headers=headers)
    # Replay refusal is only sound against a caller-supplied nonce. A derived id
    # cannot tell a captured replay apart from a legitimate re-fire of the same
    # goal, so we record the weaker regime on the receipt instead of refusing.
    enforce_replay = bool(trigger_id)
    if not trigger_id:
        trigger_id = _fallback_trigger_id(request, body)

    from bernstein.core.trigger_sources.automation_platforms import adapter_for

    try:
        from bernstein.core.security.audit import load_or_create_audit_key

        return admit_trigger(
            root=root,
            audit_dir=audit_dir,
            hmac_key=load_or_create_audit_key(),
            platform=platform,
            request_path=request.url.path,
            trigger_id=trigger_id,
            body=body,
            scope=adapter_for(platform).scope,
            timestamp=int(time.time()),
            authenticated=authenticated,
            refusal_reason=refusal_reason,
            enforce_replay=enforce_replay,
            budget=RefusalBudget(root),
            intent=intent,
            task_ids=task_ids,
        )
    except (OSError, RuntimeError, ValueError):
        logger.exception("automation bridge: could not mint a trigger receipt for %s", request.url.path)
        return None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


@router.get("/alerts")
def get_alerts(request: Request) -> JSONResponse:
    """Return current dashboard alerts as JSON.

    Builds alerts from the live task/agent state - failed tasks, blocked
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
    ``BERNSTEIN_WEBHOOK_SECRET`` must be configured (fail-closed; )
    and each request must carry a fresh ``X-Bernstein-Timestamp`` header
    plus a matching ``X-Bernstein-Webhook-Signature-256`` HMAC over
    ``f"{timestamp}.".encode() + body``. The plaintext
    ``X-Bernstein-Webhook-Secret`` fallback has been removed; callers
    relying on it must upgrade to the HMAC + timestamp flow.

    Automation bridge (#2512): an admitted trigger returns a signed,
    chain-anchored trigger receipt in ``receipt`` so the calling platform holds
    a proof of what it asked for rather than a bare task reference. A trigger
    that fails authentication, or that replays a trigger id already admitted,
    is refused with its own signed refusal receipt (HTTP 401 and 409
    respectively) -- the negative path leaves a record, never a silent drop.
    """
    raw_body = await request.body()
    denied = _verify_generic_webhook_secret(request, raw_body)
    if denied is not None:
        response, refusal_reason = denied
        if not refusal_reason:
            return response
        refusal = _mint_trigger_receipt(request, raw_body, authenticated=False, refusal_reason=refusal_reason)
        return _with_receipt(response, refusal)

    admission = _mint_trigger_receipt(request, raw_body, authenticated=True)
    if admission is not None and not admission.admitted:
        return _with_receipt(
            JSONResponse(
                status_code=409,
                content={"detail": f"Trigger refused: {admission.refusal_reason}"},
            ),
            admission,
        )

    store = _get_store(request)
    effective_body = body.model_copy(update={"tenant_id": request_tenant_id(request)})
    if effective_body.estimated_minutes is None:
        score = estimate_difficulty(effective_body.description)
        effective_body.estimated_minutes = minutes_for_level(score.level)
    task = await store.create(effective_body)
    receipt = admission.receipt.to_dict() if admission is not None and admission.receipt is not None else None
    return WebhookTaskResponse(task=task_to_response(task), receipt=receipt)


def _with_receipt(response: JSONResponse, admission: TriggerAdmission | None) -> JSONResponse:
    """Attach a refusal receipt to a refusal response, preserving its status.

    The response is returned unchanged when no receipt was minted -- an install
    with no ``.sdd`` layout, or a refusal that exceeded the refusal budget. The
    trigger is refused either way; only the per-request receipt is absent.
    """
    if admission is None or admission.receipt is None:
        return response
    import json as _json

    body: dict[str, Any] = _json.loads(response.body)
    body["receipt"] = admission.receipt.to_dict()
    return JSONResponse(status_code=response.status_code, content=body)


def _count_ci_fix_attempts(store: TaskStore, head_branch: str) -> int:
    """Count active ci-fix tasks for *head_branch* to enforce the retry cap.

    A task is "active" (counts toward the retry budget) when it is in any
    non-terminal status: ``open``, ``claimed``, ``in_progress``, or ``failed``.
    Tasks that are ``done`` or ``cancelled`` are excluded - a successful fix
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
            "CI fix retry cap reached for branch %r (%d/%d) - skipping",
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
    return []


@router.post("/webhooks/github", status_code=200)
async def github_webhook(request: Request) -> JSONResponse:
    """Receive a GitHub App webhook, verify signature, and create tasks.

    Handles the following event types:
    - ``issues`` (opened / labeled)
    - ``pull_request_review_comment`` / ``issue_comment``
    - ``push``
    - ``workflow_run`` (completed + failure) - creates a ci-fix task, capped at
      ``MAX_CI_RETRIES`` active attempts per branch.

    Reads ``GITHUB_WEBHOOK_SECRET`` from environment for HMAC verification.
    Fail-closed: when the secret is not configured the
    endpoint is disabled and returns 503; unsigned GitHub webhooks are
    never accepted.
    Replay protection: if the caller includes an
    ``X-Bernstein-Timestamp`` header the request is additionally
    checked for freshness - drift greater than five minutes returns
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

    # Verify HMAC signature - secret MUST be configured.
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
    # opt-in timestamp freshness check. GitHub itself does
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

    Fail-closed semantics: when ``GITLAB_WEBHOOK_TOKEN`` is
    not configured the endpoint is disabled and every POST returns 503;
    unsigned / unauthenticated GitLab webhooks are never accepted.
    Missing / mismatched tokens return 401.  Replay protection
    : when the caller includes ``X-Bernstein-Timestamp`` the
    request is rejected if its drift exceeds five minutes - GitLab
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
    # opt-in timestamp freshness check - mirrors the generic
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
            "CI fix retry cap reached for ref %r (%d/%d) - skipping",
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


def _handle_gitlab_merge_request(
    headers: dict[str, str],
    body: bytes,
) -> list[dict[str, Any]]:
    """Map a GitLab MR webhook event to task payloads via ``gitlab_app``."""
    from bernstein.gitlab_app.mapper import merge_request_to_tasks
    from bernstein.gitlab_app.webhooks import parse_webhook

    try:
        event = parse_webhook(headers, body)
    except ValueError:
        return []
    return list(merge_request_to_tasks(event))


def _handle_gitlab_note(
    headers: dict[str, str],
    body: bytes,
) -> list[dict[str, Any]]:
    """Map a GitLab note (MR/issue comment) event to a task payload."""
    from bernstein.gitlab_app.mapper import note_to_task
    from bernstein.gitlab_app.webhooks import parse_webhook

    try:
        event = parse_webhook(headers, body)
    except ValueError:
        return []
    task = note_to_task(event)
    return [task] if task is not None else []


@router.post("/webhooks/gitlab", status_code=200)
async def gitlab_webhook(request: Request) -> JSONResponse:
    """Receive a GitLab CI webhook, verify token, and create ci-fix tasks.

    Handles the following event types:
    - ``pipeline`` (failed) - creates a ci-fix task, capped at
      ``MAX_CI_RETRIES`` active attempts per branch.
    - ``job`` (failed) - creates a ci-fix task for the specific job.

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
    raw_headers: dict[str, str] = dict(request.headers.items())

    match event_type:
        case "pipeline":
            result = _handle_gitlab_pipeline(data, store)
        case "job":
            result = _handle_gitlab_job(data)
        case "merge_request":
            result = _handle_gitlab_merge_request(raw_headers, body_bytes)
        case "note":
            result = _handle_gitlab_note(raw_headers, body_bytes)
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
