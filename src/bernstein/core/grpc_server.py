"""gRPC server for cluster-internal communication.

Runs alongside the REST server on a separate port (default 50051).
REST remains the external API; gRPC handles node-to-node and
orchestrator-to-agent traffic with lower latency and binary encoding.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    import grpc
    from grpc import aio as grpc_aio

    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

if TYPE_CHECKING:
    from bernstein.core.cluster import NodeRegistry

logger = logging.getLogger(__name__)

_STATUS_MAP: dict[str, int] = {
    "open": 1,
    "claimed": 2,
    "in_progress": 3,
    "done": 4,
    "failed": 5,
    "blocked": 6,
    "cancelled": 7,
    "orphaned": 8,
}

_NODE_STATUS_MAP: dict[str, int] = {
    "online": 1,
    "ready": 2,
    "degraded": 3,
    "cordoned": 4,
    "draining": 5,
    "offline": 6,
}


@dataclass(frozen=True)
class GrpcServerConfig:
    """Configuration for the gRPC server."""

    host: str = "0.0.0.0"
    port: int = 50051
    max_workers: int = 10
    max_message_length: int = 16 * 1024 * 1024  # 16 MB
    enable_reflection: bool = True
    tls_cert_path: str | None = None
    tls_key_path: str | None = None


def _task_to_proto(task: dict[str, Any]) -> dict[str, Any]:
    """Convert internal task dict to proto-compatible dict."""
    return {
        "id": task.get("id", ""),
        "goal": task.get("goal", ""),
        "role": task.get("role", ""),
        "status": _STATUS_MAP.get(task.get("status", ""), 0),
        "assigned_agent": task.get("assigned_agent", ""),
        "assigned_node": task.get("assigned_node", ""),
        "priority": task.get("priority", 3),
        "model": task.get("model", ""),
        "effort": task.get("effort", ""),
        "metadata": task.get("metadata", {}),
    }


def _node_to_proto(node: Any) -> dict[str, Any]:
    """Convert NodeInfo to proto-compatible dict."""
    cap = node.capacity
    return {
        "id": node.id,
        "name": node.name,
        "url": node.url,
        "capacity": {
            "max_agents": cap.max_agents,
            "available_slots": cap.available_slots,
            "active_agents": cap.active_agents,
            "gpu_available": cap.gpu_available,
            "supported_models": list(cap.supported_models),
        },
        "status": _NODE_STATUS_MAP.get(node.status.value if hasattr(node.status, "value") else str(node.status), 0),
        "labels": dict(node.labels) if node.labels else {},
        "cell_ids": list(node.cell_ids) if node.cell_ids else [],
    }


class TaskServiceImpl:
    """gRPC implementation of TaskService, bridging to the task store."""

    def __init__(self, task_store: Any) -> None:
        self._store = task_store

    async def CreateTask(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.create(
            goal=request.goal,
            role=request.role or "backend",
            priority=request.priority or 3,
            model=request.model or None,
            effort=request.effort or None,
            metadata=dict(request.metadata) if request.metadata else None,
        )
        resp = tasks_pb2.TaskResponse()
        self._fill_task_proto(resp.task, task)
        return resp

    async def ClaimTask(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.get(request.task_id)
        if task is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        self._store.claim(request.task_id, request.agent_id)
        if request.node_id:
            task_dict = self._store.get(request.task_id)
            if task_dict and hasattr(task_dict, "__setitem__"):
                task_dict["assigned_node"] = request.node_id
        task = self._store.get(request.task_id)
        resp = tasks_pb2.TaskResponse()
        self._fill_task_proto(resp.task, task)
        return resp

    async def CompleteTask(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.get(request.task_id)
        if task is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        self._store.complete(
            request.task_id,
            result_summary=request.result_summary,
        )
        task = self._store.get(request.task_id)
        resp = tasks_pb2.TaskResponse()
        self._fill_task_proto(resp.task, task)
        return resp

    async def FailTask(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.get(request.task_id)
        if task is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        self._store.fail(request.task_id, error=request.error)
        task = self._store.get(request.task_id)
        resp = tasks_pb2.TaskResponse()
        self._fill_task_proto(resp.task, task)
        return resp

    async def ReportProgress(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.get(request.task_id)
        if task is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        resp = tasks_pb2.ProgressResponse(acknowledged=True)
        return resp

    async def ListTasks(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        status_name = None
        if request.status_filter:
            reverse_map = {v: k for k, v in _STATUS_MAP.items()}
            status_name = reverse_map.get(request.status_filter)
        tasks = self._store.list(status=status_name)
        if request.role_filter:
            tasks = [t for t in tasks if t.get("role") == request.role_filter]
        if request.node_filter:
            tasks = [t for t in tasks if t.get("assigned_node") == request.node_filter]
        limit = request.limit or 100
        tasks = tasks[:limit]
        resp = tasks_pb2.ListTasksResponse(total_count=len(tasks))
        for t in tasks:
            task_msg = resp.tasks.add()
            self._fill_task_proto(task_msg, t)
        return resp

    async def GetTask(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import tasks_pb2

        task = self._store.get(request.task_id)
        if task is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
        resp = tasks_pb2.TaskResponse()
        self._fill_task_proto(resp.task, task)
        return resp

    def _fill_task_proto(self, proto: Any, task: Any) -> None:
        if task is None:
            return
        t = task if isinstance(task, dict) else vars(task)
        proto.id = t.get("id", "")
        proto.goal = t.get("goal", "")
        proto.role = t.get("role", "")
        proto.status = _STATUS_MAP.get(t.get("status", ""), 0)
        proto.assigned_agent = t.get("assigned_agent", "") or ""
        proto.assigned_node = t.get("assigned_node", "") or ""
        proto.priority = t.get("priority", 3)
        proto.model = t.get("model", "") or ""
        proto.effort = t.get("effort", "") or ""


class ClusterServiceImpl:
    """gRPC implementation of ClusterService, bridging to NodeRegistry."""

    def __init__(self, node_registry: NodeRegistry) -> None:
        self._registry = node_registry

    async def RegisterNode(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2
        from bernstein.core.models import NodeCapacity

        cap = NodeCapacity(
            max_agents=request.capacity.max_agents or 6,
            available_slots=request.capacity.available_slots or 6,
            active_agents=request.capacity.active_agents,
            gpu_available=request.capacity.gpu_available,
            supported_models=list(request.capacity.supported_models),
        )
        node = self._registry.register(
            name=request.name,
            url=request.url,
            capacity=cap,
            labels=dict(request.labels) if request.labels else {},
            cell_ids=list(request.cell_ids) if request.cell_ids else [],
        )
        resp = cluster_pb2.RegisterNodeResponse()
        self._fill_node_proto(resp.node, node)
        return resp

    async def Heartbeat(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        node = self._registry.get(request.node_id)
        if node is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "node not registered")
        if request.HasField("capacity"):
            from bernstein.core.models import NodeCapacity

            cap = NodeCapacity(
                max_agents=request.capacity.max_agents,
                available_slots=request.capacity.available_slots,
                active_agents=request.capacity.active_agents,
                gpu_available=request.capacity.gpu_available,
                supported_models=list(request.capacity.supported_models),
            )
        else:
            cap = None
        node = self._registry.heartbeat(request.node_id, capacity=cap)
        resp = cluster_pb2.HeartbeatResponse(acknowledged=True)
        if node:
            self._fill_node_proto(resp.node, node)
        return resp

    async def StreamHeartbeats(self, request_iterator: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        async for request in request_iterator:
            node = self._registry.heartbeat(request.node_id)
            resp = cluster_pb2.HeartbeatResponse(acknowledged=True)
            if node := self._registry.get(request.node_id):
                self._fill_node_proto(resp.node, node)
            yield resp

    async def UnregisterNode(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        removed = self._registry.unregister(request.node_id)
        return cluster_pb2.UnregisterNodeResponse(removed=removed)

    async def CordonNode(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        node = self._registry.get(request.node_id)
        if node is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "node not found")
        self._registry.cordon(request.node_id)
        return cluster_pb2.NodeStatusResponse(
            node_id=request.node_id,
            status=4,  # CORDONED
        )

    async def UncordonNode(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        node = self._registry.get(request.node_id)
        if node is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "node not found")
        self._registry.uncordon(request.node_id)
        return cluster_pb2.NodeStatusResponse(
            node_id=request.node_id,
            status=1,  # ONLINE
        )

    async def DrainNode(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        node = self._registry.get(request.node_id)
        if node is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "node not found")
        self._registry.start_drain(request.node_id)
        return cluster_pb2.NodeStatusResponse(
            node_id=request.node_id,
            status=5,  # DRAINING
        )

    async def ListNodes(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        nodes = self._registry.list_nodes()
        if request.status_filter:
            reverse_map = {v: k for k, v in _NODE_STATUS_MAP.items()}
            filter_name = reverse_map.get(request.status_filter)
            if filter_name:
                nodes = [
                    n for n in nodes if (n.status.value if hasattr(n.status, "value") else str(n.status)) == filter_name
                ]
        resp = cluster_pb2.ListNodesResponse()
        for n in nodes:
            node_msg = resp.nodes.add()
            self._fill_node_proto(node_msg, n)
        return resp

    async def GetClusterStatus(self, request: Any, context: Any) -> Any:
        from bernstein.core.grpc_gen import cluster_pb2

        summary = self._registry.cluster_summary()
        resp = cluster_pb2.ClusterStatusResponse(
            topology=summary.get("topology", "star"),
            total_nodes=summary.get("total_nodes", 0),
            online_nodes=summary.get("online_nodes", 0),
            offline_nodes=summary.get("offline_nodes", 0),
            total_capacity=summary.get("total_capacity", 0),
            available_slots=summary.get("available_slots", 0),
            active_agents=summary.get("active_agents", 0),
        )
        for n in self._registry.list_nodes():
            node_msg = resp.nodes.add()
            self._fill_node_proto(node_msg, n)
        return resp

    def _fill_node_proto(self, proto: Any, node: Any) -> None:
        proto.id = node.id
        proto.name = node.name
        proto.url = node.url
        cap = node.capacity
        proto.capacity.max_agents = cap.max_agents
        proto.capacity.available_slots = cap.available_slots
        proto.capacity.active_agents = cap.active_agents
        proto.capacity.gpu_available = cap.gpu_available
        proto.capacity.supported_models[:] = list(cap.supported_models)
        proto.status = _NODE_STATUS_MAP.get(node.status.value if hasattr(node.status, "value") else str(node.status), 0)
        if node.labels:
            for k, v in node.labels.items():
                proto.labels[k] = v
        if node.cell_ids:
            proto.cell_ids[:] = list(node.cell_ids)


@dataclass
class BernsteinGrpcServer:
    """Async gRPC server wrapping task and cluster services."""

    config: GrpcServerConfig = field(default_factory=GrpcServerConfig)
    _server: Any = field(default=None, init=False, repr=False)

    async def start(
        self,
        task_store: Any,
        node_registry: NodeRegistry | None = None,
    ) -> None:
        if not GRPC_AVAILABLE:
            logger.warning("grpcio not installed — gRPC server disabled")
            return

        from bernstein.core.grpc_gen import tasks_pb2_grpc

        server = grpc_aio.server(
            options=[
                ("grpc.max_send_message_length", self.config.max_message_length),
                ("grpc.max_receive_message_length", self.config.max_message_length),
                ("grpc.keepalive_time_ms", 30_000),
                ("grpc.keepalive_timeout_ms", 10_000),
                ("grpc.keepalive_permit_without_calls", True),
            ],
        )

        tasks_pb2_grpc.add_TaskServiceServicer_to_server(TaskServiceImpl(task_store), server)

        if node_registry is not None:
            from bernstein.core.grpc_gen import cluster_pb2_grpc

            cluster_pb2_grpc.add_ClusterServiceServicer_to_server(ClusterServiceImpl(node_registry), server)

        if self.config.enable_reflection:
            try:
                from grpc_reflection.v1alpha import reflection

                from bernstein.core.grpc_gen import cluster_pb2, tasks_pb2

                service_names = [
                    tasks_pb2.DESCRIPTOR.services_by_name["TaskService"].full_name,
                ]
                if node_registry is not None:
                    service_names.append(cluster_pb2.DESCRIPTOR.services_by_name["ClusterService"].full_name)
                    service_names.append(cluster_pb2.DESCRIPTOR.services_by_name["BulletinService"].full_name)
                service_names.append(reflection.SERVICE_NAME)
                reflection.enable_server_reflection(service_names, server)
            except ImportError:
                logger.debug("grpc-reflection not installed, skipping")

        bind = f"{self.config.host}:{self.config.port}"

        if self.config.tls_cert_path and self.config.tls_key_path:
            cert = await asyncio.to_thread(Path(self.config.tls_cert_path).read_bytes)
            key = await asyncio.to_thread(Path(self.config.tls_key_path).read_bytes)
            creds = grpc.ssl_server_credentials([(key, cert)])
            server.add_secure_port(bind, creds)
            logger.info("gRPC server listening on %s (TLS)", bind)
        else:
            server.add_insecure_port(bind)
            logger.info("gRPC server listening on %s (insecure)", bind)

        await server.start()
        self._server = server

    async def stop(self, grace: float = 5.0) -> None:
        if self._server is not None:
            await self._server.stop(grace)
            self._server = None
            logger.info("gRPC server stopped")

    async def wait_for_termination(self) -> None:
        if self._server is not None:
            await self._server.wait_for_termination()
