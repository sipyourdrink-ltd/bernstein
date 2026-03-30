"""Jira webhook receiver for Bernstein.

Listens for ``jira:issue_created`` and ``jira:issue_updated`` webhook events
and creates Bernstein tasks.  Also exposes a task-update endpoint so Bernstein
can push status changes back to Jira via the REST API v3.

Environment variables
---------------------
Required:
    JIRA_BASE_URL       — e.g. https://your-org.atlassian.net
    JIRA_EMAIL          — Jira account email
    JIRA_API_TOKEN      — Jira API token (from id.atlassian.com)

Optional:
    BERNSTEIN_URL       — Bernstein task server (default: http://127.0.0.1:8052)
    JIRA_WEBHOOK_SECRET — Shared secret; include as ?secret=<value> in the
                          webhook URL or as Authorization: Bearer <value>
    JIRA_PROJECT_FILTER — Comma-separated project keys, e.g. "PROJ,BACK"
                          (empty = accept all projects)
    JIRA_LABEL_FILTER   — Comma-separated labels, e.g. "bernstein,agent"
                          (empty = no label filter; combined with AND if both set)
    JIRA_DEFAULT_ROLE   — Bernstein agent role for tasks (default: backend)

Running
-------
::

    pip install -r integrations/jira_webhook/requirements.txt
    uvicorn integrations.jira_webhook.app:app --port 8090

Jira webhook setup
------------------
In your Jira project settings register a webhook pointing at::

    https://your-host:8090/jira/webhook?secret=<JIRA_WEBHOOK_SECRET>

Events to subscribe: ``jira:issue_created``, ``jira:issue_updated``.

Bernstein → Jira sync
---------------------
Configure a post-task hook or call the task-update endpoint directly::

    POST /bernstein/task-update
    {
        "task_id": "abc123",
        "status": "done",
        "external_ref": "jira:PROJ-42"
    }
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from bernstein_sdk.adapters.jira import JiraAdapter, JiraIssueRef
from bernstein_sdk.models import TaskStatus
from bernstein_sdk.state_map import BernsteinToJira

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class _Config:
    """Runtime configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.jira_base_url: str = os.environ.get("JIRA_BASE_URL", "")
        self.jira_email: str = os.environ.get("JIRA_EMAIL", "")
        self.jira_api_token: str = os.environ.get("JIRA_API_TOKEN", "")
        self.bernstein_url: str = os.environ.get(
            "BERNSTEIN_URL", "http://127.0.0.1:8052"
        )
        self.webhook_secret: str = os.environ.get("JIRA_WEBHOOK_SECRET", "")
        self.default_role: str = os.environ.get("JIRA_DEFAULT_ROLE", "backend")

        project_raw = os.environ.get("JIRA_PROJECT_FILTER", "")
        self.project_filter: frozenset[str] = frozenset(
            k.strip().upper() for k in project_raw.split(",") if k.strip()
        )

        label_raw = os.environ.get("JIRA_LABEL_FILTER", "")
        self.label_filter: frozenset[str] = frozenset(
            lbl.strip().lower() for lbl in label_raw.split(",") if lbl.strip()
        )

    def validate(self) -> None:
        missing = [
            name
            for name, val in [
                ("JIRA_BASE_URL", self.jira_base_url),
                ("JIRA_EMAIL", self.jira_email),
                ("JIRA_API_TOKEN", self.jira_api_token),
            ]
            if not val
        ]
        if missing:
            raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


_cfg = _Config()


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[type-arg]
    _cfg.validate()
    yield


app = FastAPI(
    title="Bernstein — Jira Webhook",
    description="Bridge Jira issues to Bernstein tasks and sync status back.",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_adapter() -> JiraAdapter:
    return JiraAdapter(
        base_url=_cfg.jira_base_url,
        email=_cfg.jira_email,
        api_token=_cfg.jira_api_token,
        default_role=_cfg.default_role,
    )


def _verify_secret(provided: str) -> None:
    """Reject the request if the provided token does not match the configured secret.

    Uses :func:`hmac.compare_digest` to prevent timing attacks.
    No-ops when ``JIRA_WEBHOOK_SECRET`` is not configured.
    """
    if not _cfg.webhook_secret:
        return
    if not hmac.compare_digest(provided.encode(), _cfg.webhook_secret.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


def _matches_filter(issue: JiraIssueRef) -> bool:
    """Return True if the issue passes the configured project/label filters.

    If no filter is configured the issue always passes.
    Both filters are combined with AND: the issue must satisfy both.
    """
    if _cfg.project_filter:
        project_key = issue.key.rsplit("-", 1)[0].upper() if "-" in issue.key else ""
        if project_key not in _cfg.project_filter:
            return False

    if _cfg.label_filter:
        issue_labels = {lbl.lower() for lbl in issue.labels}
        if not (issue_labels & _cfg.label_filter):
            return False

    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/jira/webhook")
async def receive_jira_webhook(
    request: Request,
    secret: str = Query(default="", description="Shared secret (URL param)"),
    authorization: str = Header(default=""),
) -> JSONResponse:
    """Receive Jira issue events and create Bernstein tasks.

    Jira sends ``jira:issue_created`` and ``jira:issue_updated`` events.
    Issues that don't match the configured project/label filter are silently
    ignored (HTTP 200) so Jira does not disable the webhook.

    Authentication: supply the shared secret as ``?secret=<token>`` in the
    webhook URL, or as ``Authorization: Bearer <token>`` header.
    """
    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    bearer_token = ""
    if authorization.lower().startswith("bearer "):
        bearer_token = authorization[len("bearer ") :]
    token = bearer_token or secret
    _verify_secret(token)

    # ------------------------------------------------------------------
    # Parse payload
    # ------------------------------------------------------------------
    payload: dict[str, Any] = await request.json()
    webhook_event: str = payload.get("webhookEvent", "")

    if webhook_event not in ("jira:issue_created", "jira:issue_updated"):
        log.debug("Ignoring unsupported webhook event: %s", webhook_event)
        return JSONResponse({"status": "ignored", "reason": "unsupported_event"})

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------
    issue_ref = JiraIssueRef.from_webhook_payload(payload)
    if issue_ref is None:
        log.debug("No issue in payload, ignoring")
        return JSONResponse({"status": "ignored", "reason": "no_issue_in_payload"})

    if not _matches_filter(issue_ref):
        log.debug(
            "Issue %s filtered out (project_filter=%s, label_filter=%s)",
            issue_ref.key,
            _cfg.project_filter,
            _cfg.label_filter,
        )
        return JSONResponse({"status": "ignored", "reason": "filtered"})

    # ------------------------------------------------------------------
    # Convert to Bernstein task
    # ------------------------------------------------------------------
    adapter = _get_adapter()
    task_create = adapter.task_from_webhook(payload)
    if task_create is None:
        # Terminal issue (Done / Cancelled) — nothing to do
        log.debug("Issue %s is terminal, skipping task creation", issue_ref.key)
        return JSONResponse({"status": "ignored", "reason": "terminal_issue"})

    # ------------------------------------------------------------------
    # Submit to Bernstein task server
    # ------------------------------------------------------------------
    async with httpx.AsyncClient(base_url=_cfg.bernstein_url) as http_client:
        resp = await http_client.post(
            "/tasks",
            json=task_create.to_api_payload(),
            timeout=10.0,
        )
        resp.raise_for_status()
        created: dict[str, Any] = resp.json()

    task_id: str = created.get("id", "")
    log.info(
        "Created Bernstein task %s from Jira %s (%s)",
        task_id,
        issue_ref.key,
        webhook_event,
    )
    return JSONResponse(
        {"status": "created", "task_id": task_id, "jira_key": issue_ref.key},
        status_code=status.HTTP_201_CREATED,
    )


@app.post("/bernstein/task-update")
async def receive_task_update(request: Request) -> JSONResponse:
    """Sync a Bernstein task status change back to Jira.

    Expected body::

        {
            "task_id": "<bernstein-task-id>",
            "status": "<done|failed|in_progress|...>",
            "external_ref": "jira:PROJ-42"
        }

    The handler transitions the linked Jira issue using the REST API v3.
    If ``external_ref`` is not a Jira ref the request is silently ignored.
    """
    payload: dict[str, Any] = await request.json()
    task_id: str = payload.get("task_id", "")
    task_status_raw: str = payload.get("status", "")
    external_ref: str = payload.get("external_ref", "")

    if not external_ref.startswith("jira:"):
        return JSONResponse({"status": "ignored", "reason": "not_a_jira_task"})

    issue_key = external_ref[len("jira:") :]

    try:
        bernstein_status = TaskStatus(task_status_raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown task status: {task_status_raw!r}",
        )

    target_jira_status = BernsteinToJira.map(bernstein_status)
    adapter = _get_adapter()

    transitioned = adapter.transition_issue(issue_key, target_jira_status)

    log.info(
        "Synced task %s (status=%s) → Jira %s target=%r: %s",
        task_id,
        task_status_raw,
        issue_key,
        target_jira_status,
        "transitioned" if transitioned else "no_matching_transition",
    )
    return JSONResponse(
        {
            "status": "synced" if transitioned else "no_transition",
            "jira_key": issue_key,
            "target_jira_status": target_jira_status,
        }
    )
