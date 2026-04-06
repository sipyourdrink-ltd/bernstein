"""Inject Bernstein task context into Claude Code subagent configuration.

When Claude Code spawns its own subagents (via the Agent tool), those
subagents need Bernstein's task context and file ownership rules.  This
module writes configuration to CLAUDE.md and .claude/settings.local.json
in the worktree so subagents inherit the parent agent's constraints.

Extended for AGENT-012: full parent context inheritance including role
constraints, environment variables, and resource limits.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inherited context dataclass (AGENT-012)
# ---------------------------------------------------------------------------


@dataclass
class InheritedContext:
    """Full context inherited by a child agent from its parent.

    Attributes:
        parent_session_id: Parent agent's session identifier.
        parent_role: Parent agent's role.
        task_ids: Task IDs the parent is working on.
        owned_files: Files the parent is allowed to modify.
        role_constraints: Role-specific constraints (e.g. read-only for QA).
        environment: Environment variables to propagate to children.
        server_url: Bernstein task server URL.
        max_depth: Maximum nesting depth for sub-agent delegation.
        current_depth: Current depth in the delegation tree.
    """

    parent_session_id: str
    parent_role: str
    task_ids: list[str] = field(default_factory=list[str])
    owned_files: list[str] = field(default_factory=list[str])
    role_constraints: dict[str, Any] = field(default_factory=dict[str, Any])
    environment: dict[str, str] = field(default_factory=dict[str, str])
    server_url: str = "http://127.0.0.1:8052"
    max_depth: int = 3
    current_depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict.

        Returns:
            Serialized context.
        """
        return {
            "parent_session_id": self.parent_session_id,
            "parent_role": self.parent_role,
            "task_ids": self.task_ids,
            "owned_files": self.owned_files,
            "role_constraints": self.role_constraints,
            "environment": self.environment,
            "server_url": self.server_url,
            "max_depth": self.max_depth,
            "current_depth": self.current_depth,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InheritedContext:
        """Deserialize from a dict.

        Args:
            data: Dict with context fields.

        Returns:
            Parsed InheritedContext.
        """
        return cls(
            parent_session_id=str(data.get("parent_session_id", "")),
            parent_role=str(data.get("parent_role", "")),
            task_ids=list(data.get("task_ids", [])),
            owned_files=list(data.get("owned_files", [])),
            role_constraints=dict(data.get("role_constraints", {})),
            environment=dict(data.get("environment", {})),
            server_url=str(data.get("server_url", "http://127.0.0.1:8052")),
            max_depth=int(data.get("max_depth", 3)),
            current_depth=int(data.get("current_depth", 0)),
        )

    def can_delegate(self) -> bool:
        """Check if further delegation (sub-spawning) is allowed.

        Returns:
            True if current depth is below the max depth.
        """
        return self.current_depth < self.max_depth

    def child_context(self, child_session_id: str, child_role: str) -> InheritedContext:
        """Create an inherited context for a child agent.

        The child inherits the parent's file ownership and constraints,
        with incremented depth.

        Args:
            child_session_id: Session ID for the child agent.
            child_role: Role assigned to the child agent.

        Returns:
            InheritedContext for the child agent.
        """
        return InheritedContext(
            parent_session_id=child_session_id,
            parent_role=child_role,
            task_ids=list(self.task_ids),
            owned_files=list(self.owned_files),
            role_constraints=dict(self.role_constraints),
            environment=dict(self.environment),
            server_url=self.server_url,
            max_depth=self.max_depth,
            current_depth=self.current_depth + 1,
        )


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
