"""Per-agent file and command permissions.

Defines a permission matrix that controls which file paths each agent role
may modify and which shell commands are allowed or blocked.  The guardrails
system uses these permissions to hard-block diffs that violate role boundaries.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

from bernstein.core.models import GuardrailResult

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


def is_path_allowed(filepath: str, permissions: AgentPermissions) -> bool:
    """Check whether a single file path is allowed by the permission set.

    Denied paths are checked first and always win.  If ``allowed_paths`` is
    empty the path is allowed (unless denied).

    Args:
        filepath: Relative file path.
        permissions: Permission rules to apply.

    Returns:
        True if the path is permitted.
    """
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
) -> list[GuardrailResult]:
    """Check that all modified files in a diff are within the role's permissions.

    Args:
        diff: Git diff output string.
        role: Agent role name.
        overrides: Optional per-project permission overrides.

    Returns:
        A list containing a single ``GuardrailResult`` for the
        ``"file_permissions"`` check.
    """
    permissions = get_permissions_for_role(role, overrides)

    # If no restrictions defined, skip the check
    if not permissions.allowed_paths and not permissions.denied_paths:
        return [
            GuardrailResult(
                check="file_permissions",
                passed=True,
                blocked=False,
                detail=f"No file permission rules defined for role '{role}' — skipping",
            )
        ]

    changed_files = _parse_diff_files(diff)
    if not changed_files:
        return [
            GuardrailResult(
                check="file_permissions",
                passed=True,
                blocked=False,
                detail="No files changed",
            )
        ]

    violations: list[str] = []
    for filepath in changed_files:
        if not is_path_allowed(filepath, permissions):
            violations.append(filepath)

    if violations:
        return [
            GuardrailResult(
                check="file_permissions",
                passed=False,
                blocked=True,
                detail=(f"Role '{role}' is not permitted to modify {len(violations)} file(s): {', '.join(violations)}"),
                files=violations,
            )
        ]

    return [
        GuardrailResult(
            check="file_permissions",
            passed=True,
            blocked=False,
            detail=f"All {len(changed_files)} modified file(s) within role '{role}' permissions",
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
