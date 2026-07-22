"""Agent-posted task artifact routes for the task server (#2553).

``POST /tasks/{task_id}/artifacts`` stores one typed artifact content-addressed
in the evidence store, seals it into the lineage spine, appends an
``artifact_posted`` row to the task's Merkle-chained journal, and mirrors the
record into the audit chain -- the response IS the chain-anchored receipt.
``GET /tasks/{task_id}/artifacts`` lists every version with its verification
state (a tampered blob renders as tampered, never as content), and
``GET /tasks/{task_id}/progress`` returns the chain-computed progress vector.

Posting is claim-scoped: a caller may attach an artifact only to a task whose
claim it holds. A refusal is typed and audit-recorded so an operator can prove,
from the chain alone, that isolation held.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from bernstein.core.evidence.run_artifacts import (
    ArtifactPayload,
    ArtifactTooLargeError,
    ArtifactValidationError,
    RunArtifactRecordDict,
    post_run_artifact,
    read_artifact_rows,
    verify_run_artifacts,
)
from bernstein.core.routes.task_crud import _get_sse_bus, _get_store, _require_task_access
from bernstein.core.security.sanitize import sanitize_log
from bernstein.core.server import (
    TaskArtifactContentResponse,
    TaskArtifactPost,
    TaskProgressResponse,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

router = APIRouter()

_ARTIFACT_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "Caller does not hold the task's claim"},
    404: {"description": "Task not found"},
    413: {"description": "Artifact payload exceeds the per-blob cap"},
    422: {"description": "Invalid artifact payload"},
}


def _sdd_dir(request: Request) -> Path:
    return request.app.state.sdd_dir  # type: ignore[no-any-return]


def _hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _audit_chain(request: Request) -> AuditChainStore | None:
    return getattr(request.app.state, "audit_chain", None)


#: Request header carrying the calling worker's identity, injected by the auth
#: layer (mirrors the signed-identity transport convention used elsewhere, e.g.
#: ``x-bernstein-telemetry-agent-id``). The authorization principal is read from
#: here / request state, never from the request body, which a caller controls.
_AGENT_ID_HEADER = "x-bernstein-agent-id"


def _claim_holder(task: Task) -> str:
    return str(getattr(task, "claimed_by_session", None) or getattr(task, "assigned_agent", None) or "")


def _caller_identity(request: Request) -> str:
    """Return the authenticated caller identity for the request.

    Read from the request's authenticated context -- state populated by the auth
    layer, or the identity transport header -- never from the request body. An
    empty string means the caller presented no identity.
    """
    state_value = getattr(request.state, "agent_id", None) or getattr(request.state, "principal", None)
    if isinstance(state_value, str) and state_value.strip():
        return state_value.strip()
    return request.headers.get(_AGENT_ID_HEADER, "").strip()


def _refuse_artifact_post(task: Task, caller: str, request: Request, reason: str) -> None:
    """Audit-record a refusal and raise the typed 403."""
    chain = _audit_chain(request)
    if chain is not None:
        try:
            from bernstein.core.security.audit_chain import record_run_artifact_refused

            record_run_artifact_refused(
                chain=chain,
                task_id=task.id,
                key="",
                caller=caller,
                reason=reason,
            )
        except Exception as exc:  # intentional-broad-except: refusal mirror is best-effort
            logger.warning("task.artifact refusal mirror failed: %s", type(exc).__name__)
    logger.info(
        "task.artifact refused: task_id=%s caller=%s reason=%s",
        sanitize_log(task.id),
        sanitize_log(caller or "<none>"),
        reason,
    )
    raise HTTPException(
        status_code=403,
        detail=f"caller does not hold an active claim for task '{task.id}'",
    )


def _require_claim_holder(task: Task, request: Request) -> str:
    """Authorize the caller as the task's active claim holder; return its identity.

    The caller identity comes from the authenticated request context, never the
    request body. Posting is refused -- typed 403, audit-recorded -- when the
    task has no active claim (nobody to hold it), when the caller presents no
    identity, or when the caller is not the recorded claim holder.
    """
    holder = _claim_holder(task)
    caller = _caller_identity(request)
    if not holder:
        _refuse_artifact_post(task, caller, request, "unclaimed_task")
    if not caller or caller != holder:
        _refuse_artifact_post(task, caller, request, "claim_scope_violation")
    return caller


def _build_payload(body: TaskArtifactPost) -> ArtifactPayload:
    if body.artifact_type == "report":
        return ArtifactPayload.report(body.body)
    if body.artifact_type == "table":
        return ArtifactPayload.table(body.columns, body.rows)
    return ArtifactPayload.link(body.url, body.link_kind)


@router.post("/tasks/{task_id}/artifacts", status_code=201, responses=_ARTIFACT_RESPONSES)
def post_task_artifact(task_id: str, body: TaskArtifactPost, request: Request) -> TaskArtifactContentResponse:
    """Post one journal-anchored artifact against a task the caller holds."""
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    # The recorded actor is the authenticated claim holder, not a body field.
    caller = _require_claim_holder(task, request)

    try:
        payload = _build_payload(body)
    except ArtifactValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    try:
        record = post_run_artifact(
            sdd_dir=_sdd_dir(request),
            task_id=task_id,
            key=body.key,
            payload=payload,
            actor=caller,
            hmac_key=_hmac_key(),
            audit_chain=_audit_chain(request),
        )
    except ArtifactTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except ArtifactValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    # Deliver live so the task detail screen updates without a reload (AC1).
    _get_sse_bus(request).publish(
        "task.artifact",
        json.dumps(
            {
                "task_id": task_id,
                "key": record.key,
                "artifact_type": record.artifact_type,
                "version": record.version,
                "content_hash": record.content_hash,
                "spine_entry_hash": record.spine_entry_hash,
                "journal_index": record.journal_index,
            }
        ),
    )
    _publish_progress(request, task_id)
    logger.info(
        "task.artifact posted: task_id=%s key=%s type=%s version=%d",
        sanitize_log(task_id),
        sanitize_log(record.key),
        sanitize_log(record.artifact_type),
        record.version,
    )
    response = _artifact_to_response(request, task_id, record.to_dict(), verified=True, reason="")
    return response


@router.get("/tasks/{task_id}/artifacts", responses={404: {"description": "Task not found"}})
def list_task_artifacts(task_id: str, request: Request) -> list[TaskArtifactContentResponse]:
    """List every posted artifact version with its verification state."""
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    return build_artifact_views(_sdd_dir(request), task_id)


@router.get("/tasks/{task_id}/progress", responses={404: {"description": "Task not found"}})
def get_task_progress(task_id: str, request: Request) -> TaskProgressResponse:
    """Return the chain-computed progress vector for a task.

    The ledger read is resolved from the task's own authoritative run id, never
    from a client-supplied parameter, so the vector cannot be steered by pairing
    this task's journal with an arbitrary run's ledger.
    """
    task = _get_store(request).get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    return build_progress_response(_sdd_dir(request), task_id, run_id=_task_run_id_for(task))


def _task_run_id_for(task: Task) -> str:
    """Resolve a task's authoritative orchestration run id, or ``""``."""
    return str(getattr(task, "run_id", "") or "")


# ---------------------------------------------------------------------------
# Shared projection helpers (also used by the dashboard task detail route)
# ---------------------------------------------------------------------------


def build_artifact_views(sdd_dir: Path, task_id: str) -> list[TaskArtifactContentResponse]:
    """Return every artifact version as a response model, verification-checked.

    A version whose stored blob fails its hash check is marked
    ``verified=False`` with ``content=None`` -- the surface must render it as
    tampered, never as content.
    """
    records = read_artifact_rows(sdd_dir, task_id)
    if not records:
        return []
    verify_by_pos = {(r.key, r.version): r for r in verify_run_artifacts(sdd_dir, task_id, hmac_key=_hmac_key())}
    views: list[TaskArtifactContentResponse] = []
    for record in records:
        verdict = verify_by_pos.get((record.key, record.version))
        ok = verdict.ok if verdict is not None else False
        reason = verdict.reason if verdict is not None else "no verification result"
        views.append(
            _artifact_to_response(None, task_id, record.to_dict(), verified=ok, reason=reason, sdd_dir=sdd_dir)
        )
    return views


def build_progress_response(sdd_dir: Path, task_id: str, *, run_id: str = "") -> TaskProgressResponse:
    """Project a task's progress vector into its response model."""
    from bernstein.core.replay.progress import project_task_progress

    wire = project_task_progress(sdd_dir, task_id, run_id=run_id).to_wire()
    return TaskProgressResponse(**wire)


def _artifact_to_response(
    request: Request | None,
    task_id: str,
    record: RunArtifactRecordDict,
    *,
    verified: bool,
    reason: str,
    sdd_dir: Path | None = None,
) -> TaskArtifactContentResponse:
    content = None
    if verified:
        content = _decode_content(request, task_id, str(record["content_hash"]), sdd_dir=sdd_dir)
        if content is None:
            verified = False
            reason = reason or "stored blob could not be decoded"
    return TaskArtifactContentResponse(
        task_id=str(record["task_id"]),
        key=str(record["key"]),
        artifact_type=str(record["artifact_type"]),
        content_hash=str(record["content_hash"]),
        version=int(record["version"]),
        prev_version_hash=str(record["prev_version_hash"]),
        spine_entry_hash=str(record["spine_entry_hash"]),
        journal_index=int(record["journal_index"]),
        journal_event_hash=str(record["journal_event_hash"]),
        link_kind=str(record.get("link_kind", "")),
        size=int(record.get("size", 0)),
        verified=verified,
        verify_reason=reason,
        content=content,
    )


def _decode_content(
    request: Request | None, task_id: str, content_hash: str, *, sdd_dir: Path | None = None
) -> dict[str, Any] | None:
    """Return the decoded artifact content, or None when it fails the hash check."""
    from bernstein.core.evidence.bundle import EvidenceStore
    from bernstein.core.lineage.spine import content_hash_of

    root = sdd_dir if sdd_dir is not None else _sdd_dir(request)  # type: ignore[arg-type]
    blob = EvidenceStore(root / "evidence").get(content_hash)
    if blob is None or content_hash_of(blob) != content_hash:
        return None
    try:
        decoded = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _publish_progress(request: Request, task_id: str) -> None:
    """Publish the recomputed progress vector over SSE (best-effort)."""
    try:
        vector = build_progress_response(_sdd_dir(request), task_id).model_dump()
    except Exception as exc:  # intentional-broad-except: progress delivery never blocks a post
        logger.debug("task.progress projection failed: %s", type(exc).__name__)
        return
    _get_sse_bus(request).publish("task.progress", json.dumps(vector))
