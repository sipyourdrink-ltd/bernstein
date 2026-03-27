"""Tests for bernstein_sdk.models."""

from __future__ import annotations

from bernstein_sdk.models import (
    StatusSummary,
    TaskComplexity,
    TaskCreate,
    TaskResponse,
    TaskScope,
    TaskStatus,
    TaskUpdate,
)


class TestTaskCreate:
    def test_defaults(self) -> None:
        t = TaskCreate(title="Fix bug")
        assert t.role == "backend"
        assert t.priority == 2
        assert t.scope == TaskScope.MEDIUM
        assert t.complexity == TaskComplexity.MEDIUM
        assert t.estimated_minutes == 30
        assert t.depends_on == []
        assert t.metadata == {}

    def test_to_api_payload_minimal(self) -> None:
        t = TaskCreate(title="Fix bug")
        payload = t.to_api_payload()
        assert payload["title"] == "Fix bug"
        assert payload["scope"] == "medium"
        assert payload["complexity"] == "medium"
        assert "depends_on" not in payload
        assert "external_ref" not in payload
        assert "metadata" not in payload

    def test_to_api_payload_full(self) -> None:
        t = TaskCreate(
            title="Add index",
            role="qa",
            priority=1,
            scope=TaskScope.SMALL,
            complexity=TaskComplexity.HIGH,
            depends_on=["abc123"],
            external_ref="jira:PROJ-42",
            metadata={"key": "val"},
        )
        payload = t.to_api_payload()
        assert payload["role"] == "qa"
        assert payload["scope"] == "small"
        assert payload["complexity"] == "high"
        assert payload["depends_on"] == ["abc123"]
        assert payload["external_ref"] == "jira:PROJ-42"
        assert payload["metadata"] == {"key": "val"}


class TestTaskUpdate:
    def test_empty_payload(self) -> None:
        u = TaskUpdate()
        assert u.to_api_payload() == {}

    def test_status_only(self) -> None:
        u = TaskUpdate(status=TaskStatus.DONE)
        assert u.to_api_payload() == {"status": "done"}

    def test_all_fields(self) -> None:
        u = TaskUpdate(
            status=TaskStatus.FAILED,
            result_summary="oops",
            error="out of memory",
        )
        p = u.to_api_payload()
        assert p["status"] == "failed"
        assert p["result_summary"] == "oops"
        assert p["error"] == "out of memory"


class TestTaskResponse:
    def test_from_api_response_minimal(self) -> None:
        data = {"id": "abc123", "title": "Fix bug", "role": "backend", "status": "open"}
        t = TaskResponse.from_api_response(data)
        assert t.id == "abc123"
        assert t.status == TaskStatus.OPEN
        assert t.priority == 2
        assert t.scope == "medium"
        assert t.external_ref == ""
        assert t.metadata == {}

    def test_from_api_response_full(self) -> None:
        data = {
            "id": "xyz",
            "title": "T",
            "role": "qa",
            "status": "done",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "description": "desc",
            "assigned_agent": "agent-1",
            "result_summary": "done well",
            "external_ref": "jira:P-1",
            "metadata": {"k": "v"},
            "created_at": 1700000000.0,
        }
        t = TaskResponse.from_api_response(data)
        assert t.status == TaskStatus.DONE
        assert t.result_summary == "done well"
        assert t.external_ref == "jira:P-1"
        assert t.created_at == 1700000000.0


class TestStatusSummary:
    def test_from_api_response(self) -> None:
        data = {
            "total": 10,
            "open": 3,
            "claimed": 2,
            "done": 4,
            "failed": 1,
            "agents": 2,
            "cost_usd": 0.75,
        }
        s = StatusSummary.from_api_response(data)
        assert s.total == 10
        assert s.cost_usd == 0.75

    def test_defaults(self) -> None:
        s = StatusSummary.from_api_response({})
        assert s.total == 0
        assert s.cost_usd == 0.0
