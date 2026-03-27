"""Manager Intelligence — LLM-powered task decomposition and review.

The Manager is the only LLM-powered component in the orchestrator.
It takes a goal, gathers project context, calls Claude to decompose
the goal into tasks, and posts them to the task server.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx

from bernstein.core.context import available_roles, gather_project_context
from bernstein.core.llm import call_llm
from bernstein.core.metrics import get_collector
from bernstein.core.models import (
    CompletionSignal,
    Complexity,
    RiskAssessment,
    RollbackPlan,
    Scope,
    Task,
    TaskStatus,
    TaskType,
    UpgradeProposalDetails,
)
from bernstein.core.upgrade_executor import (
    FileChange,
    UpgradeExecutor,
    UpgradeStatus,
    UpgradeTransaction,
    UpgradeType,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

_VALID_VERDICTS = frozenset({"approve", "request_changes", "reject"})


@dataclass
class ReviewResult:
    """Outcome of a manager review of completed work.

    Attributes:
        verdict: One of 'approve', 'request_changes', or 'reject'.
        reasoning: Brief explanation of the decision.
        feedback: Specific actionable feedback (empty if approved).
        follow_up_tasks: Additional tasks spawned by the review.
    """

    verdict: Literal["approve", "request_changes", "reject"]
    reasoning: str
    feedback: str
    follow_up_tasks: list[Task]


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_template(templates_dir: Path, name: str) -> str:
    """Load a prompt template from templates/prompts/.

    Args:
        templates_dir: Root templates/ directory (parent of prompts/).
        name: Template filename (e.g. 'plan.md').

    Returns:
        Template content as a string.

    Raises:
        FileNotFoundError: If the template does not exist.
    """
    path = templates_dir / "prompts" / name
    if not path.is_file():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _format_roles(roles: list[str]) -> str:
    """Format available roles as a bulleted list.

    Args:
        roles: Sorted list of role names.

    Returns:
        Markdown bulleted list.
    """
    if not roles:
        return "(no roles found)"
    return "\n".join(f"- {r}" for r in roles)


def _format_existing_tasks(tasks: list[Task]) -> str:
    """Format existing tasks as a summary for the planning prompt.

    Args:
        tasks: Current tasks from the server.

    Returns:
        Summary string or a 'none' placeholder.
    """
    if not tasks:
        return "(none — this is a fresh plan)"
    lines: list[str] = []
    for t in tasks:
        dep_str = f" [depends: {', '.join(t.depends_on)}]" if t.depends_on else ""
        lines.append(f"- [{t.status.value}] {t.title} (role={t.role}){dep_str}")
    return "\n".join(lines)


def render_plan_prompt(
    goal: str,
    context: str,
    roles: list[str],
    existing_tasks: list[Task],
    templates_dir: Path,
) -> str:
    """Build the full planning prompt from the template.

    Args:
        goal: The high-level objective to decompose.
        context: Project context string from ``gather_project_context``.
        roles: Available specialist role names.
        existing_tasks: Tasks already in the server.
        templates_dir: Root templates/ directory.

    Returns:
        Fully rendered prompt ready for the LLM.
    """
    template = _load_template(templates_dir, "plan.md")
    return (
        template
        .replace("{{GOAL}}", goal)
        .replace("{{CONTEXT}}", context)
        .replace("{{AVAILABLE_ROLES}}", _format_roles(roles))
        .replace("{{EXISTING_TASKS}}", _format_existing_tasks(existing_tasks))
    )


def render_review_prompt(
    task: Task,
    context: str,
    templates_dir: Path,
) -> str:
    """Build the review prompt from the template.

    Args:
        task: Completed task to review.
        context: Project context string.
        templates_dir: Root templates/ directory.

    Returns:
        Fully rendered review prompt.
    """
    template = _load_template(templates_dir, "review.md")

    signals_str = "(none)"
    if task.completion_signals:
        signals_str = "\n".join(
            f"- {s.type}: `{s.value}`" for s in task.completion_signals
        )

    return (
        template
        .replace("{{TASK_TITLE}}", task.title)
        .replace("{{TASK_ROLE}}", task.role)
        .replace("{{TASK_DESCRIPTION}}", task.description)
        .replace("{{COMPLETION_SIGNALS}}", signals_str)
        .replace("{{RESULT_SUMMARY}}", task.result_summary or "(no summary)")
        .replace("{{CONTEXT}}", context)
    )


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> str:
    """Extract JSON from LLM output, stripping markdown fences if present.

    Args:
        raw: Raw LLM response text.

    Returns:
        Cleaned string that should be valid JSON.
    """
    text = raw.strip()
    # Strip markdown code fences.
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: text.rfind("```")]
    return text.strip()


def parse_tasks_response(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM planning response into raw task dicts.

    Args:
        raw: Raw LLM response (should be a JSON array of task objects).

    Returns:
        List of parsed task dictionaries.

    Raises:
        ValueError: If the response is not valid JSON or not a list.
    """
    cleaned = _extract_json(raw)
    try:
        parsed: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("LLM failed to produce valid JSON. Raw response:\n%s", raw)
        raise ValueError(f"LLM response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"Expected a JSON array, got {type(parsed).__name__}")
    return cast(list[dict[str, Any]], parsed)


def _parse_completion_signal(raw: dict[str, str]) -> CompletionSignal:
    """Parse a single completion signal from a JSON dict.

    Args:
        raw: Dict with 'type' and 'value' keys.

    Returns:
        CompletionSignal dataclass.

    Raises:
        ValueError: If required keys are missing or type is invalid.
    """
    valid_types = {"path_exists", "glob_exists", "test_passes", "file_contains", "llm_review"}
    sig_type = raw.get("type", "")
    sig_value = raw.get("value", "")
    if sig_type not in valid_types:
        raise ValueError(f"Invalid completion signal type: {sig_type!r}")
    if not sig_value:
        raise ValueError(f"Completion signal value cannot be empty (type={sig_type})")
    return CompletionSignal(
        type=cast(Literal["path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"], sig_type),
        value=sig_value,
    )


def _parse_risk_assessment(raw: dict[str, Any]) -> RiskAssessment:
    """Parse risk assessment from a JSON dict.

    Args:
        raw: Dict with risk assessment fields.

    Returns:
        RiskAssessment dataclass.
    """
    return RiskAssessment(
        level=raw.get("level", "medium"),
        breaking_changes=raw.get("breaking_changes", False),
        affected_components=raw.get("affected_components", []),
        mitigation=raw.get("mitigation", ""),
    )


def _parse_rollback_plan(raw: dict[str, Any]) -> RollbackPlan:
    """Parse rollback plan from a JSON dict.

    Args:
        raw: Dict with rollback plan fields.

    Returns:
        RollbackPlan dataclass.
    """
    return RollbackPlan(
        steps=raw.get("steps", []),
        revert_commit=raw.get("revert_commit"),
        data_migration=raw.get("data_migration", ""),
        estimated_rollback_minutes=int(raw.get("estimated_rollback_minutes", 30)),
    )


def _parse_upgrade_details(raw: dict[str, Any]) -> UpgradeProposalDetails:
    """Parse upgrade proposal details from a JSON dict.

    Args:
        raw: Dict with upgrade proposal fields.

    Returns:
        UpgradeProposalDetails dataclass.
    """
    risk_raw = raw.get("risk_assessment", {})
    rollback_raw = raw.get("rollback_plan", {})
    return UpgradeProposalDetails(
        current_state=raw.get("current_state", ""),
        proposed_change=raw.get("proposed_change", ""),
        benefits=raw.get("benefits", []),
        risk_assessment=_parse_risk_assessment(risk_raw),
        rollback_plan=_parse_rollback_plan(rollback_raw),
        cost_estimate_usd=float(raw.get("cost_estimate_usd", 0.0)),
        performance_impact=raw.get("performance_impact", ""),
    )


def raw_dicts_to_tasks(raw_tasks: list[dict[str, Any]], id_prefix: str = "task") -> list[Task]:
    """Convert parsed JSON task dicts into domain Task objects.

    Invalid tasks are logged and skipped rather than causing a hard failure.

    Args:
        raw_tasks: List of dicts from ``parse_tasks_response``.
        id_prefix: Prefix for generated task IDs.

    Returns:
        List of valid Task objects.
    """
    tasks: list[Task] = []
    for i, raw in enumerate(raw_tasks):
        try:
            title = raw.get("title", "")
            if not title:
                logger.warning("Skipping task %d: missing title", i)
                continue

            # Parse completion signals
            signals: list[CompletionSignal] = []
            for sig_raw in raw.get("completion_signals", []):
                try:
                    signals.append(_parse_completion_signal(sig_raw))
                except ValueError as exc:
                    logger.warning("Skipping invalid signal in task %d: %s", i, exc)

            # Parse depends_on — LLM may output titles instead of IDs.
            depends_on = raw.get("depends_on", [])
            if not isinstance(depends_on, list):
                depends_on = []

            # Parse task type
            task_type_raw = raw.get("task_type", "standard")
            try:
                task_type = TaskType(task_type_raw)
            except ValueError:
                logger.warning("Invalid task_type %r in task %d, defaulting to standard", task_type_raw, i)
                task_type = TaskType.STANDARD

            # Parse upgrade details if present
            upgrade_details = None
            if task_type == TaskType.UPGRADE_PROPOSAL and "upgrade_details" in raw:
                try:
                    upgrade_details = _parse_upgrade_details(raw["upgrade_details"])
                except (ValueError, KeyError) as exc:
                    logger.warning("Failed to parse upgrade_details in task %d: %s", i, exc)

            task = Task(
                id=f"{id_prefix}-{i + 1:03d}",
                title=title,
                description=raw.get("description", title),
                role=raw.get("role", "backend"),
                priority=int(raw.get("priority", 2)),
                scope=Scope(raw.get("scope", "medium")),
                complexity=Complexity(raw.get("complexity", "medium")),
                estimated_minutes=int(raw.get("estimated_minutes", 60)),
                status=TaskStatus.OPEN,
                task_type=task_type,
                upgrade_details=upgrade_details,
                depends_on=[str(d) for d in depends_on],
                owned_files=raw.get("owned_files", []),
                completion_signals=signals,
            )
            tasks.append(task)
        except (ValueError, KeyError) as exc:
            logger.warning("Skipping task %d due to parse error: %s", i, exc)

    return tasks


def _resolve_depends_on(tasks: list[Task]) -> None:
    """Resolve depends_on from titles to task IDs in-place.

    The LLM outputs dependency titles (because it doesn't know IDs yet).
    This maps them to the generated IDs so the server can enforce ordering.

    Args:
        tasks: List of tasks with depends_on containing titles.
    """
    title_to_id: dict[str, str] = {}
    for task in tasks:
        title_to_id[task.title] = task.id
        # Also index lowercase for fuzzy matching.
        title_to_id[task.title.lower()] = task.id

    for task in tasks:
        resolved: list[str] = []
        for dep in task.depends_on:
            # Try exact match, then case-insensitive.
            dep_id = title_to_id.get(dep) or title_to_id.get(dep.lower())
            if dep_id:
                resolved.append(dep_id)
            else:
                logger.warning(
                    "Task %s depends on %r which was not found — dropping dependency",
                    task.id,
                    dep,
                )
        task.depends_on = resolved


def parse_review_response(raw: str) -> dict[str, Any]:
    """Parse the LLM review response into a result dict.

    Args:
        raw: Raw LLM response (should be a JSON object).

    Returns:
        Parsed review dict with verdict, reasoning, feedback, follow_up_tasks.

    Raises:
        ValueError: If the response is not valid JSON or missing required keys.
    """
    cleaned = _extract_json(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM review response is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")

    verdict = parsed.get("verdict", "")
    if verdict not in _VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict!r}. Must be one of {sorted(_VALID_VERDICTS)}")

    return cast(dict[str, Any], parsed)




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

    body = {
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
    # Include upgrade_details if present
    if task.upgrade_details:
        body["upgrade_details"] = asdict(task.upgrade_details)

    resp = await client.post(f"{server_url}/tasks", json=body)
    resp.raise_for_status()
    return cast(str, resp.json()["id"])


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

        tasks.append(Task(
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
        ))
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
        import json

        try:
            # Extract JSON from response
            text = response.strip()
            if text.startswith("```"):
                text = text[text.index("\n") + 1:]
            if text.endswith("```"):
                text = text[:text.rfind("```")]
            text = text.strip()

            changes_data = json.loads(text)
            changes = []

            for item in changes_data:
                changes.append(FileChange(
                    path=item.get("path", ""),
                    operation=item.get("operation", "modify"),
                    new_content=item.get("new_content"),
                    old_content=item.get("old_content"),
                ))

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
            f"- Completed ({len(completed)}): "
            + ", ".join(t.title for t in completed[:10])
            + "\n"
            f"- Failed ({len(failed)}): "
            + ", ".join(f"{t.title}: {t.result_summary or 'unknown'}" for t in failed[:5])
            + "\n"
            f"- Remaining ({len(remaining)}): "
            + ", ".join(t.title for t in remaining[:10])
            + "\n\n"
            "Create only the NEW tasks needed to get back on track. "
            "Do not duplicate remaining tasks."
        )
        return await self.plan(progress)

if __name__ == "__main__":
    import argparse
    import asyncio
    import sys
    from pathlib import Path

    from bernstein.core.seed import parse_seed

    async def main():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        parser = argparse.ArgumentParser()
        parser.add_argument("--port", type=int, default=8052)
        parser.add_argument("--task-id", type=str, required=True)
        args = parser.parse_args()

        workdir = Path.cwd()
        server_url = f"http://127.0.0.1:{args.port}"

        # Load seed to get the model to use
        seed_path = workdir / "bernstein.yaml"
        model_name = "nvidia/nemotron-3-super-120b-a12b"
        provider_name = "openrouter_free"
        if seed_path.exists():
            try:
                seed = parse_seed(seed_path)
                if seed.model:
                    model_name = seed.model
                    provider_name = "openrouter" # Assume paid if custom model
            except Exception as exc:
                logger.warning("Failed to parse seed for model config: %s", exc)

        agent = ManagerAgent(
            server_url=server_url,
            workdir=workdir,
            templates_dir=workdir / "templates",
            model=model_name,
            provider=provider_name
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
                    f"{server_url}/tasks/{args.task_id}/complete",
                    json={"result_summary": "Planning completed."}
                )
            except Exception as e:
                logger.exception("Manager Agent failed during plan generation")
                await client.post(f"{server_url}/tasks/{args.task_id}/fail", json={"reason": str(e)})
                sys.exit(1)

    asyncio.run(main())
