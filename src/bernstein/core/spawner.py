"""Spawn short-lived CLI agents for task batches."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from bernstein.agents.registry import AgentRegistry, get_registry
from bernstein.core.context import TaskContextBuilder
from bernstein.core.git_ops import MergeResult, merge_with_conflict_detection
from bernstein.core.models import AgentSession, ModelConfig, Task
from bernstein.core.router import RouterError, TierAwareRouter
from bernstein.core.traces import AgentTrace, TraceStore, finalize_trace, new_trace
from bernstein.core.worktree import WorktreeError, WorktreeManager
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter
    from bernstein.agents.catalog import CatalogAgent, CatalogRegistry
    from bernstein.core.agency_loader import AgencyAgent
    from bernstein.core.mcp_registry import MCPRegistry
    from bernstein.core.workspace import Workspace

# ---------------------------------------------------------------------------
# Module-level file cache (mtime-keyed, automatically invalidates on change)
# ---------------------------------------------------------------------------
_FILE_CACHE: dict[str, tuple[float, str]] = {}
_DIR_CACHE: dict[str, tuple[float, list[str]]] = {}


def _read_cached(path: Path) -> str:
    """Return file contents, re-reading only when mtime changes.

    Args:
        path: File to read.

    Returns:
        File contents, or empty string if the file does not exist.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _FILE_CACHE.pop(key, None)
        return ""
    cached = _FILE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    content = path.read_text(encoding="utf-8")
    _FILE_CACHE[key] = (mtime, content)
    return content


def _list_subdirs_cached(path: Path) -> list[str]:
    """Return sorted list of immediate subdirectory names, cached by mtime.

    Args:
        path: Directory to list.

    Returns:
        Sorted subdirectory names, or empty list if path is not a directory.
    """
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _DIR_CACHE.pop(key, None)
        return []
    cached = _DIR_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    names = sorted(d.name for d in path.iterdir() if d.is_dir())
    _DIR_CACHE[key] = (mtime, names)
    return names


logger = logging.getLogger(__name__)


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
) -> str:
    """Build the full agent prompt from role template + tasks + context.

    Uses the Jinja2-style template renderer for proper variable substitution.
    Falls back to simple string concatenation if rendering fails.  When the
    template renderer fallback is used, the agency catalog is checked for
    roles not covered by templates/roles/.

    If *catalog_system_prompt* is provided it replaces the built-in role
    template entirely, so the spawner can inject catalog-defined personas.

    Args:
        tasks: Batch of 1-3 tasks (all same role).
        templates_dir: Root of templates/roles/ directory.
        workdir: Project working directory.
        agency_catalog: Optional Agency agent catalog for extended roles.
        catalog_system_prompt: Optional system prompt from a catalog agent.
            When set, this replaces the template/role-based role prompt.
        context_builder: Optional TaskContextBuilder for rich context injection.

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
    project_context = _read_cached(project_md)

    # Completion instructions with concrete curl commands
    completion_cmds = "\n".join(
        f"curl -s -X POST http://127.0.0.1:8052/tasks/{t.id}/complete "
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
        available_roles = ", ".join(_list_subdirs_cached(templates_dir))

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
                "Fall back to generic roles (backend, qa, etc.) if no specialist fits.\n\n" + "\n".join(specialists)
            )

    # Build rich task context via TaskContextBuilder
    rich_context = ""
    if context_builder is not None:
        try:
            rich_context = context_builder.build_context(tasks)
        except Exception as exc:
            logger.warning("TaskContextBuilder failed, skipping rich context: %s", exc)

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
    if rich_context:
        sections.append(f"\n{rich_context}\n")
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
        mcp_registry: MCPRegistry | None = None,
        catalog: CatalogRegistry | None = None,
        use_worktrees: bool = False,
        workspace: Workspace | None = None,
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
        self._mcp_registry = mcp_registry
        self._catalog = catalog
        self._workspace = workspace
        self._context_builder = TaskContextBuilder(workdir)
        self._procs: dict[str, subprocess.Popen[bytes] | None] = {}
        self._use_worktrees = use_worktrees
        self._worktree_mgr: WorktreeManager | None = WorktreeManager(workdir) if use_worktrees else None
        self._worktree_paths: dict[str, Path] = {}
        self._traces: dict[str, AgentTrace] = {}
        self._trace_store = TraceStore(workdir / ".sdd" / "traces")

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
        metrics_dir = self._workdir / ".sdd" / "metrics"
        base_config = _select_batch_config(
            tasks,
            templates_dir=self._templates_dir,
            metrics_dir=metrics_dir if metrics_dir.exists() else None,
        )
        model_config = base_config
        provider_name: str | None = None

        if self._router is not None and self._router.state.providers:
            try:
                decision = self._router.select_provider_for_task(tasks[0], base_config=base_config)
                model_config = decision.model_config
                provider_name = decision.provider
            except RouterError as exc:
                logger.warning("Router failed to select provider, using fallback: %s", exc)

        # Check catalog for a specialist agent before building from templates
        role = tasks[0].role
        task_description = " ".join(t.description for t in tasks)
        catalog_agent: CatalogAgent | None = None
        if self._catalog is not None:
            catalog_agent = self._catalog.match(role, task_description)

        # Render prompt (catalog system_prompt replaces role template when matched)
        prompt = _render_prompt(
            tasks,
            self._templates_dir,
            self._workdir,
            self._agency_catalog,
            catalog_system_prompt=catalog_agent.system_prompt if catalog_agent else None,
            context_builder=self._context_builder,
        )

        agent_source = catalog_agent.source if catalog_agent else "built-in"
        if catalog_agent:
            logger.info(
                "Catalog agent '%s' (source=%s) selected for role '%s'",
                catalog_agent.name,
                catalog_agent.source,
                role,
            )

        # Build session
        session_id = f"{role}-{uuid.uuid4().hex[:8]}"
        session = AgentSession(
            id=session_id,
            role=role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
            provider=provider_name,
            agent_source=agent_source,
        )

        # Use CLI adapter for all roles including manager.
        # When the adapter is Claude Code, the manager agent gets API instructions
        # in its role template and uses curl to POST tasks to the server.
        target_adapter = self._adapter

        # Determine working directory: repo-specific > worktree > shared workdir
        spawn_cwd = self._workdir

        # If the task targets a specific repo in a multi-repo workspace,
        # use that repo's path as the working directory.
        task_repo = tasks[0].repo
        if task_repo is not None and self._workspace is not None:
            try:
                spawn_cwd = self._workspace.resolve_repo(task_repo)
                logger.info("Task targets repo '%s', spawn cwd: %s", task_repo, spawn_cwd)
            except KeyError:
                logger.warning(
                    "Task repo '%s' not found in workspace, falling back to workdir",
                    task_repo,
                )

        if self._use_worktrees and self._worktree_mgr is not None:
            try:
                spawn_cwd = self._worktree_mgr.create(session_id)
                self._worktree_paths[session_id] = spawn_cwd
            except WorktreeError as exc:
                logger.warning(
                    "Worktree creation failed for %s, falling back to workdir: %s",
                    session_id,
                    exc,
                )

        # Build per-task MCP config: auto-detected servers merged with base config
        effective_mcp = self._mcp_config
        if self._mcp_registry is not None:
            effective_mcp = self._mcp_registry.resolve_for_tasks(tasks, base_config=self._mcp_config)

        # Spawn via adapter
        result = target_adapter.spawn(
            prompt=prompt,
            workdir=spawn_cwd,
            model_config=model_config,
            session_id=session_id,
            mcp_config=effective_mcp,
        )
        session.pid = result.pid
        session.status = "working"
        if result.proc is not None:
            self._procs[session_id] = result.proc  # type: ignore[assignment]

        # Create and persist the initial trace
        # Serialize task fields to JSON-safe types (convert Enums to their values)
        import dataclasses

        def _task_to_dict(t: Task) -> dict[str, Any]:
            d: dict[str, Any] = {}
            for fld in dataclasses.fields(t):
                val: Any = getattr(t, fld.name)
                if hasattr(val, "value"):  # Enum
                    val = val.value
                elif isinstance(val, list):
                    val = [v.value if hasattr(v, "value") else v for v in cast("list[Any]", val)]
                d[fld.name] = val
            return d

        task_snapshots: list[dict[str, Any]] = [_task_to_dict(t) for t in tasks]
        trace = new_trace(
            session_id=session_id,
            task_ids=[t.id for t in tasks],
            role=role,
            model=model_config.model,
            effort=model_config.effort,
            log_path=str(result.log_path) if result.log_path else "",
            task_snapshots=task_snapshots,
        )
        self._traces[session_id] = trace
        try:
            self._trace_store.write(trace)
        except Exception as exc:
            logger.warning("Failed to write initial trace for %s: %s", session_id, exc)

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

    def reap_completed_agent(self, session: AgentSession) -> MergeResult | None:
        """Terminate and wait on the subprocess for a completed agent.

        Calls proc.terminate() then proc.wait(timeout=5) to reap the OS
        process. Safe to call when no proc is stored (pid-only spawns or
        unknown sessions). Idempotent: a second call is a no-op.

        When worktrees are enabled, attempts conflict-aware merge of the
        agent branch back into the current branch.  On conflict, aborts
        the merge and returns the MergeResult so the caller can route to
        a resolver agent or re-queue the task.

        Args:
            session: The AgentSession whose underlying process should be reaped.

        Returns:
            MergeResult when worktrees are enabled (None otherwise, or if
            no proc was stored).
        """
        proc = self._procs.pop(session.id, None)
        if proc is None:
            return None
        try:
            proc.terminate()
        except Exception as exc:
            logger.warning("reap_completed_agent: terminate failed for %s: %s", session.id, exc)
        try:
            proc.wait(timeout=5)
        except Exception as exc:
            logger.warning("reap_completed_agent: wait failed for %s: %s", session.id, exc)
        logger.info("Agent %s process reaped", session.id)

        # Finalize trace with outcome and parsed log steps
        trace = self._traces.pop(session.id, None)
        if trace is not None:
            outcome = "success" if session.status != "dead" else "unknown"
            finalize_trace(trace, outcome)
            try:
                self._trace_store.write(trace)
            except Exception as exc:
                logger.warning("Failed to write finalized trace for %s: %s", session.id, exc)

        # Merge worktree branch back and clean up
        worktree_path = self._worktree_paths.pop(session.id, None)
        merge_result: MergeResult | None = None
        if worktree_path is not None and self._worktree_mgr is not None:
            merge_result = self._merge_worktree_branch(session.id)
            self._worktree_mgr.cleanup(session.id)
        return merge_result

    def update_trace_outcome(self, session_id: str, outcome: str) -> None:
        """Update the stored trace outcome for a session.

        Called by the orchestrator when it learns a task succeeded or failed
        via the task server (before the process is reaped).

        Args:
            session_id: The session whose trace should be updated.
            outcome: "success" or "failed".
        """
        trace = self._traces.get(session_id)
        if trace is None:
            return
        if outcome in ("success", "failed", "unknown"):
            trace.outcome = outcome  # type: ignore[assignment]
            try:
                self._trace_store.write(trace)
            except Exception as exc:
                logger.warning("Failed to update trace outcome for %s: %s", session_id, exc)

    def _merge_worktree_branch(self, session_id: str) -> MergeResult:
        """Merge the agent's worktree branch with conflict detection.

        Uses ``merge_with_conflict_detection`` for a safe, abort-on-conflict
        merge.  On success the branch is merged; on conflict the merge is
        aborted and the caller receives the list of conflicting files.

        Args:
            session_id: The session whose branch should be merged.

        Returns:
            MergeResult with success status and any conflicting files.
        """
        branch_name = f"agent/{session_id}"
        try:
            result = merge_with_conflict_detection(
                self._workdir,
                branch_name,
                message=f"Merge {branch_name}",
            )
            if result.success:
                logger.info("Merged worktree branch %s into current branch", branch_name)
            elif result.conflicting_files:
                logger.warning(
                    "Merge conflicts for %s in files: %s",
                    session_id,
                    ", ".join(result.conflicting_files),
                )
            else:
                logger.warning("Merge failed for %s: %s", session_id, result.error)
            return result
        except Exception as exc:
            logger.warning("Merge failed for %s: %s", session_id, exc)
            return MergeResult(success=False, conflicting_files=[], error=str(exc))


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

        raw_data: object = yaml.safe_load(_read_cached(config_path))
        if not isinstance(raw_data, dict):
            return None
        data: dict[str, Any] = cast("dict[str, Any]", raw_data)
        model = str(data.get("default_model", "sonnet"))
        effort = str(data.get("default_effort", "high"))
        return ModelConfig(model=model, effort=effort)
    except Exception as exc:
        logger.warning("Failed to load role config for '%s': %s", role, exc)
        return None


def _select_batch_config(
    tasks: list[Task],
    templates_dir: Path | None = None,
    metrics_dir: Path | None = None,
) -> ModelConfig:
    """Pick the highest-tier model config across all tasks in a batch.

    If *templates_dir* is provided, reads the role's config.yaml first and
    uses that as the baseline before falling back to heuristic routing.
    If *metrics_dir* is provided, consults the epsilon-greedy bandit for
    non-high-stakes roles to dynamically pick the cheapest viable model.
    Routes each task individually, then picks the most capable config
    so the agent can handle the hardest task in its batch.

    Args:
        tasks: Non-empty list of tasks.
        templates_dir: Optional path to templates/roles/ for config.yaml lookup.
        metrics_dir: Optional path to .sdd/metrics for bandit state.

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
    from bernstein.core.router import route_task

    def _route_for_batch(task: Task) -> ModelConfig:
        """Batch-specific routing: consult bandit when available, else heuristics."""
        if task.model or task.effort:
            return ModelConfig(model=task.model or "sonnet", effort=task.effort or "normal")
        # High-stakes roles skip bandit — always use premium models
        if task.role == "manager":
            return ModelConfig(model="opus", effort="max")
        if task.role in ("architect", "security"):
            return ModelConfig(model="opus", effort="high")
        if task.scope == Scope.LARGE and task.complexity == Complexity.HIGH:
            return ModelConfig(model="opus", effort="high")
        if task.priority == 1 or task.scope == Scope.LARGE:
            return ModelConfig(model="sonnet", effort="max")
        # Consult bandit for standard tasks
        return route_task(task, bandit_metrics_dir=metrics_dir)

    configs = [_route_for_batch(t) for t in tasks]
    # Sort by model tier (opus > sonnet > haiku) then effort (max > high > normal)
    model_rank = {"opus": 3, "sonnet": 2, "haiku": 1}
    effort_rank = {"max": 3, "high": 2, "normal": 1}
    return max(
        configs,
        key=lambda c: (model_rank.get(c.model, 0), effort_rank.get(c.effort, 0)),
    )
