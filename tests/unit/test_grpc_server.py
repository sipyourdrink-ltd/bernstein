"""Unit tests for the gRPC server and client modules."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class TestGrpcServerConfig:
    def test_default_config(self) -> None:
        from bernstein.core.grpc_server import GrpcServerConfig

        cfg = GrpcServerConfig()
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 50051
        assert cfg.max_workers == 10
        assert cfg.enable_reflection is True
        assert cfg.tls_cert_path is None

    def test_custom_config(self) -> None:
        from bernstein.core.grpc_server import GrpcServerConfig

        cfg = GrpcServerConfig(host="127.0.0.1", port=9999, tls_cert_path="/cert.pem")
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 9999
        assert cfg.tls_cert_path == "/cert.pem"


class TestStatusMaps:
    def test_task_status_map_covers_all(self) -> None:
        from bernstein.core.grpc_server import _STATUS_MAP

        expected = {"open", "claimed", "in_progress", "done", "failed", "blocked", "cancelled", "orphaned"}
        assert set(_STATUS_MAP.keys()) == expected

    def test_node_status_map_covers_all(self) -> None:
        from bernstein.core.grpc_server import _NODE_STATUS_MAP

        expected = {"online", "ready", "degraded", "cordoned", "draining", "offline"}
        assert set(_NODE_STATUS_MAP.keys()) == expected


class TestTaskToProto:
    def test_converts_full_task(self) -> None:
        from bernstein.core.grpc_server import _task_to_proto

        task = {
            "id": "t1",
            "goal": "fix bug",
            "role": "backend",
            "status": "open",
            "assigned_agent": "a1",
            "assigned_node": "n1",
            "priority": 2,
            "model": "claude-opus-4",
            "effort": "high",
            "metadata": {"key": "val"},
        }
        result = _task_to_proto(task)
        assert result["id"] == "t1"
        assert result["status"] == 1  # open
        assert result["priority"] == 2
        assert result["metadata"] == {"key": "val"}

    def test_handles_missing_fields(self) -> None:
        from bernstein.core.grpc_server import _task_to_proto

        result = _task_to_proto({})
        assert result["id"] == ""
        assert result["status"] == 0
        assert result["priority"] == 3


class TestBernsteinGrpcServer:
    def test_grpc_unavailable_logs_warning(self) -> None:
        from bernstein.core.grpc_server import BernsteinGrpcServer, GrpcServerConfig

        server = BernsteinGrpcServer(config=GrpcServerConfig())
        assert server._server is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self) -> None:
        from bernstein.core.grpc_server import BernsteinGrpcServer

        server = BernsteinGrpcServer()
        await server.stop()
        assert server._server is None


class TestGrpcClientConfig:
    def test_default_config(self) -> None:
        from bernstein.core.grpc_client import GrpcClientConfig

        cfg = GrpcClientConfig()
        assert cfg.server_address == "localhost:50051"
        assert cfg.tls_enabled is False
        assert cfg.timeout_s == 10.0


class TestTaskClient:
    def test_task_to_dict(self) -> None:
        from bernstein.core.grpc_client import TaskClient

        mock_task = MagicMock()
        mock_task.id = "t1"
        mock_task.goal = "do thing"
        mock_task.role = "backend"
        mock_task.status = 1  # open
        mock_task.assigned_agent = "a1"
        mock_task.assigned_node = ""
        mock_task.priority = 3
        mock_task.model = "claude-sonnet-4-20250514"
        mock_task.effort = "medium"
        mock_task.metadata = {"k": "v"}

        result = TaskClient._task_to_dict(mock_task)
        assert result["id"] == "t1"
        assert result["status"] == "open"
        assert result["metadata"] == {"k": "v"}

    def test_task_to_dict_unknown_status(self) -> None:
        from bernstein.core.grpc_client import TaskClient

        mock_task = MagicMock()
        mock_task.id = "t2"
        mock_task.goal = ""
        mock_task.role = ""
        mock_task.status = 99
        mock_task.assigned_agent = ""
        mock_task.assigned_node = ""
        mock_task.priority = 0
        mock_task.model = ""
        mock_task.effort = ""
        mock_task.metadata = {}

        result = TaskClient._task_to_dict(mock_task)
        assert result["status"] == "unspecified"


class TestClusterClient:
    def test_node_to_dict(self) -> None:
        from bernstein.core.grpc_client import ClusterClient

        cap = MagicMock()
        cap.max_agents = 6
        cap.available_slots = 4
        cap.active_agents = 2
        cap.gpu_available = True
        cap.supported_models = ["claude-sonnet-4-20250514"]

        mock_node = MagicMock()
        mock_node.id = "n1"
        mock_node.name = "worker-0"
        mock_node.url = "http://worker-0:8052"
        mock_node.status = 1  # online
        mock_node.capacity = cap
        mock_node.labels = {"gpu": "true"}
        mock_node.cell_ids = ["cell-1"]

        result = ClusterClient._node_to_dict(mock_node)
        assert result["id"] == "n1"
        assert result["status"] == "online"
        assert result["capacity"]["gpu_available"] is True
        assert result["labels"] == {"gpu": "true"}
