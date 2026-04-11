"""gRPC client for cluster-internal communication.

Drop-in replacement for HTTP-based cluster communication with lower
latency and binary encoding.  Falls back gracefully when grpcio is
not installed or the server is unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

try:
    import grpc
    from grpc import aio as grpc_aio

    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrpcClientConfig:
    """Configuration for gRPC client connections."""

    server_address: str = "localhost:50051"
    tls_enabled: bool = False
    tls_ca_cert_path: str | None = None
    timeout_s: float = 10.0
    max_message_length: int = 16 * 1024 * 1024


@dataclass
class TaskClient:
    """gRPC client for task operations."""

    config: GrpcClientConfig = field(default_factory=GrpcClientConfig)
    _channel: Any = field(default=None, init=False, repr=False)
    _stub: Any = field(default=None, init=False, repr=False)

    async def connect(self) -> None:
        if not GRPC_AVAILABLE:
            raise RuntimeError("grpcio not installed")

        from bernstein.core.grpc_gen import tasks_pb2_grpc

        opts = [
            ("grpc.max_send_message_length", self.config.max_message_length),
            ("grpc.max_receive_message_length", self.config.max_message_length),
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
        ]
        if self.config.tls_enabled and self.config.tls_ca_cert_path:
            with open(self.config.tls_ca_cert_path, "rb") as f:
                ca_cert = f.read()
            creds = grpc.ssl_channel_credentials(root_certificates=ca_cert)
            self._channel = grpc_aio.secure_channel(
                self.config.server_address, creds, options=opts
            )
        else:
            self._channel = grpc_aio.insecure_channel(
                self.config.server_address, options=opts
            )
        self._stub = tasks_pb2_grpc.TaskServiceStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def create_task(
        self,
        goal: str,
        role: str = "backend",
        priority: int = 3,
        model: str = "",
        effort: str = "",
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import tasks_pb2

        req = tasks_pb2.CreateTaskRequest(
            goal=goal, role=role, priority=priority, model=model, effort=effort
        )
        resp = await self._stub.CreateTask(req, timeout=self.config.timeout_s)
        return self._task_to_dict(resp.task)

    async def claim_task(
        self, task_id: str, agent_id: str, node_id: str = ""
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import tasks_pb2

        req = tasks_pb2.ClaimTaskRequest(
            task_id=task_id, agent_id=agent_id, node_id=node_id
        )
        resp = await self._stub.ClaimTask(req, timeout=self.config.timeout_s)
        return self._task_to_dict(resp.task)

    async def complete_task(
        self,
        task_id: str,
        result_summary: str = "",
        files_changed: list[str] | None = None,
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import tasks_pb2

        req = tasks_pb2.CompleteTaskRequest(
            task_id=task_id,
            result_summary=result_summary,
            files_changed=files_changed or [],
        )
        resp = await self._stub.CompleteTask(req, timeout=self.config.timeout_s)
        return self._task_to_dict(resp.task)

    async def fail_task(
        self, task_id: str, error: str = "", retryable: bool = False
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import tasks_pb2

        req = tasks_pb2.FailTaskRequest(
            task_id=task_id, error=error, retryable=retryable
        )
        resp = await self._stub.FailTask(req, timeout=self.config.timeout_s)
        return self._task_to_dict(resp.task)

    async def list_tasks(
        self,
        status: str | None = None,
        role: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        from bernstein.core.grpc_gen import tasks_pb2

        status_map = {
            "open": 1, "claimed": 2, "in_progress": 3, "done": 4,
            "failed": 5, "blocked": 6, "cancelled": 7, "orphaned": 8,
        }
        req = tasks_pb2.ListTasksRequest(
            status_filter=status_map.get(status or "", 0),
            role_filter=role or "",
            limit=limit,
        )
        resp = await self._stub.ListTasks(req, timeout=self.config.timeout_s)
        return [self._task_to_dict(t) for t in resp.tasks]

    async def get_task(self, task_id: str) -> dict[str, Any]:
        from bernstein.core.grpc_gen import tasks_pb2

        req = tasks_pb2.GetTaskRequest(task_id=task_id)
        resp = await self._stub.GetTask(req, timeout=self.config.timeout_s)
        return self._task_to_dict(resp.task)

    @staticmethod
    def _task_to_dict(task: Any) -> dict[str, Any]:
        status_names = {
            0: "unspecified", 1: "open", 2: "claimed", 3: "in_progress",
            4: "done", 5: "failed", 6: "blocked", 7: "cancelled", 8: "orphaned",
        }
        return {
            "id": task.id,
            "goal": task.goal,
            "role": task.role,
            "status": status_names.get(task.status, "unspecified"),
            "assigned_agent": task.assigned_agent,
            "assigned_node": task.assigned_node,
            "priority": task.priority,
            "model": task.model,
            "effort": task.effort,
            "metadata": dict(task.metadata),
        }


@dataclass
class ClusterClient:
    """gRPC client for cluster operations (heartbeats, node management)."""

    config: GrpcClientConfig = field(default_factory=GrpcClientConfig)
    _channel: Any = field(default=None, init=False, repr=False)
    _stub: Any = field(default=None, init=False, repr=False)

    async def connect(self) -> None:
        if not GRPC_AVAILABLE:
            raise RuntimeError("grpcio not installed")

        from bernstein.core.grpc_gen import cluster_pb2_grpc

        opts = [
            ("grpc.max_send_message_length", self.config.max_message_length),
            ("grpc.max_receive_message_length", self.config.max_message_length),
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
        ]
        if self.config.tls_enabled and self.config.tls_ca_cert_path:
            with open(self.config.tls_ca_cert_path, "rb") as f:
                ca_cert = f.read()
            creds = grpc.ssl_channel_credentials(root_certificates=ca_cert)
            self._channel = grpc_aio.secure_channel(
                self.config.server_address, creds, options=opts
            )
        else:
            self._channel = grpc_aio.insecure_channel(
                self.config.server_address, options=opts
            )
        self._stub = cluster_pb2_grpc.ClusterServiceStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def register_node(
        self,
        name: str,
        url: str,
        max_agents: int = 6,
        supported_models: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import cluster_pb2

        cap = cluster_pb2.NodeCapacity(
            max_agents=max_agents,
            available_slots=max_agents,
            supported_models=supported_models or [],
        )
        req = cluster_pb2.RegisterNodeRequest(
            name=name, url=url, capacity=cap,
            labels=labels or {},
        )
        resp = await self._stub.RegisterNode(req, timeout=self.config.timeout_s)
        return {
            "node": self._node_to_dict(resp.node),
            "auth_token": resp.auth_token,
        }

    async def heartbeat(
        self,
        node_id: str,
        available_slots: int | None = None,
        active_agents: int | None = None,
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import cluster_pb2

        cap = None
        if available_slots is not None or active_agents is not None:
            cap = cluster_pb2.NodeCapacity(
                available_slots=available_slots or 0,
                active_agents=active_agents or 0,
            )
        req = cluster_pb2.HeartbeatRequest(node_id=node_id, capacity=cap)
        resp = await self._stub.Heartbeat(req, timeout=self.config.timeout_s)
        return {
            "acknowledged": resp.acknowledged,
            "node": self._node_to_dict(resp.node) if resp.HasField("node") else None,
        }

    async def unregister_node(self, node_id: str) -> bool:
        from bernstein.core.grpc_gen import cluster_pb2

        req = cluster_pb2.UnregisterNodeRequest(node_id=node_id)
        resp = await self._stub.UnregisterNode(req, timeout=self.config.timeout_s)
        return resp.removed

    async def cluster_status(self) -> dict[str, Any]:
        from bernstein.core.grpc_gen import cluster_pb2

        req = cluster_pb2.ClusterStatusRequest()
        resp = await self._stub.GetClusterStatus(req, timeout=self.config.timeout_s)
        return {
            "topology": resp.topology,
            "total_nodes": resp.total_nodes,
            "online_nodes": resp.online_nodes,
            "offline_nodes": resp.offline_nodes,
            "total_capacity": resp.total_capacity,
            "available_slots": resp.available_slots,
            "active_agents": resp.active_agents,
            "nodes": [self._node_to_dict(n) for n in resp.nodes],
        }

    async def steal_tasks(
        self, queue_depths: dict[str, int]
    ) -> dict[str, Any]:
        from bernstein.core.grpc_gen import cluster_pb2

        req = cluster_pb2.StealTasksRequest(queue_depths=queue_depths)
        resp = await self._stub.StealTasks(req, timeout=self.config.timeout_s)
        return {
            "actions": [
                {
                    "donor_node_id": a.donor_node_id,
                    "receiver_node_id": a.receiver_node_id,
                    "task_ids": list(a.task_ids),
                }
                for a in resp.actions
            ],
            "total_stolen": resp.total_stolen,
        }

    @staticmethod
    def _node_to_dict(node: Any) -> dict[str, Any]:
        status_names = {
            0: "unspecified", 1: "online", 2: "ready", 3: "degraded",
            4: "cordoned", 5: "draining", 6: "offline",
        }
        return {
            "id": node.id,
            "name": node.name,
            "url": node.url,
            "status": status_names.get(node.status, "unspecified"),
            "capacity": {
                "max_agents": node.capacity.max_agents,
                "available_slots": node.capacity.available_slots,
                "active_agents": node.capacity.active_agents,
                "gpu_available": node.capacity.gpu_available,
                "supported_models": list(node.capacity.supported_models),
            },
            "labels": dict(node.labels),
            "cell_ids": list(node.cell_ids),
        }
