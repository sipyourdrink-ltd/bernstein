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


def _scenario_run_handle(result: dict[str, Any]) -> dict[str, Any]:
    """Reshape a scenario invocation into the ``bernstein_run`` handle shape.

    A scenario run spawns ordinary tasks, so it is polled with the ordinary
    poll tool: the response carries the same ``task_id`` / ``run_id`` /
    ``poll_after_ms`` fields ``bernstein_run`` returns (for the first
    spawned task) plus one ``{task_id, run_id}`` pair per spawned task, so
    ``bernstein_run_status`` follows any of them with no scenario-specific
    tooling (#3087).
    """
    if "error" in result:
        return result
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    # Deliberately a lazy import: bernstein.mcp.server imports this module's
    # registrar inside create_mcp_server, so a module-level import back into
    # it would be a cycle hazard.
    from bernstein.mcp.server import _POLL_AFTER_MS  # pyright: ignore[reportPrivateUsage]

    task_ids = [str(t) for t in result.get("task_ids", [])]
    handle: dict[str, Any] = dict(result)
    handle["tasks"] = [{"task_id": t, "run_id": task_run_id(t)} for t in task_ids]
    if task_ids:
        handle["task_id"] = task_ids[0]
        handle["run_id"] = task_run_id(task_ids[0])
    handle["poll_after_ms"] = _POLL_AFTER_MS
    return handle


def register_scenario_tools(mcp: FastMCP[None], server_url: str) -> None:
    """Register the consolidated ``bernstein_scenario`` MCP tool (#3087).

    One tool with an ``action`` selector replaces the former three-tool
    split (``bernstein_scenarios`` / ``bernstein_scenario`` /
    ``bernstein_scenario_status``), whose old names remain as deprecated
    aliases registered by the server module.

    Args:
        mcp: FastMCP instance to attach the tool to.
        server_url: Bernstein task server base URL the tool will hit.
    """

    @mcp.tool()
    async def bernstein_scenario(  # pyright: ignore[reportUnusedFunction]
        action: str = "run",
        scenario_id: str | None = None,
        context: str = "",
        pr_number: int | None = None,
        branch: str | None = None,
        orchestration_id: str | None = None,
    ) -> str:
        """List, run, or poll Bernstein scenarios through one ``action`` selector.

        Args:
            action: ``list`` returns the scenario library; ``run`` (the
                default) invokes ``scenario_id``, spawning one task per
                template; ``status`` aggregates a run by
                ``orchestration_id``.
            scenario_id: Identifier from the scenario library (required for
                ``run``), e.g. ``"pr-review-comprehensive"``.
            context: Free-form context (the trigger event summary, for
                example) appended to each task's description (``run`` only).
            pr_number: PR number to inject when triggered by GitHub
                (``run`` only).
            branch: Branch override (``run`` only).
            orchestration_id: Value a ``run`` returned (required for
                ``status``).

        Returns:
            ``list``: JSON array of scenario summaries. ``run``: the same
            handle shape ``bernstein_run`` returns (``task_id``, ``run_id``,
            ``poll_after_ms``) plus ``orchestration_id`` and the per-task
            ``tasks`` list, so the run is polled with
            ``bernstein_run_status``. ``status``: per-status counts and task
            details.
        """
        args: dict[str, Any] = {"action": action}
        if scenario_id is not None:
            args["scenario_id"] = scenario_id
        if context:
            args["context"] = context
        if pr_number is not None:
            args["pr_number"] = pr_number
        if branch is not None:
            args["branch"] = branch
        if orchestration_id is not None:
            args["orchestration_id"] = orchestration_id
        err = _validate_or_error("bernstein_scenario", args)
        if err is not None:
            return _validation_error_response(err)
        try:
            if action == "list":
                return json.dumps(list_scenarios(), indent=2)
            if action == "status":
                assert orchestration_id is not None  # enforced by the schema
                result = await fetch_scenario_status(orchestration_id, server_url=server_url)
                return json.dumps(result, indent=2)
            assert scenario_id is not None  # enforced by the schema
            result = await invoke_scenario_via_server(
                scenario_id,
                server_url=server_url,
                context=context,
                pr_number=pr_number,
                branch=branch,
            )
            return json.dumps(_scenario_run_handle(result), indent=2)
        except Exception as exc:
            return _error_response(exc)
