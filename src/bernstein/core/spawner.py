"""Spawn short-lived CLI agents for task batches."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from bernstein.adapters.base import RateLimitError, SpawnError, SpawnResult
from bernstein.adapters.registry import get_adapter
from bernstein.adapters.skills_injector import inject_skills
from bernstein.agents.registry import AgentRegistry, get_registry
from bernstein.bridges.base import AgentState, BridgeError, RuntimeBridge, SpawnRequest
from bernstein.core.container import ContainerConfig, ContainerError, ContainerManager
from bernstein.core.context import TaskContextBuilder
from bernstein.core.context_recommendations import RecommendationEngine
from bernstein.core.git_ops import MergeResult, merge_with_conflict_detection
from bernstein.core.heartbeat import HeartbeatMonitor
from bernstein.core.in_process_agent import InProcessAgent
from bernstein.core.lessons import gather_lessons_for_context
from bernstein.core.lifecycle import transition_agent
from bernstein.core.models import (
    AbortReason,
    AgentBackend,
    AgentSession,
    IsolationMode,
    ModelConfig,
    Task,
    TransitionReason,
)
from bernstein.core.orchestrator import ShutdownInProgress
from bernstein.core.prometheus import agent_spawn_duration, merge_duration
from bernstein.core.router import ProviderHealthStatus, RouterError, TierAwareRouter
from bernstein.core.sandbox import DockerSandbox, spawn_in_sandbox
from bernstein.core.team_state import TeamStateStore
from bernstein.core.traces import AgentTrace, TraceStore, finalize_trace, new_trace
from bernstein.core.worktree import WorktreeError, WorktreeManager, WorktreeSetupConfig
from bernstein.plugins.manager import get_plugin_manager
from bernstein.templates.renderer import TemplateError, render_role_prompt

if TYPE_CHECKING:
    import subprocess
    import threading

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


def _render_auth_section(token_path: Path) -> str:
    """Return authentication instructions to inject into every agent's prompt.

    The token file path is referenced by path rather than embedding the raw
    token so that credentials do not appear in prompt logs.

    Args:
        token_path: Path to the session-scoped JWT token file (mode 0600).

    Returns:
        Markdown block instructing the agent to authenticate all requests.
    """
    return (
        "\n## Task Server Authentication\n"
        "Your agent token is stored at (do NOT print or log its contents):\n"
        f"```\n{token_path}\n```\n"
        "Include this header in **all** task server requests:\n"
        "```bash\n"
        f'-H "Authorization: Bearer $(cat {token_path})"\n'
        "```\n"
        "Example — marking a task complete:\n"
        "```bash\n"
        f"curl -s -X POST http://127.0.0.1:8052/tasks/<TASK_ID>/complete \\\n"
        f'  -H "Authorization: Bearer $(cat {token_path})" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"result_summary": "Done"}\'\n'
        "```\n"
    )


def _health_check_interval(tasks: list[Task]) -> int:
    """Derive health-check cron interval (minutes) from task batch duration.

    Maps estimated_minutes to a polling frequency:

    - ``< 15`` min (simple tasks): check every **3** minutes
    - ``> 60`` min (complex tasks): check every **10** minutes
    - Otherwise: check every **5** minutes

    Args:
        tasks: Batch of tasks assigned to the agent.

    Returns:
        Cron interval in minutes.
    """
    if not tasks:
        return 5
    max_est = max((t.estimated_minutes for t in tasks), default=30)
    if max_est > 60:
        return 10
    if max_est < 15:
        return 3
    return 5


def _inject_scheduled_tasks(
    workdir: Path,
    session_id: str,
    health_interval_minutes: int = 5,
) -> None:
    """Write ``.claude/scheduled_tasks.json`` with a recurring health-check cron task.

    Claude Code's scheduled-task system fires the cron prompt on the given
    interval inside a running agent session.  This enables agent-internal
    monitoring: the agent self-evaluates its progress and reports via MCP
    rather than the orchestrator guessing from external heartbeat signals.

    The cron task survives context compaction — Claude Code re-fires it even
    after the context window is compressed.

    Args:
        workdir: Working directory for the agent (worktree root).
        session_id: Agent session identifier (used as the cron task ID prefix).
        health_interval_minutes: Cron interval in minutes (1-59).
    """
    tasks_path = workdir / ".claude" / "scheduled_tasks.json"
    tasks_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "tasks": [
            {
                "id": f"hc-{session_id[:8]}",
                "cron": f"*/{health_interval_minutes} * * * *",
                "prompt": (
                    "Self-check: Are you making progress on your assigned tasks? "
                    "If stuck for >2 minutes, use the bernstein MCP tool to report your status. "
                    "If token budget is >80% consumed, commit your work and wrap up."
                ),
                "createdAt": int(time.time() * 1000),
                "recurring": True,
            }
        ]
    }
    try:
        tasks_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        logger.debug(
            "Injected scheduled health-check task (interval=%dm) → %s",
            health_interval_minutes,
            tasks_path,
        )
    except OSError as exc:
        logger.debug("Failed to write scheduled_tasks.json for %s: %s", session_id, exc)


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


def _render_batch_prompt(task: Task) -> str:
    """Build a /batch prompt for homogeneous large-scale refactors.

    When a task declares ``execution_mode: batch``, Bernstein spawns a single
    Claude Code agent with this prompt.  Claude Code's built-in ``/batch``
    skill handles decomposition into 5-30 independent units, spawns worktree
    subagents in parallel, runs tests and opens a PR per unit, and tracks
    progress internally.  This is far more efficient than Bernstein spawning
    N separate agents for mechanical changes (renames, migrations, API updates).

    The outer agent needs ``--max-turns 200`` (set by the caller) to cover the
    full research → decompose → spawn → track lifecycle.

    Args:
        task: The batch-mode task to delegate.

    Returns:
        Prompt string starting with ``/batch`` that triggers the batch skill.
    """
    lines: list[str] = [f"/batch {task.description}"]
    if task.owned_files:
        lines.append(f"\nAffected paths: {', '.join(task.owned_files)}")
    lines.append(f"\nTask ID for completion reporting: {task.id}")
    lines.append(
        "\nAfter all batch units are complete, run:\n"
        f"curl -sS -X POST http://127.0.0.1:8052/tasks/{task.id}/complete "
        f'-H "Content-Type: application/json" '
        f'-d \'{{"result_summary": "Batch complete: {task.title}"}}\''
    )
    return "\n".join(lines)


def _render_prompt(
    tasks: list[Task],
    templates_dir: Path,
    workdir: Path,
    agency_catalog: dict[str, AgencyAgent] | None = None,
    spawner_config: Any | None = None,
    catalog_system_prompt: str | None = None,
    context_builder: TaskContextBuilder | None = None,
    session_id: str = "",
    bulletin_summary: str = "",
    task_graph: TaskGraph | None = None,
    token_budget: int = 0,
    meta_messages: list[str] | None = None,
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
        spawner_config: Optional spawner config used for prompt-side limits.
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
    # agents must retry on transient connection errors (--retry-connrefused).
    # Do NOT use --retry-all-errors: it retries 4xx (e.g. 409 Conflict),
    # causing infinite loops when task state has changed.
    completion_cmds = "\n".join(
        f"curl -s -w '\\n%{{http_code}}' --retry 3 --retry-delay 2 --retry-connrefused "
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
        f"**Important:** Only retry on connection refused / network errors. "
        f"If the server returns HTTP 409 or any other 4xx error, do NOT retry — "
        f"the task state has changed and retrying will not help. Just exit.\n\n"
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

    # Inject prior agent lessons based on task tags (legacy JSONL system)
    sdd_dir = workdir / ".sdd"
    lesson_tags = _extract_tags_from_tasks(tasks)
    lesson_context = gather_lessons_for_context(sdd_dir, lesson_tags)

    # Inject persistent memory from SQLite store (new system)
    persistent_memory_context = ""
    db_path = sdd_dir / "memory" / "memory.db"
    if db_path.exists():
        try:
            from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

            store = SQLiteMemoryStore(db_path)
            memories = store.get_relevant(lesson_tags, limit=10)
            if memories:
                lines = ["## Persistent Memory\nRelevant conventions and architectural decisions:"]
                for m in memories:
                    lines.append(f"- [{m.type.upper()}] {m.content}")
                persistent_memory_context = "\n".join(lines) + "\n"
        except Exception as mem_exc:
            logger.debug("Failed to fetch persistent memory: %s", mem_exc)

    # Smart context injection (RAG)
    smart_context = ""
    try:
        from bernstein.core.rag import CodebaseIndexer

        indexer = CodebaseIndexer(workdir)
        if indexer.file_count() > 0:
            query = " ".join(t.title for t in tasks)
            # Cache the query key so that identical query sets reuse the same RAG results
            from bernstein.core.section_dedup import deduplicate_section

            # Build RAG results
            # Find top N relevant files
            rag_cfg = getattr(spawner_config, "rag", None)
            max_files = rag_cfg.max_files if rag_cfg else 5
            max_chars = (rag_cfg.max_tokens if rag_cfg else 50000) * 4  # heuristic: 4 chars per token

            results = indexer.search(query, limit=max_files)
            if results:
                lines = ["## Relevant Code Context\nAutomatically identified relevant files via RAG:"]
                total_chars = 0
                for res in results:
                    if total_chars >= max_chars:
                        break
                    path = Path(res["path"])
                    if path.exists():
                        content = path.read_text(encoding="utf-8", errors="replace")
                        # Truncate if this file alone exceeds remaining budget
                        remaining = max_chars - total_chars
                        if len(content) > remaining:
                            content = content[:remaining] + "\n... (truncated)"

                        lines.append(f"### {res['path']} (score: {res['score']:.2f})\n```\n{content}\n```")
                        total_chars += len(content)
                smart_context = deduplicate_section("\n".join(lines) + "\n")
    except Exception as rag_exc:
        logger.debug("Smart context injection failed: %s", rag_exc)

    # File-scope context activation: inject role-specific context based on owned files.
    # Matches task owned_files against .gitignore-style patterns at claim time.
    file_scope_context = ""
    try:
        from bernstein.core.context_activation import activate_context_for_task

        all_owned: list[str] = []
        for t in tasks:
            all_owned.extend(t.owned_files)
        file_scope_context = activate_context_for_task(all_owned)
    except Exception as exc:
        logger.debug("File-scope context activation failed: %s", exc)

    # Assemble final prompt
    from bernstein.core.section_dedup import deduplicate_section

    sections = [role_prompt]
    if specialist_block:
        sections.append(specialist_block)
    sections.append(f"\n## Assigned tasks\n{task_block}")
    if lesson_context:
        sections.append(f"\n{lesson_context}\n")
    if persistent_memory_context:
        sections.append(deduplicate_section(f"\n{persistent_memory_context}\n"))
    if smart_context:
        sections.append(f"\n{smart_context}\n")
    if rich_context:
        sections.append(f"\n{rich_context}\n")
    if file_scope_context:
        sections.append(deduplicate_section(f"\n## File-scope context\n{file_scope_context}\n"))
    predecessor_ctx = _render_predecessor_context(tasks, task_graph)
    if predecessor_ctx:
        sections.append(predecessor_ctx)
    if bulletin_summary:
        sections.append(
            deduplicate_section(
                f"\n## Team awareness\n"
                f"Other agents are working in parallel. Recent activity:\n{bulletin_summary}\n\n"
                f"If you need to create a shared utility, check if it already exists first.\n"
                f"If you define an API endpoint, use consistent naming with existing endpoints.\n"
            )
        )
    try:
        rec_engine = RecommendationEngine(workdir)
        rec_engine.build()
        rec_section = rec_engine.render_for_prompt(role, max_chars=2000)
        if rec_section:
            sections.append(f"\n{rec_section}\n")
    except Exception as exc:
        logger.debug("Recommendation rendering failed: %s", exc)
    if project_context:
        sections.append(deduplicate_section(f"\n## Project context\n{project_context}\n"))
    if token_budget > 0:
        if token_budget >= 1_000_000:
            budget_hint = f"~{token_budget // 1_000_000}M"
        elif token_budget >= 1_000:
            budget_hint = f"~{token_budget // 1_000}K"
        else:
            budget_hint = str(token_budget)
        sections.append(
            deduplicate_section(
                f"\n## Token budget\n"
                f"You have {budget_hint} tokens for this task. Plan your work accordingly — "
                f"focus on the task, avoid unnecessary exploration, and wrap up promptly.\n"
            )
        )
    sections.append(deduplicate_section(f"\n## Instructions\n{instructions}\n"))
    if session_id:
        try:
            heartbeat_instructions = HeartbeatMonitor(workdir).inject_heartbeat_instructions(session_id)
            sections.append(
                deduplicate_section(
                    "\n## Heartbeat (background)\n"
                    "Run this in the background to report progress:\n"
                    f"```bash\n{heartbeat_instructions}\n```\n"
                )
            )
        except Exception as exc:
            logger.debug("Heartbeat instructions unavailable: %s", exc)
    if session_id:
        sections.append(deduplicate_section(_render_signal_check(session_id)))

    if meta_messages:
        nudges_block = "\n## Operational nudges\n" + "\n".join(f"- {m}" for m in meta_messages) + "\n"
        sections.append(nudges_block)

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
        sandbox: DockerSandbox | None = None,
        max_tokens_per_task: dict[str, int] | None = None,
        role_model_policy: dict[str, dict[str, str]] | None = None,
        runtime_bridge: RuntimeBridge | None = None,
        backend: AgentBackend = AgentBackend.SUBPROCESS,
    ) -> None:
        self._enable_caching = enable_caching
        self._adapter_cache: dict[str, CLIAdapter] = {}
        if enable_caching:
            from bernstein.adapters.caching_adapter import CachingAdapter

            adapter = CachingAdapter(adapter, workdir)
        self._adapter = adapter
        self._adapter_cache[self._adapter.name()] = self._adapter
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
        self._max_tokens_per_task = max_tokens_per_task or {}
        self._role_model_policy = role_model_policy or {}
        self._workspace = workspace
        self._bulletin = bulletin
        self._context_builder = TaskContextBuilder(workdir)
        self._procs: dict[str, subprocess.Popen[bytes] | None] = {}
        self._shutdown_event: threading.Event | None = None
        self._agent_failure_timestamps: dict[str, float] = {}  # adapter_name -> last failure ts
        self._use_worktrees = use_worktrees
        self._worktree_setup_config = worktree_setup_config
        self._worktree_mgr: WorktreeManager | None = None
        self._worktree_managers: dict[Path, WorktreeManager] = {}
        if use_worktrees:
            self._worktree_mgr = WorktreeManager(workdir, setup_config=worktree_setup_config)
            self._worktree_managers[workdir.resolve()] = self._worktree_mgr
            # Clean stale worktrees from prior crashed/stopped runs
            cleaned = self._worktree_mgr.cleanup_all_stale()
            if cleaned:
                logger.info("Cleaned %d stale worktree(s) from prior run", cleaned)
        self._worktree_paths: dict[str, Path] = {}
        self._worktree_roots: dict[str, Path] = {}
        self._traces: dict[str, AgentTrace] = {}
        self._trace_store = TraceStore(workdir / ".sdd" / "traces")
        self._runtime_bridge = runtime_bridge
        self._sandbox = sandbox if sandbox is not None and sandbox.enabled else None
        self._sandbox_managers: dict[str, ContainerManager] = {}
        # Container isolation
        self._container_mgr: ContainerManager | None = None
        if container_config is not None:
            try:
                self._container_mgr = ContainerManager(container_config, workdir)
            except ContainerError as exc:
                logger.warning("Container runtime unavailable, falling back to subprocess: %s", exc)

        # Backend selection
        self._backend = backend
        self._in_process: InProcessAgent | None = None
        if backend == AgentBackend.IN_PROCESS:
            pid_dir = workdir / ".sdd" / "runtime" / "pids"
            self._in_process = InProcessAgent(adapter, workdir, pid_dir=pid_dir)
            logger.info("In-process agent backend enabled (wrapping %s)", adapter.name())

        # Zero-trust: lazy agent identity store — loaded on first use.
        # Stored as a cached property so the auth directory is not created
        # until the first agent is spawned.
        self._identity_store_instance: Any = None
        # Map session_id → token file path for cleanup on reap.
        self._agent_token_files: dict[str, Path] = {}

    @property
    def _identity_store(self) -> Any:
        """Return the AgentIdentityStore, creating it on first access."""
        if self._identity_store_instance is None:
            from bernstein.core.agent_identity import AgentIdentityStore

            auth_dir = self._workdir / ".sdd" / "auth"
            self._identity_store_instance = AgentIdentityStore(auth_dir)
        return self._identity_store_instance

    def _issue_agent_token(self, session_id: str, role: str, task_ids: list[str]) -> Path:
        """Issue a short-lived task-scoped JWT and write it to a 0600 token file.

        The token file path is recorded in ``_agent_token_files`` for cleanup
        when the agent is reaped.

        Args:
            session_id: The agent session ID (used as identity ID).
            role: The agent's role.
            task_ids: Task IDs the agent is authorised to act on.

        Returns:
            Path to the written token file.
        """
        import os

        _, raw_token = self._identity_store.create_identity(
            session_id,
            role,
            task_ids=task_ids,
            metadata={"source": "spawner"},
        )

        tokens_dir = self._workdir / ".sdd" / "runtime" / "agent_tokens"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        token_path = tokens_dir / f"{session_id}.token"

        fd = os.open(str(token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, raw_token.encode("utf-8"))
        finally:
            os.close(fd)

        self._agent_token_files[session_id] = token_path
        logger.info("Issued zero-trust token for session %s (tasks=%s)", session_id, task_ids or "unrestricted")
        return token_path

    def _revoke_agent_token(self, session_id: str) -> None:
        """Revoke the agent identity and delete the token file on reap.

        Args:
            session_id: The agent session ID whose token should be revoked.
        """
        try:
            self._identity_store.revoke(session_id, reason="agent reaped", actor="spawner")
        except Exception as exc:
            logger.debug("Could not revoke identity %s: %s", session_id, exc)

        token_path = self._agent_token_files.pop(session_id, None)
        if token_path is not None and token_path.exists():
            try:
                token_path.unlink()
            except OSError as exc:
                logger.debug("Could not delete token file %s: %s", token_path, exc)

    def set_shutdown_event(self, shutdown_event: threading.Event | None) -> None:
        """Attach the orchestrator shutdown event for spawn/worktree guards."""
        self._shutdown_event = shutdown_event
        for manager in self._worktree_managers.values():
            manager.set_shutdown_event(shutdown_event)

    def _worktree_manager_for_repo(self, repo_root: Path) -> WorktreeManager | None:
        if not self._use_worktrees:
            return None
        normalized = repo_root.resolve()
        existing = self._worktree_managers.get(normalized)
        if existing is not None:
            return existing
        manager = WorktreeManager(normalized, setup_config=self._worktree_setup_config)
        manager.set_shutdown_event(self._shutdown_event)
        self._worktree_managers[normalized] = manager
        return manager

    def _infer_adapter_name_for_provider(self, provider_name: str | None, model: str) -> str:
        """Infer adapter name from provider/model identifiers."""
        text = f"{provider_name or ''} {model}".lower()
        if "gemini" in text or "google" in text:
            return "gemini"
        if "codex" in text or "openai" in text or "gpt" in text:
            return "codex"
        if "qwen" in text:
            return "qwen"
        if "claude" in text or "anthropic" in text:
            return "claude"
        return self._adapter.name()

    def _get_adapter_by_name(self, adapter_name: str) -> CLIAdapter:
        """Return cached adapter instance, creating one when needed."""
        cached = self._adapter_cache.get(adapter_name)
        if cached is not None:
            return cached

        adapter = get_adapter(adapter_name)
        if self._enable_caching:
            from bernstein.adapters.caching_adapter import CachingAdapter

            adapter = CachingAdapter(adapter, self._workdir)
        self._adapter_cache[adapter_name] = adapter
        return adapter

    def _run_bridge_call(self, awaitable: Any) -> Any:
        """Run a bridge coroutine from the sync orchestration path."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, awaitable).result()

    def _spawn_via_runtime_bridge(
        self,
        *,
        session: AgentSession,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        preferred_log_path: Path,
    ) -> bool:
        """Attempt to spawn via the configured runtime bridge.

        Returns:
            True when the remote run was accepted and ``session`` was populated.

        Raises:
            BridgeError: If the bridge rejects the spawn before acceptance.
        """
        if self._runtime_bridge is None:
            return False
        bridge_status = self._run_bridge_call(
            self._runtime_bridge.spawn(
                SpawnRequest(
                    agent_id=session.id,
                    image="openclaw-agent",
                    command=[],
                    prompt=prompt,
                    workdir=str(spawn_cwd),
                    timeout_seconds=session.timeout_s or 1800,
                    log_path=str(preferred_log_path),
                    role=session.role,
                    model=model_config.model,
                    effort=model_config.effort,
                    labels={"session_id": session.id},
                )
            )
        )
        if not isinstance(bridge_status, object):
            return False
        session.runtime_backend = self._runtime_bridge.name()
        session.pid = None
        session.log_path = str(preferred_log_path)
        session.provider = session.provider or self._runtime_bridge.name()
        session.bridge_session_key = bridge_status.metadata.get("session_key") or None
        session.bridge_run_id = bridge_status.metadata.get("run_id") or None
        transition_agent(session, "working", actor="spawner", reason="remote bridge run accepted")
        return True

    def _bridge_status(self, session: AgentSession) -> Any:
        """Fetch the latest remote runtime status for a bridge-backed session."""
        if self._runtime_bridge is None:
            raise BridgeError("No runtime bridge configured", agent_id=session.id)
        return self._run_bridge_call(self._runtime_bridge.status(session.id))

    def _bridge_cancel(self, session: AgentSession) -> None:
        """Best-effort cancellation for a bridge-backed session."""
        if self._runtime_bridge is None:
            raise BridgeError("No runtime bridge configured", agent_id=session.id)
        self._run_bridge_call(self._runtime_bridge.cancel(session.id))

    def spawn_for_tasks(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
        """Route, render prompt, and spawn an agent for a task batch."""
        from bernstein.core.telemetry import start_span

        if not tasks:
            raise ValueError("Cannot spawn agent with empty task list")

        with start_span(
            "agent.spawn",
            attributes={
                "role": tasks[0].role,
                "task_count": len(tasks),
                "model_override": model_override,
            },
        ):
            return self._spawn_for_tasks_internal(tasks, model_override=model_override)

    def _spawn_for_tasks_internal(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
        """Actual spawn implementation."""
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            raise ShutdownInProgress("Orchestrator shutting down — refusing new spawn")

        # Disk space check: refuse to spawn if less than 1 GB free.
        # Worktree creation + agent output can consume significant disk.
        try:
            usage = shutil.disk_usage(self._workdir)
            free_gb = usage.free / (1024**3)
            if free_gb < 1.0:
                logger.error("Disk space critical: %.1f GB free, skipping spawn", free_gb)
                raise SpawnError(f"Disk space critical: {free_gb:.1f} GB free (need >= 1 GB)")
        except OSError as exc:
            logger.warning("Could not check disk space: %s", exc)

        # 5min cooldown check
        now = time.time()
        adapter_name = self._adapter.name()
        last_fail = self._agent_failure_timestamps.get(adapter_name, 0.0)
        if now - last_fail < 300:
            logger.info(
                "Agent %s in cooldown (%.1fs remaining) — skipping spawn",
                adapter_name,
                300 - (now - last_fail),
            )
            raise SpawnError(f"Agent {adapter_name} is in cooldown after recent failure")

        if not tasks:
            raise ValueError("Cannot spawn agent with empty task list")

        roles = {t.role for t in tasks}
        if len(roles) > 1:
            raise ValueError(f"All tasks in a batch must share the same role, got: {roles}")

        # Route based on highest-complexity task in batch; use TierAwareRouter if available.
        # Effectiveness data is fed into the bandit as priors (via _select_batch_config →
        # route_task) so both learning systems share data rather than competing.
        metrics_dir = self._workdir / ".sdd" / "metrics"
        base_config = _select_batch_config(
            tasks,
            templates_dir=self._templates_dir,
            metrics_dir=metrics_dir if metrics_dir.exists() else None,
            workdir=self._workdir,
        )
        if model_override:
            base_config = ModelConfig(
                model=model_override,
                effort=base_config.effort,
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )
        model_config = base_config
        provider_name: str | None = None
        role_policy = self._role_model_policy.get(tasks[0].role, {})
        preferred_provider = role_policy.get("provider")

        if not tasks[0].model and role_policy.get("model"):
            model_config = ModelConfig(
                model=role_policy["model"],
                effort=role_policy.get("effort", base_config.effort),
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )
        elif not tasks[0].effort and role_policy.get("effort"):
            model_config = ModelConfig(
                model=base_config.model,
                effort=role_policy["effort"],
                max_tokens=base_config.max_tokens,
                is_batch=base_config.is_batch,
            )

        if self._router is not None and self._router.state.providers:
            try:
                decision = self._router.select_provider_for_task(
                    tasks[0],
                    base_config=model_config,
                    preferred_provider=preferred_provider,
                )
                model_config = decision.model_config
                provider_name = decision.provider
            except RouterError as exc:
                if preferred_provider:
                    logger.warning(
                        "Role policy provider override for role=%s could not be honored (%s); "
                        "falling back to normal routing",
                        tasks[0].role,
                        exc,
                    )
                    try:
                        decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                        model_config = decision.model_config
                        provider_name = decision.provider
                    except RouterError as fallback_exc:
                        logger.warning("Router failed to select provider, using fallback: %s", fallback_exc)
                else:
                    logger.warning("Router failed to select provider, using fallback: %s", exc)
        elif preferred_provider:
            provider_name = preferred_provider

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

        # Compute per-task token budget from scope (use highest scope in batch)
        _scope_order = {"small": 0, "medium": 1, "large": 2}
        max_scope = max((t.scope.value for t in tasks), key=lambda s: _scope_order.get(s, 1))
        task_token_budget = self._max_tokens_per_task.get(max_scope, 0)

        # Batch execution mode: single task delegates to Claude Code /batch skill.
        # The outer agent handles decomposition, parallel subagent spawning, and
        # PR-per-unit creation internally, so Bernstein only needs one process.
        is_batch_mode = any(t.execution_mode == "batch" for t in tasks)

        # Render prompt (catalog system_prompt replaces role template when matched)
        bulletin_summary = self._bulletin.summary() if self._bulletin is not None else ""
        meta_messages = list(tasks[0].meta_messages)
        if is_batch_mode:
            # Use the first batch task as the primary task for the /batch prompt.
            # Multi-task batches with mode=batch are unusual but we handle them by
            # using the first task's goal as the primary directive.
            prompt = _render_batch_prompt(tasks[0])
            logger.info(
                "Batch execution mode: spawning single agent with /batch prompt for task %s",
                tasks[0].id,
            )
        else:
            prompt = _render_prompt(
                tasks,
                self._templates_dir,
                self._workdir,
                self._agency_catalog,
                spawner_config=getattr(self, "_config", None),
                catalog_system_prompt=catalog_system_prompt,
                context_builder=self._context_builder,
                session_id=session_id,
                bulletin_summary=bulletin_summary,
                token_budget=task_token_budget,
                meta_messages=meta_messages,
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
            token_budget=task_token_budget,
            meta_messages=meta_messages,
        )

        # Zero-trust: issue a short-lived, task-scoped JWT for this agent.
        # The token is written to a 0600 file and its path is injected into
        # the prompt so the agent can include it in task server requests.
        # We wrap in try/except so auth failures never block spawning.
        try:
            task_ids_for_scope = [t.id for t in tasks]
            _token_path = self._issue_agent_token(session_id, role, task_ids_for_scope)
            prompt = prompt + _render_auth_section(_token_path)
        except Exception as _token_exc:
            logger.warning("Zero-trust token issuance failed for %s: %s", session_id, _token_exc)

        # Prompt size pre-check (AGENT-003): estimate token count and reject or
        # truncate before spending a worktree + adapter spawn on an oversized prompt.
        from bernstein.core.prompt_precheck import PromptAction, check_prompt_size, truncate_prompt

        _precheck = check_prompt_size(prompt, model=model_config.model)
        if _precheck.action == PromptAction.REJECT:
            logger.error("Prompt too large for session %s: %s", session_id, _precheck.message)
            raise SpawnError(f"Prompt size pre-check failed: {_precheck.message}")
        elif _precheck.action == PromptAction.TRUNCATE:
            logger.warning(
                "Prompt exceeds 80%% of context window for session %s; truncating. %s",
                session_id,
                _precheck.message,
            )
            prompt = truncate_prompt(prompt, _precheck.safe_char_limit)

        # Determine working directory: repo-specific > worktree > shared workdir
        spawn_cwd = self._workdir
        worktree_repo_root = self._workdir.resolve()

        # If the task targets a specific repo in a multi-repo workspace,
        # use that repo's path as the working directory.
        task_repo = tasks[0].repo
        if task_repo is not None and self._workspace is not None:
            try:
                spawn_cwd = self._workspace.resolve_repo(task_repo)
                worktree_repo_root = spawn_cwd.resolve()
                logger.info("Task targets repo '%s', spawn cwd: %s", task_repo, spawn_cwd)
            except KeyError:
                logger.warning(
                    "Task repo '%s' not found in workspace, falling back to workdir",
                    task_repo,
                )

        worktree_mgr = self._worktree_manager_for_repo(worktree_repo_root)
        if self._use_worktrees and worktree_mgr is not None:
            try:
                spawn_cwd = worktree_mgr.create(session_id)
                self._worktree_paths[session_id] = spawn_cwd
                self._worktree_roots[session_id] = worktree_repo_root
            except WorktreeError as exc:
                logger.warning(
                    "Cannot create workspace for agent %s. "
                    "Reason: %s. "
                    "Fix: run 'bernstein stop' then restart, or delete .sdd/worktrees/ manually",
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

        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        preferred_log_path = log_dir / f"{session_id}.log"

        # Inject role-specific skills into the worktree before spawn so the
        # agent picks up orchestration protocol and role-specific instructions.
        # Skills survive context compaction and reduce prompt boilerplate.
        inject_skills(
            workdir=spawn_cwd,
            role=role,
            tasks=tasks,
            session_id=session_id,
            templates_dir=self._templates_dir,
        )
        _inject_scheduled_tasks(
            workdir=spawn_cwd,
            session_id=session_id,
            health_interval_minutes=_health_check_interval(tasks),
        )

        remote_spawned = False
        if self._runtime_bridge is not None:
            try:
                remote_spawned = self._spawn_via_runtime_bridge(
                    session=session,
                    prompt=prompt,
                    spawn_cwd=spawn_cwd,
                    model_config=model_config,
                    preferred_log_path=preferred_log_path,
                )
            except BridgeError as exc:
                fallback_allowed = bool(self._runtime_bridge.config.extra.get("fallback_to_local", True))
                if not fallback_allowed:
                    raise SpawnError(f"OpenClaw bridge rejected spawn for {session_id}: {exc}") from exc
                logger.warning(
                    "OpenClaw bridge failed before acceptance for %s, falling back to local adapter: %s",
                    session_id,
                    exc,
                )

        # Spawn via adapter with runtime provider/adapter failover.
        # This is critical for real-world rate-limit handling where a chosen
        # provider may fail at process-start time.
        #
        # In unattended mode, wrap the spawn with persistent retry
        # (exponential backoff + heartbeats) for rate-limit errors.
        from bernstein.core.rate_limit_tracker import (
            UnattendedRetryPolicy,
            is_unattended_mode,
        )

        _unattended_policy: UnattendedRetryPolicy | None = None
        if is_unattended_mode():
            _unattended_policy = UnattendedRetryPolicy()
            logger.info("Unattended mode: retry rate-limit errors with backoff")

        _unattended_max = _unattended_policy.max_retries if _unattended_policy is not None else 1
        _unattended_attempt = 0
        result: SpawnResult | None = None

        while True:
            # Remote spawn already succeeded — skip the local adapter loop entirely
            if remote_spawned:
                break
            if not remote_spawned:
                attempt_errors: list[str] = []
                disabled_providers: dict[str, bool] = {}
                attempted: set[tuple[str | None, str, str]] = set()
                max_attempts = max(1, len(self._router.state.providers) if self._router is not None else 1) + 2
                while len(attempted) < max_attempts:
                    adapter_name = self._infer_adapter_name_for_provider(provider_name, model_config.model)
                    attempt_key = (provider_name, adapter_name, model_config.model)
                    if attempt_key in attempted:
                        break
                    attempted.add(attempt_key)

                    try:
                        target_adapter = self._get_adapter_by_name(adapter_name)
                    except Exception as exc:
                        attempt_errors.append(f"{adapter_name}: {exc}")
                        break

                    try:
                        spawn_start = time.perf_counter()
                        if self._in_process is not None and self._backend == AgentBackend.IN_PROCESS:
                            # In-process: run the adapter's subprocess via
                            # a thread inside the current Python process
                            fake_pid, actual_log_path = self._in_process.run(
                                prompt=prompt,
                                workdir=spawn_cwd,
                                model_config=model_config,
                                session_id=session_id,
                                mcp_config=effective_mcp,
                            )
                            result = SpawnResult(pid=fake_pid, log_path=actual_log_path)
                        elif self._sandbox is not None:
                            result = self._spawn_in_sandbox(
                                session_id=session_id,
                                prompt=prompt,
                                spawn_cwd=spawn_cwd,
                                model_config=model_config,
                                mcp_config=effective_mcp,
                                session=session,
                                adapter=target_adapter,
                            )
                        elif self._container_mgr is not None:
                            result = self._spawn_in_container(
                                session_id=session_id,
                                prompt=prompt,
                                spawn_cwd=spawn_cwd,
                                model_config=model_config,
                                mcp_config=effective_mcp,
                                session=session,
                                adapter=target_adapter,
                            )
                        else:
                            result = target_adapter.spawn(
                                prompt=prompt,
                                workdir=spawn_cwd,
                                model_config=model_config,
                                session_id=session_id,
                                mcp_config=effective_mcp,
                            )
                        spawn_duration = time.perf_counter() - spawn_start
                        agent_spawn_duration.labels(adapter=provider_name or adapter_name).observe(spawn_duration)
                        session.provider = (
                            provider_name
                            if provider_name is not None
                            else (adapter_name if (self._router and self._router.state.providers) else None)
                        )
                        session.model_config = model_config
                        break
                    except RateLimitError as exc:
                        attempt_errors.append(f"{adapter_name}: {exc}")
                        logger.warning(
                            "Rate-limit detected for provider=%s adapter=%s; retrying with alternate provider",
                            provider_name or adapter_name,
                            adapter_name,
                        )
                        if self._router is None or provider_name is None:
                            continue
                        provider_cfg = self._router.state.providers.get(provider_name)
                        if provider_cfg is not None:
                            provider_cfg.health.status = ProviderHealthStatus.RATE_LIMITED
                            if provider_name not in disabled_providers:
                                disabled_providers[provider_name] = provider_cfg.available
                            provider_cfg.available = False
                        try:
                            decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                            provider_name = decision.provider
                            model_config = decision.model_config
                        except RouterError:
                            provider_name = None
                    except (SpawnError, Exception) as exc:
                        attempt_errors.append(f"{adapter_name}: {exc}")

                        # Check for auth error (T499)
                        is_auth_error = False
                        log_path = spawn_cwd / ".sdd" / "logs" / f"{session_id}.log"
                        if (
                            log_path.exists()
                            and self._rate_limit_tracker is not None
                            and self._rate_limit_tracker.scan_log_for_auth_error(log_path)
                        ):
                            logger.warning(
                                "Auth error detected for provider=%s adapter=%s",
                                provider_name or adapter_name,
                                adapter_name,
                            )
                            is_auth_error = True

                        if is_auth_error and target_adapter.supports_auth_refresh():
                            refresh_key = (provider_name, "auth_refresh")
                            if refresh_key not in attempted:
                                attempted.add(refresh_key)
                                logger.info("Attempting auth refresh for %s", adapter_name)
                                if target_adapter.refresh_auth(spawn_cwd):
                                    # Re-try same provider once after refresh
                                    attempted.remove(attempt_key)
                                    continue

                        logger.warning(
                            "Agent spawn failed (session=%s provider=%s adapter=%s): %s",
                            session_id,
                            provider_name,
                            adapter_name,
                            exc,
                        )
                        if self._router is None or provider_name is None:
                            continue
                        provider_cfg = self._router.state.providers.get(provider_name)
                        if provider_cfg is not None:
                            self._router.update_provider_health(provider_name, success=False)
                            if provider_name not in disabled_providers:
                                disabled_providers[provider_name] = provider_cfg.available
                            provider_cfg.available = False
                        try:
                            decision = self._router.select_provider_for_task(tasks[0], base_config=model_config)
                            provider_name = decision.provider
                            model_config = decision.model_config
                        except RouterError:
                            provider_name = None

                for prov, was_available in disabled_providers.items():
                    provider_cfg = self._router.state.providers.get(prov) if self._router is not None else None
                    if provider_cfg is not None:
                        provider_cfg.available = was_available

                if result is None:
                    error_text = "; ".join(attempt_errors) or "no viable spawn attempts"
                    if _unattended_policy is not None:
                        _unattended_attempt += 1
                        if _unattended_attempt < _unattended_max:
                            delay = _unattended_policy.next_delay(_unattended_attempt)
                            signals_dir = spawn_cwd / ".sdd" / "runtime" / "signals"
                            logger.warning(
                                "Unattended retry: cycle %d/%d, sleeping %.0fs",
                                _unattended_attempt,
                                _unattended_max,
                                delay,
                            )
                            _unattended_policy.wait_with_heartbeats(
                                session_id,
                                _unattended_attempt,
                                f"429 rate limit ({error_text})",
                                signals_dir=signals_dir,
                            )
                            # Reset provider availability for the retry
                            if self._router is not None:
                                for _p, _was_available in disabled_providers.items():
                                    _pcfg = self._router.state.providers.get(_p)
                                    if _pcfg is not None:
                                        _pcfg.available = _was_available
                            # Re-select provider for the retry
                            if self._router is not None and self._router.state.providers:
                                try:
                                    _decision = self._router.select_provider_for_task(
                                        tasks[0], base_config=model_config
                                    )
                                    provider_name = _decision.provider
                                    model_config = _decision.model_config
                                except RouterError:
                                    pass
                            continue
                    raise RuntimeError(f"All spawn attempts failed for session {session_id}: {error_text}")
                # Success — exit the retry loop
                break

        # Post-spawn session setup
        if result is not None:
            session.pid = result.pid
            session.abort_reason = result.abort_reason
            session.abort_detail = result.abort_detail
            session.finish_reason = result.finish_reason
            if result.log_path:
                session.log_path = str(result.log_path)

            # Touch heartbeat file immediately so the agent starts with a
            # fresh timestamp — prevents idle recycling from killing agents
            # that take a long time before emitting their first stream-json
            # event (e.g. Claude Code thinking for 2+ minutes).
            try:
                hb_dir = self._workdir / ".sdd" / "runtime" / "heartbeats"
                hb_dir.mkdir(parents=True, exist_ok=True)
                (hb_dir / session_id).touch()
            except OSError:
                pass

        if session.status != "working":
            transition_agent(
                session,
                "working",
                actor="spawner",
                reason="agent process started",
            )
        if result is not None and result.proc is not None:
            self._procs[session_id] = result.proc  # type: ignore[assignment]
            # Register stdin pipe for real-time IPC (if available)
            proc_stdin = getattr(result.proc, "stdin", None)
            if proc_stdin is not None:
                from bernstein.core.agent_ipc import register_stdin_pipe

                register_stdin_pipe(session_id, proc_stdin)

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
            log_path=session.log_path,
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
            workdir=self._workdir,
        )
        role = tasks[0].role
        session_id = f"{role}-resume-{uuid.uuid4().hex[:8]}"

        meta_messages = ["This is a crash recovery session. Continue from where the previous agent left off."]
        prompt = _render_prompt(
            tasks,
            self._templates_dir,
            self._workdir,
            self._agency_catalog,
            spawner_config=getattr(self, "_config", None),
            context_builder=self._context_builder,
            session_id=session_id,
            meta_messages=meta_messages,
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
        session.abort_reason = result.abort_reason
        session.abort_detail = result.abort_detail
        session.finish_reason = result.finish_reason

        # Touch heartbeat on resume spawn (same rationale as main spawn path)
        try:
            hb_dir = self._workdir / ".sdd" / "runtime" / "heartbeats"
            hb_dir.mkdir(parents=True, exist_ok=True)
            (hb_dir / session_id).touch()
        except OSError:
            pass

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
        adapter: CLIAdapter,
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

        adapter_name = adapter.name().lower()
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
                    adapter=adapter,
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
            return adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
            )

    def _spawn_in_sandbox(
        self,
        *,
        session_id: str,
        prompt: str,
        spawn_cwd: Path,
        model_config: ModelConfig,
        mcp_config: dict[str, Any] | None,
        session: AgentSession,
        adapter: CLIAdapter,
    ) -> SpawnResult:
        """Spawn an agent in a per-session Docker or Podman sandbox.

        Args:
            session_id: Agent session identifier.
            prompt: Rendered system prompt.
            spawn_cwd: Worktree or workspace path mounted into the sandbox.
            model_config: Model and effort configuration.
            mcp_config: Optional MCP configuration for the adapter.
            session: Mutable session record to update.
            adapter: Adapter selected for this spawn attempt.

        Returns:
            Spawn result for the sandboxed process.
        """
        assert self._sandbox is not None

        from bernstein.adapters.env_isolation import build_filtered_env

        adapter_name = adapter.name().lower()
        extra_keys: list[str] = []
        if "claude" in adapter_name:
            extra_keys.append("ANTHROPIC_API_KEY")
        elif "gemini" in adapter_name:
            extra_keys.extend(["GOOGLE_API_KEY", "GEMINI_API_KEY"])
        elif "codex" in adapter_name:
            extra_keys.append("OPENAI_API_KEY")
        sandbox_env = build_filtered_env(extra_keys)

        prompt_file = spawn_cwd / ".sdd" / "runtime" / "prompts" / f"{session_id}.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")

        log_dir = spawn_cwd / ".sdd" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{session_id}.log"

        try:
            manager, handle = spawn_in_sandbox(
                sandbox=self._sandbox,
                session_id=session_id,
                adapter_name=adapter_name,
                cmd=self._adapter_cmd_for_container(
                    prompt_file=prompt_file,
                    model_config=model_config,
                    session_id=session_id,
                    mcp_config=mcp_config,
                    adapter=adapter,
                ),
                env=sandbox_env,
                workdir=spawn_cwd,
                log_path=log_path,
            )
        except ContainerError as exc:
            logger.warning(
                "Sandbox runtime unavailable for %s, falling back to worktree isolation: %s",
                session_id,
                exc,
            )
            session.isolation = IsolationMode.WORKTREE.value if self._use_worktrees else IsolationMode.NONE.value
            return adapter.spawn(
                prompt=prompt,
                workdir=spawn_cwd,
                model_config=model_config,
                session_id=session_id,
                mcp_config=mcp_config,
            )

        self._sandbox_managers[session_id] = manager
        session.container_id = handle.container_id
        session.isolation = IsolationMode.CONTAINER.value
        return SpawnResult(pid=handle.pid or 0, log_path=log_path)

    def _adapter_cmd_for_container(
        self,
        *,
        prompt_file: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None,
        adapter: CLIAdapter,
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
        adapter_name = adapter.name().lower()
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

    def _container_manager_for_session(self, session_id: str) -> ContainerManager | None:
        """Return the container manager responsible for a session."""
        return self._sandbox_managers.get(session_id, self._container_mgr)

    def _check_alive_openclaw(self, session: AgentSession) -> bool:
        """Check liveness for an OpenClaw remote-bridge session."""
        try:
            bridge_status = self._bridge_status(session)
        except BridgeError as exc:
            logger.warning("OpenClaw status check failed for %s, treating as still alive: %s", session.id, exc)
            return True
        session.exit_code = bridge_status.exit_code
        session.bridge_session_key = bridge_status.metadata.get("session_key") or session.bridge_session_key
        session.bridge_run_id = bridge_status.metadata.get("run_id") or session.bridge_run_id
        return bridge_status.state in {AgentState.PENDING, AgentState.RUNNING}

    def _check_alive_container(self, session: AgentSession) -> bool | None:
        """Check liveness via container manager. Returns None if not container-based."""
        container_mgr = self._container_manager_for_session(session.id)
        if not (session.container_id and container_mgr is not None):
            return None
        handle = container_mgr.get_handle(session.id)
        if handle is None:
            return False
        alive = container_mgr.is_alive(handle)
        if not alive:
            session.exit_code = container_mgr.get_exit_code(handle)
        return alive

    def _check_alive_process(self, session: AgentSession) -> bool | None:
        """Check liveness via stored subprocess. Returns None if no proc stored."""
        proc = self._procs.get(session.id)
        if proc is None:
            return None
        exit_code = proc.poll()
        if exit_code is not None:
            session.exit_code = exit_code
            return False
        return True

    def _check_alive_in_process(self, session: AgentSession) -> bool | None:
        """Check liveness via InProcessAgent. Returns None if not applicable."""
        if self._in_process is None:
            return None
        alive = self._in_process.is_alive(session.id)
        if not alive:
            exit_code_val = self._in_process.wait(session.id, timeout=0.1)
            if exit_code_val is not None:
                session.exit_code = exit_code_val
        return alive

    def check_alive(self, session: AgentSession) -> bool:
        """Check if the agent process is still running.

        Args:
            session: Agent session to check.

        Returns:
            True if the process is alive, False otherwise.
        """
        if session.runtime_backend == "openclaw":
            return self._check_alive_openclaw(session)

        for checker in (self._check_alive_container, self._check_alive_process, self._check_alive_in_process):
            result = checker(session)
            if result is not None:
                return result

        if session.pid is None:
            return False
        return self._adapter.is_alive(session.pid)

    def kill(self, session: AgentSession) -> None:
        """Terminate the agent process and mark session dead.

        Args:
            session: Agent session to kill.
        """
        if session.runtime_backend == "openclaw":
            try:
                self._bridge_cancel(session)
            except BridgeError as exc:
                logger.warning("OpenClaw cancellation failed for %s: %s", session.id, exc)
            if session.status != "dead":
                transition_agent(
                    session,
                    "dead",
                    actor="spawner",
                    reason="remote bridge kill requested",
                    transition_reason=TransitionReason.ABORTED,
                    abort_reason=AbortReason.SHUTDOWN_SIGNAL,
                    abort_detail="remote runtime cancellation requested by orchestrator",
                    finish_reason="kill_requested",
                )
            try:
                TeamStateStore(self._workdir / ".sdd").on_kill(session.id)
            except Exception as _ts_exc:
                logger.debug("Team state on_kill failed: %s", _ts_exc)
            return

        # Container-based agents: stop and destroy the container
        container_mgr = self._container_manager_for_session(session.id)
        if session.container_id and container_mgr is not None:
            handle = container_mgr.get_handle(session.id)
            if handle is not None:
                container_mgr.destroy(handle)
            self._sandbox_managers.pop(session.id, None)
        elif self._in_process is not None and self._backend == AgentBackend.IN_PROCESS:
            self._in_process.stop(session.id)
            exit_code_val = self._in_process.wait(session.id, timeout=5.0)
            if exit_code_val is not None:
                session.exit_code = exit_code_val
            self._in_process.cleanup(session.id)
        elif session.pid is not None:
            self._adapter.kill(session.pid)
        if session.status != "dead":
            transition_agent(
                session,
                "dead",
                actor="spawner",
                reason="kill requested",
                transition_reason=TransitionReason.ABORTED,
                abort_reason=AbortReason.SHUTDOWN_SIGNAL,
                abort_detail="local process kill requested by orchestrator",
                finish_reason="kill_requested",
            )
        try:
            TeamStateStore(self._workdir / ".sdd").on_kill(session.id)
        except Exception as _ts_exc:
            logger.debug("Team state on_kill failed: %s", _ts_exc)

    def _reap_openclaw(self, session: AgentSession) -> None:
        """Sync logs from the remote bridge for an OpenClaw session."""
        if self._runtime_bridge is not None:
            try:
                self._run_bridge_call(self._runtime_bridge.logs(session.id))
            except BridgeError as exc:
                logger.warning("OpenClaw log sync failed for %s: %s", session.id, exc)
        logger.info("Agent %s remote bridge run finalized", session.id)

    def _reap_container(self, session: AgentSession) -> None:
        """Destroy the container for a containerized agent session."""
        container_mgr = self._container_manager_for_session(session.id)
        if session.container_id and container_mgr is not None:
            handle = container_mgr.get_handle(session.id)
            if handle is not None:
                container_mgr.destroy(handle)
            self._sandbox_managers.pop(session.id, None)
            logger.info("Agent %s container destroyed", session.id)

    def _reap_in_process(self, session: AgentSession) -> bool:
        """Wait on and clean up an in-process agent. Returns True if reaped."""
        if self._in_process is None or self._backend != AgentBackend.IN_PROCESS:
            return False
        exit_code_val = self._in_process.wait(session.id, timeout=5.0)
        if exit_code_val is not None:
            session.exit_code = exit_code_val
        self._in_process.cleanup(session.id)
        logger.info("Agent %s in-process agent cleaned up", session.id)
        return True

    def _reap_subprocess(self, session: AgentSession) -> None:
        """Terminate and wait on the OS subprocess."""
        proc = self._procs.pop(session.id, None)
        if proc is not None:
            try:
                proc.terminate()
            except Exception as exc:
                logger.warning("reap_completed_agent: terminate failed for %s: %s", session.id, exc)
            try:
                session.exit_code = proc.wait(timeout=5)
            except Exception as exc:
                logger.warning("reap_completed_agent: wait failed for %s: %s", session.id, exc)
        logger.info("Agent %s process reaped", session.id)

    def _merge_and_cleanup_worktree(
        self,
        session: AgentSession,
        skip_merge: bool,
    ) -> MergeResult | None:
        """Merge worktree branch back and clean up. Returns MergeResult if applicable."""
        worktree_path = self._worktree_paths.pop(session.id, None)
        worktree_root = self._worktree_roots.pop(session.id, self._workdir.resolve())
        worktree_mgr = self._worktree_managers.get(worktree_root)
        merge_result: MergeResult | None = None

        if worktree_path is not None and worktree_mgr is not None:
            if not skip_merge:
                merge_start = time.perf_counter()
                merge_result = self._merge_worktree_branch(session.id, repo_root=worktree_root)
                merge_duration.observe(time.perf_counter() - merge_start)

                from bernstein.core.metric_collector import get_collector

                merge_ok = merge_result is not None and merge_result.success
                for task_id in session.task_ids:
                    get_collector().record_merge_result(task_id, success=merge_ok)

                if merge_result and merge_result.success:
                    from bernstein.core.git_ops import safe_push

                    push_result = safe_push(worktree_root, "main")
                    if push_result.ok:
                        logger.info("Pushed merged work from %s to origin/main", session.id)
                    else:
                        logger.warning("Push failed after merge for %s: %s", session.id, push_result.stderr)
            worktree_mgr.cleanup(session.id)

        return merge_result

    def _finalize_trace(self, session: AgentSession) -> None:
        """Write the finalized trace for a reaped session."""
        trace = self._traces.pop(session.id, None)
        if trace is not None:
            outcome = "success" if session.status != "dead" else "unknown"
            finalize_trace(trace, outcome)
            try:
                self._trace_store.write(trace)
            except Exception as exc:
                logger.warning("Failed to write finalized trace for %s: %s", session.id, exc)

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

        For in-process agents, waits on the thread and cleans up the session
        but skips worktree merge (no worktree created for in-process agents).

        Args:
            session: The AgentSession whose underlying process should be reaped.
            skip_merge: When True, skip the worktree merge but still clean up
                the process and worktree.  Used by the approval gate when the
                user rejects a task or when a PR is created instead.

        Returns:
            MergeResult when worktrees are enabled and skip_merge is False
            (None otherwise, or if no proc was stored).
        """
        from bernstein.core.agent_ipc import unregister_stdin_pipe

        unregister_stdin_pipe(session.id)

        if session.runtime_backend == "openclaw":
            self._reap_openclaw(session)
        else:
            self._reap_container(session)

            if self._reap_in_process(session):
                self._worktree_paths.pop(session.id, None)
                self._worktree_roots.pop(session.id, None)
            else:
                self._reap_subprocess(session)
                merge_result = self._merge_and_cleanup_worktree(session, skip_merge)
                outcome = "completed" if session.status != "dead" else "timed_out"
                get_plugin_manager().fire_agent_reaped(session_id=session.id, role=session.role, outcome=outcome)
                return merge_result

        self._finalize_trace(session)
        merge_result = self._merge_and_cleanup_worktree(session, skip_merge)
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

    def cleanup_worktree(self, session_id: str) -> None:
        """Remove the worktree and branch for a dead agent session.

        Best-effort: removes the worktree directory, deletes the local branch,
        runs ``git worktree prune``, and pops session from internal dicts.
        Safe to call even if the worktree was never created or already cleaned.

        Args:
            session_id: The session whose worktree should be cleaned up.
        """
        worktree_root = self._worktree_roots.get(session_id, self._workdir.resolve())
        worktree_mgr = self._worktree_managers.get(worktree_root) or self._worktree_mgr
        if worktree_mgr is not None:
            worktree_mgr.cleanup(session_id)
        else:
            # No manager available — try manual removal of the directory
            worktree_path = self._worktree_paths.get(session_id)
            if worktree_path is not None and worktree_path.exists():
                import shutil

                try:
                    shutil.rmtree(worktree_path)
                except OSError as exc:
                    logger.warning("Manual worktree removal failed for %s: %s", session_id, exc)
        self._worktree_paths.pop(session_id, None)
        self._worktree_roots.pop(session_id, None)
        logger.info("Cleaned up worktree for dead agent %s", session_id)

    def prune_orphan_worktrees(self, active_session_ids: set[str]) -> int:
        """Remove orphan worktree directories that don't correspond to active sessions.

        Runs ``git worktree prune`` then scans ``.sdd/worktrees/`` for
        directories whose name is not in *active_session_ids* and removes
        them via :class:`WorktreeManager`.

        Args:
            active_session_ids: Session IDs that are currently alive/working.

        Returns:
            Number of orphan worktrees cleaned up.
        """
        cleaned = 0
        for mgr in self._worktree_managers.values():
            # Prune git's internal worktree bookkeeping first
            import subprocess

            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=mgr.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception as exc:
                logger.debug("git worktree prune failed: %s", exc)

            base_dir = mgr.repo_root / ".sdd" / "worktrees"
            if not base_dir.exists():
                continue
            for entry in base_dir.iterdir():
                if entry.is_dir() and entry.name != ".locks" and entry.name not in active_session_ids:
                    logger.info("Removing orphan worktree: %s", entry.name)
                    mgr.cleanup(entry.name)
                    # Also pop from spawner dicts in case they were tracked
                    self._worktree_paths.pop(entry.name, None)
                    self._worktree_roots.pop(entry.name, None)
                    cleaned += 1
        return cleaned

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

    def _merge_worktree_branch(self, session_id: str, repo_root: Path | None = None) -> MergeResult:
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
        merge_root = (repo_root or self._workdir).resolve()
        try:
            result = merge_with_conflict_detection(
                merge_root,
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
    workdir: Path | None = None,
) -> ModelConfig:
    """Pick the highest-tier model config across all tasks in a batch.

    If *templates_dir* is provided, reads the role's config.yaml first and
    uses that as the baseline before falling back to heuristic routing.
    If *metrics_dir* is provided, consults the epsilon-greedy bandit for
    non-high-stakes roles to dynamically pick the cheapest viable model.
    When *workdir* is also provided, effectiveness history seeds the bandit
    so both learning systems share data.
    Routes each task individually, then picks the most capable config
    so the agent can handle the hardest task in its batch.

    Args:
        tasks: Non-empty list of tasks.
        templates_dir: Optional path to templates/roles/ for config.yaml lookup.
        metrics_dir: Optional path to .sdd/metrics for bandit state.
        workdir: Optional project root for effectiveness scorer data.

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
            return ModelConfig(model="opus", effort="max")
        # Consult bandit (seeded with effectiveness data) for standard tasks
        return route_task(task, bandit_metrics_dir=metrics_dir, workdir=workdir)

    configs = [_route_for_batch(t) for t in tasks]
    # Sort by model tier (opus > sonnet > haiku) then effort (max > high > normal)
    model_rank = {"opus": 3, "sonnet": 2, "haiku": 1}
    effort_rank = {"max": 3, "high": 2, "normal": 1}
    return max(
        configs,
        key=lambda c: (model_rank.get(c.model, 0), effort_rank.get(c.effort, 0)),
    )


# ---------------------------------------------------------------------------
# Worker tool allowlist per spawn (T578)
# ---------------------------------------------------------------------------

_TOOL_ALLOWLIST_ENV_VAR = "BERNSTEIN_TOOL_ALLOWLIST"


def build_tool_allowlist_env(allowed_tools: list[str]) -> dict[str, str]:
    """Build environment variables encoding a tool allowlist for a spawned agent (T578).

    The allowlist is passed via the ``BERNSTEIN_TOOL_ALLOWLIST`` environment
    variable as a comma-separated list.  The worker wrapper reads this and
    enforces it before dispatching tool calls.

    Args:
        allowed_tools: List of tool names the agent is permitted to invoke.

    Returns:
        Dict of environment variables to merge into the spawn environment.
    """
    if not allowed_tools:
        return {}
    return {_TOOL_ALLOWLIST_ENV_VAR: ",".join(allowed_tools)}


def parse_tool_allowlist_env() -> list[str] | None:
    """Parse the tool allowlist from the current process environment (T578).

    Returns:
        List of allowed tool names, or None if no allowlist is configured.
    """
    import os

    raw = os.environ.get(_TOOL_ALLOWLIST_ENV_VAR, "").strip()
    if not raw:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]


def check_tool_allowed(tool_name: str, allowlist: list[str] | None) -> bool:
    """Return True if *tool_name* is permitted by *allowlist* (T578).

    Args:
        tool_name: Tool to check.
        allowlist: Permitted tools, or None to allow all.

    Returns:
        True if the tool is allowed.
    """
    if allowlist is None:
        return True
    return tool_name in allowlist
