"""Manager Intelligence — LLM-powered task decomposition and review.

This module orchestrates the full Manager workflow: task planning, queue review,
and completion review using LLM calls. It imports lower-level logic from
specialized sub-modules (models, prompts, parsing) and coordinates the
high-level ManagerAgent class.

The Manager is the only LLM-powered component in the orchestrator.
It takes a goal, gathers project context, calls Claude to decompose
the goal into tasks, and posts them to the task server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import httpx

from bernstein import get_templates_dir
from bernstein.core.context import available_roles, gather_project_context
from bernstein.core.llm import call_llm
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    Complexity,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestration.manager_models import (
    QueueCorrection,
    QueueReviewResult,
    ReviewResult,
)
from bernstein.core.orchestration.manager_parsing import (
    _parse_upgrade_details,
    parse_queue_review_response,
    parse_review_response,
    parse_tasks_response,
    raw_dicts_to_tasks,
)
from bernstein.core.orchestration.manager_prompts import (
    render_plan_prompt,
    render_queue_review_prompt,
    render_review_prompt,
)
from bernstein.core.upgrade_executor import (
    FileChange,
    UpgradeExecutor,
    UpgradeStatus,
    UpgradeTransaction,
    UpgradeType,
)

logger = logging.getLogger(__name__)

# Re-export manager models and parsing functions for backward compat
__all__ = [
    # This module
    "ManagerAgent",
    # From manager_models
    "QueueCorrection",
    "QueueReviewResult",
    "ReviewResult",
    # From manager_parsing (selected)
    "parse_queue_review_response",
    "parse_review_response",
    "parse_tasks_response",
    "raw_dicts_to_tasks",
]


# ---------------------------------------------------------------------------
# Server communication
# ---------------------------------------------------------------------------


async def _post_task_to_server(
    client: httpx.AsyncClient,
    server_url: str,
    task: Task,
) -> str:
    """POST a task to the Bernstein task server.

    Upgrade proposal tasks get a priority boost (priority reduced by 1, minimum 1)
    to ensure self-evolution tasks are processed promptly.

    Args:
        client: Async HTTP client.
        server_url: Base URL of the task server.
        task: Task to create.

    Returns:
        Server-assigned task ID.

    Raises:
        httpx.HTTPStatusError: If the server rejects the request.
    """
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


# ---------------------------------------------------------------------------
# ManagerAgent
# ---------------------------------------------------------------------------


class ManagerAgent:
    """LLM-powered task decomposition and team planning.

    The Manager gathers project context, calls an LLM (Claude Opus) to
    decompose a goal into tasks, parses the structured output into Task
    objects, and posts them to the task server.

    Args:
        server_url: Base URL of the Bernstein task server.
        workdir: Project working directory.
        templates_dir: Root templates/ directory (contains roles/ and prompts/).
        model: LLM model to use for planning and review.
    """

    def __init__(
        self,
        server_url: str,
        workdir: Path,
        templates_dir: Path,
        model: str = "nvidia/nemotron-3-super-120b-a12b",
        provider: str = "openrouter_free",
    ) -> None:
        self._server_url = server_url
        self._workdir = workdir
        self._templates_dir = templates_dir
        self._model = model
        self._provider = provider

    async def plan(self, goal: str) -> list[Task]:
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

        Returns:
            List of created Task objects (with server-assigned IDs).

        Raises:
            RuntimeError: If the LLM call fails.
            ValueError: If the LLM response cannot be parsed.
        """
        # 1. Gather context
        context = gather_project_context(self._workdir)

        # 2. Discover roles
        roles = available_roles(self._templates_dir / "roles")

        # 3. Fetch existing tasks
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                existing = await _fetch_existing_tasks(client, self._server_url)
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
            query = await call_llm(research_prompt, model=self._model, provider=self._provider)
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
                templates_dir=self._templates_dir,
            )

            # 5. Call LLM
            logger.info("Calling %s (provider: %s) for task planning...", self._model, self._provider)
            try:
                logger.debug("Prompt payload being sent to LLM:\n%s", prompt)
                raw_response = await call_llm(prompt, model=self._model, provider=self._provider)
                logger.info("Successfully received response from LLM (length: %d chars)", len(raw_response))
                plan_success = True
            except Exception as exc:
                logger.error("LLM call failed: %s", exc)
                collector.record_error("llm_call_failed", self._provider, self._model, "manager")
                plan_success = False
                raw_response = ""

            # Record metrics
            plan_duration = time.time() - plan_start
            collector.record_api_call(
                provider=self._provider,
                model=self._model,
                latency_ms=plan_duration * 1000,
                tokens=0,  # Would need to parse from LLM response
                cost_usd=0.0,
                success=plan_success,
            )

            if not plan_success:
                raise RuntimeError("LLM planning call failed to produce a response.")

            # 6. Parse response
            from bernstein.core.orchestration.manager_parsing import _resolve_depends_on

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
                    server_id = await _post_task_to_server(client, self._server_url, task)
                    task.id = server_id
                    logger.info("Created task %s: %s (role=%s)", server_id, task.title, task.role)
                except httpx.HTTPError as exc:
                    logger.error("Failed to post task %s: %s", task.title, exc)

            return tasks

    async def decompose(self, task: Task, *, min_subtasks: int = 2, max_subtasks: int = 5) -> list[Task]:
        """Decompose a single large task into 2-5 atomic subtasks."""
        context = gather_project_context(self._workdir)
        roles = available_roles(self._templates_dir / "roles")
        prompt = (
            "Decompose the following Bernstein task into atomic subtasks.\n\n"
            f"## Parent task\nTitle: {task.title}\nRole: {task.role}\n"
            f"Estimated minutes: {task.estimated_minutes}\n"
            f"Description:\n{task.description}\n\n"
            f"Available roles: {', '.join(sorted(roles))}\n\n"
            "Return ONLY a JSON array with 2-5 objects. Each object must contain:\n"
            '- "title": concise task title\n'
            '- "description": clear implementation instructions\n'
            '- "role": one available role\n'
            '- "scope": "small" or "medium"\n'
            '- "complexity": "low", "medium", or "high"\n'
            '- "estimated_minutes": integer <= 60\n'
            '- "owned_files": array of file paths, optional\n\n'
            "Prefer independent subtasks that can be executed in separate agent sessions.\n\n"
            f"## Project context\n{context}"
        )
        raw = await call_llm(prompt, model=self._model, provider=self._provider)
        parsed = parse_tasks_response(raw)
        subtasks = raw_dicts_to_tasks(parsed, id_prefix=f"{task.id}-subtask")
        if not (min_subtasks <= len(subtasks) <= max_subtasks):
            raise ValueError(f"Expected {min_subtasks}-{max_subtasks} subtasks, got {len(subtasks)}")
        return subtasks[:max_subtasks]

    def decompose_sync(self, task: Task, *, min_subtasks: int = 2, max_subtasks: int = 5) -> list[Task]:
        """Synchronous wrapper around :meth:`decompose` for orchestrator call sites."""
        return asyncio.run(self.decompose(task, min_subtasks=min_subtasks, max_subtasks=max_subtasks))

    async def review(self, task: Task) -> ReviewResult:
        """Review completed work and decide: approve, request changes, or reject.

        Args:
            task: Completed task with result_summary populated.

        Returns:
            ReviewResult with verdict and optional follow-up tasks.

        Raises:
            RuntimeError: If the LLM call fails.
            ValueError: If the LLM response cannot be parsed.
        """
        collector = get_collector()
        context = gather_project_context(self._workdir)
        prompt = render_review_prompt(task, context, self._templates_dir)

        review_start = time.time()
        try:
            raw_response = await call_llm(prompt, model=self._model, provider=self._provider)
            review_success = True
        except Exception as exc:
            logger.error("LLM review call failed: %s", exc)
            collector.record_error("llm_review_failed", self._provider, self._model, "manager")
            review_success = False
            raise RuntimeError(f"LLM review call failed: {exc}") from exc

        review_duration = time.time() - review_start
        collector.record_api_call(
            provider=self._provider,
            model=self._model,
            latency_ms=review_duration * 1000,
            tokens=0,
            cost_usd=0.0,
            success=review_success,
        )

        parsed = parse_review_response(raw_response)

        # Parse follow-up tasks if any
        follow_ups: list[Task] = []
        raw_follow_ups = parsed.get("follow_up_tasks", [])
        if raw_follow_ups:
            follow_ups = raw_dicts_to_tasks(raw_follow_ups, id_prefix="followup")

        return ReviewResult(
            verdict=parsed["verdict"],
            reasoning=parsed.get("reasoning", ""),
            feedback=parsed.get("feedback", ""),
            follow_up_tasks=follow_ups,
        )

    async def execute_upgrade(
        self,
        task: Task,
    ) -> UpgradeTransaction | None:
        """Execute an upgrade proposal task.

        For tasks of type UPGRADE_PROPOSAL, this method:
        1. Uses LLM to generate the actual code changes
        2. Creates an UpgradeTransaction with the changes
        3. Submits for reviewer agent validation
        4. Executes the upgrade if approved

        Args:
            task: Upgrade proposal task with upgrade_details.

        Returns:
            UpgradeTransaction if successful, None if not an upgrade task.
        """
        if task.task_type != TaskType.UPGRADE_PROPOSAL or not task.upgrade_details:
            return None

        collector = get_collector()
        executor = UpgradeExecutor(workdir=self._workdir)

        upgrade_start = time.time()

        try:
            # Generate the actual code changes using LLM
            changes = await self._generate_upgrade_changes(task)

            if not changes:
                raise ValueError("Failed to generate upgrade changes")

            # Submit for review and execution
            transaction = await executor.submit_upgrade(
                upgrade_type=self._determine_upgrade_type(task),
                title=task.title,
                description=task.description,
                file_changes=changes,
                rollback_plan=task.upgrade_details.rollback_plan,
                task_id=task.id,
            )

            # Record metrics
            upgrade_duration = time.time() - upgrade_start
            collector.record_api_call(
                provider=self._provider,
                model=self._model,
                latency_ms=upgrade_duration * 1000,
                tokens=0,
                cost_usd=task.upgrade_details.cost_estimate_usd,
                success=transaction.status == UpgradeStatus.COMPLETED,
            )

            return transaction

        except Exception as exc:
            logger.error("Upgrade execution failed: %s", exc)
            collector.record_error("upgrade_execution_failed", self._provider, self._model)
            return None

    async def _generate_upgrade_changes(self, task: Task) -> list[FileChange]:
        """Generate file changes for an upgrade using LLM.

        Args:
            task: Upgrade proposal task.

        Returns:
            List of FileChange objects to apply.
        """
        if not task.upgrade_details:
            return []

        details = task.upgrade_details
        prompt = f"""You are implementing a system upgrade.

## Current State
{details.current_state}

## Proposed Change
{details.proposed_change}

## Benefits
{chr(10).join(f"- {b}" for b in details.benefits)}

Generate the actual code changes needed. For each file:
1. Specify the file path
2. Specify the operation (create, modify, delete)
3. Provide the full new content (for create/modify)

Respond with a JSON array of changes:
[
  {{
    "path": "src/example.py",
    "operation": "modify",
    "new_content": "..."
  }}
]

Be precise and complete. Include all necessary imports, tests, and documentation updates."""

        try:
            response = await call_llm(prompt, model=self._model, provider=self._provider)
            return self._parse_upgrade_changes(response)
        except Exception as exc:
            logger.error("Failed to generate upgrade changes: %s", exc)
            return []

    def _parse_upgrade_changes(self, response: str) -> list[FileChange]:
        """Parse LLM response into FileChange objects."""
        try:
            # Extract JSON from response
            text = response.strip()
            if text.startswith("```"):
                text = text[text.index("\n") + 1 :]
            if text.endswith("```"):
                text = text[: text.rfind("```")]
            text = text.strip()

            changes_data: Any = json.loads(text)
            changes: list[FileChange] = []

            for item in changes_data:
                changes.append(
                    FileChange(
                        path=item.get("path", ""),
                        operation=item.get("operation", "modify"),
                        new_content=item.get("new_content"),
                        old_content=item.get("old_content"),
                    )
                )

            return changes

        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to parse upgrade changes: %s", exc)
            return []

    def _determine_upgrade_type(self, task: Task) -> UpgradeType:
        """Determine the upgrade type from task details."""
        if not task.upgrade_details:
            return UpgradeType.CODE_MODIFICATION

        # Analyze the proposed change to determine type
        proposed = task.upgrade_details.proposed_change.lower()

        if "template" in proposed or "prompt" in proposed:
            return UpgradeType.TEMPLATE_UPDATE
        if "role" in proposed or "agent" in proposed:
            return UpgradeType.NEW_AGENT_ROLE
        if "config" in proposed or "setting" in proposed:
            return UpgradeType.CONFIG_ADJUSTMENT
        if "policy" in proposed or "rule" in proposed:
            return UpgradeType.POLICY_UPDATE
        if "router" in proposed or "routing" in proposed:
            return UpgradeType.ROUTING_RULE_CHANGE

        return UpgradeType.CODE_MODIFICATION

    async def replan(
        self,
        completed: list[Task],
        failed: list[Task],
        remaining: list[Task],
        goal: str,
    ) -> list[Task]:
        """Adjust the plan based on progress.

        Creates new tasks to account for failures, gaps, or changed
        requirements while avoiding duplication with remaining tasks.

        Args:
            completed: Tasks that finished successfully.
            failed: Tasks that failed.
            remaining: Tasks still open or in progress.
            goal: The original project goal (for context).

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
        return await self.plan(progress)

    async def review_queue(
        self,
        completed_count: int,
        failed_count: int,
        budget_remaining_pct: float = 1.0,
    ) -> QueueReviewResult:
        """Review the task queue and return corrections.

        Uses a cheap model (haiku) with a tight token budget. Skipped if
        budget is below 10% to conserve spend.

        Args:
            completed_count: Tasks completed since last review.
            failed_count: Tasks failed since last review.
            budget_remaining_pct: Fraction of budget remaining (0.0-1.0).

        Returns:
            QueueReviewResult with corrections to apply.
        """
        if budget_remaining_pct < 0.10:
            logger.info("Manager queue review skipped — budget below 10%%")
            return QueueReviewResult(corrections=[], reasoning="skipped: budget < 10%", skipped=True)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.get(f"{self._server_url}/tasks")
                resp.raise_for_status()
                all_tasks_raw: list[dict[str, Any]] = cast("list[dict[str, Any]]", resp.json())
            except httpx.HTTPError as exc:
                logger.warning("Manager queue review: failed to fetch tasks: %s", exc)
                return QueueReviewResult(corrections=[], reasoning="failed to fetch tasks", skipped=True)

        def _task_from_raw(t: dict[str, Any]) -> Task:
            return Task(
                id=t["id"],
                title=t["title"],
                description=t.get("description", ""),
                role=t.get("role", ""),
                priority=t.get("priority", 2),
                status=TaskStatus(t.get("status", "open")),
                assigned_agent=t.get("assigned_agent"),
                result_summary=t.get("result_summary"),
            )

        open_tasks = [_task_from_raw(t) for t in all_tasks_raw if t.get("status") == "open"]
        claimed_tasks = [_task_from_raw(t) for t in all_tasks_raw if t.get("status") in ("claimed", "in_progress")]
        failed_tasks = [_task_from_raw(t) for t in all_tasks_raw if t.get("status") == "failed"]

        prompt = render_queue_review_prompt(
            completed_count=completed_count,
            failed_count=failed_count,
            open_tasks=open_tasks,
            claimed_tasks=claimed_tasks,
            failed_tasks=failed_tasks,
            server_url=self._server_url,
        )

        try:
            raw_response = await call_llm(
                prompt,
                model=self._model,
                provider=self._provider,
                max_tokens=500,
            )
        except Exception as exc:
            logger.warning("Manager queue review LLM call failed: %s", exc)
            return QueueReviewResult(corrections=[], reasoning=f"llm error: {exc}", skipped=True)

        try:
            result = parse_queue_review_response(raw_response)
        except ValueError as exc:
            logger.warning("Manager queue review parse failed: %s", exc)
            return QueueReviewResult(corrections=[], reasoning=f"parse error: {exc}", skipped=True)

        logger.info(
            "Manager queue review: %d correction(s) — %s",
            len(result.corrections),
            result.reasoning,
        )
        return result

    def review_queue_sync(
        self,
        completed_count: int,
        failed_count: int,
        budget_remaining_pct: float = 1.0,
    ) -> QueueReviewResult:
        """Synchronous wrapper for :meth:`review_queue`.

        Safe to call from the orchestrator's synchronous tick loop.

        Args:
            completed_count: Tasks completed since last review.
            failed_count: Tasks failed since last review.
            budget_remaining_pct: Fraction of budget remaining (0.0-1.0).

        Returns:
            QueueReviewResult with corrections to apply.
        """
        import concurrent.futures

        coro = self.review_queue(completed_count, failed_count, budget_remaining_pct)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result(timeout=30)
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)


if __name__ == "__main__":
    import argparse
    import sys

    from bernstein.core.seed import parse_seed

    async def main():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        parser = argparse.ArgumentParser()
        parser.add_argument("--port", type=int, default=8052)
        parser.add_argument("--task-id", type=str, required=True)
        args = parser.parse_args()

        workdir = Path.cwd()
        server_url = f"http://127.0.0.1:{args.port}"

        # Load seed to get the model and internal LLM provider
        seed_path = workdir / "bernstein.yaml"
        model_name = "nvidia/nemotron-3-super-120b-a12b"
        provider_name = "openrouter_free"
        if seed_path.exists():
            try:
                seed = parse_seed(seed_path)
                # Prefer internal_llm_provider/model from seed config
                provider_name = seed.internal_llm_provider
                model_name = seed.internal_llm_model
                # Override with explicit model if set (backward compat)
                if seed.model:
                    model_name = seed.model
            except Exception as exc:
                logger.warning("Failed to parse seed for model config: %s", exc)

        agent = ManagerAgent(
            server_url=server_url,
            workdir=workdir,
            templates_dir=get_templates_dir(workdir),
            model=model_name,
            provider=provider_name,
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{server_url}/tasks/{args.task_id}")
            if resp.status_code != 200:
                print(f"Task {args.task_id} not found.")
                sys.exit(1)

            task_data = resp.json()
            goal = task_data.get("description", "")

            # Execute plan
            try:
                await agent.plan(goal)
                # Complete the manager task
                await client.post(
                    f"{server_url}/tasks/{args.task_id}/complete", json={"result_summary": "Planning completed."}
                )
            except Exception as e:
                logger.exception("Manager Agent failed during plan generation")
                await client.post(f"{server_url}/tasks/{args.task_id}/fail", json={"reason": str(e)})
                sys.exit(1)

    asyncio.run(main())
