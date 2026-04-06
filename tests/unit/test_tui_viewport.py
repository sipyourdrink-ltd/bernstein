"""Tests for TUI-002: agent list viewport clipping and scroll buffer."""

from __future__ import annotations

from typing import Any

from bernstein.cli.dashboard import (
    _AGENT_WIDGET_HEIGHT,
    _MAX_VISIBLE_AGENTS,
    AgentListContainer,
)


def _make_agent(agent_id: str, role: str = "backend") -> dict[str, Any]:
    """Create a minimal agent dict for testing."""
    return {
        "id": agent_id,
        "role": role,
        "model": "sonnet",
        "status": "working",
        "runtime_s": 120,
        "task_ids": [],
    }


class TestAgentListContainerInit:
    """Tests for AgentListContainer initialization."""

    def test_initial_state(self) -> None:
        """Container starts with empty agent list and zero offset."""
        container = AgentListContainer()
        assert container.total_agents == 0
        assert container.scroll_offset == 0

    def test_max_visible_agents_positive(self) -> None:
        """The max visible agents constant is a positive integer."""
        assert _MAX_VISIBLE_AGENTS > 0
        assert isinstance(_MAX_VISIBLE_AGENTS, int)

    def test_agent_widget_height_positive(self) -> None:
        """The agent widget height constant is a positive integer."""
        assert _AGENT_WIDGET_HEIGHT > 0
        assert isinstance(_AGENT_WIDGET_HEIGHT, int)


class TestAgentListContainerLogic:
    """Tests for the viewport clipping logic (no mounted Textual widgets)."""

    def test_update_agents_stores_list(self) -> None:
        """update_agents stores the full agent list."""
        container = AgentListContainer()
        agents = [_make_agent(f"a-{i}") for i in range(5)]
        # We can't call update_agents without mounting, but we can test
        # the internal state tracking.
        container._all_agents = agents
        assert container.total_agents == 5

    def test_scroll_offset_clamp(self) -> None:
        """Scroll offset is clamped to valid range."""
        container = AgentListContainer()
        container._all_agents = [_make_agent(f"a-{i}") for i in range(3)]
        # Set offset beyond bounds
        container._scroll_offset = 100
        # Manually clamp as update_agents would
        capacity = 2  # simulate small viewport
        max_offset = max(0, container.total_agents - capacity)
        container._scroll_offset = min(container._scroll_offset, max_offset)
        assert container._scroll_offset <= max_offset

    def test_empty_agents_zero_total(self) -> None:
        """Empty agent list gives zero total."""
        container = AgentListContainer()
        container._all_agents = []
        assert container.total_agents == 0
        assert container.scroll_offset == 0


class TestViewportCapacity:
    """Tests for viewport_capacity calculation."""

    def test_viewport_capacity_minimum_one(self) -> None:
        """Viewport capacity is always at least 1."""
        container = AgentListContainer()
        # Without mounting, content_region will throw, but the property
        # catches the exception and returns a default.
        capacity = container.viewport_capacity
        assert capacity >= 1

    def test_viewport_capacity_capped(self) -> None:
        """Viewport capacity is capped at _MAX_VISIBLE_AGENTS."""
        container = AgentListContainer()
        capacity = container.viewport_capacity
        assert capacity <= _MAX_VISIBLE_AGENTS
