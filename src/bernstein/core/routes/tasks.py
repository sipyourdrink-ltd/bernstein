"""Task CRUD routes, agent heartbeats, bulletin board, A2A, cluster, and session streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level
from bernstein.core.eu_ai_act import (
    TaskRiskAssessment,
    append_assessment_log,
    assess_task,
    build_log_record,
    merge_bernstein_risk,
    merge_eu_ai_act_risk,
)
from bernstein.core.lifecycle import IllegalTransitionError
from bernstein.core.models import NodeCapacity, NodeInfo, NodeStatus
from bernstein.core.role_classifier import classify_role

# Import Pydantic models from server — this works because server.py's
# __getattr__ defers the `app` creation, so the module body (class defs)
# loads without triggering create_app().
from bernstein.core.server import (
    A2AAgentCardResponse,
    A2AArtifactRequest,
    A2AArtifactResponse,
    A2AMessageRequest,
    A2AMessageResponse,
    A2ATaskResponse,
    A2ATaskSendRequest,
    BatchClaimRequest,
    BatchClaimResponse,
    BatchCreateRequest,
    BatchCreateResponse,
    BulletinMessageResponse,
    BulletinPostRequest,
    ClusterStatusResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    NodeResponse,
    PaginatedTasksResponse,
    SSEBus,
    TaskBlockRequest,
    TaskCancelRequest,
    TaskCompleteRequest,
    TaskCountsResponse,
    TaskCreate,
    TaskFailRequest,
    TaskPatchRequest,
    TaskProgressRequest,
    TaskResponse,
    TaskSelfCreate,
    TaskStealAction,
    TaskStealRequest,
    TaskStealResponse,
    TaskStore,
    TaskWaitForSubtasksRequest,
    a2a_message_to_response,
    a2a_task_to_response,
    node_to_response,
    read_log_tail,
    task_to_response,
)
from bernstein.core.task_store import ArchiveRecord, SnapshotEntry
from bernstein.core.telemetry import start_span
from bernstein.core.tenanting import request_tenant_id, resolve_tenant_scope
from bernstein.plugins.manager import HookBlockingError, get_plugin_manager

logger = logging.getLogger(__name__)

_DRAINING_DETAIL = "Server is draining -- no new claims accepted"

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from bernstein.core.a2a import A2AHandler
    from bernstein.core.cluster import NodeRegistry
    from bernstein.core.models import Task
    from bernstein.core.tenanting import TenantRegistry

router = APIRouter()


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


def _get_sse_bus(request: Request) -> SSEBus:
    return request.app.state.sse_bus  # type: ignore[no-any-return]


def _get_bulletin(request: Request) -> BulletinBoard:
    return request.app.state.bulletin  # type: ignore[no-any-return]


def _get_a2a_handler(request: Request) -> A2AHandler:
    return request.app.state.a2a_handler  # type: ignore[no-any-return]


def _get_node_registry(request: Request) -> NodeRegistry:
    return request.app.state.node_registry  # type: ignore[no-any-return]


def _verify_cluster_auth(request: Request, required_scope: str) -> None:
    """Verify cluster JWT authentication if a ClusterAuthenticator is configured.

    Raises HTTPException 401 on auth failure.
    """
    from bernstein.core.cluster_auth import (
        ClusterAuthenticator,
        ClusterAuthError,
    )

    authenticator: ClusterAuthenticator | None = getattr(request.app.state, "cluster_authenticator", None)
    if authenticator is None or not authenticator.require_auth:
        return
    try:
        authenticator.verify_request(request.headers.get("Authorization"), required_scope)
    except ClusterAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _get_runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


def _get_gate_report_path(request: Request, task_id: str) -> Path:
    return _get_runtime_dir(request) / "gates" / f"{task_id}.json"


def _get_tenant_registry(request: Request) -> TenantRegistry | None:
    registry = getattr(request.app.state, "tenant_registry", None)
    return registry if registry is not None else None


def _resolve_request_tenant_scope(request: Request, requested_tenant: str | None = None) -> str:
    """Resolve the tenant scope for the current request."""

    try:
        return resolve_tenant_scope(
            request_tenant_id(request),
            requested_tenant,
            registry=_get_tenant_registry(request),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_task_access(task: Task, request: Request, requested_tenant: str | None = None) -> None:
    """Reject access to a task outside the current tenant scope."""

    effective_tenant = _resolve_request_tenant_scope(request, requested_tenant)
    if task.tenant_id != effective_tenant:
        raise HTTPException(status_code=404, detail=f"Task '{task.id}' not found")


# ---------------------------------------------------------------------------
# Real-time behavior monitor helper
# ---------------------------------------------------------------------------


def _get_realtime_monitor(request: Request) -> object | None:
    """Return the ``RealtimeBehaviorMonitor`` from app state, if present."""
    return getattr(request.app.state, "realtime_behavior_monitor", None)


def _evict_realtime_session(request: Request, session_id: str | None) -> None:
    """Remove session state from the real-time monitor after task completion."""
    if not session_id:
        return
    monitor = _get_realtime_monitor(request)
    if monitor is None:
        return
    try:
        from bernstein.core.behavior_anomaly import RealtimeBehaviorMonitor

        if isinstance(monitor, RealtimeBehaviorMonitor):
            monitor.evict_session(session_id)
    except Exception:
        pass


def _try_check_realtime_anomaly(
    request: Request,
    task_id: str,
    session_id: str | None,
    *,
    files_changed: int,
    last_file: str,
    message: str,
) -> None:
    """Run real-time anomaly detection on a progress update (best-effort).

    Writes a kill-signal file automatically when KILL_AGENT severity is
    detected; logs warnings for lower-severity signals.  Non-blocking —
    any exception is caught and logged so the progress route always succeeds.
    """
    if not session_id:
        return
    monitor = _get_realtime_monitor(request)
    if monitor is None:
        return
    try:
        from bernstein.core.behavior_anomaly import RealtimeBehaviorMonitor

        if not isinstance(monitor, RealtimeBehaviorMonitor):
            return
        signals = monitor.record_progress(
            session_id,
            task_id,
            files_changed=files_changed,
            last_file=last_file,
            message=message,
        )
        for signal in signals:
            logger.warning(
                "Realtime anomaly [%s] agent=%s task=%s: %s",
                signal.rule,
                signal.agent_id,
                signal.task_id,
                signal.message,
            )
    except Exception:
        from bernstein.core.sanitize import sanitize_log

        logger.debug("Realtime behavior check failed for task %s", sanitize_log(str(task_id)), exc_info=True)


# ---------------------------------------------------------------------------
# Sigstore attestation helper
# ---------------------------------------------------------------------------


def _try_attest_task_completion(
    request: Request,
    task_id: str,
    agent_id: str,
    result_summary: str,
) -> None:
    """Best-effort Sigstore/Ed25519 attestation for a completed task.

    Non-blocking — logs a warning and continues if attestation fails.
    """
    import hashlib

    sdd_dir: Path | None = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is None:
        return

    try:
        from bernstein.core.sigstore_attestation import (
            AttestationConfig,
            attest_task_completion,
        )

        diff_sha256 = hashlib.sha256(result_summary.encode()).hexdigest()
        attestation_dir = sdd_dir / "attestations"
        config = AttestationConfig(attestation_dir=attestation_dir)
        record = attest_task_completion(
            task_id=task_id,
            agent_id=agent_id,
            diff_sha256=diff_sha256,
            event_hmac="",
            config=config,
        )
        method = "Ed25519 fallback" if record.fallback_used else "Sigstore/Rekor"
        logger.info(
            "Task %s attested via %s: bundle=%s",
            task_id,
            method,
            record.bundle_path,
        )
    except Exception:
        logger.warning("Attestation failed for task %s (non-fatal)", task_id, exc_info=True)


def _try_generate_sbom(request: Request) -> None:
    """Best-effort SBOM generation triggered after task completion.

    Runs only when ``BERNSTEIN_SBOM_ON_COMPLETE=1`` is set in the environment
    or when ``request.app.state.sbom_on_complete`` is truthy.  Non-blocking —
    any exception is caught and logged so the task completion route always
    succeeds.

    Artifacts are written to ``<workdir>/.sdd/artifacts/sbom/``.
    """
    import os

    sbom_enabled = os.environ.get("BERNSTEIN_SBOM_ON_COMPLETE", "").strip() in ("1", "true", "yes")
    if not sbom_enabled:
        sbom_enabled = bool(getattr(request.app.state, "sbom_on_complete", False))
    if not sbom_enabled:
        return

    workdir: Path | None = getattr(request.app.state, "workdir", None)
    if workdir is None:
        return

    try:
        from bernstein.core.sbom import SBOMGenerator

        generator = SBOMGenerator(workdir)
        sbom = generator.generate(source="pip")
        artifact_path = generator.save(sbom)
        logger.info(
            "SBOM generated on task completion: %s (%d components)",
            artifact_path,
            len(sbom.components),
        )
    except Exception:
        logger.warning("SBOM generation failed (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/tasks",
    status_code=201,
    response_model=TaskResponse,
    responses={400: {"description": "Blocked by pre-create hook"}},
)
async def create_task(body: TaskCreate, request: Request) -> TaskResponse:
    """Create a new task."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    effective_body = body.model_copy(update={"tenant_id": request_tenant_id(request)})

    # Auto-classify role if not specified
    if effective_body.role == "auto":
        effective_body.role = classify_role(effective_body.description)

    # Auto-estimate difficulty if minutes not provided
    if effective_body.estimated_minutes is None:
        score = estimate_difficulty(effective_body.description)
        effective_body.estimated_minutes = minutes_for_level(score.level)

    assessment = assess_task(effective_body)
    effective_body = effective_body.model_copy(
        update={
            "eu_ai_act_risk": merge_eu_ai_act_risk(effective_body.eu_ai_act_risk, assessment.risk_level).value,
            "approval_required": bool(effective_body.approval_required or assessment.approval_required),
            "risk_level": merge_bernstein_risk(effective_body.risk_level, assessment.bernstein_risk_level),
        }
    )

    with start_span("task.create", {"task.role": effective_body.role, "task.title": effective_body.title}):
        # ENT-001: Tenant quota enforcement
        from bernstein.core.tenant_isolation import TenantIsolationManager  # noqa: TC001

        tenant_mgr: TenantIsolationManager | None = getattr(
            request.app.state,
            "tenant_isolation_manager",
            None,
        )
        if tenant_mgr is not None:
            effective_tenant = request_tenant_id(request)
            current_count = store.count_by_status(tenant_id=effective_tenant).get("total", 0)
            allowed, reason = tenant_mgr.check_quota(effective_tenant, current_count)
            if not allowed:
                raise HTTPException(status_code=429, detail=reason)

        # Pre-create hook: may block via HookBlockingError (T719)
        try:
            pm = get_plugin_manager()
            pm.fire_pre_task_create(
                task_id="",  # ID not yet assigned — use empty string
                role=effective_body.role,
                title=effective_body.title,
                description=effective_body.description,
            )
        except HookBlockingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        task = await store.create(effective_body)
        append_assessment_log(
            request.app.state.sdd_dir,
            build_log_record(task.id, task, assessment),
        )
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)
        return task_to_response(task)


@router.post(
    "/tasks/batch",
    status_code=201,
    response_model=BatchCreateResponse,
    responses={503: {"description": "Server is draining"}},
)
async def create_tasks_batch(body: BatchCreateRequest, request: Request) -> BatchCreateResponse:
    """Create multiple tasks atomically with title dedup."""
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)

    prepared: list[TaskCreate] = []
    assessments: list[TaskRiskAssessment] = []
    for task_body in body.tasks:
        effective = task_body.model_copy(update={"tenant_id": request_tenant_id(request)})

        # Auto-classify role if not specified
        if effective.role == "auto":
            effective.role = classify_role(effective.description)

        # Auto-estimate difficulty if minutes not provided
        if effective.estimated_minutes is None:
            score = estimate_difficulty(effective.description)
            effective.estimated_minutes = minutes_for_level(score.level)

        assessment = assess_task(effective)
        effective = effective.model_copy(
            update={
                "eu_ai_act_risk": merge_eu_ai_act_risk(effective.eu_ai_act_risk, assessment.risk_level).value,
                "approval_required": bool(effective.approval_required or assessment.approval_required),
                "risk_level": merge_bernstein_risk(effective.risk_level, assessment.bernstein_risk_level),
            }
        )

        # Pre-create hook: skip individual task if blocked (don't fail entire batch)
        try:
            pm = get_plugin_manager()
            pm.fire_pre_task_create(
                task_id="",
                role=effective.role,
                title=effective.title,
                description=effective.description,
            )
        except HookBlockingError:
            logger.warning("Pre-create hook blocked task '%s' — skipping", effective.title)
            continue

        prepared.append(effective)
        assessments.append(assessment)

    created_tasks, skipped_titles = await store.create_batch(prepared, dedup_by_title=True)

    # Build a title->assessment lookup for created tasks (dedup may have dropped some)
    assessment_by_title = dict(zip([t.title for t in prepared], assessments, strict=False))
    for task in created_tasks:
        task_assessment = assessment_by_title.get(task.title)
        if task_assessment is not None:
            append_assessment_log(
                request.app.state.sdd_dir,
                build_log_record(task.id, task, task_assessment),
            )
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)

    return BatchCreateResponse(
        created=[task_to_response(t) for t in created_tasks],
        skipped_titles=skipped_titles,
    )


@router.post(
    "/tasks/self-create",
    status_code=201,
    response_model=TaskResponse,
    responses={404: {"description": "Parent task not found"}},
)
async def self_create_subtask(body: TaskSelfCreate, request: Request) -> TaskResponse:
    """Create a subtask linked to a parent task.

    Agents call this to decompose work during execution.  The parent
    task is automatically transitioned to ``WAITING_FOR_SUBTASKS`` on
    the first subtask creation (if it is not already in that state).
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)

    # Validate parent exists
    parent = store.get_task(body.parent_task_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=f"Parent task '{body.parent_task_id}' not found")

    # Build a full TaskCreate from the self-create payload
    full_body = TaskCreate(
        title=body.title,
        description=body.description,
        role=body.role if body.role != "auto" else classify_role(body.description),
        priority=body.priority,
        scope=body.scope,
        complexity=body.complexity,
        estimated_minutes=body.estimated_minutes,
        depends_on=body.depends_on,
        parent_task_id=body.parent_task_id,
        owned_files=body.owned_files,
        tenant_id=request_tenant_id(request),
    )

    # Auto-estimate difficulty if minutes not provided
    if full_body.estimated_minutes is None:
        score = estimate_difficulty(full_body.description)
        full_body.estimated_minutes = minutes_for_level(score.level)

    with start_span("task.self_create", {"parent_task_id": body.parent_task_id}):
        task = await store.create(full_body)
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))

        # Auto-transition parent to waiting if not already
        if parent.status.value not in ("waiting_for_subtasks", "done", "failed", "closed"):
            subtask_count = sum(1 for t in store.list_tasks() if t.parent_task_id == body.parent_task_id)
            try:
                await store.wait_for_subtasks(body.parent_task_id, subtask_count)
                sse_bus.publish(
                    "task_update",
                    json.dumps({"id": parent.id, "status": "waiting_for_subtasks"}),
                )
            except Exception:
                pass  # Parent may already be waiting — that's fine

        get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)
        return task_to_response(task)


@router.get(
    "/tasks/next/{role}",
    response_model=TaskResponse,
    responses={404: {"description": "No open tasks for role"}, 503: {"description": "Server is draining"}},
)
async def next_task(
    role: str,
    request: Request,
    claimed_by_session: str | None = None,
    parent_session_id: str | None = None,
) -> TaskResponse:
    """Claim the next available task for *role*.

    Pass ``claimed_by_session`` as a query param to record which parent
    orchestrator session owns the claim.

    Pass ``parent_session_id`` to restrict claiming to tasks that were
    created under that coordinator session.  Workers belonging to a
    coordinator should always pass their coordinator's session ID here
    to avoid stealing tasks from other namespaces.
    """
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    store = _get_store(request)
    task = await store.claim_next(
        role,
        tenant_id=_resolve_request_tenant_scope(request),
        claimed_by_session=claimed_by_session,
        parent_session_id=parent_session_id,
    )
    if task is None:
        raise HTTPException(status_code=404, detail=f"No open tasks for role '{role}'")
    return task_to_response(task)


@router.post(
    "/tasks/claim-batch", response_model=BatchClaimResponse, responses={503: {"description": "Server is draining"}}
)
async def claim_batch(body: BatchClaimRequest, request: Request) -> BatchClaimResponse:
    """Atomically claim multiple tasks by ID for an agent."""
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    with start_span("task.claim_batch", {"agent_id": body.agent_id, "task_count": len(body.task_ids)}):
        store = _get_store(request)
        tenant_id = _resolve_request_tenant_scope(request)
        authorized_ids: list[str] = []
        unauthorized_ids: list[str] = []
        for task_id in body.task_ids:
            task = store.get_task(task_id)
            if task is None or task.tenant_id != tenant_id:
                unauthorized_ids.append(task_id)
                continue
            authorized_ids.append(task_id)
        claimed, failed = await store.claim_batch(
            authorized_ids,
            body.agent_id,
            claimed_by_session=body.claimed_by_session,
        )
        failed.extend(unauthorized_ids)
        return BatchClaimResponse(claimed=claimed, failed=failed)


@router.post(
    "/tasks/{task_id}/claim",
    response_model=TaskResponse,
    responses={
        404: {"description": "Task not found"},
        409: {"description": "Version conflict or invalid state"},
        503: {"description": "Server is draining"},
    },
)
async def claim_task(
    task_id: str,
    request: Request,
    expected_version: int | None = None,
    claimed_by_session: str | None = None,
) -> TaskResponse:
    """Claim a specific task by ID.

    Pass ``expected_version`` as a query param for optimistic locking
    (CAS). If the task's version doesn't match, returns 409 Conflict.

    Pass ``claimed_by_session`` to record which parent orchestrator
    session owns this claim.
    """
    if request.app.state.draining:  # type: ignore[attr-defined]
        raise HTTPException(
            status_code=503,
            detail=_DRAINING_DETAIL,
        )
    with start_span("task.claim", {"task.id": task_id}):
        store = _get_store(request)
        sse_bus = _get_sse_bus(request)
        try:
            task = store.get_task(task_id)
            if task is None:
                raise KeyError
            _require_task_access(task, request)
            task = await store.claim_by_id(
                task_id,
                expected_version=expected_version,
                claimed_by_session=claimed_by_session,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "claimed"}))
        return task_to_response(task)


@router.post(
    "/tasks/{task_id}/complete",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def complete_task(task_id: str, body: TaskCompleteRequest, request: Request) -> TaskResponse:
    """Mark a task as done with a result summary."""
    with start_span("task.complete", {"task.id": task_id}):
        store = _get_store(request)
        sse_bus = _get_sse_bus(request)
        try:
            task = store.get_task(task_id)
            if task is None:
                raise KeyError
            _require_task_access(task, request)
            # Auto-claim if task reverted to "open" (e.g. after orchestrator
            # restart reconciliation).  Prevents agents from looping on 409.
            if task.status.value == "open":
                await store.claim_by_id(task_id)
            task = await store.complete(task_id, body.result_summary)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
        except IllegalTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "done"}))
        get_plugin_manager().fire_task_completed(task_id=task.id, role=task.role, result_summary=body.result_summary)

        # Sigstore/Ed25519 attestation for the task completion (fire-and-forget)
        _try_attest_task_completion(request, task.id, task.role, body.result_summary)

        # SBOM generation on task completion (fire-and-forget, opt-in via env/state)
        _try_generate_sbom(request)

        # Evict session from the real-time monitor to free memory
        _evict_realtime_session(request, task.claimed_by_session)

        return task_to_response(task)


@router.post(
    "/tasks/{task_id}/wait-for-subtasks",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def wait_for_subtasks(task_id: str, body: TaskWaitForSubtasksRequest, request: Request) -> TaskResponse:
    """Mark a parent task as waiting until its generated subtasks complete."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.wait_for_subtasks(task_id, body.subtask_count)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/fail",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def fail_task(task_id: str, body: TaskFailRequest, request: Request) -> TaskResponse:
    """Mark a task as failed."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        # Auto-claim if task reverted to "open" (same rationale as /complete).
        if existing_task.status.value == "open":
            await store.claim_by_id(task_id)
        task = await store.fail(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "failed"}))
    get_plugin_manager().fire_task_failed(task_id=task.id, role=task.role, error=body.reason)
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/close",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def close_task(task_id: str, request: Request) -> TaskResponse:
    """Mark a verified task as closed (terminal success state)."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.close(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "closed"}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def cancel_task(task_id: str, body: TaskCancelRequest, request: Request) -> TaskResponse:
    """Cancel a task that has not yet finished."""
    store = _get_store(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.cancel(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/block",
    response_model=TaskResponse,
    responses={404: {"description": "Task not found"}, 409: {"description": "Invalid state transition"}},
)
async def block_task(task_id: str, body: TaskBlockRequest, request: Request) -> TaskResponse:
    """Mark a task as blocked -- requires human intervention to unblock."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.block(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "blocked"}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/progress", response_model=TaskResponse, responses={404: {"description": "Task not found"}}
)
async def progress_task(task_id: str, body: TaskProgressRequest, request: Request) -> TaskResponse:
    """Append an intermediate progress update to a task.

    Also stores a progress snapshot for stall detection when snapshot
    fields (files_changed, tests_passing, errors) are provided.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.add_progress(task_id, body.message, body.percent)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    # Store structured snapshot for stall detection when snapshot fields present
    if body.files_changed is not None or body.tests_passing is not None:
        store.add_snapshot(
            task_id,
            files_changed=body.files_changed if body.files_changed is not None else 0,
            tests_passing=body.tests_passing if body.tests_passing is not None else -1,
            errors=body.errors if body.errors is not None else 0,
            last_file=body.last_file,
        )

    # Real-time behavior anomaly detection — checks file access, output size,
    # and file-change velocity against learned baselines.  Kill signals are
    # written automatically for KILL_AGENT severity detections.
    _try_check_realtime_anomaly(
        request,
        task_id,
        task.claimed_by_session,
        files_changed=body.files_changed or 0,
        last_file=body.last_file,
        message=body.message or "",
    )

    sse_bus.publish(
        "task_progress",
        json.dumps({"id": task.id, "message": body.message, "percent": body.percent}),
    )
    return task_to_response(task)


@router.get("/tasks/{task_id}/snapshots", responses={404: {"description": "Task not found"}})
def get_task_snapshots(task_id: str, request: Request) -> list[SnapshotEntry]:
    """Return stored progress snapshots for a task (oldest-first, up to 10)."""
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    snapshots = store.get_snapshots(task_id)
    return [
        SnapshotEntry(
            timestamp=s.timestamp,
            files_changed=s.files_changed,
            tests_passing=s.tests_passing,
            errors=s.errors,
            last_file=s.last_file,
        )
        for s in snapshots
    ]


@router.get("/tasks")
def list_tasks(
    request: Request,
    status: str | None = None,
    cell_id: str | None = None,
    tenant: str | None = None,
    claimed_by_session: str | None = None,
    parent_session_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
) -> PaginatedTasksResponse | list[TaskResponse]:
    """List tasks, optionally filtered by status, cell_id, and/or claim owner.

    When ``limit`` or ``offset`` query params are provided the response is a
    paginated envelope (``{tasks, total, limit, offset}``).  Without them,
    the legacy flat list is returned for backward compatibility.

    Args:
        request: FastAPI request.
        status: If provided, only tasks with this status are returned.
        cell_id: If provided, only tasks in this cell are returned.
        tenant: Tenant scope override.
        claimed_by_session: If provided, only tasks claimed by this parent
            orchestrator session are returned.
        limit: Maximum number of tasks to return (max 500).  Triggers
            paginated response when present.
        offset: Number of tasks to skip.  Triggers paginated response
            when present.

    Returns:
        Paginated response **or** plain list of TaskResponse dicts.
    """
    store = _get_store(request)
    effective_tenant = _resolve_request_tenant_scope(request, tenant)
    all_tasks = store.list_tasks(
        status,
        cell_id,
        tenant_id=effective_tenant,
        claimed_by_session=claimed_by_session,
        parent_session_id=parent_session_id,
    )

    paginate = limit is not None or offset is not None
    if paginate:
        effective_limit = max(1, min(limit or 100, 500))
        effective_offset = max(0, offset or 0)
        total = len(all_tasks)
        page = all_tasks[effective_offset : effective_offset + effective_limit]
        return PaginatedTasksResponse(
            tasks=[task_to_response(t) for t in page],
            total=total,
            limit=effective_limit,
            offset=effective_offset,
        )

    # Legacy: return a flat list for callers that don't pass pagination params.
    return [task_to_response(t) for t in all_tasks]


@router.get("/tasks/counts", response_model=TaskCountsResponse)
def task_counts(
    request: Request,
    tenant: str | None = None,
) -> TaskCountsResponse:
    """Return task counts per status without serialising task bodies.

    This is the lightweight alternative to GET /tasks for orchestrator
    tick summaries and dashboard polling.
    """
    store = _get_store(request)
    effective_tenant = _resolve_request_tenant_scope(request, tenant)
    counts = store.count_by_status(tenant_id=effective_tenant)
    return TaskCountsResponse(
        open=counts.get("open", 0),
        claimed=counts.get("claimed", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
        blocked=counts.get("blocked", 0),
        cancelled=counts.get("cancelled", 0),
        total=counts.get("total", 0),
    )


@router.get("/tasks/archive")
def get_archive(request: Request, limit: int = 50, tenant: str | None = None) -> list[ArchiveRecord]:
    """Return the last N archived (done/failed) task records."""
    store = _get_store(request)
    return store.read_archive(limit=limit, tenant_id=_resolve_request_tenant_scope(request, tenant))


@router.get("/tasks/graph")
def get_task_graph(request: Request) -> JSONResponse:
    """Return the task dependency graph as JSON (nodes + edges + critical path).

    Builds a DAG from all current tasks and returns:
    - ``nodes``: list of {id, role, status, estimated_minutes, title}
    - ``edges``: list of {from, to, type, semantic_type}
    - ``critical_path``: ordered list of task IDs on the longest chain
    - ``critical_path_minutes``: total estimated minutes on the critical path
    - ``parallel_width``: max tasks that can run concurrently
    - ``bottlenecks``: task IDs that block the most downstream work
    """
    from bernstein.core.graph import TaskGraph

    store = _get_store(request)
    tasks = store.list_tasks(tenant_id=_resolve_request_tenant_scope(request))
    graph = TaskGraph(tasks)
    data = graph.to_dict()
    # Enrich nodes with title for CLI rendering
    task_map = {t.id: t for t in tasks}
    for node in data["nodes"]:
        node["title"] = task_map[node["id"]].title if node["id"] in task_map else ""
    return JSONResponse(content=data)


@router.get("/tasks/{task_id}", response_model=TaskResponse, responses={404: {"description": "Task not found"}})
def get_task(task_id: str, request: Request) -> TaskResponse:
    """Get a single task by ID."""
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)
    return task_to_response(task)


@router.get(
    "/tasks/{task_id}/gates",
    responses={404: {"description": "Task or gate report not found"}, 500: {"description": "Gate report unreadable"}},
)
def get_task_gates(task_id: str, request: Request) -> JSONResponse:
    """Return the persisted quality-gate report for a task."""
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    _require_task_access(task, request)

    report_path = _get_gate_report_path(request, task_id)
    if not report_path.exists():
        raise HTTPException(status_code=404, detail=f"Gate report for task '{task_id}' not found")
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Gate report for task '{task_id}' is unreadable") from exc
    return JSONResponse(content=payload)


@router.patch("/tasks/{task_id}", responses={404: {"description": "Task not found"}})
async def patch_task(task_id: str, body: TaskPatchRequest, request: Request) -> TaskResponse:
    """Update mutable task fields (role, priority, model) — manager corrections.

    Used by the manager agent or dashboard to correct mis-assigned tasks,
    adjust priority, or change model without interrupting the orchestrator.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.update(task_id, role=body.role, priority=body.priority, model=body.model)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post("/tasks/{task_id}/prioritize", responses={404: {"description": "Task not found"}})
async def prioritize_task(task_id: str, request: Request) -> TaskResponse:
    """Bump a task to priority 0 so the orchestrator picks it up next."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.prioritize(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post(
    "/tasks/{task_id}/force-claim",
    responses={404: {"description": "Task not found"}, 409: {"description": "Cannot force-claim terminal task"}},
)
async def force_claim_task(task_id: str, request: Request) -> TaskResponse:
    """Force a task back to open with priority 0 for immediate pickup.

    Resets claimed/in_progress tasks back to open so the orchestrator's
    next tick will spawn a fresh agent for them.  Terminal tasks
    (done/failed/cancelled) are rejected with 409.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        existing_task = store.get_task(task_id)
        if existing_task is None:
            raise KeyError
        _require_task_access(existing_task, request)
        task = await store.force_claim(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "open"}))
    return task_to_response(task)


# ---------------------------------------------------------------------------
# Agent heartbeats and session management
# ---------------------------------------------------------------------------


@router.post("/agents/{agent_id}/heartbeat", response_model=HeartbeatResponse)
def agent_heartbeat(agent_id: str, body: HeartbeatRequest, request: Request) -> HeartbeatResponse:
    """Register an agent heartbeat."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    ts = store.heartbeat(agent_id, body.role, body.status)
    sse_bus.publish("agent_update", json.dumps({"agent_id": agent_id, "status": body.status}))
    return HeartbeatResponse(agent_id=agent_id, acknowledged=True, server_ts=ts)


@router.get("/agents/{session_id}/logs", responses={404: {"description": "No log file for session"}})
def agent_logs(session_id: str, request: Request, tail_bytes: int = 0) -> JSONResponse:
    """Return log file content for a session.

    Args:
        session_id: Agent session ID.
        tail_bytes: If > 0, return only the last N bytes of the log.
    """
    runtime_dir = _get_runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"No log file for session '{session_id}'")
    size = log_path.stat().st_size
    offset = max(0, size - tail_bytes) if tail_bytes > 0 else 0
    content = read_log_tail(log_path, offset)
    return JSONResponse(
        content={
            "session_id": session_id,
            "content": content,
            "size": size,
        }
    )


@router.post("/agents/{session_id}/kill")
def agent_kill(session_id: str, request: Request) -> JSONResponse:
    """Request that an agent session be killed.

    Writes a ``.kill`` signal file that the orchestrator picks up on
    its next tick.
    """
    runtime_dir = _get_runtime_dir(request)
    sse_bus = _get_sse_bus(request)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    kill_path = runtime_dir / f"{session_id}.kill"
    kill_path.write_text(str(time.time()))
    sse_bus.publish(
        "session_kill",
        json.dumps({"session_id": session_id}),
    )
    return JSONResponse(
        content={
            "session_id": session_id,
            "kill_requested": True,
        }
    )


@router.get("/agents/{session_id}/stream")
def agent_stream(session_id: str, request: Request) -> StreamingResponse:
    """SSE stream of live log output for a session."""
    runtime_dir = _get_runtime_dir(request)
    log_path = runtime_dir / f"{session_id}.log"

    async def _generate() -> AsyncGenerator[str, None]:
        # Initial connection event
        yield f"data: {json.dumps({'connected': True, 'session_id': session_id})}\n\n"

        offset = 0
        idle_ticks = 0
        max_idle = 60  # stop after ~30s of no file

        while True:
            if not log_path.exists():
                idle_ticks += 1
                if idle_ticks >= max_idle:
                    yield f"data: {json.dumps({'done': True, 'reason': 'no_log_file'})}\n\n"
                    return
                await asyncio.sleep(0.5)
                continue

            size = log_path.stat().st_size
            if size > offset:
                chunk = read_log_tail(log_path, offset)
                offset = size
                idle_ticks = 0
                for line in chunk.splitlines():
                    if line.strip():
                        yield f"data: {json.dumps({'line': line})}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks >= max_idle:
                    yield f"data: {json.dumps({'done': True, 'reason': 'idle'})}\n\n"
                    return

            await asyncio.sleep(0.5)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Bulletin board
# ---------------------------------------------------------------------------


@router.post("/bulletin", status_code=201)
def post_bulletin(body: BulletinPostRequest, request: Request) -> BulletinMessageResponse:
    """Append a message to the bulletin board."""
    bulletin = _get_bulletin(request)
    msg = BulletinMessage(
        agent_id=body.agent_id,
        type=body.type,
        content=body.content,
        cell_id=body.cell_id,
    )
    stored = bulletin.post(msg)

    # Broadcast to SSE bus
    sse_bus = _get_sse_bus(request)
    sse_bus.publish(
        "bulletin",
        json.dumps(
            {
                "agent_id": stored.agent_id,
                "type": stored.type,
                "content": stored.content,
                "timestamp": stored.timestamp,
                "cell_id": stored.cell_id,
            }
        ),
    )

    return BulletinMessageResponse(
        agent_id=stored.agent_id,
        type=stored.type,
        content=stored.content,
        timestamp=stored.timestamp,
        cell_id=stored.cell_id,
    )


@router.get("/bulletin")
def get_bulletin(request: Request, since: float = 0.0) -> list[BulletinMessageResponse]:
    """Get bulletin messages since a given timestamp."""
    bulletin = _get_bulletin(request)
    messages = bulletin.read_since(since)
    return [
        BulletinMessageResponse(
            agent_id=m.agent_id,
            type=m.type,
            content=m.content,
            timestamp=m.timestamp,
            cell_id=m.cell_id,
        )
        for m in messages
    ]


# ---------------------------------------------------------------------------
# A2A protocol
# ---------------------------------------------------------------------------


@router.get("/.well-known/agent.json")
def agent_card(request: Request) -> A2AAgentCardResponse:
    """Publish the Bernstein orchestrator Agent Card (A2A spec)."""
    a2a_handler = _get_a2a_handler(request)
    card = a2a_handler.orchestrator_card()
    d = card.to_dict()
    return A2AAgentCardResponse(**d)


@router.get("/a2a/agents")
def list_a2a_agents(request: Request) -> A2AAgentCardResponse:
    """Return Bernstein's A2A agent card via the task API namespace."""

    return agent_card(request)


@router.post(
    "/a2a/message",
    status_code=201,
    responses={404: {"description": "Task not found"}},
)
async def a2a_message(body: A2AMessageRequest, request: Request) -> A2AMessageResponse:
    """Receive an inbound A2A message and inject it into the target task context."""

    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    a2a_handler = _get_a2a_handler(request)

    task = store.get_task(body.task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{body.task_id}' not found")
    _require_task_access(task, request)

    message = a2a_handler.receive_message(
        sender=body.sender,
        recipient=body.recipient,
        content=body.content,
        task_id=body.task_id,
    )
    injected_context = f"[A2A:{body.sender}->{body.recipient}] {body.content}"
    await store.add_progress(body.task_id, injected_context, 0)
    sse_bus.publish(
        "a2a_message",
        json.dumps(
            {
                "id": message.id,
                "task_id": message.task_id,
                "sender": message.sender,
                "recipient": message.recipient,
            }
        ),
    )
    return a2a_message_to_response(message)


@router.post("/a2a/tasks/send", status_code=201)
async def a2a_send_task(body: A2ATaskSendRequest, request: Request) -> A2ATaskResponse:
    """Receive a task from an external A2A agent.

    Creates both an A2A task record and a corresponding Bernstein task,
    linking them together for lifecycle synchronisation.
    """
    store = _get_store(request)
    a2a_handler = _get_a2a_handler(request)
    a2a_task = a2a_handler.create_task(
        sender=body.sender,
        message=body.message,
        role=body.role,
    )
    # Create the corresponding Bernstein task.
    bernstein_task = await store.create(
        TaskCreate(
            title=f"[A2A] {body.message[:80]}",
            description=body.message,
            role=body.role,
            tenant_id=request_tenant_id(request),
            estimated_minutes=minutes_for_level(estimate_difficulty(body.message).level),
        )
    )
    a2a_handler.link_bernstein_task(a2a_task.id, bernstein_task.id)
    return a2a_task_to_response(a2a_task)


@router.get("/a2a/tasks/{a2a_task_id}", responses={404: {"description": "A2A task not found"}})
def a2a_get_task(a2a_task_id: str, request: Request) -> A2ATaskResponse:
    """Get an A2A task by ID, syncing status from the Bernstein task."""
    store = _get_store(request)
    a2a_handler = _get_a2a_handler(request)
    a2a_task = a2a_handler.get_task(a2a_task_id)
    if a2a_task is None:
        raise HTTPException(status_code=404, detail=f"A2A task '{a2a_task_id}' not found")
    # Sync status from the underlying Bernstein task.
    if a2a_task.bernstein_task_id is not None:
        bt = store.get_task(a2a_task.bernstein_task_id)
        if bt is not None:
            a2a_handler.sync_status(a2a_task.id, bt.status.value)
    return a2a_task_to_response(a2a_task)


@router.post(
    "/a2a/tasks/{a2a_task_id}/artifacts",
    status_code=201,
    responses={404: {"description": "A2A task not found"}},
)
def a2a_add_artifact(a2a_task_id: str, body: A2AArtifactRequest, request: Request) -> A2AArtifactResponse:
    """Attach an artifact to an A2A task."""
    a2a_handler = _get_a2a_handler(request)
    try:
        artifact = a2a_handler.add_artifact(
            a2a_task_id=a2a_task_id,
            name=body.name,
            data=body.data,
            content_type=body.content_type,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"A2A task '{a2a_task_id}' not found") from None
    return A2AArtifactResponse(
        name=artifact.name,
        content_type=artifact.content_type,
        data=artifact.data,
        created_at=artifact.created_at,
    )


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


@router.post("/cluster/nodes", status_code=201)
def register_node(body: NodeRegisterRequest, request: Request) -> NodeResponse:
    """Register a new node in the cluster."""
    from bernstein.core.cluster_auth import SCOPE_NODE_REGISTER

    _verify_cluster_auth(request, SCOPE_NODE_REGISTER)
    node_registry = _get_node_registry(request)
    capacity = NodeCapacity(
        max_agents=body.capacity.max_agents,
        available_slots=body.capacity.available_slots,
        active_agents=body.capacity.active_agents,
        gpu_available=body.capacity.gpu_available,
        supported_models=body.capacity.supported_models,
    )
    node = NodeInfo(
        name=body.name,
        url=body.url,
        capacity=capacity,
        labels=body.labels,
        cell_ids=body.cell_ids,
    )
    registered = node_registry.register(node)
    return node_to_response(registered)


@router.post(
    "/cluster/nodes/{node_id}/heartbeat",
    responses={404: {"description": "Node not registered"}},
)
def node_heartbeat(node_id: str, body: NodeHeartbeatRequest, request: Request) -> NodeResponse:
    """Record a heartbeat from a cluster node."""
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT

    _verify_cluster_auth(request, SCOPE_NODE_HEARTBEAT)
    node_registry = _get_node_registry(request)
    capacity: NodeCapacity | None = None
    if body.capacity is not None:
        capacity = NodeCapacity(
            max_agents=body.capacity.max_agents,
            available_slots=body.capacity.available_slots,
            active_agents=body.capacity.active_agents,
            gpu_available=body.capacity.gpu_available,
            supported_models=body.capacity.supported_models,
        )
    node = node_registry.heartbeat(node_id, capacity)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not registered")
    return node_to_response(node)


@router.delete("/cluster/nodes/{node_id}", status_code=204, responses={404: {"description": "Node not found"}})
def unregister_node(node_id: str, request: Request) -> Response:
    """Remove a node from the cluster."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node_registry = _get_node_registry(request)
    if not node_registry.unregister(node_id):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return Response(status_code=204)


@router.post("/cluster/nodes/{node_id}/cordon")
def cordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Cordon a node -- exclude from scheduling."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.cordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "cordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/uncordon")
def uncordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Uncordon a node -- resume accepting tasks."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.uncordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "uncordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/drain")
def drain_node(node_id: str, request: Request) -> dict[str, str]:
    """Start draining a node -- cordon + signal agents to finish."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.start_drain(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "draining", "node_id": node_id}


@router.get("/cluster/nodes", responses={400: {"description": "Invalid node status"}})
def list_nodes(request: Request, status: str | None = None) -> list[NodeResponse]:
    """List all cluster nodes, optionally filtered by status."""
    node_registry = _get_node_registry(request)
    node_status: NodeStatus | None = None
    if status is not None:
        try:
            node_status = NodeStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid node status: {status}") from None
    return [node_to_response(n) for n in node_registry.list_nodes(node_status)]


@router.get("/cluster/status", response_model=ClusterStatusResponse)
def cluster_status(request: Request) -> ClusterStatusResponse:
    """Get cluster status summary."""
    node_registry = _get_node_registry(request)
    summary = node_registry.cluster_summary()
    return ClusterStatusResponse(
        topology=summary["topology"],
        total_nodes=summary["total_nodes"],
        online_nodes=summary["online_nodes"],
        offline_nodes=summary["offline_nodes"],
        total_capacity=summary["total_capacity"],
        available_slots=summary["available_slots"],
        active_agents=summary["active_agents"],
        nodes=[NodeResponse(**n) for n in summary["nodes"]],
    )


@router.post("/cluster/steal")
async def steal_tasks(body: TaskStealRequest, request: Request) -> TaskStealResponse:
    """Evaluate task stealing policy and reassign claimed tasks between nodes.

    Workers report their queue depths; the server runs the steal policy and
    returns a list of task reassignments.  Stolen tasks are reset to ``open``
    so the receiver node can claim them.
    """
    from bernstein.core.cluster import TaskStealPolicy

    node_registry = _get_node_registry(request)
    store = _get_store(request)

    policy = TaskStealPolicy()
    pairs = policy.find_steal_pairs(node_registry, body.queue_depths)

    actions: list[TaskStealAction] = []
    total_stolen = 0

    for donor_id, receiver_id, count in pairs:
        # Find claimed tasks that could be released from the donor.
        # The task store's list_tasks is sync; filter by cell_id or
        # assigned_agent that maps to the donor node.
        claimed = store.list_tasks(status="claimed")
        donor_tasks = [t for t in claimed if getattr(t, "assigned_node", None) == donor_id][:count]

        # If no tasks tagged with assigned_node, fall back to taking the
        # oldest claimed tasks (best-effort redistribution).
        if not donor_tasks and claimed:
            donor_tasks = sorted(claimed, key=lambda t: t.version)[:count]

        stolen_ids: list[str] = []
        for task in donor_tasks:
            try:
                await store.force_claim(task.id)
                stolen_ids.append(task.id)
            except (KeyError, ValueError):
                continue

        if stolen_ids:
            actions.append(
                TaskStealAction(
                    donor_node_id=donor_id,
                    receiver_node_id=receiver_id,
                    task_ids=stolen_ids,
                )
            )
            total_stolen += len(stolen_ids)

    return TaskStealResponse(actions=actions, total_stolen=total_stolen)
