"""Approval routes — list, approve, and reject pending approval requests.

Provides a TUI-friendly API over the file-based approval gate handshake:
- Lists pending approvals from ``.sdd/runtime/pending_approvals/``
- Approves or rejects by writing decision files to ``.sdd/runtime/approvals/``
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approvals", tags=["approvals"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PendingApproval(BaseModel):
    """A single pending approval request."""

    task_id: str
    task_title: str
    session_id: str
    diff: str = ""
    test_summary: str = ""


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


def _validate_task_id(task_id: str) -> None:
    """Raise 400 if task_id contains unexpected characters."""
    if not _TASK_ID_RE.fullmatch(task_id):
        raise HTTPException(status_code=400, detail="Invalid task_id format")


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
    resolved = (base / filename).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise HTTPException(status_code=400, detail="Invalid task_id")
    return resolved


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
    except Exception as exc:
        logger.warning("Failed to parse pending approval %s: %s", filepath.name, exc)
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_approvals() -> ListApprovalsResponse:
    """List all pending approval requests."""
    pending_dir = _pending_dir()
    if not pending_dir.exists():
        return ListApprovalsResponse(pending=[])

    results: list[PendingApproval] = []
    for fpath in sorted(pending_dir.glob("*.json")):
        approval = _load_pending(fpath)
        if approval is not None:
            results.append(approval)

    return ListApprovalsResponse(pending=results)


@router.post(
    "/{task_id}/approve",
    responses={400: {"description": "Invalid task_id format"}, 404: {"description": "No pending approval for task"}},
)
async def approve_task(task_id: str, body: ApprovalDecisionRequest) -> dict[str, str]:
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
        raise HTTPException(status_code=400, detail="Invalid task_id format")
    safe_id = Path(task_id).name  # Strip any directory components
    approvals_dir = _approvals_dir()
    pending_path = _safe_child(_pending_dir(), f"{safe_id}.json")
    approved_path = _safe_child(approvals_dir, f"{safe_id}.approved")

    if not pending_path.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    approved_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending file
    pending_path.unlink(missing_ok=True)

    logger.info("Approval routes: task %r approved via TUI/API", task_id.replace("\n", "\\n").replace("\r", "\\r"))
    return {"status": "approved", "task_id": task_id}


@router.post(
    "/{task_id}/reject",
    responses={400: {"description": "Invalid task_id format"}, 404: {"description": "No pending approval for task"}},
)
async def reject_task(task_id: str, body: ApprovalDecisionRequest) -> dict[str, str]:
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
        raise HTTPException(status_code=400, detail="Invalid task_id format")
    safe_id = Path(task_id).name  # Strip any directory components
    approvals_dir = _approvals_dir()
    pending_path = _safe_child(_pending_dir(), f"{safe_id}.json")
    rejected_path = _safe_child(approvals_dir, f"{safe_id}.rejected")

    if not pending_path.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    rejected_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending file
    pending_path.unlink(missing_ok=True)

    logger.info("Approval routes: task %r rejected via TUI/API", task_id.replace("\n", "\\n").replace("\r", "\\r"))
    return {"status": "rejected", "task_id": task_id}
