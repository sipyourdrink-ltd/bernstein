"""Spawn short-lived CLI agents for task batches."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, cast

from bernstein.adapters.base import SpawnResult
from bernstein.agents.registry import AgentRegistry, get_registry
from bernstein.core.container import ContainerConfig, ContainerError, ContainerManager
from bernstein.core.context import TaskContextBuilder
from bernstein.core.git_ops import MergeResult, merge_with_conflict_detection
from bernstein.core.lessons import gather_lessons_for_context
from bernstein.core.lifecycle import transition_agent
from bernstein.core.models import AgentSession, IsolationMode, ModelConfig, Task
from bernstein.core.router import RouterError, TierAwareRouter
from bernstein.core.traces import AgentTrace, TraceStore, finalize_trace, new_trace
from bernstein.core.worktree import WorktreeError, WorktreeManager, WorktreeSetupConfig
from bernstein.plugins.manager import get_plugin_manager
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    import subprocess
    from pathlib import Path

    from bernstein.adapters.base import CLIAdapter
    from bernstein.agents.catalog import CatalogAgent, CatalogRegistry
    from bernstein.core.agency_loader import AgencyAgent
    from bernstein.core.bulletin import BulletinBoard
    from bernstein.core.graph import TaskGraph
    from bernstein.core.mcp_manager import MCPManager
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


def _render_signal_check(session_id: str) -> str:
    """Return signal-check instructions to append to every agent's system prompt.

    Args:
        session_id: The session ID assigned to this agent.

    Returns:
        Markdown block instructing the agent to poll signal files.
    """
    return (
        "\n## Signal files — check periodically\n"
        "Every 60 seconds, check for orchestrator signals:\n"
        "```bash\n"
        f"cat .sdd/runtime/signals/{session_id}/WAKEUP 2>/dev/null\n"
        f"cat .sdd/runtime/signals/{session_id}/SHUTDOWN 2>/dev/null\n"
        "```\n"
        "If **SHUTDOWN** exists:\n"
        "```bash\n"
        'git add -A && git commit -m "[WIP] <task title>" 2>/dev/null || true\n'
        "exit 0\n"
        "```\n"
        "If **WAKEUP** exists: read it, address the concern, then continue working.\n"
    )


def _extract_tags_from_tasks(tasks: list[Task]) -> list[str]:
    """Derive lesson-retrieval tags from a batch of tasks.

    Uses the role and significant title words as tags.

    Args:
        tasks: Batch of tasks.

    Returns:
        List of lowercase tags for lesson lookup.
    """
    stop_words = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "shall",
        "can",
        "not",
        "no",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "into",
        "out",
        "up",
        "down",
        "over",
        "this",
        "that",
        "it",
        "its",
    }
    tags: set[str] = set()
    for task in tasks:
        tags.add(task.role.lower())
        for word in task.title.lower().split():
            cleaned = word.strip("—-_.,;:!?()[]{}\"'`#")
            if len(cleaned) > 2 and cleaned not in stop_words:
                tags.add(cleaned)
    return sorted(tags)


def _render_predecessor_context(tasks: list[Task], task_graph: TaskGraph | None) -> str:
    """Build a context section from INFORMS/TRANSFORMS predecessor outputs.

    Args:
        tasks: Batch of tasks being assigned.
        task_graph: Optional task graph for looking up typed edges.

    Returns:
        Markdown section with predecessor results, or empty string.
    """
    if task_graph is None:
        return ""

    lines: list[str] = []
    for task in tasks:
        pred_ctx = task_graph.predecessor_context(task.id)
        for item in pred_ctx:
            summary = item["result_summary"]
            if not summary:
                continue
            edge_label = "informed by" if item["edge_type"] == "informs" else "transforms output of"
            lines.append(f"- **{item['title']}** ({edge_label}): {summary}")

    if not lines:
        return ""
    return (
        "\n## Predecessor context\n"
        "The following completed tasks provide context for your work:\n" + "\n".join(lines) + "\n"
    )


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
    session_id: str = "",
    bulletin_summary: str = "",
    task_graph: TaskGraph | None = None,
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
        bulletin_summary: Optional recent bulletin activity to inject as a
            team-awareness section. Empty string means no section is added.
        task_graph: Optional task graph for injecting typed-edge predecessor
            context (INFORMS / TRANSFORMS outputs).

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

    # Completion instructions with concrete curl commands and retry logic.
    # The server may briefly restart during hot-reload (evolve mode), so
    # agents must retry on transient connection errors.
    completion_cmds = "\n".join(
        f"curl -s --retry 3 --retry-delay 2 --retry-all-errors "
        f"-X POST http://127.0.0.1:8052/tasks/{t.id}/complete "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"result_summary": "Completed: {t.title}"}}\''
        for t in tasks
    )
    instructions = (
        f"Complete these tasks. When ALL are done:\n\n"
        f"**Step 1: Commit your changes**\n"
        f"```bash\n"
        f'git add -A && git commit -m "feat: <brief summary of what you did>"\n'
        f"```\n\n"
        f"**Step 2: Mark tasks complete on the task server**\n"
        f"```bash\n{completion_cmds}\n```\n\n"
        f"**Note:** If a curl request fails with a connection error, retry up to 3 times "
        f"with a 2-second delay. The server may briefly restart during code updates.\n\n"
        f"**Step 3: Exit**"
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

    # Use catalog system prompt when available (Agency specialist prompt),
    # otherwise fall back to role template or built-in default.
    if catalog_system_prompt:
        role_prompt = catalog_system_prompt
    else:
        try:
            role_prompt = render_role_prompt(role, context, templates_dir=templates_dir)
        except (FileNotFoundError, TemplateError) as exc:
            logger.debug("Template render failed for role %s, using fallback: %s", role, exc)
            role_prompt = _render_fallback(role, templates_dir, agency_catalog)

    # Inject prior agent lessons based on task tags
    sdd_dir = workdir / ".sdd"
    lesson_tags = _extract_tags_from_tasks(tasks)
    lesson_context = gather_lessons_for_context(sdd_dir, lesson_tags)

    # Assemble final prompt
    sections = [role_prompt]
    if specialist_block:
        sections.append(specialist_block)
    sections.append(f"\n## Assigned tasks\n{task_block}")
    if lesson_context:
        sections.append(f"\n{lesson_context}\n")
    if rich_context:
        sections.append(f"\n{rich_context}\n")
    predecessor_ctx = _render_predecessor_context(tasks, task_graph)
    if predecessor_ctx:
        sections.append(predecessor_ctx)
    if bulletin_summary:
        sections.append(
            f"\n## Team awareness\n"
            f"Other agents are working in parallel. Recent activity:\n{bulletin_summary}\n\n"
            f"If you need to create a shared utility, check if it already exists first.\n"
            f"If you define an API endpoint, use consistent naming with existing endpoints.\n"
        )
    if project_context:
        sections.append(f"\n## Project context\n{project_context}\n")
    sections.append(f"\n## Instructions\n{instructions}\n")
    if session_id:
        sections.append(_render_signal_check(session_id))

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
        mcp_manager: MCPManager | None = None,
        catalog: CatalogRegistry | None = None,
        use_worktrees: bool = True,
        worktree_setup_config: WorktreeSetupConfig | None = None,
        workspace: Workspace | None = None,
        bulletin: BulletinBoard | None = None,
        enable_caching: bool = False,
        container_config: ContainerConfig | None = None,
    ) -> None:
        if enable_caching:
            from bernstein.adapters.caching_adapter import CachingAdapter

            adapter = CachingAdapter(adapter, workdir)
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
        self._mcp_manager = mcp_manager
        self._catalog = catalog
        self._workspace = workspace
        self._bulletin = bulletin
        self._context_builder = TaskContextBuilder(workdir)
        self._procs: dict[str, subprocess.Popen[bytes] | None] = {}
        self._use_worktrees = use_worktrees
        self._worktree_mgr: WorktreeManager | None = None
        if use_worktrees:
            self._worktree_mgr = WorktreeManager(workdir, setup_config=worktree_setup_config)
            # Clean stale worktrees from prior crashed/stopped runs
            cleaned = self._worktree_mgr.cleanup_all_stale()
            if cleaned:
                logger.info("Cleaned %d stale worktree(s) from prior run", cleaned)
        self._worktree_paths: dict[str, Path] = {}
        self._traces: dict[str, AgentTrace] = {}
        self._trace_store = TraceStore(workdir / ".sdd" / "traces")
        # Container isolation
        self._container_mgr: ContainerManager | None = None
        if container_config is not None:
            try:
                self._container_mgr = ContainerManager(container_config, workdir)
            except ContainerError as exc:
                logger.warning("Container runtime unavailable, falling back to subprocess: %s", exc)

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

        # Build session ID early so we can inject it into the prompt for signal checks
        session_id = f"{role}-{uuid.uuid4().hex[:8]}"

        # Build catalog system prompt, appending tool preferences when present
        catalog_system_prompt: str | None = None
        if catalog_agent and catalog_agent.system_prompt:
            catalog_system_prompt = catalog_agent.system_prompt
            if catalog_agent.tools:
                tools_hint = "\n\n## Preferred tools\nUse these tools when available: " + ", ".join(
                    f"`{t}`" for t in catalog_agent.tools
                )
                catalog_system_prompt = catalog_system_prompt + tools_hint

        # Render prompt (catalog system_prompt replaces role template when matched)
        bulletin_summary = self._bulletin.summary() if self._bulletin is not None else ""
        prompt = _render_prompt(
            tasks,
            self._templates_dir,
            self._workdir,
            self._agency_catalog,
            catalog_system_prompt=catalog_system_prompt,
            context_builder=self._context_builder,
            session_id=session_id,
            bulletin_summary=bulletin_summary,
        )

        agent_source = catalog_agent.source if catalog_agent else "built-in"
        if catalog_agent:
            logger.info(
                "Catalog agent '%s' (source=%s) selected for role '%s'",
                catalog_agent.name,
                catalog_agent.source,
                role,
            )
        # Determine isolation mode
        isolation_mode = IsolationMode.NONE
        if self._container_mgr is not None:
            isolation_mode = IsolationMode.CONTAINER
        elif self._use_worktrees:
            isolation_mode = IsolationMode.WORKTREE

        session = AgentSession(
            id=session_id,
            role=role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
            provider=provider_name,
            agent_source=agent_source,
            isolation=isolation_mode.value,
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

        # Layer MCPManager servers on top (task-requested MCP servers)
        if self._mcp_manager is not None:
            # Collect MCP server names requested by tasks in this batch
            task_server_names: list[str] = []
            for t in tasks:
                task_server_names.extend(t.mcp_servers)
            # Deduplicate while preserving order
            seen: set[str] = set()
            unique_names: list[str] = []
            for n in task_server_names:
                if n not in seen:
                    seen.add(n)
                    unique_names.append(n)
            # Pass None to get all servers when no specific ones requested
            requested = unique_names if unique_names else None
            effective_mcp = self._mcp_manager.build_mcp_config_for_task(
                task_mcp_servers=requested,
                base_config=effective_mcp,
            )

        # Spawn via adapter (with optional container isolation)
        if self._container_mgr is not None:
            result = self._spawn_in_container(
                session_id=session_id,
                prompt=prompt,
                spawn_cwd=spawn_cwd,
                model_config=model_config,
                mcp_config=effective_mcp,
                session=session,
            )
        else:
            result = target_adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=effective_mcp,
            )
        session.pid = result.pid
        transition_agent(session, "working", actor="spawner", reason="agent process started")
        if result.log_path:
            session.log_path = str(result.log_path)
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

        get_plugin_manager().fire_agent_spawned(
            session_id=session.id, role=session.role, model=session.model_config.model
        )
        return session

    def spawn_for_resume(
        self,
        tasks: list[Task],
        *,
        worktree_path: Path,
        changed_files: list[str],
    ) -> AgentSession:
        """Spawn a new agent to resume work in a crashed agent's worktree.

        Builds a prompt that includes context about the previous crash and the
        files already modified, then spawns the agent in the preserved worktree
        directory instead of creating a new one.

        Args:
            tasks: Batch of tasks (same role) to resume.
            worktree_path: Path to the preserved worktree from the crashed agent.
            changed_files: Files already modified by the crashed agent.

        Returns:
            AgentSession with PID and metadata populated.
        """
        if not tasks:
            raise ValueError("Cannot resume with empty task list")

        # Build resume context prefix
        files_list = "\n".join(f"  - {f}" for f in changed_files) if changed_files else "  (none)"
        resume_header = (
            "## Crash recovery\n"
            "The previous agent assigned to this task crashed. "
            "Continue from where it left off.\n"
            f"Files already modified by the previous agent:\n{files_list}\n\n"
        )

        metrics_dir = self._workdir / ".sdd" / "metrics"
        model_config = _select_batch_config(
            tasks,
            templates_dir=self._templates_dir,
            metrics_dir=metrics_dir if metrics_dir.exists() else None,
        )
        role = tasks[0].role
        session_id = f"{role}-resume-{uuid.uuid4().hex[:8]}"

        prompt = _render_prompt(
            tasks,
            self._templates_dir,
            self._workdir,
            self._agency_catalog,
            context_builder=self._context_builder,
            session_id=session_id,
        )
        # Prepend crash recovery context
        prompt = resume_header + prompt

        session = AgentSession(
            id=session_id,
            role=role,
            task_ids=[t.id for t in tasks],
            model_config=model_config,
            status="starting",
        )

        result = self._adapter.spawn(
            prompt=prompt,
            workdir=worktree_path,
            model_config=model_config,
            session_id=session_id,
        )
        session.pid = result.pid
        transition_agent(session, "working", actor="spawner", reason="agent process started in worktree")
        if result.log_path:
            session.log_path = str(result.log_path)
        if result.proc is not None:
            self._procs[session_id] = result.proc  # type: ignore[assignment]

        # Track worktree so reap_completed_agent can merge+clean up
        self._worktree_paths[session_id] = worktree_path

        return session

    def _spawn_in_container(
        self,
        *,
        session_id: str,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        session: AgentSession,
    ) -> SpawnResult:
        """Spawn an agent inside a container.

        Builds the adapter command, then runs it inside a container
        managed by the ContainerManager.  Falls back to direct subprocess
        spawn if container creation fails.

        Args:
            session_id: Agent session ID.
            prompt: Rendered agent prompt.
            spawn_cwd: Working directory for the agent.
            model_config: Model and effort configuration.
            mcp_config: MCP server configuration.
            session: AgentSession to update with container metadata.

        Returns:
            SpawnResult with PID and log path.
        """
        assert self._container_mgr is not None

        # Build environment for the container from the adapter's filtered env
        from bernstein.adapters.env_isolation import build_filtered_env

        adapter_name = self._adapter.name().lower()
        extra_keys: list[str] = []
        if "claude" in adapter_name:
            extra_keys.append("ANTHROPIC_API_KEY")
        elif "gemini" in adapter_name:
            extra_keys.extend(["GOOGLE_API_KEY", "GEMINI_API_KEY"])
        elif "codex" in adapter_name:
            extra_keys.append("OPENAI_API_KEY")
        container_env = build_filtered_env(extra_keys)

        # Write the prompt to a temp file inside the workspace so the
        # container can read it
        prompt_file = spawn_cwd / ".sdd" / "runtime" / "prompts" / f"{session_id}.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        # Build the CLI command the adapter would normally run
        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        # --- Two-phase sandbox (Codex-style) ---
        # Phase 1: run dependency installation with network access.
        # Phase 2: run the agent with network disabled.
        from bernstein.core.container import NetworkMode, _detect_setup_commands

        two_phase_cfg = self._container_mgr.config.two_phase_sandbox
        phase2_network_override: NetworkMode | None = None

        if two_phase_cfg is not None:
            setup_cmds = list(two_phase_cfg.setup_commands) or _detect_setup_commands(spawn_cwd)
            if setup_cmds:
                ok = self._container_mgr.run_phase1_setup(
                    session_id=session_id,
                    setup_cmds=setup_cmds,
                    env=container_env,
                    workspace_override=spawn_cwd,
                    timeout_s=two_phase_cfg.phase1_timeout_s,
                )
                if not ok:
                    logger.warning(
                        "Phase 1 setup failed for %s — proceeding to Phase 2 anyway",
                        session_id,
                    )
            phase2_network_override = two_phase_cfg.phase2_network_mode

        try:
            handle = self._container_mgr.spawn_in_container(
                session_id=session_id,
                cmd=self._adapter_cmd_for_container(
                    prompt_file=prompt_file,
                    model_config=model_config,
                    session_id=session_id,
                    mcp_config=mcp_config,
                ),
                env=container_env,
                workspace_override=spawn_cwd,
                log_path=log_path,
                network_mode_override=phase2_network_override,
            )
            session.container_id = handle.container_id
            session.isolation = IsolationMode.CONTAINER.value
            return SpawnResult(pid=handle.pid or 0, log_path=log_path)
        except ContainerError as exc:
            logger.warning(
                "Container spawn failed for %s, falling back to subprocess: %s",
                session_id,
                exc,
            )
            session.isolation = IsolationMode.NONE.value
            return self._adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
            )

    def _adapter_cmd_for_container(
        self,
        *,
        prompt_file: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None,
    ) -> list[str]:
        """Build the CLI command to run inside the container.

        Reads the prompt from the prompt file instead of passing it as
        a command-line argument (which can hit ARG_MAX limits).

        Args:
            prompt_file: Path to the prompt file inside the workspace.
            model_config: Model and effort config.
            session_id: Session ID for the worker wrapper.
            mcp_config: MCP configuration dict.

        Returns:
            Command argument list.
        """
        # Map container path: host workspace is mounted at /workspace
        container_prompt = f"/workspace/.sdd/runtime/prompts/{session_id}.md"

        # Build a generic shell command that reads the prompt and pipes it
        # to the adapter CLI. This works across all adapters.
        adapter_name = self._adapter.name().lower()
        if "claude" in adapter_name:
            cmd = [
                "sh",
                "-c",
                f"claude --model {model_config.model} "
                f"--effort {model_config.effort} "
                f"--max-turns 50 "
                f"--dangerously-skip-permissions "
                f"--output-format stream-json "
                f'-p "$(cat {container_prompt})"',
            ]
        else:
            # Generic: assume the adapter CLI reads from stdin or -p flag
            cmd = [
                "sh",
                "-c",
                f'cat {container_prompt} | {adapter_name} -p "$(cat {container_prompt})"',
            ]
        return cmd

    def check_alive(self, session: AgentSession) -> bool:
        """Check if the agent process is still running.

        Args:
            session: Agent session to check.

        Returns:
            True if the process is alive, False otherwise.
        """
        # Container-based agents: check container status
        if session.container_id and self._container_mgr is not None:
            handle = self._container_mgr.get_handle(session.id)
            if handle is not None:
                return self._container_mgr.is_alive(handle)
            return False

        if session.pid is None:
            return False
        return self._adapter.is_alive(session.pid)

    def kill(self, session: AgentSession) -> None:
        """Terminate the agent process and mark session dead.

        Args:
            session: Agent session to kill.
        """
        # Container-based agents: stop and destroy the container
        if session.container_id and self._container_mgr is not None:
            handle = self._container_mgr.get_handle(session.id)
            if handle is not None:
                self._container_mgr.destroy(handle)
        elif session.pid is not None:
            self._adapter.kill(session.pid)
        if session.status != "dead":
            transition_agent(session, "dead", actor="spawner", reason="kill requested")

    def reap_completed_agent(
        self,
        session: AgentSession,
        skip_merge: bool = False,
    ) -> MergeResult | None:
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
            skip_merge: When True, skip the worktree merge but still clean up
                the process and worktree.  Used by the approval gate when the
                user rejects a task or when a PR is created instead.

        Returns:
            MergeResult when worktrees are enabled and skip_merge is False
            (None otherwise, or if no proc was stored).
        """
        # Clean up container if this was a containerized agent
        if session.container_id and self._container_mgr is not None:
            handle = self._container_mgr.get_handle(session.id)
            if handle is not None:
                self._container_mgr.destroy(handle)
            logger.info("Agent %s container destroyed", session.id)

        proc = self._procs.pop(session.id, None)
        if proc is not None:
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
            if not skip_merge:
                merge_result = self._merge_worktree_branch(session.id)
                # Push merged work to remote so nothing is lost
                if merge_result and merge_result.success:
                    from bernstein.core.git_ops import safe_push

                    push_result = safe_push(self._workdir, "main")
                    if push_result.ok:
                        logger.info("Pushed merged work from %s to origin/main", session.id)
                    else:
                        logger.warning("Push failed after merge for %s: %s", session.id, push_result.stderr)
            self._worktree_mgr.cleanup(session.id)

        outcome = "completed" if session.status != "dead" else "timed_out"
        get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
        return merge_result

    def get_worktree_path(self, session_id: str) -> Path | None:
        """Return the worktree path for *session_id*, or None if not registered.

        Args:
            session_id: The session whose worktree path to look up.

        Returns:
            Path to the agent's git worktree, or None if not using worktrees or
            the session was already reaped.
        """
        return self._worktree_paths.get(session_id)

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
        # Architect/security always get opus/max — they need deep reasoning
        if task.role in ("architect", "security"):
            return ModelConfig(model="opus", effort="max")
        # Large-scope tasks always get opus/max — they fail at lower tiers
        if task.scope == Scope.LARGE:
            return ModelConfig(model="opus", effort="max")
        # High-complexity tasks get opus/high minimum
        if task.complexity == Complexity.HIGH:
            return ModelConfig(model="opus", effort="high")
        if task.priority == 1:
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
