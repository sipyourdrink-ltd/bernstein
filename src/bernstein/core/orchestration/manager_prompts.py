"""Manager prompt templates and rendering.

Loads template files from templates/prompts/ and renders them with
context data for the LLM planning, review, and queue review tasks.

Supports versioned prompts via the PromptRegistry when ``.sdd/prompts/``
contains versioned variants.  Falls back to static templates.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


def _load_template(
    templates_dir: Path,
    name: str,
    *,
    sdd_dir: Path | None = None,
    task_id: str = "",
) -> str:
    """Load a prompt template, preferring versioned prompts when available.

    If *sdd_dir* is provided and the prompt is registered in the
    PromptRegistry, the active (or A/B-selected) version is returned.
    Otherwise falls back to the static file in ``templates/prompts/``.

    Args:
        templates_dir: Root templates/ directory (parent of prompts/).
        name: Template filename (e.g. 'plan.md').
        sdd_dir: Path to ``.sdd/`` directory for versioned lookup.
        task_id: Task ID for deterministic A/B assignment.

    Returns:
        Template content as a string.

    Raises:
        FileNotFoundError: If the template does not exist anywhere.
    """
    # Try versioned prompt first
    if sdd_dir is not None:
        try:
            from bernstein.core.prompt_versioning import PromptRegistry

            registry = PromptRegistry(sdd_dir)
            stem = name.removesuffix(".md")
            version = registry.select_version(stem, task_id=task_id)
            if version is not None:
                pv = registry.get_version(stem, version)
                if pv and pv.content:
                    logger.debug("Using versioned prompt %r v%d", stem, version)
                    return pv.content
        except Exception:
            logger.debug("Versioned prompt lookup failed for %r, falling back", name)

    # Fallback: static template
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
    *,
    sdd_dir: Path | None = None,
    task_id: str = "",
) -> str:
    """Build the full planning prompt from the template.

    Args:
        goal: The high-level objective to decompose.
        context: Project context string from ``gather_project_context``.
        roles: Available specialist role names.
        existing_tasks: Tasks already in the server.
        templates_dir: Root templates/ directory.
        sdd_dir: Path to ``.sdd/`` for versioned prompt lookup.
        task_id: Task ID for deterministic A/B assignment.

    Returns:
        Fully rendered prompt ready for the LLM.
    """
    template = _load_template(templates_dir, "plan.md", sdd_dir=sdd_dir, task_id=task_id)
    return (
        template.replace("{{GOAL}}", goal)
        .replace("{{CONTEXT}}", context)
        .replace("{{AVAILABLE_ROLES}}", _format_roles(roles))
        .replace("{{EXISTING_TASKS}}", _format_existing_tasks(existing_tasks))
    )


def render_queue_review_prompt(
    completed_count: int,
    failed_count: int,
    open_tasks: list[Task],
    claimed_tasks: list[Task],
    failed_tasks: list[Task],
    server_url: str,
) -> str:
    """Build the queue review prompt for the manager.

    The manager sees the current task queue and can issue corrections:
    reassign, cancel, change_priority, or add_task.

    Args:
        completed_count: Total tasks completed since last review.
        failed_count: Total tasks failed since last review.
        open_tasks: Tasks waiting to be claimed.
        claimed_tasks: Tasks currently being worked on.
        failed_tasks: Tasks that failed (recent).
        _server_url: Task server URL (part of interface).

    Returns:
        Rendered prompt string.
    """

    _ = server_url  # Part of interface; not included in prompt text

    def _fmt_task(t: Task) -> str:
        age = ""
        agent = f" claimed by {t.assigned_agent}" if t.assigned_agent else ""
        return f'  - [{t.role}] [{t.id}] "{t.title}" — {t.status.value}{agent}{age}'

    lines: list[str] = [
        f"{completed_count} task(s) completed, {failed_count} failed since last review.",
        "",
        "## Current queue",
    ]
    if open_tasks:
        lines.append("### Open (waiting):")
        lines.extend(_fmt_task(t) for t in open_tasks)
    if claimed_tasks:
        lines.append("### In progress:")
        lines.extend(_fmt_task(t) for t in claimed_tasks)
    if failed_tasks:
        lines.append("### Recently failed:")
        lines.extend(_fmt_task(t) for t in failed_tasks[:5])

    lines += [
        "",
        "## Your job",
        "Review the queue for problems: wrong role assignments, stalled agents, tasks that are "
        "too large, or missing work. Issue ONLY corrections that are clearly needed.",
        "",
        "Respond with a JSON object — no markdown, no preamble:",
        "{",
        '  "reasoning": "<one sentence overall assessment>",',
        '  "corrections": [',
        "    // reassign a mis-routed task:",
        '    {"action": "reassign", "task_id": "<id>", "new_role": "<role>", "reason": "..."},',
        "    // cancel a stalled or pointless task:",
        '    {"action": "cancel", "task_id": "<id>", "reason": "..."},',
        "    // change priority (1=critical, 2=normal, 3=nice-to-have):",
        '    {"action": "change_priority", "task_id": "<id>", "new_priority": 1, "reason": "..."},',
        "    // inject a missing task:",
        '    {"action": "add_task", "title": "...", "role": "...", '
        '"description": "...", "priority": 2, "reason": "..."}',
        "  ]",
        "}",
        "",
        "Rules:",
        "- Empty corrections list is fine — only add what is genuinely needed.",
        "- Never cancel an in-progress task unless it has been stuck for >5 minutes.",
        "- Only reassign tasks that are clearly in the wrong role bucket.",
        "- Max 500 tokens total output.",
    ]
    return "\n".join(lines)


def render_review_prompt(
    task: Task,
    context: str,
    templates_dir: Path,
    *,
    sdd_dir: Path | None = None,
) -> str:
    """Build the review prompt from the template.

    Args:
        task: Completed task to review.
        context: Project context string.
        templates_dir: Root templates/ directory.
        sdd_dir: Path to ``.sdd/`` for versioned prompt lookup.

    Returns:
        Fully rendered review prompt.
    """
    template = _load_template(templates_dir, "review.md", sdd_dir=sdd_dir, task_id=task.id)

    signals_str = "(none)"
    if task.completion_signals:
        signals_str = "\n".join(f"- {s.type}: `{s.value}`" for s in task.completion_signals)

    return (
        template.replace("{{TASK_TITLE}}", task.title)
        .replace("{{TASK_ROLE}}", task.role)
        .replace("{{TASK_DESCRIPTION}}", task.description)
        .replace("{{COMPLETION_SIGNALS}}", signals_str)
        .replace("{{RESULT_SUMMARY}}", task.result_summary or "(no summary)")
        .replace("{{CONTEXT}}", context)
    )
