"""Tests for structured TUI task search parsing and matching."""

from __future__ import annotations

from dataclasses import dataclass

from bernstein.tui.task_search import TaskSearchQuery, matches_task_search, parse_task_search


@dataclass(frozen=True)
class _TaskStub:
    task_id: str
    title: str
    role: str
    status: str
    priority: int
    model: str
    assigned_agent: str
    blocked_reason: str


class TestParseTaskSearch:
    def test_plain_text_terms(self) -> None:
        parsed = parse_task_search("auth retry")
        assert parsed == TaskSearchQuery(text_terms=("auth", "retry"))

    def test_structured_filters(self) -> None:
        parsed = parse_task_search("status:failed role:backend priority:1")
        assert parsed.status == "failed"
        assert parsed.role == "backend"
        assert parsed.priority == 1

    def test_invalid_priority_is_ignored(self) -> None:
        parsed = parse_task_search("priority:urgent")
        assert parsed.priority is None


class TestMatchesTaskSearch:
    def test_matches_plain_text(self) -> None:
        task = _TaskStub("abc123", "Fix auth retry loop", "backend", "failed", 1, "sonnet", "agent-1", "")
        assert matches_task_search(task, parse_task_search("auth retry")) is True

    def test_matches_structured_filters(self) -> None:
        task = _TaskStub("abc123", "Fix auth", "backend", "failed", 1, "sonnet", "agent-1", "")
        assert matches_task_search(task, parse_task_search("status:failed role:backend priority:1")) is True

    def test_rejects_non_matching_status(self) -> None:
        task = _TaskStub("abc123", "Fix auth", "backend", "open", 1, "sonnet", "agent-1", "")
        assert matches_task_search(task, parse_task_search("status:failed")) is False

    def test_matches_assigned_agent_and_blocker_text(self) -> None:
        task = _TaskStub("abc123", "Fix auth", "backend", "blocked", 2, "sonnet", "agent-123", "waiting on deps")
        assert matches_task_search(task, parse_task_search("agent:123 deps")) is True
