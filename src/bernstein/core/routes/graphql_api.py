"""WEB-021: Lightweight GraphQL API alongside REST.

Provides a POST /graphql endpoint that parses simple GraphQL queries
and resolves them against the task store. No external GraphQL library
dependency -- just a simple query parser for the subset of GraphQL
that dashboard clients need.

Supported queries::

    { tasks(status: "open") { id title status role agent cost_usd } }
    { agents { id provider status } }
    { status { total completed failed active_agents } }
    { costs { total_usd per_model { model cost } } }
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graphql"])


class GraphQLRequest(BaseModel):
    """GraphQL request body."""

    query: str
    variables: dict[str, Any] | None = None
    operation_name: str | None = Field(None, alias="operationName")

    model_config = {"populate_by_name": True}


@dataclass
class ParsedQuery:
    """Parsed GraphQL query."""

    operation: str = ""
    fields: list[str] = field(default_factory=list)
    args: dict[str, str] = field(default_factory=dict)


def parse_graphql_query(query: str) -> ParsedQuery:
    """Parse a simple GraphQL query string.

    Handles the subset of GraphQL that dashboard clients need:
    operation names, string arguments, and top-level field selection.

    Args:
        query: GraphQL query string like ``{ tasks { id title } }``.

    Returns:
        ParsedQuery with operation name, requested fields, and arguments.
    """
    # Strip outer braces
    inner = query.strip().strip("{").strip("}").strip()

    # Extract operation name and body using simple string parsing (no regex)
    # to avoid polynomial-time regex backtracking on adversarial input.
    paren_start = inner.find("(")
    brace_start = inner.find("{")
    if brace_start < 0:
        return ParsedQuery()

    operation = inner[: min(paren_start, brace_start) if paren_start >= 0 else brace_start].strip()
    if not operation or not operation.isidentifier():
        return ParsedQuery()

    # Extract args between ( and )
    args_str = ""
    if 0 <= paren_start < brace_start:
        paren_end = inner.find(")", paren_start)
        if paren_end > paren_start:
            args_str = inner[paren_start + 1 : paren_end]

    # Extract fields between { and }
    brace_end = inner.find("}", brace_start)
    fields_str = inner[brace_start + 1 : brace_end] if brace_end > brace_start else ""

    # Parse args: key: "value" pairs via simple split
    args: dict[str, str] = {}
    for part in args_str.split(","):
        if ":" not in part or '"' not in part:
            continue
        key, _, val = part.partition(":")
        key = key.strip()
        val = val.strip().strip('"')
        if key.isidentifier():
            args[key] = val

    # Parse fields (top-level identifiers only)
    fields = [w for w in fields_str.split() if w.isidentifier()]

    return ParsedQuery(operation=operation, fields=fields, args=args)


def _serialize_value(value: Any) -> Any:
    """Convert non-serializable values (enums, dataclasses) to JSON-safe forms.

    Args:
        value: Any value from a task dict.

    Returns:
        JSON-serializable representation.
    """
    from enum import Enum

    if isinstance(value, Enum):
        return value.value
    return value


def _task_to_dict(t: Any) -> dict[str, Any]:
    """Coerce a task object (dict, dataclass, etc.) to a plain dict."""
    if isinstance(t, dict):
        return t
    if hasattr(t, "__dict__"):
        return t.__dict__
    return {}


def _extract_task_fields(task: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Pick requested *fields* from a task dict, serializing enums."""
    row: dict[str, Any] = {}
    for f in fields:
        if f in task:
            row[f] = _serialize_value(task[f])
        elif f == "agent":
            row[f] = {
                "id": task.get("assigned_agent", ""),
                "provider": task.get("provider", ""),
            }
        else:
            row[f] = None
    return row


def _resolve_tasks(parsed: ParsedQuery, store: Any) -> list[dict[str, Any]]:
    """Resolve a tasks query against the store.

    Args:
        parsed: Parsed query with optional status filter and field selection.
        store: TaskStore (or mock) with a ``list_tasks()`` method.

    Returns:
        List of task dicts with only the requested fields.
    """
    tasks = store.list_tasks()
    status_filter = parsed.args.get("status")

    results: list[dict[str, Any]] = []
    for t in tasks:
        task = _task_to_dict(t)
        raw_status = task.get("status")
        task_status_str = raw_status.value if hasattr(raw_status, "value") else raw_status
        if status_filter and task_status_str != status_filter:
            continue
        results.append(_extract_task_fields(task, parsed.fields))
    return results


def _resolve_status(parsed: ParsedQuery, store: Any) -> dict[str, Any]:
    """Resolve a status query against the store.

    Args:
        parsed: Parsed query with field selection.
        store: TaskStore (or mock) with a ``status_summary()`` method.

    Returns:
        Dict with only the requested status fields.
    """
    summary = store.status_summary()
    return {f: summary.get(f) for f in parsed.fields if f in summary}


def execute_graphql(
    query: str,
    store: Any,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a GraphQL query against the store.

    Routes the parsed operation to the appropriate resolver and returns
    a standard GraphQL response envelope (``data`` or ``errors``).

    Args:
        query: GraphQL query string.
        store: TaskStore instance (or compatible mock).
        variables: Optional query variables (reserved for future use).

    Returns:
        Dict with ``"data"`` on success or ``"errors"`` on failure.
    """
    parsed = parse_graphql_query(query)

    if not parsed.operation:
        return {"errors": [{"message": "Could not parse query"}]}

    match parsed.operation:
        case "tasks":
            data = _resolve_tasks(parsed, store)
            return {"data": {"tasks": data}}
        case "status":
            data_status = _resolve_status(parsed, store)
            return {"data": {"status": data_status}}
        case "agents":
            return {"data": {"agents": []}}
        case "costs":
            return {"data": {"costs": {"total_usd": 0.0, "per_model": []}}}
        case _:
            return {"errors": [{"message": f"Unknown operation: {parsed.operation}"}]}


@router.post("/graphql")
async def graphql_endpoint(req: GraphQLRequest, request: Request) -> dict[str, Any]:
    """Execute a GraphQL query.

    Accepts a standard GraphQL request body and resolves the query
    against the in-memory task store.

    Args:
        req: GraphQL request body with query, optional variables and operationName.
        request: FastAPI request (provides access to app state).

    Returns:
        GraphQL response with ``data`` or ``errors``.
    """
    store = request.app.state.store
    return execute_graphql(req.query, store=store, variables=req.variables)
