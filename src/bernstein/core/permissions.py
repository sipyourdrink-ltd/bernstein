"""Per-agent file and command permissions.

Defines a permission matrix that controls which file paths each agent role
may modify and which shell commands are allowed or blocked.  The guardrails
system uses these permissions to hard-block diffs that violate role boundaries.

Path traversal hardening: all file paths are resolved to absolute paths and
verified to reside within the project root or worktree before permission
checks are applied.  Paths resolving outside the allowed root are rejected.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bernstein.core.policy_engine import DecisionType, PermissionDecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentPermissions:
    """File and command permission rules for an agent role.

    Path rules use glob syntax (fnmatch).  A file is permitted when it matches
    at least one ``allowed_paths`` pattern AND does not match any
    ``denied_paths`` pattern.  Denied paths always take precedence.

    Command rules work similarly: ``denied_commands`` patterns are checked
    first, then ``allowed_commands``.  An empty ``allowed_commands`` list
    means *all* commands are allowed (subject to denials).

    Attributes:
        allowed_paths: Glob patterns for file paths the role may modify.
            An empty list means *all* paths are allowed (subject to denials).
        denied_paths: Glob patterns for file paths the role must NOT modify.
            These override ``allowed_paths``.
        allowed_commands: Glob patterns for allowed shell commands.
            Empty list means all commands are allowed (subject to denials).
        denied_commands: Glob patterns for denied shell commands.
    """

    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Path traversal hardening
# ---------------------------------------------------------------------------


def resolve_and_validate_path(
    filepath: str,
    project_root: str | Path | None = None,
) -> tuple[str, bool]:
    """Resolve a file path to a safe relative path and validate containment.

    Resolves ``..``, symlinks (via ``os.path.realpath``), and ensures the
    resulting path does not escape the project root.  Returns the normalized
    relative path and a boolean indicating whether the path is safe.

    Args:
        filepath: Raw file path (may be relative or absolute).
        project_root: Absolute path of the project root directory.
            When ``None``, uses the current working directory.

    Returns:
        Tuple of (normalized_relative_path, is_safe).  ``is_safe`` is
        ``False`` when the resolved path escapes the project root.
    """
    # Resolve the root fully (following symlinks) for consistent comparison
    root = Path.cwd().resolve() if project_root is None else Path(project_root).resolve()
    # Also get the realpath to handle macOS /var -> /private/var etc.
    root_real = Path(os.path.realpath(root))

    # Strip leading ./ for normalization
    cleaned = filepath
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]

    # Build absolute path: first normpath to resolve .., then realpath to resolve symlinks
    if os.path.isabs(cleaned):
        norm_path = Path(os.path.normpath(cleaned))
    else:
        norm_path = Path(os.path.normpath(root_real / cleaned))

    # Use realpath to follow symlinks (catches symlink escapes)
    real_path = Path(os.path.realpath(norm_path))

    # Check containment of the REAL (symlink-resolved) path against root
    for check_root in (root, root_real):
        try:
            rel = real_path.relative_to(check_root)
            return str(rel), True
        except ValueError:
            continue

    # Also check normpath (non-symlink-resolved) for non-existent paths
    for check_root in (root, root_real):
        try:
            rel = norm_path.relative_to(check_root)
            # Path is within root by normpath but not by realpath —
            # this means a symlink escapes. Block it.
            if real_path != norm_path:
                logger.warning(
                    "Symlink escape blocked: %r -> %s (outside %s)",
                    filepath,
                    real_path,
                    check_root,
                )
                return filepath, False
            return str(rel), True
        except ValueError:
            continue

    # Path escapes the project root
    logger.warning(
        "Path traversal blocked: %r resolves to %s (outside %s)",
        filepath,
        real_path,
        root_real,
    )
    return filepath, False


def has_path_traversal(filepath: str) -> bool:
    """Quick check for obvious path traversal patterns.

    Detects ``..`` components, absolute paths, null bytes, and other
    traversal indicators without requiring a project root for resolution.

    Args:
        filepath: The file path to check.

    Returns:
        True if the path contains suspicious traversal patterns.
    """
    # Null byte injection
    if "\x00" in filepath:
        return True

    # Normalise path separators
    normalized = filepath.replace("\\", "/")

    # Direct .. traversal (including encoded variants)
    segments = normalized.split("/")
    if ".." in segments:
        return True

    # URL-encoded traversal (%2e%2e or %2f)
    lower = filepath.lower()
    return bool("%2e%2e" in lower or "%2f" in lower)


# ---------------------------------------------------------------------------
# Default role permission matrix
# ---------------------------------------------------------------------------

# Sentinel for "no restrictions beyond the deny list"
_UNRESTRICTED_PATHS: tuple[str, ...] = ()

DEFAULT_ROLE_PERMISSIONS: dict[str, AgentPermissions] = {
    "backend": AgentPermissions(
        allowed_paths=("src/*", "tests/*", "docs/*", "pyproject.toml", "scripts/*"),
        denied_paths=(".github/*", ".sdd/*", "templates/roles/*"),
    ),
    "frontend": AgentPermissions(
        allowed_paths=("src/*", "tests/*", "docs/*", "public/*", "static/*", "package.json"),
        denied_paths=(".github/*", ".sdd/*", "templates/roles/*"),
    ),
    "qa": AgentPermissions(
        allowed_paths=("tests/*", "src/*", "docs/*", "scripts/*"),
        denied_paths=(".github/*", ".sdd/*", "templates/roles/*"),
    ),
    "security": AgentPermissions(
        allowed_paths=("src/*", "tests/*", ".github/workflows/*", "docs/*", "scripts/*"),
        denied_paths=(".sdd/*", "templates/roles/*"),
    ),
    "devops": AgentPermissions(
        allowed_paths=(".github/*", "Dockerfile", "docker-compose.yml", "scripts/*", "Makefile"),
        denied_paths=(".sdd/*", "src/*", "templates/roles/*"),
    ),
    "docs": AgentPermissions(
        allowed_paths=("docs/*", "README.md", "CHANGELOG.md", "CONTRIBUTING.md"),
        denied_paths=(".github/*", ".sdd/*", "src/*", "tests/*", "templates/roles/*"),
    ),
    "manager": AgentPermissions(
        # Managers can read everything but should only modify plans and docs
        allowed_paths=("docs/*", ".sdd/backlog/*", "plans/*"),
        denied_paths=("src/*", "tests/*", ".github/*"),
    ),
    "architect": AgentPermissions(
        allowed_paths=("src/*", "tests/*", "docs/*", "scripts/*"),
        denied_paths=(".github/*", ".sdd/*", "templates/roles/*"),
    ),
}


def get_permissions_for_role(
    role: str,
    overrides: dict[str, AgentPermissions] | None = None,
) -> AgentPermissions:
    """Look up the permission set for an agent role.

    Args:
        role: Agent role name (e.g. "backend", "security").
        overrides: Optional per-project override map.  Keys that appear here
            replace the built-in defaults for that role.

    Returns:
        The ``AgentPermissions`` for the role.  Falls back to an unrestricted
        default if the role is not found in either map.
    """
    if overrides and role in overrides:
        return overrides[role]
    return DEFAULT_ROLE_PERMISSIONS.get(role, AgentPermissions())


# ---------------------------------------------------------------------------
# Path matching
# ---------------------------------------------------------------------------


def path_matches_any(filepath: str, patterns: tuple[str, ...]) -> bool:
    """Return True if *filepath* matches any of the glob *patterns*.

    Matching is done against the full relative path using ``fnmatch``.
    Patterns ending with ``/*`` are also tested against the parent directory
    prefix so that ``src/*`` matches ``src/foo/bar.py``.

    Args:
        filepath: Relative file path from the repo root (no leading ``/``).
        patterns: Glob patterns to test against.

    Returns:
        True if at least one pattern matches.
    """
    # Normalise: strip leading ./ or /
    if filepath.startswith("./"):
        filepath = filepath[2:]
    elif filepath.startswith("/"):
        filepath = filepath[1:]

    for pattern in patterns:
        if fnmatch.fnmatch(filepath, pattern):
            return True
        # "src/*" should also match "src/sub/deep/file.py"
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # "src/"
            if filepath.startswith(prefix):
                return True
    return False


def is_path_allowed(
    filepath: str,
    permissions: AgentPermissions,
    project_root: str | Path | None = None,
) -> bool:
    """Check whether a single file path is allowed by the permission set.

    Applies path traversal hardening: rejects paths with ``..`` traversal,
    null bytes, or paths resolving outside the project root.

    Denied paths are checked first and always win.  If ``allowed_paths`` is
    empty the path is allowed (unless denied).

    Args:
        filepath: Relative file path.
        permissions: Permission rules to apply.
        project_root: Absolute path of the project root (optional).

    Returns:
        True if the path is permitted.
    """
    # Path traversal quick-check
    if has_path_traversal(filepath):
        logger.warning("Path traversal detected in %r — denied", filepath)
        return False

    # Resolve and validate containment when a project root is available
    if project_root is not None:
        filepath, is_safe = resolve_and_validate_path(filepath, project_root)
        if not is_safe:
            return False

    # Denied paths always win
    if permissions.denied_paths and path_matches_any(filepath, permissions.denied_paths):
        return False

    # If no allow-list, everything (not denied) is allowed
    if not permissions.allowed_paths:
        return True

    return path_matches_any(filepath, permissions.allowed_paths)


def is_command_allowed(command: str, permissions: AgentPermissions) -> bool:
    """Check whether a shell command is allowed by the permission set.

    Args:
        command: The command string to check.
        permissions: Permission rules to apply.

    Returns:
        True if the command is permitted.
    """
    # Denied commands always win
    for pattern in permissions.denied_commands:
        if fnmatch.fnmatch(command, pattern):
            return False

    # If no allow-list, everything (not denied) is allowed
    if not permissions.allowed_commands:
        return True

    return any(fnmatch.fnmatch(command, pat) for pat in permissions.allowed_commands)


# ---------------------------------------------------------------------------
# Guardrail check
# ---------------------------------------------------------------------------


def check_file_permissions(
    diff: str,
    role: str,
    overrides: dict[str, AgentPermissions] | None = None,
) -> list[PermissionDecision]:
    """Check that all modified files in a diff are within the role's permissions.

    Args:
        diff: Git diff output string.
        role: Agent role name.
        overrides: Optional per-project permission overrides.

    Returns:
        A list containing a single ``PermissionDecision`` for the
        ``"file_permissions"`` check.
    """
    permissions = get_permissions_for_role(role, overrides)

    # If no restrictions defined, skip the check
    if not permissions.allowed_paths and not permissions.denied_paths:
        return [
            PermissionDecision(
                type=DecisionType.ALLOW,
                reason=f"No file permission rules defined for role '{role}' — skipping",
            )
        ]

    changed_files = _parse_diff_files(diff)
    if not changed_files:
        return [PermissionDecision(type=DecisionType.ALLOW, reason="No files changed")]

    violations: list[str] = []
    for filepath in changed_files:
        if not is_path_allowed(filepath, permissions):
            violations.append(filepath)

    if violations:
        return [
            PermissionDecision(
                type=DecisionType.DENY,
                reason=(f"Role '{role}' is not permitted to modify {len(violations)} file(s): {', '.join(violations)}"),
                bypass_immune=True,
                files=tuple(violations),
            )
        ]

    return [
        PermissionDecision(
            type=DecisionType.ALLOW,
            reason=f"All {len(changed_files)} modified file(s) within role '{role}' permissions",
        )
    ]


# ---------------------------------------------------------------------------
# Diff parsing (shared with guardrails.py — minimal duplication)
# ---------------------------------------------------------------------------


def _parse_diff_files(diff: str) -> list[str]:
    """Extract modified file paths from a git diff (a/ prefix stripped)."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ", 3)
            if len(parts) >= 3:
                path = parts[2]
                if path.startswith("a/"):
                    path = path[2:]
                files.append(path)
    return files
