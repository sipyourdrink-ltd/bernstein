"""Cluster management routes: node registration, heartbeats, draining, task stealing."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from bernstein.core.models import NodeCapacity, NodeInfo, NodeStatus
from bernstein.core.protocols.cluster.cluster import (
    SHARD_VNODES,
    _compute_node_id_sharded,
    _find_owner_shard,
    _get_shard_url,
)
from bernstein.core.security.auth_middleware import enforce_agent_task_scope_for_ids
from bernstein.core.server import (
    ClaimGossipRequest,
    ClaimGossipResponse,
    ClaimGossipResult,
    ClusterStatusResponse,
    NodeHeartbeatBatchRequest,
    NodeHeartbeatBatchResponse,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    NodeResponse,
    ShardMapResponse,
    ShardNodeInfo,
    TaskStealAction,
    TaskStealRequest,
    TaskStealResponse,
    TaskStore,
    node_to_response,
)

if TYPE_CHECKING:
    from bernstein.core.cluster import NodeRegistry
    from bernstein.core.protocols.cluster.mesh_coordinator import MeshCoordinator

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


def _get_mesh_coordinator(request: Request) -> MeshCoordinator:
    """Return the wired MESH coordinator, or 409 when the node is not MESH.

    Refusing rather than lazily constructing one is deliberate: gossiping into
    a node whose topology is STAR would fold receipts into a journal nothing
    reads, so the peer would believe it had converged with a node that is still
    taking its assignments from a central server.
    """
    coordinator: MeshCoordinator | None = getattr(request.app.state, "mesh_coordinator", None)
    if coordinator is None:
        raise HTTPException(
            status_code=409,
            detail="claim gossip requires cluster.topology 'mesh'; this node is not running MESH",
        )
    return coordinator


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


@router.post(
    "/cluster/nodes",
    responses=_AUTH_RESPONSES
    | {
        201: {"description": "Node registered"},
        421: {"description": "Node misdirected to another shard"},
    },
)
def register_node(body: NodeRegisterRequest, request: Request):
    """Register a node, or update the entry of one that is re-registering."""
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
    existing = node_registry.find_by_identity(body.name, body.url)
    node = NodeInfo(
        name=body.name,
        url=body.url,
        capacity=capacity,
        labels=body.labels,
        cell_ids=body.cell_ids,
    )
    if existing is not None:
        node.id = existing.id
    elif node_registry.config.shards:
        node.id = _compute_node_id_sharded(body.name, body.url)

    owner_shard = _find_owner_shard(node.id, node_registry.config.shards)
    if owner_shard is not None and owner_shard != node_registry.config.shard_id:
        owner_url = _get_shard_url(owner_shard, node_registry.config.shards) or ""
        return JSONResponse(status_code=421, content={"shard_id": owner_shard, "url": owner_url})

    registered = node_registry.register(node)
    response = node_to_response(registered)
    return JSONResponse(
        status_code=201, content=response.model_dump() if hasattr(response, "model_dump") else response.dict()
    )


@router.post(
    "/cluster/nodes/{node_id}/heartbeat",
    responses=_AUTH_RESPONSES
    | {
        404: {"description": "Node not registered"},
        421: {"description": "Node misdirected to another shard"},
    },
)
def node_heartbeat(node_id: str, body: NodeHeartbeatRequest, request: Request):
    """Record a heartbeat from a cluster node."""
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT

    _verify_cluster_auth(request, SCOPE_NODE_HEARTBEAT)
    node_registry = _get_node_registry(request)

    owner_shard = _find_owner_shard(node_id, node_registry.config.shards)
    if owner_shard is not None and owner_shard != node_registry.config.shard_id:
        owner_url = _get_shard_url(owner_shard, node_registry.config.shards) or ""
        return JSONResponse(status_code=421, content={"shard_id": owner_shard, "url": owner_url})

    capacity: NodeCapacity | None = None
    if body.capacity is not None:
        capacity = NodeCapacity(
            max_agents=body.capacity.max_agents,
            available_slots=body.capacity.available_slots,
            active_agents=body.capacity.active_agents,
            gpu_available=body.capacity.gpu_available,
            supported_models=body.capacity.supported_models,
            disk_free_mb=body.capacity.disk_free_mb,
            mem_used_pct=body.capacity.mem_used_pct,
            mesh_rtt_ms=body.capacity.mesh_rtt_ms,
            platform=body.capacity.platform,
        )
    node = node_registry.heartbeat(node_id, capacity)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not registered")
    return node_to_response(node)


@router.post(
    "/cluster/nodes/heartbeats",
    responses=_AUTH_RESPONSES | {422: {"description": "Too many heartbeats (max 500)"}},
)
def batch_node_heartbeat(body: NodeHeartbeatBatchRequest, request: Request) -> NodeHeartbeatBatchResponse:
    """Record heartbeats from multiple cluster nodes."""
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT

    if len(body.heartbeats) > 500:
        raise HTTPException(status_code=422, detail="Maximum 500 heartbeats per request")
    _verify_cluster_auth(request, SCOPE_NODE_HEARTBEAT)
    node_registry = _get_node_registry(request)
    accepted: list[str] = []
    unknown: list[str] = []
    misdirected: dict[str, str] = {}
    pending: list[tuple[str, NodeCapacity | None]] = []
    for heartbeat in body.heartbeats:
        owner_shard = _find_owner_shard(heartbeat.node_id, node_registry.config.shards)
        if owner_shard is not None and owner_shard != node_registry.config.shard_id:
            owner_url = _get_shard_url(owner_shard, node_registry.config.shards) or ""
            misdirected[heartbeat.node_id] = owner_url
            continue

        capacity: NodeCapacity | None = None
        if heartbeat.capacity is not None:
            capacity = NodeCapacity(
                max_agents=heartbeat.capacity.max_agents,
                available_slots=heartbeat.capacity.available_slots,
                active_agents=heartbeat.capacity.active_agents,
                gpu_available=heartbeat.capacity.gpu_available,
                supported_models=heartbeat.capacity.supported_models,
                disk_free_mb=heartbeat.capacity.disk_free_mb,
                mem_used_pct=heartbeat.capacity.mem_used_pct,
                mesh_rtt_ms=heartbeat.capacity.mesh_rtt_ms,
                platform=heartbeat.capacity.platform,
            )
        pending.append((heartbeat.node_id, capacity))
    for (node_id, _), node in zip(pending, node_registry.heartbeat_batch(pending), strict=True):
        if node is not None:
            accepted.append(node_id)
        else:
            unknown.append(node_id)
    return NodeHeartbeatBatchResponse(accepted=accepted, unknown=unknown, misdirected=misdirected)


@router.delete("/cluster/nodes/{node_id}", status_code=204, responses=_AUTH_404_RESPONSES)
def unregister_node(node_id: str, request: Request) -> Response:
    """Remove a node from the cluster."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node_registry = _get_node_registry(request)
    if not node_registry.unregister(node_id):
        raise HTTPException(status_code=404, detail=f"Node '{node_id}' not found")
    # Release the departed node's in-flight claims so a surviving worker can
    # pick them up. Covers the graceful-leave path (docker stop / drain);
    # the timeout path is handled by the node reaper (#2801).
    _get_store(request).reopen_tasks_for_node(node_id)
    return Response(status_code=204)


@router.post("/cluster/nodes/{node_id}/cordon", responses=_AUTH_404_RESPONSES)
def cordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Cordon a node -- exclude from scheduling."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node = _get_node_registry(request).cordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "cordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/uncordon", responses=_AUTH_404_RESPONSES)
def uncordon_node(node_id: str, request: Request) -> dict[str, str]:
    """Uncordon a node -- resume accepting tasks."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node = _get_node_registry(request).uncordon(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"Node {node_id} not found")
    return {"status": "uncordoned", "node_id": node_id}


@router.post("/cluster/nodes/{node_id}/drain", responses=_AUTH_404_RESPONSES)
def drain_node(node_id: str, request: Request) -> dict[str, str]:
    """Start draining a node -- cordon + signal agents to finish."""
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node = _get_node_registry(request).start_drain(node_id)
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


@router.get("/cluster/shard-map")
def get_shard_map(request: Request) -> ShardMapResponse:
    """Get shard topology map."""
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT

    _verify_cluster_auth(request, SCOPE_NODE_HEARTBEAT)
    registry = _get_node_registry(request)
    config = registry.config

    if not config.shards or not config.shard_id:
        raise HTTPException(status_code=404, detail="Cluster is not sharded")

    shard_nodes = [ShardNodeInfo(id=sid, url=url) for sid, url in config.shards]
    return ShardMapResponse(shard_id=config.shard_id, vnodes=SHARD_VNODES, shards=shard_nodes)


@router.get("/cluster/status")
def cluster_status(request: Request) -> ClusterStatusResponse:
    """Get cluster status summary."""
    summary = _get_node_registry(request).cluster_summary()
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


@router.post(
    "/cluster/claims/gossip",
    responses={
        401: {"description": "Cluster authentication failed"},
        409: {"description": "Node is not running the MESH topology"},
    },
)
def gossip_claims(body: ClaimGossipRequest, request: Request) -> ClaimGossipResponse:
    """Fold peer claim receipts into this node's signed journal (#2558).

    The leaderless counterpart to ``POST /cluster/steal``: no node decides who
    gets what here. Each receipt is folded only after its Ed25519 signature and
    its chain link both verify, so an unverifiable receipt is never written.

    A receipt that does not extend the local head is *not* merged. It produces
    a signed ``fork`` receipt carrying the divergence entry index, which the
    response surfaces through ``forked``. Silent merge would be the one failure
    mode a leaderless design cannot recover from: two partitions would each
    hold a coherent-looking journal describing incompatible work.

    Authorisation reuses the node-heartbeat scope: gossip is a peer-to-peer
    fleet-membership operation, not an administrative one.
    """
    from bernstein.core.cluster_auth import SCOPE_NODE_HEARTBEAT
    from bernstein.core.orchestration.tracker_pipeline import ClaimReceipt

    _verify_cluster_auth(request, SCOPE_NODE_HEARTBEAT)
    coordinator = _get_mesh_coordinator(request)

    try:
        receipts = [ClaimReceipt.from_dict(raw) for raw in body.receipts]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"malformed claim receipt: {exc}") from exc

    now = time.time()
    results: list[ClaimGossipResult] = []
    accepted = 0
    forked = False
    for receipt, outcome in zip(receipts, coordinator.ingest_many(receipts, now=now), strict=False):
        results.append(
            ClaimGossipResult(
                entry_hash=receipt.entry_hash,
                status=outcome.status,
                reason=outcome.reason,
                divergence_index=outcome.divergence_index,
            )
        )
        if outcome.status == "applied":
            accepted += 1
        if outcome.status == "forked":
            forked = True
    return ClaimGossipResponse(
        head=coordinator.head(),
        accepted=accepted,
        results=results,
        forked=forked,
    )


@router.post("/cluster/steal", responses=_AUTH_RESPONSES)
async def steal_tasks(body: TaskStealRequest, request: Request) -> TaskStealResponse:
    """Evaluate task stealing policy and reassign claimed tasks between nodes.

    Workers report their queue depths; the server runs the steal policy and
    returns a list of task reassignments.  Stolen tasks are reset to ``open``
    so the receiver node can claim them.

    Authorisation requires the node-admin scope, like the other node-registry
    mutations (cordon, uncordon, drain, unregister) this sits beside in the
    operational-primitives table.  It is deliberately NOT the heartbeat scope
    that ``POST /cluster/claims/gossip`` uses: gossip proves each receipt with
    its own Ed25519 signature and chain link inside the handler, so its bearer
    scope only has to establish fleet membership, whereas here the caller's
    reported queue depths drive ``force_claim`` directly with no further proof
    to check.
    """
    from bernstein.core.cluster import TaskStealPolicy
    from bernstein.core.cluster_auth import SCOPE_NODE_ADMIN

    _verify_cluster_auth(request, SCOPE_NODE_ADMIN)
    node_registry = _get_node_registry(request)
    store = _get_store(request)

    pairs = TaskStealPolicy().find_steal_pairs(node_registry, body.queue_depths)

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

        # The caller names no task here - the policy picks them - but the
        # force-claim below is the same mutation ``POST /tasks/{id}/force-claim``
        # performs behind the path-level scope gate. Bind the identity to the
        # ids the policy resolved so a task-scoped token cannot reset a task
        # it does not hold. Cluster peers authenticate with the shared secret
        # or a cluster JWT and never populate an agent identity, so this is a
        # no-op on the real redistribution path.
        enforce_agent_task_scope_for_ids(request, [task.id for task in donor_tasks])

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
