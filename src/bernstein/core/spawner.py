"""Spawn short-lived CLI agents for task batches."""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter
    from bernstein.core.agency_loader import AgencyAgent

from bernstein.agents.registry import AgentRegistry, get_registry
from bernstein.core.models import AgentSession, ModelConfig, Task
from bernstein.core.router import RouterError, TierAwareRouter
from bernstein.templates.renderer import TemplateError, render_role_prompt

logger = logging.getLogger(__name__)


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
) -> str:
    """Build the full agent prompt from role template + tasks + context.

    Uses the Jinja2-style template renderer for proper variable substitution.
    Falls back to simple string concatenation if rendering fails.  When the
    template renderer fallback is used, the agency catalog is checked for
    roles not covered by templates/roles/.

    Args:
        tasks: Batch of 1-3 tasks (all same role).
        templates_dir: Root of templates/roles/ directory.
        workdir: Project working directory.
        agency_catalog: Optional Agency agent catalog for extended roles.

    Returns:
        Complete prompt string ready for the CLI adapter.
    """
    role = tasks[0].role

    # Build task descriptions block
    task_lines: list[str] = []
    for i, task in enumerate(tasks, 1):
        task_lines.append(f"### Task {i}: {task.title} (id={task.id})")
        task_lines.append(task.description)
        if task.owned_files:
            task_lines.append(f"Files: {', '.join(task.owned_files)}")
        task_lines.append("")
    task_block = "\n".join(task_lines)

    # Project context from .sdd/project.md if it exists
    project_md = workdir / ".sdd" / "project.md"
    project_context = project_md.read_text(encoding="utf-8") if project_md.exists() else ""

    # Completion instructions with concrete curl commands
    completion_cmds = "\n".join(
        f'curl -s -X POST http://127.0.0.1:8052/tasks/{t.id}/complete '
        f'-H "Content-Type: application/json" '
        f'-d \'{{"result_summary": "Completed: {t.title}"}}\''
        for t in tasks
    )
    instructions = (
        f"Complete these tasks. When ALL are done, mark each complete on the task server:\n\n"
        f"```bash\n{completion_cmds}\n```\n\n"
        f"Then exit."
    )

    # Available roles from templates directory
    available_roles = ""
    if templates_dir.is_dir():
        available_roles = ", ".join(
            d.name for d in sorted(templates_dir.iterdir()) if d.is_dir()
        )

    # Specialist agents from agency catalog
    specialist_block = ""
    if agency_catalog and role == "manager":
        specialists: list[str] = []
        for agent in sorted(agency_catalog.values(), key=lambda a: a.role):
            specialists.append(f"- **{agent.name}** ({agent.role}): {agent.description}")
        if specialists:
            specialist_block = (
                "\n\n## Available specialist agents (from Agency catalog)\n"
                "When creating tasks, prefer assigning to a specialist role if one matches.\n"
                "Fall back to generic roles (backend, qa, etc.) if no specialist fits.\n\n"
                + "\n".join(specialists)
            )

    # Build template context for renderer
    context = {
        "GOAL": tasks[0].title,
        "TASK_DESCRIPTION": task_block,
        "PROJECT_STATE": project_context,
        "AVAILABLE_ROLES": available_roles,
        "INSTRUCTIONS": instructions,
        "SPECIALISTS": specialist_block,
    }

    # Try renderer first, fall back to string concat on failure
    try:
        role_prompt = render_role_prompt(role, context, templates_dir=templates_dir)
    except (FileNotFoundError, TemplateError) as exc:
        logger.debug("Template render failed for role %s, using fallback: %s", role, exc)
        role_prompt = _render_fallback(role, templates_dir, agency_catalog)

    # Assemble final prompt
    sections = [role_prompt]
    if specialist_block:
        sections.append(specialist_block)
    sections.append(f"\n## Assigned tasks\n{task_block}")
    if project_context:
        sections.append(f"\n## Project context\n{project_context}\n")
    sections.append(f"\n## Instructions\n{instructions}\n")

    return "".join(sections)


def _render_fallback(
    role: str,
    templates_dir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
) -> str:
    """Fallback: read raw template, check agency catalog, or generate default.

    Args:
        role: Role name.
        templates_dir: Root of templates/roles/ directory.
        agency_catalog: Optional Agency agent catalog to check for roles
            not found in templates/roles/.

    Returns:
        Raw role prompt string without variable substitution.
    """
    template_path = templates_dir / role / "system_prompt.md"
    if template_path.exists():
        return template_path.read_text(encoding="utf-8")

    # Check agency catalog: look for an agent whose name or role matches.
    if agency_catalog:
        agent = agency_catalog.get(role)
        if agent is None:
            # Try matching by mapped role name.
            for a in agency_catalog.values():
                if a.role == role:
                    agent = a
                    break
        if agent and agent.prompt_body:
            logger.info("Using Agency agent '%s' for role '%s'", agent.name, role)
            return agent.prompt_body

    return f"You are a {role} specialist."


class AgentSpawner:
    """Spawns short-lived CLI agents for task batches.

    Agents are spawned per-batch and expected to exit after completion.
    No long-running sessions -- see ADR-001.

    Args:
        adapter: CLI adapter for launching agent processes.
        templates_dir: Path to templates/roles/ directory.
        workdir: Project working directory.
        agent_registry: Optional agent registry for dynamic agent types.
    """

    def __init__(
        self,
        adapter: CLIAdapter,
        templates_dir: Path,
        workdir: Path,
        agent_registry: AgentRegistry | None = None,
        agency_catalog: dict[str, AgencyAgent] | None = None,
        router: TierAwareRouter | None = None,
        mcp_config: dict[str, Any] | None = None,
    ) -> None:
        self._adapter = adapter
        self._templates_dir = templates_dir
        self._workdir = workdir
        self._registry = agent_registry or get_registry(
            definitions_dir=workdir / ".sdd" / "agents" / "definitions",
            auto_reload=True,
        )
        self._agency_catalog = agency_catalog
        self._router = router
        self._mcp_config = mcp_config

    def spawn_for_tasks(self, tasks: list[Task]) -> AgentSession:
        """Route, render prompt, and spawn an agent for a task batch.

        Args:
            tasks: Batch of 1-3 tasks. All must share the same role.

        Returns:
            AgentSession with PID and metadata populated.

        Raises:
            ValueError: If tasks list is empty or roles are mixed.
        """
        if not tasks:
            raise ValueError("Cannot spawn agent with empty task list")

        roles = {t.role for t in tasks}
        if len(roles) > 1:
            raise ValueError(f"All tasks in a batch must share the same role, got: {roles}")

        # Route based on highest-complexity task in batch; use TierAwareRouter if available
        base_config = _select_batch_config(tasks, templates_dir=self._templates_dir)
        model_config = base_config
        provider_name: str | None = None

        if self._router is not None and self._router.state.providers:
            try:
                decision = self._router.select_provider_for_task(tasks[0], base_config=base_config)
                model_config = decision.model_config
                provider_name = decision.provider
            except RouterError as exc:
                logger.warning("Router failed to select provider, using fallback: %s", exc)

        # Render prompt
        prompt = _render_prompt(tasks, self._templates_dir, self._workdir, self._agency_catalog)

        # Build session
        session_id = f"{tasks[0].role}-{uuid.uuid4().hex[:8]}"
        session = AgentSession(
            id=session_id,
            role=tasks[0].role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
            provider=provider_name,
        )

        # Use CLI adapter for all roles including manager.
        # When the adapter is Claude Code, the manager agent gets API instructions
        # in its role template and uses curl to POST tasks to the server.
        target_adapter = self._adapter

        # Spawn via adapter
        result = target_adapter.spawn(
            prompt=prompt,
            workdir=self._workdir,
            model_config=model_config,
            session_id=session_id,
            mcp_config=self._mcp_config,
        )
        session.pid = result.pid
        session.status = "working"

        return session

    def check_alive(self, session: AgentSession) -> bool:
        """Check if the agent process is still running.

        Args:
            session: Agent session to check.

        Returns:
            True if the process is alive, False otherwise.
        """
        if session.pid is None:
            return False
        return self._adapter.is_alive(session.pid)

    def kill(self, session: AgentSession) -> None:
        """Terminate the agent process and mark session dead.

        Args:
            session: Agent session to kill.
        """
        if session.pid is not None:
            self._adapter.kill(session.pid)
        session.status = "dead"


def _load_role_config(role: str, templates_dir: Path) -> ModelConfig | None:
    """Load ModelConfig from a role's config.yaml if present.

    Args:
        role: Role name (e.g. "backend", "manager").
        templates_dir: Root of templates/roles/ directory.

    Returns:
        ModelConfig from config.yaml, or None if not found / unreadable.
    """
    config_path = templates_dir / role / "config.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        data: object = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        model = str(data.get("default_model", "sonnet"))
        effort = str(data.get("default_effort", "high"))
        return ModelConfig(model=model, effort=effort)
    except Exception as exc:
        logger.warning("Failed to load role config for '%s': %s", role, exc)
        return None


def _select_batch_config(
    tasks: list[Task],
    templates_dir: Path | None = None,
) -> ModelConfig:
    """Pick the highest-tier model config across all tasks in a batch.

    If *templates_dir* is provided, reads the role's config.yaml first and
    uses that as the baseline before falling back to heuristic routing.
    Routes each task individually, then picks the most capable config
    so the agent can handle the hardest task in its batch.

    Args:
        tasks: Non-empty list of tasks.
        templates_dir: Optional path to templates/roles/ for config.yaml lookup.

    Returns:
        ModelConfig suitable for the entire batch.
    """
    # If a role-level config.yaml exists, use it as the baseline
    role = tasks[0].role
    if templates_dir is not None:
        role_config = _load_role_config(role, templates_dir)
        if role_config is not None:
            return role_config

    from bernstein.core.models import Complexity, Scope

    def _route_for_batch(task: Task) -> ModelConfig:
        """Batch-specific routing: conservative default, escalates for complex tasks."""
        if task.model or task.effort:
            return ModelConfig(model=task.model or "sonnet", effort=task.effort or "normal")
        if task.role == "manager":
            return ModelConfig(model="opus", effort="max")
        if task.role in ("architect", "security"):
            return ModelConfig(model="opus", effort="high")
        if task.scope == Scope.LARGE and task.complexity == Complexity.HIGH:
            return ModelConfig(model="opus", effort="high")
        if task.priority == 1 or task.scope == Scope.LARGE:
            return ModelConfig(model="sonnet", effort="max")
        if task.complexity == Complexity.HIGH:
            return ModelConfig(model="sonnet", effort="high")
        return ModelConfig(model="sonnet", effort="normal")

    configs = [_route_for_batch(t) for t in tasks]
    # Sort by model tier (opus > sonnet) then effort (max > high > normal)
    model_rank = {"opus": 2, "sonnet": 1}
    effort_rank = {"max": 3, "high": 2, "normal": 1}
    return max(
        configs,
        key=lambda c: (model_rank.get(c.model, 0), effort_rank.get(c.effort, 0)),
    )
