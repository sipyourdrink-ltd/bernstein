"""Inject per-task Claude Code skills into the worktree before spawn.

Claude Code's skill system (``.claude/skills/*.md``) provides context-triggered
capabilities.  Skills have frontmatter declaring when they should activate and
markdown content with instructions.  The model loads relevant skills on-demand
based on conversation context, and re-injects them after context compaction.

Bernstein writes role-specific skills into the worktree's ``.claude/skills/``
directory before spawning an agent so that:

- Orchestration protocols (completion, signal-check) survive context compaction
- Prompt size is reduced by 30-40% — boilerplate moves to skills loaded only
  when relevant
- Skills compose cleanly: a backend agent automatically gets the test-runner
  skill, a commit skill, and orchestration protocol skills

Template substitution uses simple ``{{PLACEHOLDER}}`` tokens (no Jinja2
dependency) so skills can be rendered without external libraries.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import Task

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role → skill template mapping
# Always-injected (every role): completion protocol + signal check
# Role-specific: test runner for backend/qa, commit protocol for backend/docs
# ---------------------------------------------------------------------------

#: Skills always injected regardless of role.
_ALWAYS_INJECT: list[str] = [
    "bernstein-completion-protocol.md",
    "bernstein-signal-check.md",
]

#: Additional skills injected per role.
ROLE_SKILL_MAP: dict[str, list[str]] = {
    "backend": [
        "bernstein-test-runner.md",
        "bernstein-commit-protocol.md",
    ],
    "qa": [
        "bernstein-test-runner.md",
    ],
    "docs": [
        "bernstein-commit-protocol.md",
    ],
    "security": [],
}


def render_skill_template(
    content: str,
    *,
    session_id: str = "",
    tasks: list[Task] | None = None,
) -> str:
    """Render a skill template by substituting ``{{PLACEHOLDER}}`` tokens.

    Supported placeholders:

    - ``{{SESSION_ID}}``: agent session identifier
    - ``{{COMPLETE_CMDS}}``: curl commands to mark all tasks complete
    - ``{{TASK_IDS}}``: space-separated task ID list

    Args:
        content: Raw skill template content.
        session_id: Agent session identifier.
        tasks: Tasks assigned to this agent.  Used to generate completion commands.

    Returns:
        Rendered skill content with placeholders substituted.
    """
    task_list = tasks or []

    # Build per-task completion curl commands
    complete_cmds_parts: list[str] = []
    for task in task_list:
        cmd = (
            "```bash\n"
            f"curl -s --retry 3 -X POST http://127.0.0.1:8052/tasks/{task.id}/complete \\\n"
            '  -H "Content-Type: application/json" \\\n'
            f'  -d \'{{"result_summary": "Completed: {task.title}"}}\'\n'
            "```"
        )
        complete_cmds_parts.append(cmd)
    complete_cmds = (
        "\n\n".join(complete_cmds_parts)
        if complete_cmds_parts
        else ("```bash\n# No task IDs available — check with the orchestrator\n```")
    )

    task_ids = " ".join(t.id for t in task_list)

    result = content
    result = result.replace("{{SESSION_ID}}", session_id)
    result = result.replace("{{COMPLETE_CMDS}}", complete_cmds)
    result = result.replace("{{TASK_IDS}}", task_ids)
    return result


def inject_skills(
    workdir: Path,
    role: str,
    tasks: list[Task],
    session_id: str,
    templates_dir: Path,
) -> None:
    """Write role-specific Claude Code skills into the worktree.

    Copies skills from ``templates/skills/`` to ``workdir/.claude/skills/``,
    rendering ``{{PLACEHOLDER}}`` tokens with task-specific data.

    Always injects orchestration protocol skills (completion, signal-check).
    Additional skills are injected based on the role via :data:`ROLE_SKILL_MAP`.

    Args:
        workdir: Working directory for the agent (worktree root).
        role: Agent role (e.g. ``"backend"``, ``"qa"``, ``"security"``).
        tasks: Tasks assigned to the agent.
        session_id: Agent session identifier, embedded in signal-check paths.
        templates_dir: Path to ``templates/roles/`` directory.  Skills are
            resolved from the sibling ``../skills/`` directory.
    """
    skills_source_dir = templates_dir.parent / "skills"
    if not skills_source_dir.is_dir():
        _logger.debug(
            "Skills templates directory not found: %s — skipping injection",
            skills_source_dir,
        )
        return

    skills_dest_dir = workdir / ".claude" / "skills"
    skills_dest_dir.mkdir(parents=True, exist_ok=True)

    templates_to_inject = list(_ALWAYS_INJECT) + ROLE_SKILL_MAP.get(role, [])

    for template_name in templates_to_inject:
        source_path = skills_source_dir / template_name
        if not source_path.exists():
            _logger.debug("Skill template not found: %s — skipping", source_path)
            continue

        try:
            raw = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            _logger.debug("Failed to read skill template %s: %s", source_path, exc)
            continue

        rendered = render_skill_template(raw, session_id=session_id, tasks=tasks)

        dest_path = skills_dest_dir / template_name
        try:
            dest_path.write_text(rendered, encoding="utf-8")
            _logger.debug("Injected skill: %s → %s", template_name, dest_path)
        except OSError as exc:
            _logger.debug("Failed to write skill %s: %s", dest_path, exc)
