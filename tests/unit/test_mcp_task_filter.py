"""Tests for MCP-010: Per-task MCP server filtering."""

from __future__ import annotations

import pytest

from bernstein.core.mcp_task_filter import (
    FilterResult,
    RoleServerRule,
    TaskMCPFilter,
)
from bernstein.core.models import Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    role: str = "backend",
    mcp_servers: list[str] | None = None,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id="t1",
        title="Test task",
        description="A test task",
        role=role,
        mcp_servers=mcp_servers or [],
        owned_files=owned_files or [],
    )


ALL_SERVERS = ["github", "database", "test-runner", "coverage", "security-scanner"]


# ---------------------------------------------------------------------------
# Tests — RoleServerRule
# ---------------------------------------------------------------------------


class TestRoleServerRule:
    def test_matches_exact_role(self) -> None:
        rule = RoleServerRule(role="backend", allowed_servers=("github",))
        assert rule.matches_role("backend") is True
        assert rule.matches_role("qa") is False

    def test_wildcard_matches_all(self) -> None:
        rule = RoleServerRule(role="*", allowed_servers=("github",))
        assert rule.matches_role("backend") is True
        assert rule.matches_role("qa") is True

    def test_scope_pattern_matching(self) -> None:
        rule = RoleServerRule(
            role="backend",
            allowed_servers=("database",),
            scope_patterns=(r"\.sql$", r"migrations/"),
        )
        assert rule.matches_scope(["src/models.py"]) is False
        assert rule.matches_scope(["migrations/001.sql"]) is True

    def test_no_scope_patterns_always_matches(self) -> None:
        rule = RoleServerRule(role="backend", allowed_servers=("github",))
        assert rule.matches_scope([]) is True
        assert rule.matches_scope(["anything.py"]) is True


# ---------------------------------------------------------------------------
# Tests — TaskMCPFilter with explicit task servers
# ---------------------------------------------------------------------------


class TestExplicitTaskServers:
    def test_explicit_mcp_servers_override_rules(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("backend", ["github", "database"])
        task = _make_task(role="backend", mcp_servers=["github"])
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert result.allowed == ["github"]
        assert "database" in result.blocked

    def test_filter_names_convenience(self) -> None:
        filt = TaskMCPFilter()
        task = _make_task(role="backend", mcp_servers=["github", "database"])
        names = filt.filter_names(task, ALL_SERVERS)
        assert names == ["github", "database"]


# ---------------------------------------------------------------------------
# Tests — TaskMCPFilter with role rules
# ---------------------------------------------------------------------------


class TestRoleRules:
    def test_backend_role_filter(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("backend", ["github", "database"])
        task = _make_task(role="backend")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert set(result.allowed) == {"github", "database"}
        assert "test-runner" in result.blocked

    def test_qa_role_filter(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("qa", ["test-runner", "coverage"])
        task = _make_task(role="qa")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert set(result.allowed) == {"test-runner", "coverage"}

    def test_wildcard_rule_combined(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("*", ["github"])
        filt.add_role_rule("backend", ["database"])
        task = _make_task(role="backend")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert set(result.allowed) == {"github", "database"}

    def test_no_matching_rule_blocks_all(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("security", ["security-scanner"])
        task = _make_task(role="backend")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert result.allowed == []
        assert len(result.blocked) == len(ALL_SERVERS)

    def test_no_rules_configured_allows_all(self) -> None:
        filt = TaskMCPFilter()
        task = _make_task(role="backend")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert set(result.allowed) == set(ALL_SERVERS)


# ---------------------------------------------------------------------------
# Tests — Default allow all
# ---------------------------------------------------------------------------


class TestDefaultAllowAll:
    def test_default_allow_all(self) -> None:
        filt = TaskMCPFilter(default_allow_all=True)
        filt.add_role_rule("security", ["security-scanner"])
        task = _make_task(role="unknown")
        result = filt.filter_for_task(task, ALL_SERVERS)
        assert set(result.allowed) == set(ALL_SERVERS)


# ---------------------------------------------------------------------------
# Tests — Scope-based filtering
# ---------------------------------------------------------------------------


class TestScopeFiltering:
    def test_scope_pattern_restricts(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("backend", ["database"], scope_patterns=[r"\.sql$"])
        task_match = _make_task(role="backend", owned_files=["migrations/001.sql"])
        task_no = _make_task(role="backend", owned_files=["src/app.py"])
        assert "database" in filt.filter_names(task_match, ALL_SERVERS)
        assert "database" not in filt.filter_names(task_no, ALL_SERVERS)


# ---------------------------------------------------------------------------
# Tests — Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_filter_result_to_dict(self) -> None:
        result = FilterResult(
            task_id="t1",
            role="backend",
            allowed=["github"],
            blocked=["database"],
            reasons={"database": "no rule"},
        )
        d = result.to_dict()
        assert d["task_id"] == "t1"
        assert d["allowed"] == ["github"]

    def test_filter_to_dict(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("backend", ["github"])
        d = filt.to_dict()
        assert len(d["rules"]) == 1
        assert d["rules"][0]["role"] == "backend"

    def test_rules_property(self) -> None:
        filt = TaskMCPFilter()
        filt.add_role_rule("backend", ["github"])
        assert len(filt.rules) == 1
