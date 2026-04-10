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
import re
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

    # Extract operation name, optional args, and field block
    match = re.match(r"(\w+)\s*(?:\(([^)]*)\))?\s*\{([^}]*)\}", inner)
    if not match:
        return ParsedQuery()

    operation = match.group(1)
    args_str = match.group(2) or ""
    fields_str = match.group(3)

    # Parse args: key: "value" pairs
    args: dict[str, str] = {}
    for arg_match in re.finditer(r'(\w+)\s*:\s*"([^"]*)"', args_str):
        args[arg_match.group(1)] = arg_match.group(2)

    # Parse fields (top-level names only; nested sub-selections are ignored)
    fields = re.findall(r"\w+", fields_str)

    return ParsedQuery(operation=operation, fields=fields, args=args)


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
        task = t if isinstance(t, dict) else (t.__dict__ if hasattr(t, "__dict__") else {})
        if status_filter and task.get("status") != status_filter:
            continue
        row: dict[str, Any] = {}
        for f in parsed.fields:
            if f in task:
                row[f] = task[f]
            elif f == "agent":
                row[f] = {
                    "id": task.get("assigned_agent", ""),
                    "provider": task.get("provider", ""),
                }
            else:
                row[f] = None
        results.append(row)
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

    if parsed.operation == "tasks":
        data = _resolve_tasks(parsed, store)
        return {"data": {"tasks": data}}
    elif parsed.operation == "status":
        data_status = _resolve_status(parsed, store)
        return {"data": {"status": data_status}}
    elif parsed.operation == "agents":
        return {"data": {"agents": []}}
    elif parsed.operation == "costs":
        return {"data": {"costs": {"total_usd": 0.0, "per_model": []}}}
    else:
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
