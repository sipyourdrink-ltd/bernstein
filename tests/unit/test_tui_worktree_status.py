"""Tests for the TUI runtime/worktree health panel."""

from __future__ import annotations

from bernstein.tui.worktree_status import RuntimeHealthPanel, render_runtime_health


def test_render_runtime_health_empty() -> None:
    """Missing runtime data renders an intentional empty state."""
    text = render_runtime_health(None)
    assert "unavailable" in text.plain.lower()


def test_render_runtime_health_snapshot() -> None:
    """Runtime health text includes the high-signal runtime fields."""
    text = render_runtime_health(
        {
            "git_branch": "main",
            "active_worktrees": 3,
            "restart_count": 1,
            "memory_mb": 128.5,
            "disk_usage_mb": 42.0,
            "config_hash": "abcdef1234567890",
        }
    )
    plain = text.plain
    assert "Runtime Health" in plain
    assert "main" in plain
    assert "3 / 1" in plain
    assert "128.5 MB / 42.0 MB" in plain
    assert "abcdef123456" in plain


def test_runtime_health_panel_renders_snapshot() -> None:
    """Widget render delegates to the runtime-health formatter."""
    widget = RuntimeHealthPanel()
    widget.set_snapshot({"git_branch": "feature/runtime", "active_worktrees": 2})
    assert "feature/runtime" in widget.render().plain
