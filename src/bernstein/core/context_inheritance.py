"""Inject Bernstein task context into Claude Code subagent configuration.

When Claude Code spawns its own subagents (via the Agent tool), those
subagents need Bernstein's task context and file ownership rules.  This
module writes configuration to CLAUDE.md and .claude/settings.local.json
in the worktree so subagents inherit the parent agent's constraints.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def build_subagent_context(
    *,
    session_id: str,
    role: str,
    task_ids: list[str],
    owned_files: list[str],
    server_url: str = "http://127.0.0.1:8052",
) -> str:
    """Build context instructions that subagents should inherit.

    Args:
        session_id: Parent agent's session identifier.
        role: Parent agent's role.
        task_ids: IDs of tasks assigned to the parent agent.
        owned_files: File paths the parent agent is allowed to modify.
        server_url: Bernstein task server URL.

    Returns:
        Markdown-formatted context block for subagent CLAUDE.md.
    """
    lines: list[str] = [
        "## Bernstein orchestration context (inherited)",
        "",
        f"You are a subagent spawned by **{role}** agent `{session_id}`.",
        f"Task server: `{server_url}`",
        "",
    ]

    if task_ids:
        lines.append("### Parent task IDs")
        for tid in task_ids:
            lines.append(f"- `{tid}`")
        lines.append("")

    if owned_files:
        lines.append("### File ownership rules")
        lines.append("Only modify files within the parent agent's scope:")
        for fp in sorted(set(owned_files)):
            lines.append(f"- `{fp}`")
        lines.append("")
        lines.append(
            "Do NOT create or modify files outside these paths. "
            "If you need to touch other files, report back to the parent agent."
        )
        lines.append("")

    lines.extend(
        [
            "### Coordination rules",
            "- Do NOT call the task server to complete or fail tasks. Only the parent agent manages task lifecycle.",
            "- Report your findings and changes back to the parent agent.",
            "- Follow the same git safety rules as the parent (no force-push, no --no-verify).",
            "",
        ]
    )

    return "\n".join(lines)


def inject_subagent_config(
    worktree_path: Path,
    *,
    session_id: str,
    role: str,
    task_ids: list[str],
    owned_files: list[str],
    server_url: str = "http://127.0.0.1:8052",
) -> None:
    """Write subagent context to the worktree's .claude/settings.local.json.

    Configures Claude Code's subagent behaviour by:
    1. Setting environment context in settings.local.json
    2. Appending inherited context to the worktree's CLAUDE.md

    Args:
        worktree_path: Root of the agent's git worktree.
        session_id: Parent agent's session identifier.
        role: Parent agent's role.
        task_ids: IDs of tasks assigned to the parent agent.
        owned_files: File paths the parent agent owns.
        server_url: Bernstein task server URL.
    """
    # Build context text
    context_text = build_subagent_context(
        session_id=session_id,
        role=role,
        task_ids=task_ids,
        owned_files=owned_files,
        server_url=server_url,
    )

    # Append to CLAUDE.md so subagents pick up context automatically
    claude_md_path = worktree_path / "CLAUDE.md"
    try:
        existing = ""
        if claude_md_path.exists():
            existing = claude_md_path.read_text(encoding="utf-8")

        # Only append if not already present (idempotent)
        marker = "## Bernstein orchestration context (inherited)"
        if marker not in existing:
            separator = "\n\n" if existing else ""
            claude_md_path.write_text(
                existing + separator + context_text,
                encoding="utf-8",
            )
            logger.info("Injected subagent context into %s", claude_md_path)
    except OSError as exc:
        logger.warning("Failed to inject subagent context into CLAUDE.md: %s", exc)

    # Write file ownership rules to settings.local.json so Claude Code
    # can enforce them on subagent tool usage
    _update_settings_json(
        worktree_path,
        session_id=session_id,
        role=role,
        task_ids=task_ids,
        owned_files=owned_files,
    )


def _update_settings_json(
    worktree_path: Path,
    *,
    session_id: str,
    role: str,
    task_ids: list[str],
    owned_files: list[str],
) -> None:
    """Merge Bernstein context into .claude/settings.local.json.

    Preserves existing settings (hooks, permissions, etc.) while adding
    Bernstein orchestration metadata.

    Args:
        worktree_path: Root of the agent's git worktree.
        session_id: Parent agent's session identifier.
        role: Parent agent's role.
        task_ids: Task IDs for the parent agent.
        owned_files: Files the parent agent owns.
    """
    settings_dir = worktree_path / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.local.json"

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                existing = cast("dict[str, Any]", raw)
        except (json.JSONDecodeError, OSError):
            pass

    # Add Bernstein orchestration context as a custom section.
    # Claude Code ignores unknown keys but they're preserved for
    # debugging and for Bernstein's own hooks to read.
    existing["bernstein_context"] = {
        "parent_session_id": session_id,
        "parent_role": role,
        "task_ids": task_ids,
        "owned_files": owned_files,
    }

    try:
        settings_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        logger.debug("Updated settings.local.json with Bernstein context at %s", settings_path)
    except OSError as exc:
        logger.warning("Failed to write settings.local.json: %s", exc)
