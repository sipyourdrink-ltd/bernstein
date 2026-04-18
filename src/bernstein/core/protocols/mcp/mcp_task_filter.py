"""MCP-010: Per-task MCP server filtering.

Only expose MCP tools relevant to a task's role and scope.  When an
agent is spawned for a task, this module computes which MCP servers
it should have access to based on:

1. Task role (e.g. "backend", "qa", "security").
2. Task scope (e.g. file patterns, module names).
3. Explicit per-task MCP server list (from the task definition).
4. Role-to-server mapping rules.

This prevents agents from accessing tools outside their assignment,
reducing prompt noise and improving security.

Usage::

    from bernstein.core.protocols.mcp_task_filter import TaskMCPFilter

    filt = TaskMCPFilter()
    filt.add_role_rule("qa", ["test-runner", "coverage-reporter"])
    filt.add_role_rule("backend", ["github", "database-tools"])

    allowed = filt.filter_for_task(task, all_server_names)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleServerRule:
    """Maps a task role to allowed MCP servers.

    Attributes:
        role: Task role name (e.g. "backend", "qa").
        allowed_servers: Server names this role may access.
        scope_patterns: Optional file-path regex patterns that further
            restrict access.  If non-empty, the task must own at least
            one file matching a pattern for the servers to be included.
    """

    role: str
    allowed_servers: tuple[str, ...] = ()
    scope_patterns: tuple[str, ...] = ()

    def matches_role(self, task_role: str) -> bool:
        """Return True if this rule applies to the given role."""
        return self.role == "*" or self.role == task_role

    def matches_scope(self, owned_files: list[str]) -> bool:
        """Return True if scope patterns match at least one owned file.

        If no scope_patterns are defined, always returns True.
        """
        if not self.scope_patterns:
            return True
        for pat in self.scope_patterns:
            compiled = re.compile(pat, re.IGNORECASE)
            for f in owned_files:
                if compiled.search(f):
                    return True
        return False


@dataclass
class FilterResult:
    """Result of per-task MCP server filtering.

    Attributes:
        task_id: The task ID that was filtered.
        role: The task role used for filtering.
        allowed: Server names allowed for this task.
        blocked: Server names blocked for this task.
        reasons: Per-server explanation of why it was blocked.
    """

    task_id: str
    role: str
    allowed: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    blocked: list[str] = field(default_factory=list)  # type: ignore[reportUnknownVariableType]
    reasons: dict[str, str] = field(default_factory=dict)  # type: ignore[reportUnknownVariableType]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "task_id": self.task_id,
            "role": self.role,
            "allowed": self.allowed,
            "blocked": self.blocked,
            "reasons": self.reasons,
        }


class TaskMCPFilter:
    """Per-task MCP server filtering based on role and scope.

    Args:
        default_allow_all: If True, servers not covered by any rule are
            allowed.  If False (default), uncovered servers are blocked.
    """

    def __init__(self, *, default_allow_all: bool = False) -> None:
        self._rules: list[RoleServerRule] = []
        self._default_allow_all = default_allow_all

    def add_role_rule(
        self,
        role: str,
        allowed_servers: list[str],
        scope_patterns: list[str] | None = None,
    ) -> None:
        """Add a role-to-server mapping rule.

        Args:
            role: Task role (or "*" for wildcard).
            allowed_servers: Server names this role may use.
            scope_patterns: Optional file-path patterns for scope matching.
        """
        rule = RoleServerRule(
            role=role,
            allowed_servers=tuple(allowed_servers),
            scope_patterns=tuple(scope_patterns) if scope_patterns else (),
        )
        self._rules.append(rule)
        logger.debug("Added role rule: role=%s servers=%s", role, allowed_servers)

    def filter_for_task(
        self,
        task: Task,
        all_server_names: list[str],
    ) -> FilterResult:
        """Compute which MCP servers a task should access.

        Logic:
        1. If the task has an explicit ``mcp_servers`` list, use it.
        2. Otherwise, collect servers allowed by matching rules.
        3. If no rules match, apply ``default_allow_all`` policy.

        Args:
            task: The task to filter for.
            all_server_names: All available MCP server names.

        Returns:
            A FilterResult with allowed and blocked lists.
        """
        result = FilterResult(task_id=task.id, role=task.role)

        if task.mcp_servers:
            return self._filter_explicit(result, task.mcp_servers, all_server_names)

        allowed_set = self._collect_allowed(task)
        self._apply_allowed_set(result, allowed_set, all_server_names, task.role)
        return result

    def _filter_explicit(
        self, result: FilterResult, mcp_servers: list[str], all_server_names: list[str]
    ) -> FilterResult:
        """Apply explicit mcp_servers list to the filter result."""
        explicit_set = set(mcp_servers)
        result.allowed = [n for n in all_server_names if n in explicit_set]
        result.blocked = [n for n in all_server_names if n not in explicit_set]
        for name in result.blocked:
            result.reasons[name] = "not in task.mcp_servers explicit list"
        return result

    def _collect_allowed(self, task: Task) -> set[str]:
        """Collect allowed server names from matching rules."""
        allowed_set: set[str] = set()
        for rule in self._rules:
            if rule.matches_role(task.role) and rule.matches_scope(task.owned_files):
                allowed_set.update(rule.allowed_servers)
        return allowed_set

    def _apply_allowed_set(
        self, result: FilterResult, allowed_set: set[str], all_server_names: list[str], role: str
    ) -> None:
        """Apply allowed_set to the filter result, using default policy as fallback."""
        if allowed_set:
            result.allowed = [n for n in all_server_names if n in allowed_set]
            result.blocked = [n for n in all_server_names if n not in allowed_set]
            for name in result.blocked:
                result.reasons[name] = f"no rule allows server for role '{role}'"
        elif self._default_allow_all or not self._rules:
            result.allowed = list(all_server_names)
        else:
            result.blocked = list(all_server_names)
            for name in result.blocked:
                result.reasons[name] = f"no matching rule for role '{role}'"

    def filter_names(self, task: Task, all_server_names: list[str]) -> list[str]:
        """Convenience method returning just the allowed server names.

        Args:
            task: The task to filter for.
            all_server_names: All available MCP server names.

        Returns:
            List of allowed server names.
        """
        return self.filter_for_task(task, all_server_names).allowed

    @property
    def rules(self) -> list[RoleServerRule]:
        """Return all registered rules."""
        return list(self._rules)

    def to_dict(self) -> dict[str, Any]:
        """Serialize filter configuration to a dict."""
        return {
            "default_allow_all": self._default_allow_all,
            "rules": [
                {
                    "role": r.role,
                    "allowed_servers": list(r.allowed_servers),
                    "scope_patterns": list(r.scope_patterns),
                }
                for r in self._rules
            ],
        }
