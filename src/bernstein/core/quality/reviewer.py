"""Task review: LLM-powered completion review and queue correction.

Contains the review methods of ManagerAgent:
- review(task): Review completed work and decide: approve, request changes, reject
- review_queue(completed_count, failed_count, budget): Review task queue for corrections
- review_queue_sync(): Synchronous wrapper for review_queue
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import time
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.context import gather_project_context
from bernstein.core.llm import call_llm
from bernstein.core.manager_models import QueueReviewResult, ReviewResult
from bernstein.core.manager_parsing import parse_queue_review_response, parse_review_response, raw_dicts_to_tasks
from bernstein.core.manager_prompts import render_queue_review_prompt, render_review_prompt
from bernstein.core.metrics import get_collector
from bernstein.core.models import Task, TaskStatus
from bernstein.core.upgrade_executor import FileChange, UpgradeExecutor, UpgradeType

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


async def review(
    task: Task,
    workdir: Path,
    templates_dir: Path,
    model: str,
    provider: str,
) -> ReviewResult:
    """Review completed work and decide: approve, request changes, or reject.

    Args:
        task: Completed task with result_summary populated.
        workdir: Project working directory.
        templates_dir: Root templates/ directory.
        model: LLM model to use for review.
        provider: LLM provider.

    Returns:
        ReviewResult with verdict and optional follow-up tasks.

    Raises:
        RuntimeError: If the LLM call fails.
        ValueError: If the LLM response cannot be parsed.
    """
    collector = get_collector()
    context = gather_project_context(workdir)
    prompt = render_review_prompt(task, context, templates_dir)

    review_start = time.time()
    try:
        raw_response = await call_llm(prompt, model=model, provider=provider)
        review_success = True
    except Exception as exc:
        logger.error("LLM review call failed: %s", exc)
        collector.record_error("llm_review_failed", provider, model, "manager")
        review_success = False
        raise RuntimeError(f"LLM review call failed: {exc}") from exc

    review_duration = time.time() - review_start
    collector.record_api_call(
        provider=provider,
        model=model,
        latency_ms=review_duration * 1000,
        tokens=0,
        _cost_usd=0.0,
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


async def review_queue(
    completed_count: int,
    failed_count: int,
    budget_remaining_pct: float,
    server_url: str,
    model: str,
    provider: str,
    _templates_dir: Path,
) -> QueueReviewResult:
    """Review the task queue and return corrections.

    Uses a cheap model (haiku) with a tight token budget. Skipped if
    budget is below 10% to conserve spend.

    Args:
        completed_count: Tasks completed since last review.
        failed_count: Tasks failed since last review.
        budget_remaining_pct: Fraction of budget remaining (0.0-1.0).
        server_url: Base URL of the task server.
        model: LLM model to use.
        provider: LLM provider.
        _templates_dir: Root templates/ directory (part of interface).

    Returns:
        QueueReviewResult with corrections to apply.
    """
    if budget_remaining_pct < 0.10:
        logger.info("Manager queue review skipped — budget below 10%%")
        return QueueReviewResult(corrections=[], reasoning="skipped: budget < 10%", skipped=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{server_url}/tasks")
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
        _server_url=server_url,
    )

    try:
        raw_response = await call_llm(
            prompt,
            model=model,
            provider=provider,
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
    completed_count: int,
    failed_count: int,
    budget_remaining_pct: float,
    server_url: str,
    model: str,
    provider: str,
    _templates_dir: Path,
) -> QueueReviewResult:
    """Synchronous wrapper for review_queue.

    Safe to call from the orchestrator's synchronous tick loop.

    Args:
        completed_count: Tasks completed since last review.
        failed_count: Tasks failed since last review.
        budget_remaining_pct: Fraction of budget remaining (0.0-1.0).
        server_url: Base URL of the task server.
        model: LLM model to use.
        provider: LLM provider.
        _templates_dir: Root templates/ directory (part of interface).

    Returns:
        QueueReviewResult with corrections to apply.
    """
    coro = review_queue(
        completed_count,
        failed_count,
        budget_remaining_pct,
        server_url,
        model,
        provider,
        _templates_dir,
    )
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=30)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def execute_upgrade(
    task: Task,
    workdir: Path,
    model: str,
    provider: str,
) -> Any:
    """Execute an upgrade proposal task.

    For tasks of type UPGRADE_PROPOSAL, this method:
    1. Uses LLM to generate the actual code changes
    2. Creates an UpgradeTransaction with the changes
    3. Submits for reviewer agent validation
    4. Executes the upgrade if approved

    Args:
        task: Upgrade proposal task with upgrade_details.
        workdir: Project working directory.
        model: LLM model to use.
        provider: LLM provider.

    Returns:
        UpgradeTransaction if successful, None if not an upgrade task.
    """
    from bernstein.core.models import TaskType

    if task.task_type != TaskType.UPGRADE_PROPOSAL or not task.upgrade_details:
        return None

    collector = get_collector()
    executor = UpgradeExecutor(workdir=workdir)

    upgrade_start = time.time()

    try:
        # Generate the actual code changes using LLM
        changes = await _generate_upgrade_changes(task, model, provider)

        if not changes:
            raise ValueError("Failed to generate upgrade changes")

        # Submit for review and execution
        transaction = await executor.submit_upgrade(
            upgrade_type=_determine_upgrade_type(task),
            title=task.title,
            description=task.description,
            file_changes=changes,
            rollback_plan=task.upgrade_details.rollback_plan,
            task_id=task.id,
        )

        # Record metrics
        upgrade_duration = time.time() - upgrade_start
        collector.record_api_call(
            provider=provider,
            model=model,
            latency_ms=upgrade_duration * 1000,
            tokens=0,
            _cost_usd=task.upgrade_details.cost_estimate_usd,
            success=transaction.status.value == "completed",
        )

        return transaction

    except Exception as exc:
        logger.error("Upgrade execution failed: %s", exc)
        collector.record_error("upgrade_execution_failed", provider, model)
        return None


async def _generate_upgrade_changes(
    task: Task,
    model: str,
    provider: str,
) -> list[FileChange]:
    """Generate file changes for an upgrade using LLM.

    Args:
        task: Upgrade proposal task.
        model: LLM model to use.
        provider: LLM provider.

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
        response = await call_llm(prompt, model=model, provider=provider)
        return _parse_upgrade_changes(response)
    except Exception as exc:
        logger.error("Failed to generate upgrade changes: %s", exc)
        return []


def _parse_upgrade_changes(response: str) -> list[FileChange]:
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


def _determine_upgrade_type(task: Task) -> UpgradeType:
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
