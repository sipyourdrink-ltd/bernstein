"""MCP tools for the rt-003 Routine <-> Scenario bridge.

This module exposes two flavours of helpers:

* **Pure list/detail helpers** (``list_scenarios``, ``get_scenario_detail``)
  used by tests and the CLI.
* **MCP tool registration** (:func:`register_scenario_tools`) which wires
  ``bernstein_scenario``, ``bernstein_scenarios``, and
  ``bernstein_scenario_status`` onto a FastMCP server.

The MCP tools delegate to the Bernstein task server over HTTP - they do not
run orchestration in-process - keeping the MCP layer thin and stateless.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.planning.routine_bridge import build_task_payloads, estimate_minutes
from bernstein.core.planning.scenario_library import (
    load_scenario_library,
)
from bernstein.mcp.input_validation import (
    ValidationError,
    validate_or_error,
    validation_error_response,
)

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Default scenario directory shipped with the package.
_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "templates" / "scenarios"

# HTTP timeout for the task-server calls these tools make (seconds).
_HTTP_TIMEOUT = 5.0

# Env var holding the bearer token; mirrors mcp.server convention.
_AUTH_TOKEN_ENV = "BERNSTEIN_AUTH_TOKEN"


def list_scenarios(scenarios_dir: Path | None = None) -> list[dict[str, Any]]:
    """List all available Bernstein scenarios.

    Returns a list of scenario summaries with id, name, description,
    tags, task_count, and roles.
    """
    root = scenarios_dir or _SCENARIOS_DIR
    library = load_scenario_library(root)
    return [
        {
            "id": recipe.scenario_id,
            "name": recipe.name,
            "description": recipe.description,
            "tags": list(recipe.tags),
            "task_count": len(recipe.tasks),
            "roles": sorted({t.role for t in recipe.tasks}),
            "version": recipe.version,
        }
        for recipe in library.scenarios.values()
    ]


def get_scenario_detail(scenario_id: str, scenarios_dir: Path | None = None) -> dict[str, Any] | None:
    """Get detailed information about a specific scenario.

    Returns full scenario with task breakdown, or None if not found.
    """
    root = scenarios_dir or _SCENARIOS_DIR
    recipe = load_scenario_library(root).get(scenario_id)
    if recipe is None:
        return None
    return {
        "id": recipe.scenario_id,
        "name": recipe.name,
        "description": recipe.description,
        "tags": list(recipe.tags),
        "version": recipe.version,
        "tasks": [
            {
                "title": t.title,
                "description": t.description,
                "role": t.role,
                "priority": t.priority,
                "scope": t.scope,
                "complexity": t.complexity,
            }
            for t in recipe.tasks
        ],
    }


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get(_AUTH_TOKEN_ENV, "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def invoke_scenario_via_server(
    scenario_id: str,
    *,
    server_url: str,
    context: str = "",
    pr_number: int | None = None,
    branch: str | None = None,
    scenarios_dir: Path | None = None,
) -> dict[str, Any]:
    """Spawn one task per scenario template by POSTing at ``server_url``.

    Args:
        scenario_id: Source scenario id.
        server_url: Bernstein task server base URL.
        context: Free-form trigger context appended to each task description.
        pr_number: Optional PR number injected into descriptions.
        branch: Optional branch override.
        scenarios_dir: Override the bundled scenarios directory.

    Returns:
        A dict with ``orchestration_id``, ``scenario_id``, ``task_count``,
        ``estimated_minutes``, and ``task_ids``. On failure to POST, returns
        a dict with an ``error`` key.
    """
    recipe = load_scenario_library(scenarios_dir or _SCENARIOS_DIR).get(scenario_id)
    if recipe is None:
        return {"error": f"Unknown scenario: {scenario_id}"}

    payloads = build_task_payloads(
        recipe,
        context=context,
        pr_number=pr_number,
        branch=branch,
    )
    if not payloads:
        return {"error": f"Scenario {scenario_id} has no tasks"}

    orchestration_id = payloads[0].orchestration_id
    task_ids: list[str] = []
    headers = _auth_headers()
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for payload in payloads:
            try:
                resp = await client.post(
                    f"{server_url}/tasks",
                    json=payload.as_server_payload(),
                    headers=headers,
                )
                resp.raise_for_status()
                data: object = resp.json()
            except Exception as exc:
                logger.warning("scenario task POST failed: %s", exc)
                continue
            if isinstance(data, dict):
                data_dict = cast("dict[str, Any]", data)
                raw_id = data_dict.get("id", "")
                tid = str(raw_id).strip() if raw_id else ""
            else:
                tid = ""
            if tid:
                task_ids.append(tid)

    return {
        "orchestration_id": orchestration_id,
        "scenario_id": recipe.scenario_id,
        "task_count": len(payloads),
        "estimated_minutes": estimate_minutes(recipe),
        "task_ids": task_ids,
    }


async def fetch_scenario_status(
    orchestration_id: str,
    *,
    server_url: str,
) -> dict[str, Any]:
    """Aggregate the status of all tasks belonging to an orchestration.

    Args:
        orchestration_id: Identifier shared by every task of one scenario run
            (set in ``metadata.orchestration_id`` at spawn time).
        server_url: Bernstein task server base URL.

    Returns:
        A dict with per-status counts and the matched task list (truncated
        to a sensible size).
    """
    headers = _auth_headers()
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(f"{server_url}/tasks", headers=headers)
            resp.raise_for_status()
            tasks_raw: object = resp.json()
    except Exception as exc:
        return {"error": str(exc)}
    if not isinstance(tasks_raw, list):
        return {"error": "Unexpected /tasks response"}

    matched: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    tasks_list = cast("list[Any]", tasks_raw)
    for raw_obj in tasks_list:
        if not isinstance(raw_obj, dict):
            continue
        raw = cast("dict[str, Any]", raw_obj)
        meta_obj: object = raw.get("metadata") or {}
        meta: dict[str, Any] = cast("dict[str, Any]", meta_obj) if isinstance(meta_obj, dict) else {}
        orch = meta.get("orchestration_id")
        if orch != orchestration_id:
            continue
        status = str(raw.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        matched.append(
            {
                "id": raw.get("id"),
                "title": raw.get("title"),
                "role": raw.get("role"),
                "status": status,
                "result_summary": raw.get("result_summary"),
            }
        )
    return {
        "orchestration_id": orchestration_id,
        "task_count": len(matched),
        "status_counts": counts,
        "tasks": matched[:50],
    }


def _error_response(exc: Exception) -> str:
    logger.warning("scenario MCP tool error: %s", exc)
    return json.dumps({"error": str(exc)})


def _validation_error_response(err: ValidationError) -> str:
    """Render a validation failure as the JSON string FastMCP tools return."""
    return validation_error_response(err)


def _validate_or_error(tool_name: str, params: dict[str, Any]) -> ValidationError | None:
    """Run schema validation, returning the failure or ``None``."""
    return validate_or_error(tool_name, params)


def register_scenario_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register the ``bernstein_scenario(s|_status)`` MCP tools.

    Args:
        mcp: FastMCP instance to attach tools to.
        server_url: Bernstein task server base URL the tools will hit.
    """

    @mcp.tool()
    async def bernstein_scenarios() -> str:  # pyright: ignore[reportUnusedFunction]
        """List all Bernstein scenarios known to the local library.

        Returns:
            JSON array of scenario summaries (id, name, description, tags,
            task_count, roles).
        """
        try:
            return json.dumps(list_scenarios(), indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_scenario(  # pyright: ignore[reportUnusedFunction]
        scenario_id: str,
        context: str = "",
        pr_number: int | None = None,
        branch: str | None = None,
    ) -> str:
        """Invoke a Bernstein scenario by id, spawning one task per template.

        Args:
            scenario_id: Identifier from the scenario library
                (e.g. ``"pr-review-comprehensive"``).
            context: Free-form context (the trigger event summary, for
                example) appended to each task's description.
            pr_number: PR number to inject when triggered by GitHub.
            branch: Branch override.

        Returns:
            JSON with ``orchestration_id``, ``scenario_id``, ``task_count``,
            ``estimated_minutes`` and ``task_ids``.
        """
        err = _validate_or_error(
            "bernstein_scenario",
            {
                "scenario_id": scenario_id,
                "context": context,
                "pr_number": pr_number,
                "branch": branch,
            },
        )
        if err is not None:
            return _validation_error_response(err)
        try:
            result = await invoke_scenario_via_server(
                scenario_id,
                server_url=server_url,
                context=context,
                pr_number=pr_number,
                branch=branch,
            )
            return json.dumps(result, indent=2)
        except Exception as exc:
            return _error_response(exc)

    @mcp.tool()
    async def bernstein_scenario_status(  # pyright: ignore[reportUnusedFunction]
        orchestration_id: str,
    ) -> str:
        """Aggregate the status of all tasks of a running scenario.

        Args:
            orchestration_id: Value returned by ``bernstein_scenario``.

        Returns:
            JSON with status counts and per-task details.
        """
        err = _validate_or_error("bernstein_scenario_status", {"orchestration_id": orchestration_id})
        if err is not None:
            return _validation_error_response(err)
        try:
            result = await fetch_scenario_status(orchestration_id, server_url=server_url)
            return json.dumps(result, indent=2)
        except Exception as exc:
            return _error_response(exc)
