"""Tests for WEB-021: GraphQL API alongside REST."""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core.routes.graphql_api import (
    GraphQLRequest,
    ParsedQuery,
    execute_graphql,
    parse_graphql_query,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockStore:
    """Minimal mock for testing GraphQL resolvers."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def list_tasks(self) -> list[dict[str, Any]]:
        return self._tasks

    def status_summary(self) -> dict[str, Any]:
        return {
            "total": len(self._tasks),
            "completed": 0,
            "failed": 0,
            "open": len(self._tasks),
        }


# ---------------------------------------------------------------------------
# parse_graphql_query
# ---------------------------------------------------------------------------


class TestParseQuery:
    def test_parse_simple_query(self) -> None:
        result = parse_graphql_query("{ tasks { id title status } }")
        assert result.operation == "tasks"
        assert "id" in result.fields
        assert "title" in result.fields
        assert "status" in result.fields

    def test_parse_with_args(self) -> None:
        result = parse_graphql_query('{ tasks(status: "open") { id title } }')
        assert result.operation == "tasks"
        assert result.args.get("status") == "open"

    def test_parse_nested_fields(self) -> None:
        result = parse_graphql_query("{ tasks { id agent { id provider } } }")
        assert result.operation == "tasks"
        assert "id" in result.fields

    def test_parse_agents_query(self) -> None:
        result = parse_graphql_query("{ agents { id provider status } }")
        assert result.operation == "agents"

    def test_parse_status_query(self) -> None:
        result = parse_graphql_query("{ status { total completed failed } }")
        assert result.operation == "status"

    def test_parse_costs_query(self) -> None:
        result = parse_graphql_query("{ costs { total_usd } }")
        assert result.operation == "costs"
        assert "total_usd" in result.fields

    def test_parse_empty_returns_blank(self) -> None:
        result = parse_graphql_query("")
        assert result.operation == ""
        assert result.fields == []
        assert result.args == {}

    def test_parse_garbage_returns_blank(self) -> None:
        result = parse_graphql_query("not a query")
        assert result.operation == ""

    def test_parsed_query_defaults(self) -> None:
        pq = ParsedQuery()
        assert pq.operation == ""
        assert pq.fields == []
        assert pq.args == {}

    def test_parse_multiple_args(self) -> None:
        result = parse_graphql_query('{ tasks(status: "open", role: "backend") { id } }')
        assert result.args.get("status") == "open"
        assert result.args.get("role") == "backend"


# ---------------------------------------------------------------------------
# execute_graphql
# ---------------------------------------------------------------------------


class TestExecuteGraphQL:
    def test_tasks_query(self) -> None:
        mock_store = _MockStore(
            [
                {"id": "t1", "title": "Task 1", "status": "open", "role": "backend"},
            ]
        )
        result = execute_graphql("{ tasks { id title status } }", store=mock_store)
        assert "errors" not in result
        assert len(result["data"]["tasks"]) == 1
        assert result["data"]["tasks"][0]["id"] == "t1"

    def test_tasks_query_field_filtering(self) -> None:
        mock_store = _MockStore(
            [
                {"id": "t1", "title": "Task 1", "status": "open", "role": "backend"},
            ]
        )
        result = execute_graphql("{ tasks { id } }", store=mock_store)
        task = result["data"]["tasks"][0]
        assert "id" in task
        assert "title" not in task

    def test_tasks_query_status_filter(self) -> None:
        mock_store = _MockStore(
            [
                {"id": "t1", "title": "Task 1", "status": "open"},
                {"id": "t2", "title": "Task 2", "status": "done"},
            ]
        )
        result = execute_graphql(
            '{ tasks(status: "open") { id title status } }',
            store=mock_store,
        )
        assert len(result["data"]["tasks"]) == 1
        assert result["data"]["tasks"][0]["id"] == "t1"

    def test_tasks_query_missing_field_is_none(self) -> None:
        mock_store = _MockStore([{"id": "t1"}])
        result = execute_graphql("{ tasks { id cost_usd } }", store=mock_store)
        task = result["data"]["tasks"][0]
        assert task["id"] == "t1"
        assert task["cost_usd"] is None

    def test_tasks_agent_virtual_field(self) -> None:
        mock_store = _MockStore([{"id": "t1", "assigned_agent": "a1", "provider": "claude"}])
        result = execute_graphql("{ tasks { id agent } }", store=mock_store)
        task = result["data"]["tasks"][0]
        assert task["agent"]["id"] == "a1"
        assert task["agent"]["provider"] == "claude"

    def test_status_query(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ status { total completed } }", store=mock_store)
        assert "data" in result
        assert result["data"]["status"]["total"] == 0
        assert result["data"]["status"]["completed"] == 0

    def test_agents_query(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ agents { id provider status } }", store=mock_store)
        assert "data" in result
        assert result["data"]["agents"] == []

    def test_costs_query(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ costs { total_usd } }", store=mock_store)
        assert "data" in result
        assert result["data"]["costs"]["total_usd"] == pytest.approx(0.0)

    def test_unknown_operation(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("{ unknown { id } }", store=mock_store)
        assert "errors" in result
        assert "Unknown operation" in result["errors"][0]["message"]

    def test_unparseable_query(self) -> None:
        mock_store = _MockStore([])
        result = execute_graphql("garbage", store=mock_store)
        assert "errors" in result
        assert "Could not parse" in result["errors"][0]["message"]


# ---------------------------------------------------------------------------
# GraphQLRequest model
# ---------------------------------------------------------------------------


class TestGraphQLRequestModel:
    def test_request_model(self) -> None:
        req = GraphQLRequest(query="{ tasks { id } }")
        assert req.query == "{ tasks { id } }"
        assert req.variables is None
        assert req.operation_name is None

    def test_request_model_with_variables(self) -> None:
        req = GraphQLRequest(
            query="{ tasks { id } }",
            variables={"status": "open"},
        )
        assert req.variables == {"status": "open"}

    def test_request_model_operation_name_alias(self) -> None:
        req = GraphQLRequest(
            query="{ tasks { id } }",
            operationName="GetTasks",  # type: ignore[call-arg]
        )
        assert req.operation_name == "GetTasks"
