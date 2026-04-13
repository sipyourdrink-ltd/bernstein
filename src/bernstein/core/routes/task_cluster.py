"""Cluster management routes: node registration, heartbeats, draining, task stealing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response

from bernstein.core.models import NodeCapacity, NodeInfo, NodeStatus
from bernstein.core.server import (
    ClusterStatusResponse,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    NodeResponse,
    TaskStealAction,
    TaskStealRequest,
    TaskStealResponse,
    TaskStore,
    node_to_response,
)

if TYPE_CHECKING:
    from bernstein.core.cluster import NodeRegistry

router = APIRouter()

_AUTH_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Cluster authentication failed"},
}
_AUTH_404_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Cluster authentication failed"},
    404: {"description": "Node not found"},
}


def _get_store(request: Request) -> TaskStore:
    return request.app.state.store  # type: ignore[no-any-return]


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


@router.post("/cluster/nodes", status_code=201, responses=_AUTH_RESPONSES)
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
    responses={**_AUTH_RESPONSES, 404: {"description": "Node not registered"}},
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


@router.delete("/cluster/nodes/{node_id}", status_code=204, responses=_AUTH_404_RESPONSES)
def unregister_node(node_id: str, request: Request) -> Response:
    """Remove a node from the cluster."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node_registry = _get_node_registry(request)
    if not node_registry.unregister(node_id):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    return Response(status_code=204)


@router.post("/cluster/nodes/{node_id}/cordon", responses=_AUTH_404_RESPONSES)
def cordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Cordon a node -- exclude from scheduling."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.cordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "cordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/uncordon", responses=_AUTH_404_RESPONSES)
def uncordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Uncordon a node -- resume accepting tasks."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.uncordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "uncordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/drain", responses=_AUTH_404_RESPONSES)
def drain_node(node_id: str, request: Request) -> dict[str, str]:
    """Start draining a node -- cordon + signal agents to finish."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    registry = _get_node_registry(request)
    node = registry.start_drain(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "draining", "node_id": node_id}


@router.get(
    "/cluster/nodes",
    responses={400: {"description": "Invalid node status"}},
)
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


@router.get("/cluster/status")
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
