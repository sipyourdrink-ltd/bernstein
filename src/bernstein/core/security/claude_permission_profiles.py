"""CLAUDE-010: Permission profile injection (allowedTools, denyPatterns per task).

Builds Claude Code permission profiles (allowedTools, disallowedTools,
denyPatterns) based on agent role, task type, and project policy.
These profiles are injected into the agent's settings.json to enforce
least-privilege tool access.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

_PATTERN_KEY = "*.key"

_PATTERN_ENV = "*.env"

_PATTERN_PEM = "*.pem"

_PATTERN_SECRETS = "**/secrets/**"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    """A tool permission profile for a Claude Code agent.

    Attributes:
        role: Agent role this profile applies to.
        allowed_tools: Explicit list of allowed tool names.
            Empty means all tools are allowed (subject to denials).
        disallowed_tools: Explicit list of denied tool names.
        deny_patterns: File path glob patterns the agent must not access.
        allow_patterns: File path glob patterns the agent may access.
        description: Human-readable description of the profile.
    """

    role: str
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    deny_patterns: tuple[str, ...] = ()
    allow_patterns: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        result: dict[str, Any] = {"role": self.role}
        if self.allowed_tools:
            result["allowedTools"] = list(self.allowed_tools)
        if self.disallowed_tools:
            result["disallowedTools"] = list(self.disallowed_tools)
        if self.deny_patterns:
            result["denyPatterns"] = list(self.deny_patterns)
        if self.allow_patterns:
            result["allowPatterns"] = list(self.allow_patterns)
        if self.description:
            result["description"] = self.description
        return result

    def to_settings_json(self) -> dict[str, Any]:
        """Convert to Claude Code settings.json format.

        Returns:
            Dict suitable for merging into .claude/settings.json.
        """
        settings: dict[str, Any] = {}
        if self.allowed_tools:
            settings["allowedTools"] = list(self.allowed_tools)
        if self.disallowed_tools:
            settings["disallowedTools"] = list(self.disallowed_tools)
        return settings


# ---------------------------------------------------------------------------
# Built-in profiles per role
# ---------------------------------------------------------------------------

_PROFILES: dict[str, PermissionProfile] = {
    "backend": PermissionProfile(
        role="backend",
        allowed_tools=("Bash", "Read", "Write", "Edit", "Grep", "Glob", "Agent"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY, _PATTERN_SECRETS),
        description="Full code editing with secret file protection.",
    ),
    "frontend": PermissionProfile(
        role="frontend",
        allowed_tools=("Bash", "Read", "Write", "Edit", "Grep", "Glob"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY, "**/backend/**", _PATTERN_SECRETS),
        description="Frontend development with backend isolation.",
    ),
    "qa": PermissionProfile(
        role="qa",
        allowed_tools=("Bash", "Read", "Grep", "Glob", "Agent"),
        disallowed_tools=("Write", "Edit"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY),
        description="Read-only code access for testing and review.",
    ),
    "security": PermissionProfile(
        role="security",
        allowed_tools=("Bash", "Read", "Grep", "Glob"),
        disallowed_tools=("Write", "Edit"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY),
        description="Read-only access for security auditing.",
    ),
    "docs": PermissionProfile(
        role="docs",
        allowed_tools=("Read", "Write", "Edit", "Grep", "Glob"),
        allow_patterns=("*.md", "*.rst", "*.txt", "docs/**"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY, _PATTERN_SECRETS),
        description="Documentation files only.",
    ),
    "reviewer": PermissionProfile(
        role="reviewer",
        allowed_tools=("Bash", "Read", "Grep", "Glob"),
        disallowed_tools=("Write", "Edit"),
        description="Read-only access for code review.",
    ),
    "devops": PermissionProfile(
        role="devops",
        allowed_tools=("Bash", "Read", "Write", "Edit", "Grep", "Glob"),
        allow_patterns=("Dockerfile*", "*.yaml", "*.yml", "*.toml", ".github/**", "scripts/**"),
        deny_patterns=(_PATTERN_ENV, _PATTERN_PEM, _PATTERN_KEY),
        description="Infrastructure and CI/CD configuration.",
    ),
}


@dataclass
class PermissionProfileManager:
    """Manages permission profiles for Claude Code agents.

    Builds and injects permission profiles based on role and task.

    Attributes:
        profiles: Registry of per-role permission profiles.
        overrides: Per-task permission overrides.
    """

    profiles: dict[str, PermissionProfile] = field(default_factory=lambda: dict(_PROFILES))
    overrides: dict[str, PermissionProfile] = field(default_factory=dict[str, PermissionProfile])

    def get_profile(self, role: str) -> PermissionProfile:
        """Get the permission profile for a role.

        Falls back to a permissive default if no profile is registered.

        Args:
            role: Agent role name.

        Returns:
            PermissionProfile for the role.
        """
        # Check overrides first.
        if role in self.overrides:
            return self.overrides[role]
        return self.profiles.get(
            role,
            PermissionProfile(role=role, description="Default permissive profile."),
        )

    def set_override(self, role: str, profile: PermissionProfile) -> None:
        """Set a per-task permission override.

        Args:
            role: Role to override.
            profile: Custom permission profile.
        """
        self.overrides[role] = profile

    def clear_overrides(self) -> None:
        """Clear all per-task overrides."""
        self.overrides.clear()

    def build_settings(self, role: str, workdir: Path | None = None) -> dict[str, Any]:
        """Build Claude Code settings dict for a role.

        Args:
            role: Agent role name.
            workdir: Project directory (for writing settings.json).

        Returns:
            Dict suitable for .claude/settings.json.
        """
        profile = self.get_profile(role)
        return profile.to_settings_json()

    def inject_settings(self, role: str, workdir: Path) -> Path:
        """Write permission settings to the agent's working directory.

        Creates or updates .claude/settings.json in the workdir with
        the role's permission profile.

        Args:
            role: Agent role name.
            workdir: Agent's working directory.

        Returns:
            Path to the written settings.json file.
        """
        settings = self.build_settings(role, workdir)
        settings_dir = workdir / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.json"

        # Merge with existing settings if present.
        existing: dict[str, Any] = {}
        if settings_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                existing = json.loads(settings_path.read_text(encoding="utf-8"))

        existing.update(settings)
        settings_path.write_text(
            json.dumps(existing, indent=2),
            encoding="utf-8",
        )

        logger.info(
            "Injected permission profile for role '%s' at %s",
            role,
            settings_path,
        )
        return settings_path

    def available_roles(self) -> list[str]:
        """Return all roles with registered profiles.

        Returns:
            Sorted list of role names.
        """
        return sorted(set(self.profiles) | set(self.overrides))
