"""SEC-015: Command allowlist per task scope.

Tasks with scope=small get restricted commands.  Tasks with scope=large
get full access.  This module defines per-scope allowlists and provides
enforcement functions.

Usage::

    from bernstein.core.command_allowlist import (
        ScopeAllowlist,
        ScopeAllowlistConfig,
        check_command,
    )

    config = ScopeAllowlistConfig()
    result = check_command("rm -rf /tmp/junk", scope="small", config=config)
    assert not result.allowed
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllowlistVerdict:
    """Result of checking a command against the scope allowlist.

    Attributes:
        allowed: Whether the command is allowed.
        command: The command that was checked.
        scope: The task scope used for evaluation.
        reason: Human-readable explanation.
        matched_pattern: The pattern that matched (if any).
    """

    allowed: bool
    command: str
    scope: str
    reason: str
    matched_pattern: str = ""


@dataclass(frozen=True)
class ScopeAllowlist:
    """Allowlist configuration for a specific task scope.

    Attributes:
        scope: The scope this allowlist applies to.
        allowed_commands: Command prefixes/patterns that are allowed.
            Empty means allow all (subject to deny list).
        denied_commands: Command prefixes/patterns that are always denied.
            Deny always takes precedence over allow.
    """

    scope: str
    allowed_commands: tuple[str, ...] = ()
    denied_commands: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Default per-scope allowlists
# ---------------------------------------------------------------------------

_SMALL_SCOPE_ALLOWLIST = ScopeAllowlist(
    scope="small",
    allowed_commands=(
        "cat",
        "grep",
        "head",
        "tail",
        "wc",
        "ls",
        "find",
        "diff",
        "echo",
        "test",
        "pwd",
        "basename",
        "dirname",
        "sort",
        "uniq",
        "tr",
        "sed",
        "awk",
        "python",
        "uv run pytest",
        "uv run python",
        "uv run ruff",
        "uv run pyright",
        "git status",
        "git diff",
        "git log",
        "git show",
        "git branch",
    ),
    denied_commands=(
        "rm -rf",
        "git push --force",
        "git reset --hard",
        "sudo",
        "chmod 777",
        "curl | bash",
        "wget | bash",
        "docker",
        "podman",
        "kubectl",
    ),
)

_MEDIUM_SCOPE_ALLOWLIST = ScopeAllowlist(
    scope="medium",
    allowed_commands=(
        "cat",
        "grep",
        "head",
        "tail",
        "wc",
        "ls",
        "find",
        "diff",
        "echo",
        "test",
        "pwd",
        "sort",
        "uniq",
        "tr",
        "sed",
        "awk",
        "python",
        "uv run",
        "git",
        "mkdir",
        "cp",
        "mv",
        "touch",
        "npm",
        "pip",
    ),
    denied_commands=(
        "rm -rf /",
        "git push --force",
        "sudo",
        "chmod 777",
        "curl | bash",
        "docker run",
    ),
)

_LARGE_SCOPE_ALLOWLIST = ScopeAllowlist(
    scope="large",
    # Large scope: no allowlist restriction, only deny dangerous commands
    allowed_commands=(),
    denied_commands=(
        "rm -rf /",
        "sudo rm",
        "chmod 777 /",
        "mkfs",
    ),
)


@dataclass(frozen=True)
class ScopeAllowlistConfig:
    """Configuration for scope-based command allowlists.

    Attributes:
        enabled: Whether allowlist enforcement is active.
        scope_lists: Mapping of scope name to allowlist configuration.
            Defaults are provided for small, medium, and large scopes.
    """

    enabled: bool = True
    scope_lists: dict[str, ScopeAllowlist] = field(default_factory=dict[str, ScopeAllowlist])

    def get_allowlist(self, scope: str) -> ScopeAllowlist | None:
        """Return the allowlist for a scope.

        Falls back to default allowlists if not explicitly configured.

        Args:
            scope: The task scope.

        Returns:
            The allowlist for the scope, or None if no rules apply.
        """
        if scope in self.scope_lists:
            return self.scope_lists[scope]

        defaults: dict[str, ScopeAllowlist] = {
            "small": _SMALL_SCOPE_ALLOWLIST,
            "medium": _MEDIUM_SCOPE_ALLOWLIST,
            "large": _LARGE_SCOPE_ALLOWLIST,
        }
        return defaults.get(scope)


def _command_matches(command: str, pattern: str) -> bool:
    """Check if a command matches a pattern (prefix or substring match).

    Args:
        command: The full command string.
        pattern: The pattern to check against.

    Returns:
        True if the command matches the pattern.
    """
    normalized = command.strip()
    # Check prefix match
    if normalized.startswith(pattern):
        return True
    # Check if pattern appears as a word boundary in the command
    escaped = re.escape(pattern)
    return bool(re.search(rf"(?:^|\s|/){escaped}(?:\s|$|/)", normalized))


def check_command(
    command: str,
    scope: str,
    config: ScopeAllowlistConfig | None = None,
) -> AllowlistVerdict:
    """Check a command against the scope-based allowlist.

    Args:
        command: The command to check.
        scope: The task scope (``"small"``, ``"medium"``, ``"large"``).
        config: Allowlist configuration. Uses defaults if not provided.

    Returns:
        Verdict indicating whether the command is allowed.
    """
    if config is None:
        config = ScopeAllowlistConfig()

    if not config.enabled:
        return AllowlistVerdict(
            allowed=True,
            command=command,
            scope=scope,
            reason="Allowlist enforcement disabled",
        )

    allowlist = config.get_allowlist(scope)
    if allowlist is None:
        return AllowlistVerdict(
            allowed=True,
            command=command,
            scope=scope,
            reason=f"No allowlist defined for scope={scope}",
        )

    # Deny list always takes precedence
    for pattern in allowlist.denied_commands:
        if _command_matches(command, pattern):
            return AllowlistVerdict(
                allowed=False,
                command=command,
                scope=scope,
                reason=f"Denied by scope={scope} deny pattern: {pattern!r}",
                matched_pattern=pattern,
            )

    # If no allow list, everything not denied is allowed
    if not allowlist.allowed_commands:
        return AllowlistVerdict(
            allowed=True,
            command=command,
            scope=scope,
            reason=f"No allowlist for scope={scope}; command not denied",
        )

    # Check against allow list
    for pattern in allowlist.allowed_commands:
        if _command_matches(command, pattern):
            return AllowlistVerdict(
                allowed=True,
                command=command,
                scope=scope,
                reason=f"Allowed by scope={scope} pattern: {pattern!r}",
                matched_pattern=pattern,
            )

    return AllowlistVerdict(
        allowed=False,
        command=command,
        scope=scope,
        reason=f"Not in allowlist for scope={scope}",
    )
