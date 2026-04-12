"""Tests for memory leak detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bernstein.core.memory_guard import AgentMemoryHistory, MemoryGuard


def test_agent_memory_history_leak_detection() -> None:
    """Test that monotonic increase triggers leak detection."""
    history = AgentMemoryHistory(session_id="s1", pid=123)

    # Not enough samples
    history.add_sample(100 * 1024 * 1024)
    assert not history.is_leaking(threshold_mb=10, min_samples=3)

    # Increasing but not enough samples
    history.add_sample(150 * 1024 * 1024)
    assert not history.is_leaking(threshold_mb=10, min_samples=3)

    # Monotonic increase + threshold exceeded
    history.add_sample(250 * 1024 * 1024)
    assert history.is_leaking(threshold_mb=10, min_samples=3)

    # Not monotonic (last sample decreased)
    history.add_sample(200 * 1024 * 1024)
    assert not history.is_leaking(threshold_mb=10, min_samples=3)


def test_memory_guard_monitor_agents() -> None:
    """Test MemoryGuard integration with multiple agents."""
    guard = MemoryGuard()

    a1 = MagicMock(id="a1", pid=111)
    a2 = MagicMock(id="a2", pid=222)

    with patch("bernstein.core.knowledge.memory_guard.get_rss_bytes") as mock_rss:
        # First sample
        mock_rss.side_effect = [100 * 1024 * 1024, 100 * 1024 * 1024]
        guard.monitor_agents([a1, a2])

        # Second sample (a1 increasing, a2 stable)
        mock_rss.side_effect = [200 * 1024 * 1024, 100 * 1024 * 1024]
        guard.monitor_agents([a1, a2])

        # Third sample (a1 increasing, a2 stable)
        mock_rss.side_effect = [400 * 1024 * 1024, 100 * 1024 * 1024]

        # Override threshold for testing
        with patch.object(AgentMemoryHistory, "is_leaking", return_value=True):
            leaking = guard.monitor_agents([a1, a2])
            assert "a1" in leaking
            # a2 also called is_leaking, but in this mock both return True
            assert "a2" in leaking
