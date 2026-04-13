"""Composable sandbox capability profiles for agent execution.

Defines fine-grained security profiles that control what a sandboxed agent is
permitted to do: network endpoints, filesystem paths, environment variables,
and shell commands.  Profiles are frozen dataclasses that can be composed
(unioned) to build up the exact capability set an agent needs.

Built-in profiles cover common roles (web-backend, frontend, ci-runner,
minimal).  Custom profiles can be created and composed freely.

Usage::

    from bernstein.core.security.sandbox_profiles import (
        compose_profiles,
        get_builtin_profile,
        render_profile_summary,
        validate_profile,
    )

    backend = get_builtin_profile("web-backend")
    ci = get_builtin_profile("ci-runner")
    combined = compose_profiles(backend, ci)
    issues = validate_profile(combined)
    print(render_profile_summary(combined))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core rule types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkRule:
    """A single allowed network endpoint.

    Args:
        host: Hostname or IP address the agent may connect to.
        port: Port number.  Use ``0`` to allow all ports on *host*.
        protocol: Transport protocol.
    """

    host: str
    port: int
    protocol: Literal["tcp", "udp"] = "tcp"

    def __post_init__(self) -> None:
        if not self.host:
            msg = "NetworkRule.host must be a non-empty string"
            raise ValueError(msg)
        if self.port < 0 or self.port > 65535:
            msg = f"NetworkRule.port must be 0-65535, got {self.port}"
            raise ValueError(msg)


@dataclass(frozen=True)
class FileSystemRule:
    """A single filesystem access rule.

    Args:
        path: Absolute path (directory or file) the rule applies to.
        permissions: Access level granted.
    """

    path: str
    permissions: Literal["read", "write", "execute"] = "read"

    def __post_init__(self) -> None:
        if not self.path:
            msg = "FileSystemRule.path must be a non-empty string"
            raise ValueError(msg)


# ---------------------------------------------------------------------------
# Sandbox profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxProfile:
    """Immutable capability set describing what a sandboxed agent may access.

    All collection fields use tuples so the profile is fully hashable and
    immutable.

    Args:
        name: Human-readable profile identifier.
        network_rules: Allowed network endpoints.
        fs_rules: Allowed filesystem paths and their permissions.
        env_vars: Names of environment variables the agent may read.
        allowed_commands: Shell commands the agent may execute.
        description: Optional prose description of the profile's purpose.
    """

    name: str
    network_rules: tuple[NetworkRule, ...] = ()
    fs_rules: tuple[FileSystemRule, ...] = ()
    env_vars: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    description: str = ""


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def compose_profiles(*profiles: SandboxProfile) -> SandboxProfile:
    """Merge multiple profiles into a single profile via set-union.

    The resulting profile's name is the ``+``-joined names of the inputs.
    Rules, env vars, and commands are deduplicated while preserving stable
    ordering.

    Args:
        *profiles: One or more profiles to compose.

    Returns:
        A new ``SandboxProfile`` that is the union of all inputs.

    Raises:
        ValueError: If no profiles are provided.
    """
    if not profiles:
        msg = "compose_profiles requires at least one profile"
        raise ValueError(msg)

    if len(profiles) == 1:
        return profiles[0]

    name = " + ".join(p.name for p in profiles)
    description = " | ".join(p.description for p in profiles if p.description)

    # Use dict.fromkeys to deduplicate while preserving order.
    net: dict[NetworkRule, None] = {}
    fs: dict[FileSystemRule, None] = {}
    env: dict[str, None] = {}
    cmds: dict[str, None] = {}

    for p in profiles:
        for r in p.network_rules:
            net[r] = None
        for r in p.fs_rules:
            fs[r] = None
        for v in p.env_vars:
            env[v] = None
        for c in p.allowed_commands:
            cmds[c] = None

    return SandboxProfile(
        name=name,
        network_rules=tuple(net),
        fs_rules=tuple(fs),
        env_vars=tuple(env),
        allowed_commands=tuple(cmds),
        description=description,
    )


# ---------------------------------------------------------------------------
# Built-in profiles
# ---------------------------------------------------------------------------

_BUILTIN_PROFILES: dict[str, SandboxProfile] = {
    "web-backend": SandboxProfile(
        name="web-backend",
        description="Backend service with database and cache access.",
        network_rules=(
            NetworkRule(host="localhost", port=5432, protocol="tcp"),
            NetworkRule(host="localhost", port=6379, protocol="tcp"),
        ),
        fs_rules=(
            FileSystemRule(path="/app", permissions="read"),
            FileSystemRule(path="/app/src", permissions="write"),
            FileSystemRule(path="/tmp", permissions="write"),
        ),
        env_vars=("DATABASE_URL", "REDIS_URL", "SECRET_KEY"),
        allowed_commands=("python", "pip", "uv", "alembic"),
    ),
    "frontend": SandboxProfile(
        name="frontend",
        description="Frontend build tooling with no network access.",
        network_rules=(),
        fs_rules=(
            FileSystemRule(path="/app", permissions="read"),
            FileSystemRule(path="/app/src", permissions="write"),
            FileSystemRule(path="/app/node_modules", permissions="write"),
            FileSystemRule(path="/tmp", permissions="write"),
        ),
        env_vars=("NODE_ENV", "NEXT_PUBLIC_API_URL"),
        allowed_commands=("node", "npm", "npx", "tsc"),
    ),
    "ci-runner": SandboxProfile(
        name="ci-runner",
        description="CI runner with full network but restricted filesystem.",
        network_rules=(
            NetworkRule(host="0.0.0.0", port=0, protocol="tcp"),
            NetworkRule(host="0.0.0.0", port=0, protocol="udp"),
        ),
        fs_rules=(
            FileSystemRule(path="/workspace", permissions="write"),
            FileSystemRule(path="/tmp", permissions="write"),
        ),
        env_vars=("CI", "GITHUB_TOKEN", "GITHUB_SHA", "GITHUB_REF"),
        allowed_commands=("git", "make", "docker", "pytest", "ruff"),
    ),
    "minimal": SandboxProfile(
        name="minimal",
        description="No network, read-only filesystem.",
        network_rules=(),
        fs_rules=(FileSystemRule(path="/app", permissions="read"),),
        env_vars=(),
        allowed_commands=(),
    ),
}


def get_builtin_profile(name: str) -> SandboxProfile:
    """Return a built-in sandbox profile by name.

    Args:
        name: One of ``"web-backend"``, ``"frontend"``, ``"ci-runner"``,
            or ``"minimal"``.

    Returns:
        The requested ``SandboxProfile``.

    Raises:
        KeyError: If *name* is not a recognised built-in profile.
    """
    try:
        return _BUILTIN_PROFILES[name]
    except KeyError:
        available = ", ".join(sorted(_BUILTIN_PROFILES))
        msg = f"Unknown builtin profile {name!r}. Available: {available}"
        raise KeyError(msg) from None


def list_builtin_profiles() -> tuple[str, ...]:
    """Return names of all built-in profiles.

    Returns:
        Sorted tuple of profile name strings.
    """
    return tuple(sorted(_BUILTIN_PROFILES))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProfileConflict:
    """Describes a single conflict detected during profile validation.

    Args:
        kind: Category of the conflict.
        message: Human-readable explanation.
    """

    kind: str
    message: str


def validate_profile(profile: SandboxProfile) -> list[ProfileConflict]:
    """Check a profile for internal conflicts and suspicious rules.

    Detected issues include:

    - Contradictory filesystem rules (same path with both ``"read"`` and
      ``"write"`` or ``"execute"`` permissions).
    - Wildcard network rules (port 0 allows all ports on a host).
    - Empty profile (no rules at all).

    Args:
        profile: The profile to validate.

    Returns:
        A list of ``ProfileConflict`` objects.  An empty list means the
        profile is clean.
    """
    conflicts: list[ProfileConflict] = []

    # Check for contradictory fs rules on the same path.
    fs_by_path: dict[str, list[FileSystemRule]] = {}
    for rule in profile.fs_rules:
        fs_by_path.setdefault(rule.path, []).append(rule)

    for path, rules in fs_by_path.items():
        perms = {r.permissions for r in rules}
        if "read" in perms and "write" in perms:
            conflicts.append(
                ProfileConflict(
                    kind="contradictory_fs",
                    message=(
                        f"Path {path!r} has both 'read' and 'write' rules. "
                        "Write access implies read; the read-only rule is "
                        "contradictory."
                    ),
                )
            )
        if "read" in perms and "execute" in perms:
            conflicts.append(
                ProfileConflict(
                    kind="contradictory_fs",
                    message=(
                        f"Path {path!r} has both 'read' and 'execute' rules. "
                        "Execute access implies read; the read-only rule is "
                        "contradictory."
                    ),
                )
            )

    # Wildcard network rules.
    for rule in profile.network_rules:
        if rule.port == 0:
            conflicts.append(
                ProfileConflict(
                    kind="wildcard_network",
                    message=(
                        f"Network rule for {rule.host!r} uses port 0 "
                        "(all ports). Consider restricting to specific ports."
                    ),
                )
            )

    # Empty profile warning.
    if not profile.network_rules and not profile.fs_rules and not profile.env_vars and not profile.allowed_commands:
        conflicts.append(
            ProfileConflict(
                kind="empty_profile",
                message="Profile has no rules at all; agents will have zero capabilities.",
            )
        )

    return conflicts


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_list_section(
    lines: list[str],
    heading: str,
    items: list[str],
    empty_label: str,
) -> None:
    """Render a Markdown section with a bullet list or an empty placeholder."""
    lines.append(f"## {heading}")
    lines.append("")
    if items:
        lines.extend(f"- {item}" for item in items)
    else:
        lines.append(f"- *{empty_label}*")
    lines.append("")


def render_profile_summary(profile: SandboxProfile) -> str:
    """Render a Markdown summary of a sandbox profile.

    Args:
        profile: The profile to summarise.

    Returns:
        A Markdown-formatted string.
    """
    lines: list[str] = [f"# Sandbox Profile: {profile.name}", ""]

    if profile.description:
        lines.append(profile.description)
        lines.append("")

    network_items = [
        f"`{r.host}:{'all ports' if r.port == 0 else r.port}` ({r.protocol})" for r in profile.network_rules
    ]
    _render_list_section(lines, "Network Rules", network_items, "No network access")

    fs_items = [f"`{r.path}` [{r.permissions}]" for r in profile.fs_rules]
    _render_list_section(lines, "Filesystem Rules", fs_items, "No filesystem rules")

    env_items = [f"`{v}`" for v in profile.env_vars]
    _render_list_section(lines, "Environment Variables", env_items, "None")

    cmd_items = [f"`{c}`" for c in profile.allowed_commands]
    _render_list_section(lines, "Allowed Commands", cmd_items, "None")

    return "\n".join(lines)
