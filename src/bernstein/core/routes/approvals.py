"""Approval routes - list, approve, and reject pending approval requests.

Provides a TUI-friendly API over the file-based approval gate handshake:
- Lists pending approvals from ``.sdd/runtime/pending_approvals/``
- Approves or rejects by writing decision files to ``.sdd/runtime/approvals/``

op-002 adds the interactive tool-call endpoints:
- ``GET /approvals?session_id=...`` - list pending tool-call approvals.
- ``POST /approvals/{id}/resolve`` - record an ``allow|reject|always``
  decision for a specific approval id.
- ``GET /approvals/live-fragment?session_id=...`` - HTML fragment that
  the live-session page embeds so operators can resolve approvals from
  the web UI.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from bernstein.core.approval.models import (
    ApprovalDecision,
    ApprovalNonceExpired,
    ApprovalNonceMismatch,
)
from bernstein.core.approval.models import PendingApproval as QueuedApproval
from bernstein.core.approval.queue import get_default_queue, promote_to_always_allow
from bernstein.core.sanitize import sanitize_log
from bernstein.core.security.path_containment import (
    PathContainmentError,
    contained_path,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PendingApproval(BaseModel):
    """A single pending approval request (task-level or pre-spawn)."""

    task_id: str
    task_title: str
    session_id: str
    diff: str = ""
    test_summary: str = ""
    mechanism: str = "task_review"
    prompt: str = ""
    default_action: str = ""
    timeout_at_iso: str = ""
    created_iso: str = ""
    timeout_seconds: int | None = None
    unblocks: str = "task completion and merge"
    resolution_endpoint: str = ""


class ListApprovalsResponse(BaseModel):
    """Response for GET /approvals."""

    pending: list[PendingApproval]


class ApprovalDecisionRequest(BaseModel):
    """Body for POST /approvals/{task_id}/approve or /reject."""

    reason: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PENDING_DIR = Path(".sdd/runtime/pending_approvals")
_APPROVALS_DIR = Path(".sdd/runtime/approvals")

_TASK_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

#: Error detail returned when a task_id fails validation.
_INVALID_TASK_ID_MSG = "Invalid task_id format"


def _validate_task_id(task_id: str) -> None:
    """Raise 400 if task_id contains unexpected characters."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=400, detail=_INVALID_TASK_ID_MSG)


def _pending_dir() -> Path:
    """Return the pending approvals directory, creating it if needed."""
    return _PENDING_DIR


def _approvals_dir() -> Path:
    """Return the approvals (decision) directory, creating it if needed."""
    _APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    return _APPROVALS_DIR


def _safe_child(base: Path, filename: str) -> Path:
    """Resolve *filename* under *base* and verify it stays within *base*.

    Raises HTTPException(400) if the resolved path escapes the directory.
    """
    try:
        return contained_path(base, filename, label="task_id")
    except PathContainmentError as exc:
        # The API's own wording is kept so the response shape is unchanged;
        # the barrier's message omits the base, and so must this one.
        raise HTTPException(status_code=400, detail="Invalid task_id") from exc


def _load_pending(filepath: Path) -> PendingApproval | None:
    """Parse a pending approval JSON file.

    Args:
        filepath: Path to a .json file in the pending directory.

    Returns:
        Parsed PendingApproval, or None on failure.
    """
    try:
        data = json.loads(filepath.read_text())
        return PendingApproval(**data)
    # bot-ack: pre-existing-1723 (pydantic + json may raise diverse errors)
    except Exception as exc:
        logger.warning("Failed to parse pending approval %s: %s", filepath.name, exc)
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
def list_approvals() -> ListApprovalsResponse:
    """List all pending approval requests across task-review and pre-spawn gates."""
    results: list[PendingApproval] = []

    # 1. Task-level review pending entries (.sdd/runtime/pending_approvals/*.json)
    pending_dir = _pending_dir()
    if pending_dir.exists():
        for fpath in sorted(pending_dir.glob("*.json")):
            approval = _load_pending(fpath)
            if approval is not None:
                approval.mechanism = "task_review"
                approval.unblocks = "task completion and merge"
                approval.resolution_endpoint = f"/approvals/{approval.task_id}/approve"
                results.append(approval)

    # 2. Pre-spawn gate pending entries (.sdd/runtime/approvals/*.pending)
    approvals_dir = _approvals_dir()
    if approvals_dir.exists():
        for sentinel in sorted(approvals_dir.glob("*.pending")):
            try:
                data = json.loads(sentinel.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("approval routes: skipping unreadable sentinel %s: %s", sentinel.name, exc)
                continue
            if not isinstance(data, dict):
                continue
            task_id = str(data.get("task_id") or sentinel.stem)
            prompt = str(data.get("prompt", ""))
            timeout_seconds_raw = data.get("timeout_seconds")
            timeout_seconds = int(timeout_seconds_raw) if isinstance(timeout_seconds_raw, (int, float)) else None
            pre_spawn_approval = PendingApproval(
                task_id=task_id,
                task_title=prompt or task_id,
                session_id=str(data.get("session_id", "")),
                diff="",
                test_summary="",
                mechanism="pre_spawn",
                prompt=prompt,
                default_action=str(data.get("default_action", "reject")),
                timeout_at_iso=str(data.get("timeout_at_iso", "")),
                created_iso=str(data.get("created_iso", "")),
                timeout_seconds=timeout_seconds,
                unblocks="agent spawn and task execution",
                resolution_endpoint=f"/approvals/{task_id}/approve",
            )
            results.append(pre_spawn_approval)

    return ListApprovalsResponse(pending=results)


@router.post(
    "/{task_id}/approve",
    responses={400: {"description": _INVALID_TASK_ID_MSG}, 404: {"description": "No pending approval for task"}},
)
def approve_task(task_id: str, body: ApprovalDecisionRequest) -> dict[str, str]:
    """Approve a pending approval request.

    Writes a .approved decision file so the orchestrator poll loop unblocks.
    The pending file is then removed.

    Args:
        task_id: Task ID to approve.
        body: Optional reason metadata.

    Returns:
        Success message.
    """
    _validate_task_id(task_id)
    if ".." in task_id or "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=400, detail=_INVALID_TASK_ID_MSG)
    safe_id = Path(task_id).name  # Strip any directory components
    approvals_dir = _approvals_dir()
    pending_path = _safe_child(_pending_dir(), f"{safe_id}.json")
    pre_spawn_pending = _safe_child(approvals_dir, f"{safe_id}.pending")
    approved_path = _safe_child(approvals_dir, f"{safe_id}.approved")

    if not pending_path.exists() and not pre_spawn_pending.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    approved_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending files
    pending_path.unlink(missing_ok=True)
    pre_spawn_pending.unlink(missing_ok=True)

    logger.info("Approval routes: task %r approved via TUI/API", task_id.replace("\n", "\\n").replace("\r", "\\r"))
    return {"status": "approved", "task_id": task_id}


@router.post(
    "/{task_id}/reject",
    responses={400: {"description": _INVALID_TASK_ID_MSG}, 404: {"description": "No pending approval for task"}},
)
def reject_task(task_id: str, body: ApprovalDecisionRequest) -> dict[str, str]:
    """Reject a pending approval request.

    Writes a .rejected decision file so the orchestrator poll loop unblocks.
    The pending file is then removed.

    Args:
        task_id: Task ID to reject.
        body: Optional reason metadata.

    Returns:
        Success message.
    """
    _validate_task_id(task_id)
    if ".." in task_id or "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=400, detail=_INVALID_TASK_ID_MSG)
    safe_id = Path(task_id).name  # Strip any directory components
    approvals_dir = _approvals_dir()
    pending_path = _safe_child(_pending_dir(), f"{safe_id}.json")
    pre_spawn_pending = _safe_child(approvals_dir, f"{safe_id}.pending")
    rejected_path = _safe_child(approvals_dir, f"{safe_id}.rejected")

    if not pending_path.exists() and not pre_spawn_pending.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    rejected_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending files
    pending_path.unlink(missing_ok=True)
    pre_spawn_pending.unlink(missing_ok=True)

    logger.info("Approval routes: task %r rejected via TUI/API", task_id.replace("\n", "\\n").replace("\r", "\\r"))
    return {"status": "rejected", "task_id": task_id}


# ---------------------------------------------------------------------------
# op-002: interactive tool-call approval queue endpoints
# ---------------------------------------------------------------------------


class QueuedApprovalResponse(BaseModel):
    """One queued tool-call approval from the op-002 approval queue.

    The ``nonce`` field is the hex-encoded single-use token the reply
    must echo. It travels only over the human-channel surface (TUI,
    dashboard, chat bridge) and never reaches agent stdin or any
    rendered prompt template.
    """

    id: str
    session_id: str
    agent_role: str
    tool_name: str
    tool_args: dict[str, object]
    created_at: float
    ttl_seconds: int
    nonce: str


class QueuedApprovalsResponse(BaseModel):
    """Response envelope for ``GET /approvals/queue``."""

    pending: list[QueuedApprovalResponse]


class ResolveRequest(BaseModel):
    """Body for ``POST /approvals/{id}/resolve``.

    The reply must echo the exact ``nonce`` hex string issued when the
    approval was queued. ``nonce`` defaults to an empty string at the
    schema layer so a missing field flows through the handler as a
    nonce mismatch (``409 NONCE_MISMATCH``) rather than a Pydantic 422
    validation error, matching the documented contract.
    """

    decision: Literal["allow", "reject", "always"]
    nonce: str = ""
    reason: str = ""


def _to_response(approval: QueuedApproval) -> QueuedApprovalResponse:
    """Map a :class:`QueuedApproval` to its serialisable response form."""
    return QueuedApprovalResponse(
        id=approval.id,
        session_id=approval.session_id,
        agent_role=approval.agent_role,
        tool_name=approval.tool_name,
        tool_args=dict(approval.tool_args),
        created_at=approval.created_at,
        ttl_seconds=approval.ttl_seconds,
        nonce=approval.nonce_hex,
    )


@router.get("/queue")
def list_queued_approvals(session_id: str | None = None) -> QueuedApprovalsResponse:
    """List pending tool-call approvals (op-002).

    Args:
        session_id: Optional filter; when given only approvals for that
            session are returned.
    """
    queue = get_default_queue()
    return QueuedApprovalsResponse(pending=[_to_response(a) for a in queue.list_pending(session_id=session_id)])


@router.post(
    "/{approval_id}/resolve",
    responses={
        400: {"description": "Invalid approval id or decision"},
        404: {"description": "No pending approval with that id"},
        409: {"description": "NONCE_MISMATCH"},
        410: {"description": "NONCE_EXPIRED"},
    },
)
def resolve_queued_approval(approval_id: str, body: ResolveRequest) -> dict[str, str]:
    """Resolve a queued approval with ``allow``, ``reject``, or ``always``.

    The request body must echo the ``nonce`` the gate issued when the
    approval was queued. Mismatches return ``409 NONCE_MISMATCH``; a
    nonce replayed against an already-resolved or evicted approval
    returns ``410 NONCE_EXPIRED``.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", approval_id):
        raise HTTPException(status_code=400, detail="Invalid approval id format")
    queue = get_default_queue()
    approval = queue.get(approval_id)
    if approval is None:
        # An already-resolved approval with a supplied nonce is reported
        # as gone rather than missing so replay attempts are visible.
        if queue.get_resolution(approval_id) is not None:
            raise HTTPException(status_code=410, detail="NONCE_EXPIRED")
        raise HTTPException(status_code=404, detail=f"No pending approval {approval_id}")
    try:
        decision = ApprovalDecision(body.decision)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid decision: {body.decision}") from exc
    try:
        resolution = queue.resolve(approval_id, decision, reason=body.reason, nonce=body.nonce)
    except ApprovalNonceMismatch as exc:
        raise HTTPException(status_code=409, detail="NONCE_MISMATCH") from exc
    except ApprovalNonceExpired as exc:
        raise HTTPException(status_code=410, detail="NONCE_EXPIRED") from exc
    if decision is ApprovalDecision.ALWAYS:
        try:
            promote_to_always_allow(approval)
        except OSError as exc:
            logger.warning("Could not promote approval %s: %s", sanitize_log(approval_id), exc)
    return {"status": "resolved", "id": approval_id, "decision": resolution.decision.value}


@router.get("/live-fragment", response_class=HTMLResponse)
def approvals_live_fragment(session_id: str | None = None) -> HTMLResponse:
    """Return an HTML fragment the live-session page embeds.

    Each pending approval becomes a row with three buttons that POST the
    resolution back to ``/approvals/{id}/resolve``. The fragment is
    intentionally minimal so it can be inlined into the existing live
    dashboard without pulling a new framework.
    """
    pending = get_default_queue().list_pending(session_id=session_id)
    if not pending:
        return HTMLResponse(
            '<div class="approvals-empty">No pending approvals</div>',
            media_type="text/html",
        )
    rows: list[str] = []
    for approval in pending:
        args_json = html.escape(json.dumps(approval.tool_args))
        nonce_attr = html.escape(approval.nonce_hex)
        rows.append(
            f'<div class="approval-row" data-id="{html.escape(approval.id)}" data-nonce="{nonce_attr}">'
            f'<div class="approval-meta">'
            f'<span class="approval-tool">{html.escape(approval.tool_name)}</span> '
            f'<span class="approval-role">{html.escape(approval.agent_role)}</span> '
            f'<span class="approval-session">{html.escape(approval.session_id)}</span>'
            f"</div>"
            f'<pre class="approval-args">{args_json}</pre>'
            f'<div class="approval-actions">'
            f"<button onclick=\"resolveApproval('{html.escape(approval.id)}', 'allow')\">Approve</button>"
            f"<button onclick=\"resolveApproval('{html.escape(approval.id)}', 'reject')\">Reject</button>"
            f"<button onclick=\"resolveApproval('{html.escape(approval.id)}', 'always')\">Always</button>"
            f"</div></div>"
        )
    script = (
        "<script>"
        "function resolveApproval(id, decision){"
        "var el=document.querySelector('[data-id=\"'+id+'\"]');"
        "var nonce=el?el.getAttribute('data-nonce'):'';"
        "fetch('/approvals/'+encodeURIComponent(id)+'/resolve',"
        "{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({decision:decision,nonce:nonce})})"
        ".then(function(r){if(r.ok && el){el.remove();}});}"
        "</script>"
    )
    body = f'<section id="approvals-panel"><h3>Pending approvals</h3>{"".join(rows)}{script}</section>'
    return HTMLResponse(body, media_type="text/html")
