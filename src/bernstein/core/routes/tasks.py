"""Task CRUD routes, agent heartbeats, bulletin board, A2A, cluster, and session streaming."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

from bernstein.core.bulletin import BulletinBoard, BulletinMessage
from bernstein.core.lifecycle import IllegalTransitionError
from bernstein.core.models import NodeCapacity, NodeInfo, NodeStatus

# Import Pydantic models from server — this works because server.py's
# __getattr__ defers the `app` creation, so the module body (class defs)
# loads without triggering create_app().
from bernstein.core.server import (
    A2AAgentCardResponse,
    A2AArtifactRequest,
    A2AArtifactResponse,
    A2ATaskResponse,
    A2ATaskSendRequest,
    BatchClaimRequest,
    BatchClaimResponse,
    BulletinMessageResponse,
    BulletinPostRequest,
    ClusterStatusResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    NodeResponse,
    SSEBus,
    TaskBlockRequest,
    TaskCancelRequest,
    TaskCompleteRequest,
    TaskCreate,
    TaskFailRequest,
    TaskPatchRequest,
    TaskProgressRequest,
    TaskResponse,
    TaskStealAction,
    TaskStealRequest,
    TaskStealResponse,
    TaskStore,
    a2a_task_to_response,
    node_to_response,
    read_log_tail,
    task_to_response,
)
from bernstein.core.task_store import ArchiveRecord, SnapshotEntry
from bernstein.plugins.manager import get_plugin_manager

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from bernstein.core.a2a import A2AHandler
    from bernstein.core.cluster import NodeRegistry

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


def _get_runtime_dir(request: Request) -> Path:
    return request.app.state.runtime_dir  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------


@router.post("/tasks", response_model=TaskResponse, status_code=201)
async def create_task(body: TaskCreate, request: Request) -> TaskResponse:
    """Create a new task."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    task = await store.create(body)
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    get_plugin_manager().fire_task_created(task_id=task.id, role=task.role, title=task.title)
    return task_to_response(task)


@router.get("/tasks/next/{role}", response_model=TaskResponse)
async def next_task(role: str, request: Request) -> TaskResponse:
    """Claim the next available task for *role*."""
    store = _get_store(request)
    task = await store.claim_next(role)
    if task is None:
        raise HTTPException(status_code=404, detail=f"No open tasks for role '{role}'")
    return task_to_response(task)


@router.post("/tasks/claim-batch", response_model=BatchClaimResponse)
async def claim_batch(body: BatchClaimRequest, request: Request) -> BatchClaimResponse:
    """Atomically claim multiple tasks by ID for an agent."""
    store = _get_store(request)
    claimed, failed = await store.claim_batch(body.task_ids, body.agent_id)
    return BatchClaimResponse(claimed=claimed, failed=failed)


@router.post("/tasks/{task_id}/claim", response_model=TaskResponse)
async def claim_task(task_id: str, request: Request, expected_version: int | None = None) -> TaskResponse:
    """Claim a specific task by ID.

    Pass ``expected_version`` as a query param for optimistic locking
    (CAS). If the task's version doesn't match, returns 409 Conflict.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.claim_by_id(task_id, expected_version=expected_version)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "claimed"}))
    return task_to_response(task)


@router.post("/tasks/{task_id}/complete", response_model=TaskResponse)
async def complete_task(task_id: str, body: TaskCompleteRequest, request: Request) -> TaskResponse:
    """Mark a task as done with a result summary."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.complete(task_id, body.result_summary)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "done"}))
    get_plugin_manager().fire_task_completed(task_id=task.id, role=task.role, result_summary=body.result_summary)
    return task_to_response(task)


@router.post("/tasks/{task_id}/fail", response_model=TaskResponse)
async def fail_task(task_id: str, body: TaskFailRequest, request: Request) -> TaskResponse:
    """Mark a task as failed."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.fail(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "failed"}))
    get_plugin_manager().fire_task_failed(task_id=task.id, role=task.role, error=body.reason)
    return task_to_response(task)


@router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: str, body: TaskCancelRequest, request: Request) -> TaskResponse:
    """Cancel a task that has not yet finished."""
    store = _get_store(request)
    try:
        task = await store.cancel(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return task_to_response(task)


@router.post("/tasks/{task_id}/block", response_model=TaskResponse)
async def block_task(task_id: str, body: TaskBlockRequest, request: Request) -> TaskResponse:
    """Mark a task as blocked -- requires human intervention to unblock."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.block(task_id, body.reason)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": "blocked"}))
    return task_to_response(task)


@router.post("/tasks/{task_id}/progress", response_model=TaskResponse)
async def progress_task(task_id: str, body: TaskProgressRequest, request: Request) -> TaskResponse:
    """Append an intermediate progress update to a task.

    Also stores a progress snapshot for stall detection when snapshot
    fields (files_changed, tests_passing, errors) are provided.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
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
    sse_bus.publish(
        "task_progress",
        json.dumps({"id": task.id, "message": body.message, "percent": body.percent}),
    )
    return task_to_response(task)


@router.get("/tasks/{task_id}/snapshots", response_model=list[SnapshotEntry])
async def get_task_snapshots(task_id: str, request: Request) -> list[SnapshotEntry]:
    """Return stored progress snapshots for a task (oldest-first, up to 10)."""
    store = _get_store(request)
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


@router.get("/tasks", response_model=list[TaskResponse])
async def list_tasks(
    request: Request,
    status: str | None = None,
    cell_id: str | None = None,
) -> list[TaskResponse]:
    """List all tasks, optionally filtered by status and/or cell_id."""
    store = _get_store(request)
    return [task_to_response(t) for t in store.list_tasks(status, cell_id)]


@router.get("/tasks/archive", response_model=list[ArchiveRecord])
async def get_archive(request: Request, limit: int = 50) -> list[ArchiveRecord]:
    """Return the last N archived (done/failed) task records."""
    store = _get_store(request)
    return store.read_archive(limit=limit)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str, request: Request) -> TaskResponse:
    """Get a single task by ID."""
    store = _get_store(request)
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task_to_response(task)


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def patch_task(task_id: str, body: TaskPatchRequest, request: Request) -> TaskResponse:
    """Update mutable task fields (role, priority, model) — manager corrections.

    Used by the manager agent or dashboard to correct mis-assigned tasks,
    adjust priority, or change model without interrupting the orchestrator.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.update(task_id, role=body.role, priority=body.priority, model=body.model)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post("/tasks/{task_id}/prioritize", response_model=TaskResponse)
async def prioritize_task(task_id: str, request: Request) -> TaskResponse:
    """Bump a task to priority 0 so the orchestrator picks it up next."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
        task = await store.prioritize(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found") from None
    sse_bus.publish("task_update", json.dumps({"id": task.id, "status": task.status.value}))
    return task_to_response(task)


@router.post("/tasks/{task_id}/force-claim", response_model=TaskResponse)
async def force_claim_task(task_id: str, request: Request) -> TaskResponse:
    """Force a task back to open with priority 0 for immediate pickup.

    Resets claimed/in_progress tasks back to open so the orchestrator's
    next tick will spawn a fresh agent for them.  Terminal tasks
    (done/failed/cancelled) are rejected with 409.
    """
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    try:
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
async def agent_heartbeat(agent_id: str, body: HeartbeatRequest, request: Request) -> HeartbeatResponse:
    """Register an agent heartbeat."""
    store = _get_store(request)
    sse_bus = _get_sse_bus(request)
    ts = store.heartbeat(agent_id, body.role, body.status)
    sse_bus.publish("agent_update", json.dumps({"agent_id": agent_id, "status": body.status}))
    return HeartbeatResponse(agent_id=agent_id, acknowledged=True, server_ts=ts)


@router.get("/agents/{session_id}/logs")
async def agent_logs(session_id: str, request: Request, tail_bytes: int = 0) -> JSONResponse:
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
async def agent_kill(session_id: str, request: Request) -> JSONResponse:
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
async def agent_stream(session_id: str, request: Request) -> StreamingResponse:
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


@router.post("/bulletin", response_model=BulletinMessageResponse, status_code=201)
async def post_bulletin(body: BulletinPostRequest, request: Request) -> BulletinMessageResponse:
    """Append a message to the bulletin board."""
    bulletin = _get_bulletin(request)
    msg = BulletinMessage(
        agent_id=body.agent_id,
        type=body.type,
        content=body.content,
        cell_id=body.cell_id,
    )
    stored = bulletin.post(msg)
    return BulletinMessageResponse(
        agent_id=stored.agent_id,
        type=stored.type,
        content=stored.content,
        timestamp=stored.timestamp,
        cell_id=stored.cell_id,
    )


@router.get("/bulletin", response_model=list[BulletinMessageResponse])
async def get_bulletin(request: Request, since: float = 0.0) -> list[BulletinMessageResponse]:
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


@router.get("/.well-known/agent.json", response_model=A2AAgentCardResponse)
async def agent_card(request: Request) -> A2AAgentCardResponse:
    """Publish the Bernstein orchestrator Agent Card (A2A spec)."""
    a2a_handler = _get_a2a_handler(request)
    card = a2a_handler.orchestrator_card()
    d = card.to_dict()
    return A2AAgentCardResponse(**d)


@router.post("/a2a/tasks/send", response_model=A2ATaskResponse, status_code=201)
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
        )
    )
    a2a_handler.link_bernstein_task(a2a_task.id, bernstein_task.id)
    return a2a_task_to_response(a2a_task)


@router.get("/a2a/tasks/{a2a_task_id}", response_model=A2ATaskResponse)
async def a2a_get_task(a2a_task_id: str, request: Request) -> A2ATaskResponse:
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
    response_model=A2AArtifactResponse,
    status_code=201,
)
async def a2a_add_artifact(a2a_task_id: str, body: A2AArtifactRequest, request: Request) -> A2AArtifactResponse:
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


@router.post("/cluster/nodes", response_model=NodeResponse, status_code=201)
async def register_node(body: NodeRegisterRequest, request: Request) -> NodeResponse:
    """Register a new node in the cluster."""
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


@router.post("/cluster/nodes/{node_id}/heartbeat", response_model=NodeResponse)
async def node_heartbeat(node_id: str, body: NodeHeartbeatRequest, request: Request) -> NodeResponse:
    """Record a heartbeat from a cluster node."""
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


@router.delete("/cluster/nodes/{node_id}", status_code=204)
async def unregister_node(node_id: str, request: Request) -> None:
    """Remove a node from the cluster."""
    node_registry = _get_node_registry(request)
    if not node_registry.unregister(node_id):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")


@router.get("/cluster/nodes", response_model=list[NodeResponse])
async def list_nodes(request: Request, status: str | None = None) -> list[NodeResponse]:
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
async def cluster_status(request: Request) -> ClusterStatusResponse:
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


@router.post("/cluster/steal", response_model=TaskStealResponse)
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
        donor_tasks = [
            t for t in claimed
            if getattr(t, "assigned_node", None) == donor_id
        ][:count]

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
            actions.append(TaskStealAction(
                donor_node_id=donor_id,
                receiver_node_id=receiver_id,
                task_ids=stolen_ids,
            ))
            total_stolen += len(stolen_ids)

    return TaskStealResponse(actions=actions, total_stolen=total_stolen)
