"""TEST-017: API contract tests for all HTTP endpoints.

Tests that request/response schemas match the Pydantic models
defined in server.py.  Validates field presence, types, and
required vs optional fields for every public endpoint schema.
"""

from __future__ import annotations

import json
from typing import Any, get_type_hints

import pytest
from pydantic import BaseModel, ValidationError

from bernstein.core.server import (
    BatchClaimRequest,
    BatchClaimResponse,
    BulletinMessageResponse,
    BulletinPostRequest,
    ClusterStatusResponse,
    HealthResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    NodeCapacitySchema,
    NodeHeartbeatRequest,
    NodeRegisterRequest,
    NodeResponse,
    PaginatedTasksResponse,
    RoleCounts,
    StatusResponse,
    TaskBlockRequest,
    TaskCancelRequest,
    TaskCompleteRequest,
    TaskCountsResponse,
    TaskCreate,
    TaskFailRequest,
    TaskPatchRequest,
    TaskProgressRequest,
    TaskResponse,
    TaskStealRequest,
    TaskStealResponse,
    TaskWaitForSubtasksRequest,
)

# ---------------------------------------------------------------------------
# Contract helpers
# ---------------------------------------------------------------------------


def _validate_round_trip(model_cls: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    """Validate data through the model and ensure JSON round-trip."""
    instance = model_cls.model_validate(data)
    dumped = instance.model_dump()
    reparsed = model_cls.model_validate(dumped)
    assert reparsed.model_dump() == dumped
    return instance


def _assert_rejects(model_cls: type[BaseModel], data: dict[str, Any]) -> None:
    """Assert that the model rejects the given data."""
    with pytest.raises(ValidationError):
        model_cls.model_validate(data)


# ---------------------------------------------------------------------------
# TaskCreate contract
# ---------------------------------------------------------------------------


class TestTaskCreateContract:
    """POST /tasks request body contract."""

    def test_minimal_valid(self) -> None:
        _validate_round_trip(
            TaskCreate,
            {
                "title": "Fix bug",
                "description": "Fix the login bug",
            },
        )

    def test_full_valid(self) -> None:
        _validate_round_trip(
            TaskCreate,
            {
                "title": "Add feature",
                "description": "Implement OAuth",
                "role": "backend",
                "tenant_id": "acme",
                "priority": 1,
                "scope": "large",
                "complexity": "high",
                "estimated_minutes": 120,
                "depends_on": ["task-001"],
                "owned_files": ["src/auth.py"],
                "task_type": "standard",
                "model": "opus",
                "effort": "max",
                "batch_eligible": False,
            },
        )

    def test_missing_title_rejected(self) -> None:
        _assert_rejects(TaskCreate, {"description": "no title"})

    def test_missing_description_rejected(self) -> None:
        _assert_rejects(TaskCreate, {"title": "no desc"})


# ---------------------------------------------------------------------------
# TaskResponse contract
# ---------------------------------------------------------------------------


class TestTaskResponseContract:
    """Task response body contract."""

    _FULL_RESPONSE: dict[str, Any] = {
        "id": "task-001",
        "title": "Fix bug",
        "description": "Fix the login bug",
        "role": "backend",
        "tenant_id": "default",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "eu_ai_act_risk": "minimal",
        "approval_required": False,
        "risk_level": "low",
        "estimated_minutes": 30,
        "status": "open",
        "depends_on": [],
        "parent_task_id": None,
        "depends_on_repo": None,
        "owned_files": [],
        "assigned_agent": None,
        "result_summary": None,
        "cell_id": None,
        "repo": None,
        "task_type": "standard",
        "upgrade_details": None,
        "model": None,
        "effort": None,
        "created_at": 1700000000.0,
    }

    def test_valid_response(self) -> None:
        _validate_round_trip(TaskResponse, self._FULL_RESPONSE)

    def test_all_status_values_accepted(self) -> None:
        from bernstein.core.models import TaskStatus

        for s in TaskStatus:
            data = {**self._FULL_RESPONSE, "status": s.value}
            _validate_round_trip(TaskResponse, data)

    def test_json_serializable(self) -> None:
        resp = TaskResponse.model_validate(self._FULL_RESPONSE)
        json_str = json.dumps(resp.model_dump())
        assert json.loads(json_str)["id"] == "task-001"


# ---------------------------------------------------------------------------
# TaskCompleteRequest contract
# ---------------------------------------------------------------------------


class TestTaskCompleteRequestContract:
    def test_valid(self) -> None:
        _validate_round_trip(TaskCompleteRequest, {"result_summary": "All tests pass"})

    def test_missing_summary_rejected(self) -> None:
        _assert_rejects(TaskCompleteRequest, {})


# ---------------------------------------------------------------------------
# TaskFailRequest contract
# ---------------------------------------------------------------------------


class TestTaskFailRequestContract:
    def test_with_reason(self) -> None:
        _validate_round_trip(TaskFailRequest, {"reason": "compile error"})

    def test_empty_reason_default(self) -> None:
        req = TaskFailRequest.model_validate({})
        assert req.reason == ""


# ---------------------------------------------------------------------------
# TaskCancelRequest contract
# ---------------------------------------------------------------------------


class TestTaskCancelRequestContract:
    def test_with_reason(self) -> None:
        _validate_round_trip(TaskCancelRequest, {"reason": "no longer needed"})

    def test_empty_default(self) -> None:
        req = TaskCancelRequest.model_validate({})
        assert req.reason == ""


# ---------------------------------------------------------------------------
# TaskBlockRequest contract
# ---------------------------------------------------------------------------


class TestTaskBlockRequestContract:
    def test_with_reason(self) -> None:
        _validate_round_trip(TaskBlockRequest, {"reason": "waiting for dependency"})


# ---------------------------------------------------------------------------
# TaskPatchRequest contract
# ---------------------------------------------------------------------------


class TestTaskPatchRequestContract:
    def test_partial_update(self) -> None:
        _validate_round_trip(TaskPatchRequest, {"role": "qa"})

    def test_all_fields(self) -> None:
        _validate_round_trip(TaskPatchRequest, {"role": "qa", "priority": 1, "model": "opus"})

    def test_empty_valid(self) -> None:
        _validate_round_trip(TaskPatchRequest, {})


# ---------------------------------------------------------------------------
# TaskProgressRequest contract
# ---------------------------------------------------------------------------


class TestTaskProgressRequestContract:
    def test_minimal(self) -> None:
        _validate_round_trip(TaskProgressRequest, {})

    def test_full(self) -> None:
        _validate_round_trip(
            TaskProgressRequest,
            {
                "message": "50% done",
                "percent": 50,
                "files_changed": 3,
                "tests_passing": 10,
                "errors": 0,
                "last_file": "src/foo.py",
            },
        )


# ---------------------------------------------------------------------------
# StatusResponse contract
# ---------------------------------------------------------------------------


class TestStatusResponseContract:
    def test_valid(self) -> None:
        _validate_round_trip(
            StatusResponse,
            {
                "total": 5,
                "open": 2,
                "claimed": 1,
                "done": 1,
                "failed": 1,
                "per_role": [
                    {"role": "backend", "open": 1, "claimed": 1, "done": 0, "failed": 0},
                ],
            },
        )

    def test_empty_per_role(self) -> None:
        _validate_round_trip(
            StatusResponse,
            {
                "total": 0,
                "open": 0,
                "claimed": 0,
                "done": 0,
                "failed": 0,
                "per_role": [],
            },
        )


# ---------------------------------------------------------------------------
# HealthResponse contract
# ---------------------------------------------------------------------------


class TestHealthResponseContract:
    def test_valid(self) -> None:
        _validate_round_trip(
            HealthResponse,
            {
                "status": "ok",
                "uptime_s": 3600.0,
                "task_count": 10,
                "agent_count": 3,
            },
        )

    def test_full(self) -> None:
        _validate_round_trip(
            HealthResponse,
            {
                "status": "ok",
                "uptime_s": 7200.0,
                "task_count": 20,
                "agent_count": 5,
                "task_queue_depth": 8,
                "memory_mb": 256.0,
                "restart_count": 1,
                "is_readonly": False,
                "components": {"store": {"status": "ok"}},
            },
        )


# ---------------------------------------------------------------------------
# HeartbeatRequest/Response contracts
# ---------------------------------------------------------------------------


class TestHeartbeatContract:
    def test_request_valid(self) -> None:
        _validate_round_trip(HeartbeatRequest, {"role": "backend", "status": "working"})

    def test_request_defaults(self) -> None:
        req = HeartbeatRequest.model_validate({})
        assert req.status == "working"

    def test_response_valid(self) -> None:
        _validate_round_trip(
            HeartbeatResponse,
            {
                "agent_id": "agent-001",
                "acknowledged": True,
                "server_ts": 1700000000.0,
            },
        )


# ---------------------------------------------------------------------------
# BulletinPostRequest contract
# ---------------------------------------------------------------------------


class TestBulletinPostRequestContract:
    def test_valid(self) -> None:
        _validate_round_trip(
            BulletinPostRequest,
            {
                "agent_id": "agent-001",
                "content": "Found a blocker in auth module",
            },
        )


# ---------------------------------------------------------------------------
# BatchClaimRequest/Response contracts
# ---------------------------------------------------------------------------


class TestBatchClaimContract:
    def test_request_valid(self) -> None:
        _validate_round_trip(
            BatchClaimRequest,
            {
                "task_ids": ["t1", "t2"],
                "agent_id": "agent-001",
            },
        )

    def test_response_valid(self) -> None:
        _validate_round_trip(
            BatchClaimResponse,
            {
                "claimed": ["t1"],
                "failed": ["t2"],
            },
        )


# ---------------------------------------------------------------------------
# TaskCountsResponse contract
# ---------------------------------------------------------------------------


class TestTaskCountsResponseContract:
    def test_defaults(self) -> None:
        resp = TaskCountsResponse.model_validate({})
        assert resp.total == 0
        assert resp.open == 0

    def test_full(self) -> None:
        _validate_round_trip(
            TaskCountsResponse,
            {
                "open": 5,
                "claimed": 3,
                "done": 10,
                "failed": 1,
                "blocked": 2,
                "cancelled": 0,
                "total": 21,
            },
        )


# ---------------------------------------------------------------------------
# PaginatedTasksResponse contract
# ---------------------------------------------------------------------------


class TestPaginatedTasksResponseContract:
    def test_empty(self) -> None:
        _validate_round_trip(
            PaginatedTasksResponse,
            {
                "tasks": [],
                "total": 0,
                "limit": 50,
                "offset": 0,
            },
        )


# ---------------------------------------------------------------------------
# ClusterStatusResponse contract
# ---------------------------------------------------------------------------


class TestClusterStatusResponseContract:
    def test_valid(self) -> None:
        _validate_round_trip(
            ClusterStatusResponse,
            {
                "topology": "star",
                "total_nodes": 2,
                "online_nodes": 2,
                "offline_nodes": 0,
                "total_capacity": 12,
                "available_slots": 8,
                "active_agents": 4,
                "nodes": [],
            },
        )
