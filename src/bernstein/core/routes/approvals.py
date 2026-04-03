"""Approval routes — list, approve, and reject pending approval requests.

Provides a TUI-friendly API over the file-based approval gate handshake:
- Lists pending approvals from ``.sdd/runtime/pending_approvals/``
- Approves or rejects by writing decision files to ``.sdd/runtime/approvals/``
"""

from __future__ import annotations

import json
import logging
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


def _pending_dir() -> Path:
    """Return the pending approvals directory, creating it if needed."""
    return _PENDING_DIR


def _approvals_dir() -> Path:
    """Return the approvals (decision) directory, creating it if needed."""
    _APPROVALS_DIR.mkdir(parents=True, exist_ok=True)
    return _APPROVALS_DIR


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


@router.post("/{task_id}/approve")
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
    approvals_dir = _approvals_dir()
    pending_path = _pending_dir() / f"{task_id}.json"
    approved_path = approvals_dir / f"{task_id}.approved"

    if not pending_path.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    approved_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending file
    pending_path.unlink(missing_ok=True)

    logger.info("Approval routes: task %s approved via TUI/API", task_id)
    return {"status": "approved", "task_id": task_id}


@router.post("/{task_id}/reject")
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
    approvals_dir = _approvals_dir()
    pending_path = _pending_dir() / f"{task_id}.json"
    rejected_path = approvals_dir / f"{task_id}.rejected"

    if not pending_path.exists():
        raise HTTPException(status_code=404, detail=f"No pending approval for task {task_id}")

    # Write decision
    rejected_path.write_text(json.dumps({"reason": body.reason}, indent=2))

    # Remove pending file
    pending_path.unlink(missing_ok=True)

    logger.info("Approval routes: task %s rejected via TUI/API", task_id)
    return {"status": "rejected", "task_id": task_id}
