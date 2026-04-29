"""Task planning: LLM-powered goal decomposition and replan.

Contains the planning methods of ManagerAgent:
- plan(goal): Decompose a goal into tasks
- replan(completed, failed, remaining, goal): Adjust plan based on progress
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.context import available_roles, gather_project_context
from bernstein.core.llm import call_llm
from bernstein.core.manager_parsing import (
    _parse_upgrade_details,
    _resolve_depends_on,
    parse_tasks_response,
    raw_dicts_to_tasks,
)
from bernstein.core.manager_prompts import render_plan_prompt
from bernstein.core.metrics import get_collector
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.semantic_cache import SemanticCacheManager

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def _post_task_to_server(
    client: httpx.AsyncClient,
    server_url: str,
    task: Task,
    *,
    plan_mode: bool = False,
) -> str:
    """POST a task to the Bernstein task server.

    Upgrade proposal tasks get a priority boost (priority reduced by 1, minimum 1)
    to ensure self-evolution tasks are processed promptly.

    Args:
        client: Async HTTP client.
        server_url: Base URL of the task server.
        task: Task to create.
        plan_mode: If True, create the task with status=planned.

    Returns:
        Server-assigned task ID.

    Raises:
        httpx.HTTPStatusError: If the server rejects the request.
    """
    from dataclasses import asdict

    # Apply priority boost for upgrade proposals
    priority = task.priority
    if task.task_type == TaskType.UPGRADE_PROPOSAL:
        priority = max(1, task.priority - 1)

    body: dict[str, Any] = {
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "depends_on": task.depends_on,
        "owned_files": task.owned_files,
        "task_type": task.task_type.value,
    }
    # Forward routing hints declared on the task (per-step plan fields).
    if task.cli:
        body["cli"] = task.cli
    # Include upgrade_details if present
    if task.upgrade_details:
        body["upgrade_details"] = asdict(task.upgrade_details)

    # Plan mode: tasks start as PLANNED instead of OPEN
    if plan_mode:
        body["status"] = "planned"

    resp = await client.post(f"{server_url}/tasks", json=body)
    resp.raise_for_status()
    return cast("str", resp.json()["id"])


async def _fetch_existing_tasks(
    client: httpx.AsyncClient,
    server_url: str,
) -> list[Task]:
    """Fetch all tasks from the server for replan context.

    Args:
        client: Async HTTP client.
        server_url: Base URL of the task server.

    Returns:
        List of existing tasks.
    """
    resp = await client.get(f"{server_url}/tasks")
    resp.raise_for_status()
    tasks: list[Task] = []
    for raw in resp.json():
        # Parse task type
        task_type = TaskType.STANDARD
        if "task_type" in raw:
            try:
                task_type = TaskType(raw["task_type"])
            except ValueError:
                logger.warning("Invalid task_type %r from server", raw["task_type"])

        # Parse upgrade details if present
        upgrade_details = None
        if raw.get("upgrade_details"):
            try:
                upgrade_details = _parse_upgrade_details(raw["upgrade_details"])
            except (ValueError, KeyError) as exc:
                logger.warning("Failed to parse upgrade_details from server: %s", exc)

        tasks.append(
            Task(
                id=raw["id"],
                title=raw["title"],
                description=raw.get("description", ""),
                role=raw.get("role", ""),
                priority=raw.get("priority", 2),
                scope=Scope(raw.get("scope", "medium")),
                complexity=Complexity(raw.get("complexity", "medium")),
                estimated_minutes=raw.get("estimated_minutes", 30),
                status=TaskStatus(raw.get("status", "open")),
                task_type=task_type,
                upgrade_details=upgrade_details,
                depends_on=raw.get("depends_on", []),
                owned_files=raw.get("owned_files", []),
                assigned_agent=raw.get("assigned_agent"),
                result_summary=raw.get("result_summary"),
            )
        )
    return tasks


async def plan(
    goal: str,
    server_url: str,
    workdir: Path,
    templates_dir: Path,
    model: str,
    provider: str,
) -> list[Task]:
    """Decompose a goal into tasks using the LLM.

    Steps:
        1. Gather context: file tree, README, .sdd/project.md
        2. Discover available roles from templates/
        3. Fetch existing tasks from the server
        4. Build prompt with goal + context + roles + existing tasks
        5. Call Claude via CLI subprocess
        6. Parse JSON response into Task objects
        7. Resolve dependency titles to IDs
        8. POST each task to the server

    Args:
        goal: Free-text project goal to decompose.
        server_url: Base URL of the task server.
        workdir: Project working directory.
        templates_dir: Root templates/ directory (contains roles/ and prompts/).
        model: LLM model to use for planning.
        provider: LLM provider.

    Returns:
        List of created Task objects (with server-assigned IDs).

    Raises:
        RuntimeError: If the LLM call fails.
        ValueError: If the LLM response cannot be parsed.
    """
    # 0. Semantic cache — skip LLM if we've planned a similar goal before
    sem_cache = SemanticCacheManager(workdir)

    # 1. Gather context
    context = gather_project_context(workdir)

    # 2. Discover roles
    roles = available_roles(templates_dir / "roles")

    # 3. Fetch existing tasks
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            existing = await _fetch_existing_tasks(client, server_url)
        except httpx.HTTPError:
            logger.warning("Could not fetch existing tasks — planning from scratch")
            existing = []

        # Record metrics start
        collector = get_collector()
        plan_start = time.time()

        # 3.5 Pre-planning Web Research
        research_prompt = (
            f"Goal: {goal}\n"
            "Do you need to search the web for any documentation, library updates, or context to plan this? "
            "Reply with ONLY the search query string, or exactly 'NONE' if no research is needed."
        )
        query = await call_llm(research_prompt, model=model, provider=provider)
        query = query.strip()

        if query and query.upper() != "NONE" and not query.startswith("```"):
            from bernstein.core.llm import tavily_search

            logger.info("Manager requested web research for: %r", query)
            search_results = await tavily_search(query)
            if search_results:
                context += f"\n\n## Web Research Context (Query: {query})\n{search_results}"

        # 4. Build prompt
        prompt = render_plan_prompt(
            goal=goal,
            context=context,
            roles=roles,
            existing_tasks=existing,
            templates_dir=templates_dir,
        )

        # 5. Call LLM (or serve from semantic cache)
        cached_response, similarity = sem_cache.lookup(goal, model=model)
        if cached_response is not None:
            logger.info(
                "Semantic cache hit (similarity=%.3f) — skipping LLM call for goal: %r",
                similarity,
                goal[:80],
            )
            raw_response = cached_response
            plan_success = True
            plan_duration = time.time() - plan_start
            collector.record_api_call(
                provider="cache",
                model=model,
                latency_ms=plan_duration * 1000,
                tokens=0,
                _cost_usd=0.0,
                success=True,
            )
        else:
            logger.info("Calling %s (provider: %s) for task planning...", model, provider)
            try:
                logger.debug("Prompt payload being sent to LLM:\n%s", prompt)
                raw_response = await call_llm(prompt, model=model, provider=provider)
                logger.info("Successfully received response from LLM (length: %d chars)", len(raw_response))
                plan_success = True
                # Store in semantic cache for future similar goals
                sem_cache.store(goal, raw_response, model=model)
                sem_cache.save()
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                collector.record_error("llm_call_failed", provider, model, "manager")
                plan_success = False
                raw_response = ""

            # Record metrics
            plan_duration = time.time() - plan_start
            collector.record_api_call(
                provider=provider,
                model=model,
                latency_ms=plan_duration * 1000,
                tokens=0,  # Would need to parse from LLM response
                _cost_usd=0.0,
                success=plan_success,
            )

        if not plan_success:
            raise RuntimeError("LLM planning call failed to produce a response.")

        # 6. Parse response
        raw_tasks = parse_tasks_response(raw_response)
        tasks = raw_dicts_to_tasks(raw_tasks)

        if not tasks:
            logger.warning("LLM returned no valid tasks")
            return []

        # 7. Resolve dependency titles to IDs
        _resolve_depends_on(tasks)

        # 8. Post to server
        for task in tasks:
            try:
                server_id = await _post_task_to_server(client, server_url, task)
                task.id = server_id
                logger.info("Created task %s: %s (role=%s)", server_id, task.title, task.role)
            except httpx.HTTPError as exc:
                logger.error("Failed to post task %s: %s", task.title, exc)

    return tasks


async def replan(
    completed: list[Task],
    failed: list[Task],
    remaining: list[Task],
    goal: str,
    server_url: str,
    workdir: Path,
    templates_dir: Path,
    model: str,
    provider: str,
) -> list[Task]:
    """Adjust the plan based on progress.

    Creates new tasks to account for failures, gaps, or changed
    requirements while avoiding duplication with remaining tasks.

    Args:
        completed: Tasks that finished successfully.
        failed: Tasks that failed.
        remaining: Tasks still open or in progress.
        goal: The original project goal (for context).
        server_url: Base URL of the task server.
        workdir: Project working directory.
        templates_dir: Root templates/ directory.
        model: LLM model to use.
        provider: LLM provider.

    Returns:
        List of newly created tasks (already posted to server).
    """
    # Build a replan goal that includes progress summary
    progress = (
        f"Original goal: {goal}\n\n"
        f"Progress:\n"
        f"- Completed ({len(completed)}): " + ", ".join(t.title for t in completed[:10]) + "\n"
        f"- Failed ({len(failed)}): "
        + ", ".join(f"{t.title}: {t.result_summary or 'unknown'}" for t in failed[:5])
        + "\n"
        f"- Remaining ({len(remaining)}): " + ", ".join(t.title for t in remaining[:10]) + "\n\n"
        "Create only the NEW tasks needed to get back on track. "
        "Do not duplicate remaining tasks."
    )
    return await plan(progress, server_url, workdir, templates_dir, model, provider)
